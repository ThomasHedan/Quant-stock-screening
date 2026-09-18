"""The broad collector: snapshot in, lake rows out.

Sits between :mod:`app.sources.tradingview` and :mod:`app.storage.lake`, and
holds the small amount of state a snapshot alone cannot provide — what each
ticker's price and volume were when the current window opened, which is what
``window_change_pct`` and ``window_volume`` are measured against (CLAUDE.md
5.2).

Deliberately not a scheduler: it is driven by an explicit ``now`` so the whole
collection path can be replayed in a test without waiting for a clock.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime

from app.core import metrics
from app.core.collection import (
    CollectionCandidate,
    CollectionFilter,
    decide,
    top_dollar_volume_tickers,
)
from app.core.timeutils import et_trading_date, to_utc
from app.sources.tradingview import SnapshotResult
from app.storage.lake import LakeRow
from app.storage.reference import ReferenceTracker

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WindowBaseline:
    """A ticker's price and session volume when the current window opened."""

    price: float | None
    session_volume: float | None
    opened_at: datetime


@dataclass(slots=True)
class WindowBaselines:
    """First observation per ticker within the current window.

    Reset when a window opens. A ticker first seen mid-window becomes its own
    baseline, so its ``window_change_pct`` is 0 rather than a number measured
    against some other window — a misleading value is worse than a missing one.
    """

    window_start: datetime
    _baselines: dict[str, WindowBaseline] = field(default_factory=dict)

    def observe(
        self, ticker: str, price: float | None, volume: float | None, *, at: datetime
    ) -> WindowBaseline:
        """Return the ticker's baseline, recording it on first sight."""
        existing = self._baselines.get(ticker)
        if existing is not None:
            return existing
        baseline = WindowBaseline(price=price, session_volume=volume, opened_at=to_utc(at))
        self._baselines[ticker] = baseline
        return baseline

    def reset(self, window_start: datetime) -> None:
        """Start a new window, discarding the previous one's baselines."""
        self.window_start = to_utc(window_start)
        self._baselines.clear()

    @property
    def tracked(self) -> int:
        """Tickers with a baseline in the current window."""
        return len(self._baselines)


@dataclass(frozen=True, slots=True)
class CollectionStats:
    """What one collection poll did, for ``data_quality`` and the logs."""

    considered: int
    collected: int
    reference_rows: int
    rules: dict[str, int]
    missing_columns: tuple[str, ...]
    unparsable: int


@dataclass(frozen=True, slots=True)
class CollectionOutput:
    """Rows ready for the lake, plus the stats of the poll that produced them."""

    snapshots: list[LakeRow]
    reference: list[LakeRow]
    stats: CollectionStats


def snapshot_rows(
    result: SnapshotResult,
    *,
    rules: CollectionFilter,
    session_fraction: float,
    baselines: WindowBaselines,
    reference_tracker: ReferenceTracker,
    float_turnover_low_confidence: float,
    prev_closes: dict[str, float] | None = None,
) -> CollectionOutput:
    """Turn one snapshot into the rows the lake should keep.

    ``prev_closes`` comes from the daily bar source, never from an earlier
    snapshot: snapshots are unadjusted on split days, so reading one back turns
    a 1:10 reverse split into a -90% move (CLAUDE.md 6.4.1). When a prev close
    is unavailable, ``gap_pct`` falls back to TradingView's own change figure
    and the row records which it used.
    """
    poll_ts = result.poll_ts_utc
    day: date = et_trading_date(poll_ts)
    closes = prev_closes or {}

    candidates = [
        CollectionCandidate(
            ticker=row.ticker,
            price=row.close,
            change_pct=row.change_pct,
            volume=row.volume,
            average_volume_10d=row.average_volume_10d,
        )
        for row in result.rows
    ]
    top_tickers = top_dollar_volume_tickers(candidates, rules.top_dollar_volume_n)

    snapshots: list[LakeRow] = []
    references: list[LakeRow] = []
    rule_counts: dict[str, int] = {}

    for row, candidate in zip(result.rows, candidates, strict=True):
        decision = decide(
            candidate,
            rules,
            session_fraction=session_fraction,
            in_top_dollar_volume=candidate.ticker in top_tickers,
        )
        rule_counts[decision.rule] = rule_counts.get(decision.rule, 0) + 1
        if not decision.collected:
            continue

        baseline = baselines.observe(row.ticker, row.close, row.volume, at=poll_ts)
        prev_close = closes.get(row.ticker)
        gap = (
            metrics.gap_pct(row.close, prev_close)
            if row.close is not None and prev_close is not None
            else row.change_pct
        )
        turnover = metrics.float_turnover(row.volume or 0.0, row.float_shares)
        confidence = metrics.float_confidence(
            float_shares=row.float_shares,
            turnover=turnover,
            float_asof=None,
            as_of=poll_ts,
            turnover_low_threshold=float_turnover_low_confidence,
            max_age_days=0,
        )

        snapshots.append(
            {
                "ticker": row.ticker,
                "date": day,
                "poll_ts_utc": poll_ts,
                "price": row.close,
                "session_volume": row.volume,
                "gap_pct": gap,
                "window_change_pct": metrics.window_change_pct(row.close, baseline.price)
                if row.close is not None
                else None,
                "window_volume": metrics.window_volume(row.volume, baseline.session_volume)
                if row.volume is not None
                else None,
                "dollar_volume": metrics.dollar_volume(row.close, row.volume)
                if row.close is not None and row.volume is not None
                else None,
                "written_at_utc": poll_ts,
            }
        )

        reference_row = reference_tracker.observe(
            row.ticker,
            day,
            {
                "sector": row.sector,
                "industry": row.industry,
                "float_shares_outstanding": row.float_shares,
                "total_shares_outstanding": row.shares_outstanding,
                "average_volume_10d_calc": row.average_volume_10d,
                "average_volume_30d_calc": row.average_volume_30d,
                "market_cap": row.market_cap,
                "float_source": "tradingview",
                "float_asof": None,
                "float_confidence": confidence.value,
                "float_turnover": turnover,
            },
            asof=poll_ts,
        )
        if reference_row is not None:
            references.append({**reference_row, "written_at_utc": poll_ts})

    stats = CollectionStats(
        considered=len(result.rows),
        collected=len(snapshots),
        reference_rows=len(references),
        rules=rule_counts,
        missing_columns=result.missing_columns,
        unparsable=result.unparsable,
    )
    logger.info(
        "Collector poll at %s: %s of %s rows collected (%s reference rows)",
        poll_ts.isoformat(),
        stats.collected,
        stats.considered,
        stats.reference_rows,
    )
    return CollectionOutput(snapshots=snapshots, reference=references, stats=stats)
