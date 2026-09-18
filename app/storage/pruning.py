"""Retrospective pruning of Tier 1, and the lake retention job.

The rule that makes this work is the separation in CLAUDE.md 6.0: **collection
is loose and decided live; retention is strict and decided retrospectively.**
At 08:05 nobody knows which stock ends the day +80%, so the collector keeps far
too much. At 20:45 the outcome is known, and this module decides what survives.

What it must never do is keep winners only. Three classes survive at full
resolution — movers, anything that reached Watch tier, and a deterministic
random *control* sample of everything else — and every dropped ticker still
leaves a summary row behind, so the count and distribution of what was thrown
away stays knowable. Tier 0 keeps their daily OHLCV regardless.

Any research query estimating a rate has to re-weight the control rows by
``100 / control_sample_pct``. That is the single easiest way to get a wrong
answer out of this lake, which is why the weight is a tested function in
:mod:`app.core.collection` rather than a note in a notebook.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from app.core.collection import in_control_sample
from app.core.moves import MoveMetrics, is_mover
from app.core.timeutils import to_utc
from app.core.types import RetentionClass, Tier
from app.storage import lake
from app.storage.schemas import NEVER_PRUNED, SCHEMA_VERSION

logger = logging.getLogger(__name__)

#: Tiers that make a ticker-day worth keeping at full resolution regardless of
#: how the price ended up. A Watch-tier evaluation is a row the missed-runner
#: analysis will want, whether or not the stock went anywhere.
SIGNAL_TIERS: frozenset[Tier] = frozenset({Tier.WATCH, Tier.B, Tier.A})


@dataclass(frozen=True, slots=True)
class TickerDaySummary:
    """What the pruning job knows about one ticker on one day."""

    ticker: str
    metrics: MoveMetrics
    best_tier: Tier
    poll_count: int
    first_poll_ts_utc: datetime | None = None
    last_poll_ts_utc: datetime | None = None
    session_volume: float | None = None
    last_price: float | None = None
    max_gap_pct: float | None = None
    max_rvol: float | None = None


@dataclass(frozen=True, slots=True)
class RetentionDecision:
    """Whether a ticker-day's snapshots survive, and why."""

    ticker: str
    retention_class: RetentionClass | None
    reason: str

    @property
    def kept(self) -> bool:
        """True when the rows stay at full resolution."""
        return self.retention_class is not None


def classify(
    summary: TickerDaySummary,
    day: date,
    *,
    move_threshold_pct: float,
    control_sample_pct: int,
) -> RetentionDecision:
    """Decide the fate of one ticker-day.

    Order matters only for the label: a mover that also reached tier A is
    recorded as a mover. What matters is that the control branch is evaluated
    for everything else, so the sample stays an unbiased slice of the
    uninteresting population rather than the leftovers of a manual rule.
    """
    if is_mover(summary.metrics, move_threshold_pct):
        return RetentionDecision(
            summary.ticker,
            RetentionClass.MOVER,
            reason=f"moved at least {move_threshold_pct:g}%",
        )
    if summary.best_tier in SIGNAL_TIERS:
        return RetentionDecision(
            summary.ticker,
            RetentionClass.SIGNAL,
            reason=f"reached tier {summary.best_tier}",
        )
    if in_control_sample(summary.ticker, day, control_sample_pct):
        return RetentionDecision(
            summary.ticker,
            RetentionClass.CONTROL,
            reason=f"control sample ({control_sample_pct}%)",
        )
    return RetentionDecision(summary.ticker, None, reason="not a mover, no signal, not sampled")


def summary_row(
    summary: TickerDaySummary,
    decision: RetentionDecision,
    day: date,
    *,
    now: datetime,
) -> lake.LakeRow:
    """Build the ``pruned_summary`` row for a dropped ticker.

    Dropped tickers are never silently deleted: this row keeps the count and
    the distribution of what was thrown away, so a later question about the
    denominator has an answer (CLAUDE.md 6.1).
    """
    return {
        "ticker": summary.ticker,
        "date": day,
        "poll_count": summary.poll_count,
        "first_poll_ts_utc": summary.first_poll_ts_utc,
        "last_poll_ts_utc": summary.last_poll_ts_utc,
        "session_high": summary.metrics.day_high,
        "session_low": summary.metrics.day_low,
        "last_price": summary.last_price,
        "session_volume": summary.session_volume,
        "max_gap_pct": summary.max_gap_pct,
        "max_rvol": summary.max_rvol,
        "up_move_pct": summary.metrics.up_move_pct,
        "down_move_pct": summary.metrics.down_move_pct,
        "best_tier": summary.best_tier.value,
        "drop_reason": decision.reason,
        "written_at_utc": to_utc(now),
    }


@dataclass(frozen=True, slots=True)
class PruneReport:
    """What one night's pruning did, for ``data_quality`` and the dashboard."""

    day: date
    move_threshold_pct: float
    control_sample_pct: int
    rows_before: int
    rows_after: int
    mover_count: int
    signal_count: int
    control_count: int
    dropped_count: int

    @property
    def kept_count(self) -> int:
        """Tickers whose snapshots survived at full resolution."""
        return self.mover_count + self.signal_count + self.control_count


def prune_day(
    root: Path,
    day: date,
    summaries: dict[str, TickerDaySummary],
    *,
    now: datetime,
    move_threshold_pct: float,
    control_sample_pct: int,
    compression: str = "zstd",
) -> tuple[PruneReport, list[lake.LakeRow]]:
    """Rewrite a day's ``snapshots`` partition, keeping only what survives.

    Returns the report and the ``pruned_summary`` rows the caller writes. The
    partition is rewritten exactly once, here, and never again — that is the
    append-only invariant of the lake (CLAUDE.md 6.3a).

    A ticker present in the partition but absent from ``summaries`` is kept
    rather than dropped: missing move metrics mean the outcome job failed, and
    discarding unrepeatable snapshots on the strength of a failed job is the
    one mistake here that cannot be undone.
    """
    table = lake.read_day(root, "snapshots", day)
    rows = table.to_pylist()
    rows_before = len(rows)

    by_ticker: dict[str, list[lake.LakeRow]] = defaultdict(list)
    for row in rows:
        by_ticker[str(row["ticker"])].append(row)

    kept_rows: list[lake.LakeRow] = []
    pruned_rows: list[lake.LakeRow] = []
    counts: dict[RetentionClass, int] = dict.fromkeys(RetentionClass, 0)
    dropped = 0

    for ticker, ticker_rows in by_ticker.items():
        summary = summaries.get(ticker)
        if summary is None:
            logger.warning(
                "No move metrics for %s on %s; keeping its snapshots rather than "
                "discarding data that cannot be refetched",
                ticker,
                day,
            )
            kept_rows.extend(
                {**row, "retention_class": RetentionClass.SIGNAL.value} for row in ticker_rows
            )
            counts[RetentionClass.SIGNAL] += 1
            continue

        decision = classify(
            summary,
            day,
            move_threshold_pct=move_threshold_pct,
            control_sample_pct=control_sample_pct,
        )
        if decision.retention_class is None:
            dropped += 1
            pruned_rows.append(summary_row(summary, decision, day, now=now))
            continue
        counts[decision.retention_class] += 1
        kept_rows.extend(
            {**row, "retention_class": decision.retention_class.value} for row in ticker_rows
        )

    lake.rewrite_day(root, "snapshots", day, kept_rows, now=now, compression=compression)
    report = PruneReport(
        day=day,
        move_threshold_pct=move_threshold_pct,
        control_sample_pct=control_sample_pct,
        rows_before=rows_before,
        rows_after=len(kept_rows),
        mover_count=counts[RetentionClass.MOVER],
        signal_count=counts[RetentionClass.SIGNAL],
        control_count=counts[RetentionClass.CONTROL],
        dropped_count=dropped,
    )
    logger.info(
        "Pruned %s: %s -> %s rows (%s movers, %s signals, %s control, %s dropped) "
        "at threshold %g%%",
        day,
        rows_before,
        len(kept_rows),
        report.mover_count,
        report.signal_count,
        report.control_count,
        report.dropped_count,
        move_threshold_pct,
    )
    return report, pruned_rows


# --- retention ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """Age limits per table, from ``config.yaml``."""

    bars_1m_raw_days: int
    bars_1m_thinned_minutes: int
    snapshots_months: int
    max_lake_gb: int
    warn_fraction: float


@dataclass(frozen=True, slots=True)
class RetentionAction:
    """One thing the retention job did or would do."""

    table: str
    day: date
    action: str
    rows_before: int
    rows_after: int


def thin_bars(rows: list[lake.LakeRow], *, minutes: int) -> list[lake.LakeRow]:
    """Aggregate 1-minute bars into ``minutes``-minute OHLCV buckets.

    Open is the first bar's open, close the last bar's close, high and low the
    extremes, volume the sum. Getting this wrong silently corrupts every
    outcome computed from thinned history, which is why the test builds the
    expected bucket by hand rather than round-tripping the function.
    """
    if minutes <= 0:
        msg = f"minutes must be positive, got {minutes}"
        raise ValueError(msg)
    buckets: dict[tuple[str, datetime], list[lake.LakeRow]] = defaultdict(list)
    for row in rows:
        minute = to_utc(row["minute_utc"])
        bucket_start = minute - timedelta(
            minutes=minute.minute % minutes, seconds=minute.second, microseconds=minute.microsecond
        )
        buckets[(str(row["ticker"]), bucket_start)].append(row)

    thinned: list[lake.LakeRow] = []
    ordered_buckets = sorted(buckets.items(), key=lambda item: (item[0][1], item[0][0]))
    for (ticker, start), group in ordered_buckets:
        ordered = sorted(group, key=lambda r: to_utc(r["minute_utc"]))
        highs = [r["high"] for r in ordered if r["high"] is not None]
        lows = [r["low"] for r in ordered if r["low"] is not None]
        volumes = [r["volume"] for r in ordered if r["volume"] is not None]
        thinned.append(
            {
                "ticker": ticker,
                "date": ordered[0]["date"],
                "minute_utc": start,
                "open": ordered[0]["open"],
                "high": max(highs) if highs else None,
                "low": min(lows) if lows else None,
                "close": ordered[-1]["close"],
                "volume": sum(volumes) if volumes else None,
                "trade_count": sum(r["trade_count"] or 0 for r in ordered) or None,
                "vwap": None,  # not reconstructible from thinned OHLCV; left absent
                "resolution_minutes": minutes,
                "written_at_utc": ordered[-1]["written_at_utc"],
                "source": ordered[-1]["source"],
                "schema_version": SCHEMA_VERSION,
            }
        )
    return thinned


def apply_retention(
    root: Path,
    *,
    today: date,
    policy: RetentionPolicy,
    now: datetime,
    dry_run: bool = False,
) -> list[RetentionAction]:
    """Thin or drop partitions that have aged past the policy.

    Never touches the tables in ``NEVER_PRUNED``: Tier 0 and the small research
    tables are what every future statistic is computed against, and no size
    pressure justifies losing them (CLAUDE.md 6.3b).
    """
    actions: list[RetentionAction] = []

    for day in lake.partition_days(root, "bars_1m"):
        age_days = (today - day).days
        if age_days <= policy.bars_1m_raw_days:
            continue
        rows = lake.read_day(root, "bars_1m", day).to_pylist()
        if not rows or all(r["resolution_minutes"] != 1 for r in rows):
            continue
        thinned = thin_bars(rows, minutes=policy.bars_1m_thinned_minutes)
        if not dry_run:
            lake.rewrite_day(root, "bars_1m", day, thinned, now=now)
        actions.append(
            RetentionAction(
                table="bars_1m",
                day=day,
                action=f"thinned to {policy.bars_1m_thinned_minutes}m",
                rows_before=len(rows),
                rows_after=len(thinned),
            )
        )

    cutoff = today - timedelta(days=int(policy.snapshots_months * 30.44))
    for day in lake.partition_days(root, "snapshots"):
        if day >= cutoff:
            continue
        rows_before = lake.row_count(root, "snapshots", day)
        if not dry_run:
            for part in lake.partition_path(root, "snapshots", day).glob("*.parquet"):
                part.unlink()
        actions.append(
            RetentionAction(
                table="snapshots",
                day=day,
                action=f"dropped (older than {policy.snapshots_months} months)",
                rows_before=rows_before,
                rows_after=0,
            )
        )

    for action in actions:
        logger.info(
            "Retention%s: %s %s on %s (%s -> %s rows)",
            " (dry run)" if dry_run else "",
            action.table,
            action.action,
            action.day,
            action.rows_before,
            action.rows_after,
        )
    return actions


def size_warning(sizes: dict[str, int], policy: RetentionPolicy) -> str | None:
    """A message when the lake approaches ``max_lake_gb``, else ``None``."""
    total = sum(sizes.values())
    limit = policy.max_lake_gb * 1024**3
    if total < limit * policy.warn_fraction:
        return None
    biggest = sorted(sizes.items(), key=lambda item: -item[1])[:3]
    detail = ", ".join(f"{name} {size / 1024**3:.2f} GB" for name, size in biggest)
    return (
        f"Lake is at {total / 1024**3:.2f} GB of {policy.max_lake_gb} GB "
        f"({total / limit:.0%}); largest tables: {detail}"
    )


def protected_tables() -> frozenset[str]:
    """Tables the retention job must never delete from."""
    return NEVER_PRUNED
