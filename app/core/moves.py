"""Move metrics computed at 20:15, from bars and the daily rollup.

These drive both retention (which Tier 1 rows survive the night) and the runner
definition, so the choice of metric decides what the lake can be asked later.
Two choices are worth restating next to the code (CLAUDE.md 6.3):

* **High and low, not close.** A stock that opens flat, runs to +70% by 10:00
  and closes +4% is a textbook momentum event that any close-based filter
  misses entirely. ``up_move_pct`` catches it; ``fade_pct`` then records that
  it gave the move back, which is itself the label worth learning from.
* **``max_runup_pct`` as well as ``up_move_pct``.** A stock already gapped +40%
  that grinds to +50% is a different trade from one that goes -5% to +45%
  intraday. The first metric cannot tell them apart; the second can.

Down moves are kept at the same threshold on purpose: a low-float stock with
fresh news that *dumps* is the failure mode of the exact setup being traded,
and those rows are what teach the scanner to tell the two apart.

Everything here is pure and takes bars explicitly — including any ``as_of``
cutoff, so a metric can never see a bar from later than the moment it claims
to describe.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time

from app.core.timeutils import minutes_since_et_open, to_utc

logger = logging.getLogger(__name__)

_EPSILON = 1e-9


@dataclass(frozen=True, slots=True)
class Bar:
    """One minute of trading. ``minute`` is the bar's opening instant, UTC."""

    minute: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: float | None = None
    trade_count: int | None = None


@dataclass(frozen=True, slots=True)
class MoveMetrics:
    """The day's shape for one ticker, all relative to the previous close.

    Every field is optional because a ticker can have no bars, no previous
    close, or only a regular-hours session. A missing metric stays ``None``
    rather than becoming zero, which would read as "went nowhere".
    """

    up_move_pct: float | None = None
    down_move_pct: float | None = None
    range_pct: float | None = None
    max_runup_pct: float | None = None
    max_drawdown_pct: float | None = None
    pre_to_post_pct: float | None = None
    fade_pct: float | None = None
    minutes_to_high: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    session_close: float | None = None
    dollar_volume: float | None = None


def sorted_bars(bars: list[Bar], *, as_of: datetime | None = None) -> list[Bar]:
    """Bars in time order, optionally truncated at ``as_of``.

    The truncation is the point-in-time guard: a metric described as "at 08:05"
    must not be able to see the 09:30 bar, whatever the caller passed in.
    """
    ordered = sorted(bars, key=lambda bar: to_utc(bar.minute))
    if as_of is None:
        return ordered
    cutoff = to_utc(as_of)
    return [bar for bar in ordered if to_utc(bar.minute) <= cutoff]


def max_runup_pct(bars: list[Bar]) -> float | None:
    """Largest rise from any low to a later-or-equal high, as a percentage.

    Scans once, carrying the lowest low seen so far. Intrabar moves count: a
    single bar that trades from 2.00 to 3.40 is a real 70% run for anyone
    watching, and excluding it would understate exactly the fastest names.
    """
    ordered = sorted_bars(bars)
    if not ordered:
        return None
    best: float | None = None
    lowest = ordered[0].low
    for bar in ordered:
        lowest = min(lowest, bar.low)
        if lowest > _EPSILON:
            candidate = (bar.high / lowest - 1.0) * 100.0
            best = candidate if best is None else max(best, candidate)
    return best


def max_drawdown_pct(bars: list[Bar]) -> float | None:
    """Largest fall from any high to a later-or-equal low, as a percentage.

    The mirror of :func:`max_runup_pct`, and not merely bookkeeping: it is what
    separates a clean run from one that round-tripped twice on the way, which
    is the difference between a tradeable move and an unhold-able one.
    """
    ordered = sorted_bars(bars)
    if not ordered:
        return None
    worst: float | None = None
    highest = ordered[0].high
    for bar in ordered:
        highest = max(highest, bar.high)
        if highest > _EPSILON:
            candidate = (bar.low / highest - 1.0) * 100.0
            worst = candidate if worst is None else min(worst, candidate)
    return worst


def compute(
    bars: list[Bar],
    *,
    prev_close: float | None,
    day_start_et: time = time(4, 0),
    pre_open: float | None = None,
    post_close: float | None = None,
    as_of: datetime | None = None,
) -> MoveMetrics:
    """Compute the full set of move metrics for one ticker-day.

    ``prev_close`` must come from the daily bar source, which is consistently
    split-adjusted. Deriving it from an earlier snapshot would make every
    reverse-split day look like a catastrophic decline (CLAUDE.md 6.4.1).
    """
    ordered = sorted_bars(bars, as_of=as_of)
    if not ordered:
        return MoveMetrics()

    day_high = max(bar.high for bar in ordered)
    day_low = min(bar.low for bar in ordered)
    session_close = ordered[-1].close
    high_bar = next(bar for bar in ordered if bar.high >= day_high)

    def against_prev(value: float) -> float | None:
        if prev_close is None or prev_close <= _EPSILON:
            return None
        return (value / prev_close - 1.0) * 100.0

    range_pct = (
        (day_high - day_low) / prev_close * 100.0
        if prev_close is not None and prev_close > _EPSILON
        else None
    )
    fade = (session_close / day_high - 1.0) * 100.0 if day_high > _EPSILON else None
    drift = (
        (post_close / pre_open - 1.0) * 100.0
        if pre_open is not None and post_close is not None and pre_open > _EPSILON
        else None
    )

    return MoveMetrics(
        up_move_pct=against_prev(day_high),
        down_move_pct=against_prev(day_low),
        range_pct=range_pct,
        max_runup_pct=max_runup_pct(ordered),
        max_drawdown_pct=max_drawdown_pct(ordered),
        pre_to_post_pct=drift,
        fade_pct=fade,
        minutes_to_high=minutes_since_et_open(high_bar.minute, day_start_et),
        day_high=day_high,
        day_low=day_low,
        session_close=session_close,
        dollar_volume=sum(bar.close * bar.volume for bar in ordered),
    )


def is_mover(metrics: MoveMetrics, threshold_pct: float) -> bool:
    """Whether a ticker-day qualifies as a mover for retention purposes.

    Up moves, down moves and intraday run-ups all count at the same threshold
    (CLAUDE.md 6.1). A metric that could not be computed never qualifies: an
    unknown day is not a quiet day, and treating it as one would let a data
    outage quietly prune away the most interesting rows of the week.
    """
    candidates = (
        (metrics.up_move_pct, threshold_pct),
        (metrics.max_runup_pct, threshold_pct),
    )
    if any(value is not None and value >= limit for value, limit in candidates):
        return True
    return metrics.down_move_pct is not None and metrics.down_move_pct <= -threshold_pct


def suspect_price(
    price: float,
    prev_close: float | None,
    *,
    threshold: float,
    volume_surge: bool,
    corporate_action_on_record: bool,
) -> bool:
    """Whether a price move is more likely bad data than a real move.

    A >80% move with no matching volume surge and no corporate action on
    record is almost always an unadjusted print, a stale quote, or a bad tick.
    Such rows are excluded from move metrics and RVOL baselines and logged for
    review rather than silently trusted (CLAUDE.md 6.4.1).
    """
    if prev_close is None or prev_close <= _EPSILON:
        return False
    if corporate_action_on_record or volume_surge:
        return False
    move = abs(price / prev_close - 1.0)
    is_suspect = move > threshold
    if is_suspect:
        logger.warning(
            "Suspect price: %.2f against prev_close %.2f (%.0f%%) with no volume surge "
            "and no corporate action on record",
            price,
            prev_close,
            move * 100,
        )
    return is_suspect
