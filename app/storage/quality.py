"""Nightly data-quality accounting.

One row per table per day, plus the integrity metrics of CLAUDE.md 6.4. The
point is stated plainly in the spec: any of these drifting is a louder signal
than a missed alert. A scanner that quietly stopped receiving news, or whose
float figures all went stale, produces clean-looking alerts that mean nothing.

Everything here is pure aggregation over counters the jobs already collect; it
reads no clock and no file. The caller writes the resulting rows to the lake.
"""

from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from app.core.timeutils import to_utc

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class QualityCounters:
    """Counters a day's jobs accumulate, one instance per table.

    Plain mutable counters rather than a metrics library: this has to survive a
    process restart by being re-derived from the lake, and a dependency that
    keeps state elsewhere would make that harder, not easier.
    """

    table_name: str
    rows_collected: int = 0
    polls_expected: int = 0
    polls_completed: int = 0
    missing_field_count: int = 0
    api_errors: int = 0
    ws_disconnect_minutes: float = 0.0
    suspect_price_rows: int = 0
    corporate_actions_applied: int = 0
    rvol_baselines_recomputed: int = 0
    halt_count: int = 0
    inferred_halt_count: int = 0
    pillar5_evaluations: int = 0
    pillar5_low_confidence: int = 0
    news_latencies_s: list[float] = field(default_factory=list)
    outcome_rows: int = 0
    tradeable_rows: int = 0
    mover_count: int = 0
    control_count: int = 0
    dropped_count: int = 0
    move_threshold_pct: float | None = None
    notes: list[str] = field(default_factory=list)

    def note(self, message: str) -> None:
        """Record a human-readable anomaly for the dashboard header."""
        logger.warning("data_quality[%s]: %s", self.table_name, message)
        self.notes.append(message)


def _share(numerator: int, denominator: int) -> float | None:
    """A ratio, or ``None`` when there is nothing to divide by.

    Returning ``0.0`` for "no pillar-5 evaluations today" would read on the
    dashboard as "no confidence problems today", which is the opposite of what
    an empty day means.
    """
    if denominator <= 0:
        return None
    return numerator / denominator


def percentile(values: list[float], fraction: float) -> float | None:
    """Nearest-rank percentile of ``values``.

    Nearest-rank (``ceil(fraction * n)``) rather than interpolated: with a
    handful of news items on a quiet day, an interpolated p95 invents a latency
    nobody observed. Python's ``round`` would also break ties to even, which
    silently shifts the median of a short list.
    """
    if not values:
        return None
    if not 0 < fraction <= 1:
        msg = f"fraction must be in (0, 1], got {fraction}"
        raise ValueError(msg)
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def build_row(counters: QualityCounters, day: date, *, now: datetime) -> dict[str, Any]:
    """Turn a day's counters into a ``data_quality`` row."""
    latencies = counters.news_latencies_s
    return {
        "date": day,
        "table_name": counters.table_name,
        "rows_collected": counters.rows_collected,
        "polls_expected": counters.polls_expected or None,
        "polls_completed": counters.polls_completed or None,
        "missing_field_count": counters.missing_field_count,
        "api_errors": counters.api_errors,
        "ws_disconnect_minutes": counters.ws_disconnect_minutes,
        "suspect_price_rows": counters.suspect_price_rows,
        "corporate_actions_applied": counters.corporate_actions_applied,
        "rvol_baselines_recomputed": counters.rvol_baselines_recomputed,
        "halt_count": counters.halt_count,
        "inferred_halt_count": counters.inferred_halt_count,
        "pillar5_low_confidence_share": _share(
            counters.pillar5_low_confidence, counters.pillar5_evaluations
        ),
        "news_latency_median_s": statistics.median(latencies) if latencies else None,
        "news_latency_p95_s": percentile(latencies, 0.95),
        "tradeable_share": _share(counters.tradeable_rows, counters.outcome_rows),
        "mover_count": counters.mover_count,
        "control_count": counters.control_count,
        "dropped_count": counters.dropped_count,
        "move_threshold_pct": counters.move_threshold_pct,
        "note": "; ".join(counters.notes) or None,
        "written_at_utc": to_utc(now),
    }


@dataclass(frozen=True, slots=True)
class QualityWarning:
    """A condition worth showing in the dashboard header."""

    code: str
    message: str


def warnings_for(row: dict[str, Any], *, latency_p95_warn_s: float) -> tuple[QualityWarning, ...]:
    """Conditions in a ``data_quality`` row that the trader should see.

    These are the failures that make the numbers lie rather than merely go
    missing — a feed running a minute behind turns the 15-minute freshness rule
    into a measure of feed lag, and a lake full of low-confidence floats makes
    pillar 5 unusable. Both are findings to surface, not bugs to hide
    (CLAUDE.md 6.4.4, 6.4.6).
    """
    found: list[QualityWarning] = []
    p95 = row.get("news_latency_p95_s")
    if p95 is not None and p95 > latency_p95_warn_s:
        found.append(
            QualityWarning(
                code="news_feed_latency",
                message=(
                    f"News p95 latency {p95:.0f}s exceeds {latency_p95_warn_s:.0f}s: "
                    "the freshness rule is measuring feed lag, not market reaction"
                ),
            )
        )
    share = row.get("pillar5_low_confidence_share")
    if share is not None and share > 0.5:
        found.append(
            QualityWarning(
                code="float_confidence",
                message=(
                    f"{share:.0%} of pillar-5 evaluations ran at low float confidence: "
                    "pillar 5 may not be usable with this data"
                ),
            )
        )
    disconnects = row.get("ws_disconnect_minutes") or 0.0
    if disconnects > 0:
        found.append(
            QualityWarning(
                code="news_feed_gap",
                message=f"News WebSocket was disconnected for {disconnects:.0f} minutes",
            )
        )
    if row.get("api_errors"):
        found.append(
            QualityWarning(
                code="api_errors",
                message=f"{row['api_errors']} API errors on {row['table_name']}",
            )
        )
    if row.get("suspect_price_rows"):
        found.append(
            QualityWarning(
                code="suspect_price",
                message=(
                    f"{row['suspect_price_rows']} rows flagged suspect_price and excluded "
                    "from move metrics"
                ),
            )
        )
    return tuple(found)
