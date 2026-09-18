"""Momentum metrics: gap, window change, relative volume, composite rank.

Pure arithmetic over values the caller has already fetched. Every function that
is point-in-time takes its reference explicitly, and every function returns
``None`` rather than a plausible-looking zero when an input is missing — a
zeroed float is exactly the kind of value that sails through a threshold check
(CLAUDE.md 1.1).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from app.core.timeutils import to_utc
from app.core.types import FloatConfidence, RankWeights, RvolSource

logger = logging.getLogger(__name__)

#: Below this, a price or volume denominator is treated as absent rather than
#: dividing by something meaningless. It is not a tunable threshold: it is the
#: boundary of arithmetic validity, which is why it is here and not in config.
_EPSILON = 1e-9


def pct_change(current: float, reference: float) -> float | None:
    """Percent change of ``current`` against ``reference``, or ``None``.

    Returns ``None`` when the reference is missing or non-positive: a stock
    cannot have moved a defined percentage from a zero price, and reporting
    ``0.0`` there would quietly mark a data gap as "flat".
    """
    if reference is None or reference <= _EPSILON:
        return None
    return (current / reference - 1.0) * 100.0


def gap_pct(price: float, prev_close: float | None) -> float | None:
    """Session change against the previous official close.

    ``prev_close`` must come from the daily bar source, never from an earlier
    snapshot: snapshots are unadjusted on split days, so reading one back turns
    a 1:10 reverse split into a -90% "move" (CLAUDE.md 6.4.1).
    """
    if prev_close is None:
        return None
    return pct_change(price, prev_close)


def window_change_pct(price: float, window_open_price: float | None) -> float | None:
    """Move since this ticker's first snapshot inside the current window."""
    if window_open_price is None:
        return None
    return pct_change(price, window_open_price)


def window_volume(
    session_volume: float, session_volume_at_window_start: float | None
) -> float | None:
    """Volume traded since the window opened.

    Clamped at zero: session volume is cumulative and must never decrease, so a
    negative result means the feed reset or served a stale print. Clamping keeps
    downstream ranking sane; the anomaly is logged rather than swallowed.
    """
    if session_volume_at_window_start is None:
        return None
    delta = session_volume - session_volume_at_window_start
    if delta < 0:
        logger.warning(
            "Session volume decreased within a window (%s -> %s); clamping to 0",
            session_volume_at_window_start,
            session_volume,
        )
        return 0.0
    return delta


def rvol_time_of_day(session_volume: float, baseline_volume: float | None) -> float | None:
    """Volume so far against the same ET minute over the baseline days.

    Standard RVOL divides by a *full* day's average volume, which understates
    pre-market activity by an order of magnitude and makes the pillar-2
    threshold meaningless before the open. This compares like with like:
    volume by 08:05 against the average volume by 08:05.
    """
    if baseline_volume is None or baseline_volume <= _EPSILON:
        return None
    return session_volume / baseline_volume


def rvol_fallback(
    session_volume: float,
    average_volume_10d: float | None,
    session_fraction: float,
) -> float | None:
    """Crude RVOL for tickers with no time-of-day baseline yet.

    Approximates the expected volume by this point in the day as a fixed
    fraction of an average full day. It is materially worse than the baseline
    figure, which is why callers must label it ``RvolSource.FALLBACK`` and why
    research can filter it out.
    """
    if average_volume_10d is None or average_volume_10d <= _EPSILON:
        return None
    if session_fraction <= _EPSILON:
        msg = f"session_fraction must be positive, got {session_fraction}"
        raise ValueError(msg)
    return session_volume / (average_volume_10d * session_fraction)


def resolve_rvol(
    session_volume: float,
    baseline_volume: float | None,
    average_volume_10d: float | None,
    session_fraction: float,
) -> tuple[float | None, RvolSource]:
    """Best available RVOL, with the source that produced it.

    Prefers the time-of-day baseline and degrades explicitly; an unusable pair
    of inputs yields ``(None, UNKNOWN)`` so pillar 2 becomes unknown rather
    than failing a ticker for a missing baseline.
    """
    from_baseline = rvol_time_of_day(session_volume, baseline_volume)
    if from_baseline is not None:
        return from_baseline, RvolSource.BASELINE
    from_fallback = rvol_fallback(session_volume, average_volume_10d, session_fraction)
    if from_fallback is not None:
        return from_fallback, RvolSource.FALLBACK
    return None, RvolSource.UNKNOWN


def baseline_volume_at(
    minute_volumes_by_day: dict[object, dict[datetime, float]],
    as_of_minute_offset: int,
) -> float | None:
    """Mean cumulative volume reached ``as_of_minute_offset`` minutes into a day.

    ``minute_volumes_by_day`` maps a baseline day to that day's per-minute
    volumes, keyed by the minute's offset expressed as a UTC instant; the caller
    has already aligned each day on the same ET clock time, which is what makes
    the comparison valid across the DST switch.
    """
    if as_of_minute_offset < 0:
        msg = f"as_of_minute_offset must be non-negative, got {as_of_minute_offset}"
        raise ValueError(msg)
    totals: list[float] = []
    for minutes in minute_volumes_by_day.values():
        if not minutes:
            continue
        start = min(minutes)
        cutoff = start + timedelta(minutes=as_of_minute_offset)
        totals.append(sum(v for ts, v in minutes.items() if to_utc(ts) < cutoff))
    if not totals:
        return None
    return sum(totals) / len(totals)


def percentile_ranks(values: list[float | None]) -> list[float | None]:
    """Rank each value in ``[0, 1]``, averaging ties, ``None`` passing through.

    Ranking rather than raw values keeps the composite score scale-free, so a
    single 400x RVOL print cannot dominate the ordering.
    """
    present = [(i, v) for i, v in enumerate(values) if v is not None]
    if not present:
        return [None] * len(values)
    if len(present) == 1:
        out: list[float | None] = [None] * len(values)
        out[present[0][0]] = 1.0
        return out

    ordered = sorted(present, key=lambda pair: pair[1])
    ranks: dict[int, float] = {}
    position = 0
    while position < len(ordered):
        end = position
        while end + 1 < len(ordered) and ordered[end + 1][1] == ordered[position][1]:
            end += 1
        shared = (position + end) / 2.0 / (len(ordered) - 1)
        for index, _value in ordered[position : end + 1]:
            ranks[index] = shared
        position = end + 1
    return [ranks.get(i) for i in range(len(values))]


def rank_score(
    window_change_rank: float | None,
    rvol_rank: float | None,
    gap_rank: float | None,
    weights: RankWeights,
) -> float | None:
    """Composite ordering score, renormalised over the components present.

    Kept as one small function so the weighting can be retuned in one place
    once the research of CLAUDE.md 6.8 says what actually carries information.
    Missing components are dropped and the remaining weights rescaled, so a
    ticker with no RVOL yet is not pushed to the bottom of the table.
    """
    parts = (
        (window_change_rank, weights.window_change),
        (rvol_rank, weights.rvol),
        (gap_rank, weights.gap),
    )
    usable = [(rank, weight) for rank, weight in parts if rank is not None and weight > 0]
    if not usable:
        return None
    total_weight = sum(weight for _rank, weight in usable)
    if total_weight <= _EPSILON:
        return None
    return sum(rank * weight for rank, weight in usable) / total_weight


def float_turnover(session_volume: float, float_shares: int | None) -> float | None:
    """How many times the reported float has changed hands today.

    Above ~10x, the float figure itself is the likelier explanation than the
    stock genuinely trading its entire float ten times over (CLAUDE.md 6.4.4).
    """
    if float_shares is None or float_shares <= 0:
        return None
    return session_volume / float_shares


def float_confidence(
    *,
    float_shares: int | None,
    turnover: float | None,
    float_asof: datetime | None,
    as_of: datetime,
    turnover_low_threshold: float,
    max_age_days: int,
) -> FloatConfidence:
    """Trust level of a float figure, defaulting to distrust.

    Any of: no figure at all, implausible turnover, or a stale as-of date
    downgrades to ``LOW``, which callers must translate into pillar 5 being
    *unknown* — never a pass and never a fail. ``MEDIUM`` means the figure
    looks sane but carries no as-of date to confirm it.
    """
    if float_shares is None or float_shares <= 0:
        return FloatConfidence.LOW
    if turnover is not None and turnover > turnover_low_threshold:
        return FloatConfidence.LOW
    if float_asof is None:
        return FloatConfidence.MEDIUM
    age_days = (to_utc(as_of) - to_utc(float_asof)).days
    if age_days > max_age_days:
        return FloatConfidence.LOW
    return FloatConfidence.HIGH


def dollar_volume(price: float, volume: float) -> float:
    """Traded value, the only volume figure comparable across price levels."""
    return price * volume
