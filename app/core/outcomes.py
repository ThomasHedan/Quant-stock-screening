"""Outcome labels: what actually happened after each reference time.

Computed from 1-minute bars at 11:05 and 20:15 ET for a scoped set of tickers
(CLAUDE.md 6.5). These are the labels every future research question is asked
against, so three properties matter more than the arithmetic:

* **No lookahead.** Every metric takes the reference instant explicitly and
  reads only bars at or after it. A forward return that accidentally includes
  the bar before entry is a beautiful, worthless number.
* **Honest about liquidity.** Every row carries ``tradeable``,
  ``dollar_volume_in_window`` and ``est_spread_pct``, so research can report
  tradeable and all-rows figures side by side instead of quietly reporting the
  flattering one (CLAUDE.md 6.4.5).
* **Honest about halts.** A return measured across a halt is meaningless, so
  the row says ``spans_halt`` rather than pretending otherwise.

A metric that cannot be computed is ``None``. It is never zero: "the stock did
not move" and "we have no bars" are opposite findings.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from app.core.integrity import HaltWindow, assess_tradability, spans_halt
from app.core.moves import Bar, sorted_bars
from app.core.timeutils import et_datetime, et_trading_date, minutes_since_et_open, to_utc

logger = logging.getLogger(__name__)

_EPSILON = 1e-9


@dataclass(frozen=True, slots=True)
class OutcomeMetrics:
    """Forward performance from one reference time."""

    reference_ts_utc: datetime
    reference_price: float | None = None
    forward_returns: dict[int, float | None] = field(default_factory=dict)
    ret_to_open_pct: float | None = None
    ret_to_1100_pct: float | None = None
    ret_to_close_pct: float | None = None
    mfe_pct: float | None = None
    mae_pct: float | None = None
    minutes_to_high: float | None = None
    high_of_day_pct: float | None = None
    held_above_vwap_1000: bool | None = None
    dollar_volume_in_window: float | None = None
    est_spread_pct: float | None = None
    tradeable: bool = False
    spans_halt: bool = False


def price_at(bars: list[Bar], moment: datetime) -> float | None:
    """The close of the bar covering ``moment``, or the last one before it.

    Falls back to the previous bar because a thin small cap often has no print
    in the reference minute at all; using the last trade is what a trader would
    actually see on the screen.
    """
    cutoff = to_utc(moment)
    candidates = [bar for bar in sorted_bars(bars) if to_utc(bar.minute) <= cutoff]
    return candidates[-1].close if candidates else None


def forward_return_pct(
    bars: list[Bar], *, reference_ts: datetime, minutes: int, reference_price: float | None = None
) -> float | None:
    """Percent change from the reference to ``minutes`` later.

    Returns ``None`` when either end is missing rather than reaching for the
    nearest available bar: a "+60 min" return computed from a bar four hours
    later is not the number it claims to be.
    """
    start_price = reference_price if reference_price is not None else price_at(bars, reference_ts)
    if start_price is None or start_price <= _EPSILON:
        return None
    target = to_utc(reference_ts) + timedelta(minutes=minutes)
    window_end = target + timedelta(minutes=2)  # tolerate a couple of silent minutes
    later = [bar for bar in sorted_bars(bars) if target <= to_utc(bar.minute) <= window_end]
    if not later:
        return None
    return (later[0].close / start_price - 1.0) * 100.0


def excursions(
    bars: list[Bar],
    *,
    reference_ts: datetime,
    until: datetime,
    reference_price: float | None = None,
) -> tuple[float | None, float | None]:
    """Maximum favourable and adverse excursion, as percentages.

    MFE and MAE rather than only the final return, because they are what say
    whether a move was holdable: +40% at the high with -25% along the way is a
    different trade from +40% that never drew down.
    """
    start_price = reference_price if reference_price is not None else price_at(bars, reference_ts)
    if start_price is None or start_price <= _EPSILON:
        return None, None
    start, end = to_utc(reference_ts), to_utc(until)
    window = [bar for bar in sorted_bars(bars) if start <= to_utc(bar.minute) <= end]
    if not window:
        return None, None
    high = max(bar.high for bar in window)
    low = min(bar.low for bar in window)
    return (high / start_price - 1.0) * 100.0, (low / start_price - 1.0) * 100.0


def held_above_vwap(bars: list[Bar], *, at: datetime) -> bool | None:
    """Whether price was above the session VWAP at a given moment.

    A simple, widely watched hold-or-fail signal. ``None`` when there are no
    bars to judge it from — which is not the same as "it failed".
    """
    cutoff = to_utc(at)
    upto = [bar for bar in sorted_bars(bars) if to_utc(bar.minute) <= cutoff]
    if not upto:
        return None
    volume = sum(bar.volume for bar in upto)
    if volume <= _EPSILON:
        return None
    weighted = sum((bar.vwap if bar.vwap is not None else bar.close) * bar.volume for bar in upto)
    return upto[-1].close > (weighted / volume)


def compute(
    bars: list[Bar],
    *,
    reference_ts: datetime,
    forward_minutes: tuple[int, ...],
    prev_close: float | None,
    session_end_et: time = time(20, 0),
    mfe_until_et: time = time(11, 0),
    regular_open_et: time = time(9, 30),
    vwap_check_et: time = time(10, 0),
    halts: tuple[HaltWindow, ...] = (),
    min_tradeable_dollar_volume: float = 50_000.0,
    max_tradeable_spread_pct: float = 2.0,
    tradability_window_minutes: int = 5,
) -> OutcomeMetrics:
    """Compute every outcome metric for one ticker at one reference time.

    ``mfe_until_et`` differs between pre-market and post-market references
    (11:00 versus the 20:00 session end), and the caller passes the right one:
    measuring a pre-market signal's excursion out to 20:00 would credit it with
    a move that happened six hours and one session later.
    """
    day = et_trading_date(reference_ts)
    reference_price = price_at(bars, reference_ts)
    until = et_datetime(day, mfe_until_et)
    if to_utc(until) <= to_utc(reference_ts):
        until = et_datetime(day, session_end_et)

    mfe, mae = excursions(
        bars, reference_ts=reference_ts, until=until, reference_price=reference_price
    )
    forward = {
        minutes: forward_return_pct(
            bars, reference_ts=reference_ts, minutes=minutes, reference_price=reference_price
        )
        for minutes in forward_minutes
    }

    ordered = sorted_bars(bars)
    day_high = max((bar.high for bar in ordered), default=None)
    high_bar = next((bar for bar in ordered if day_high is not None and bar.high >= day_high), None)

    to_open = _return_to(bars, reference_ts, et_datetime(day, regular_open_et), reference_price)
    to_1100 = _return_to(bars, reference_ts, et_datetime(day, time(11, 0)), reference_price)
    to_close = _return_to(bars, reference_ts, et_datetime(day, session_end_et), reference_price)

    verdict = assess_tradability(
        bars,
        reference_ts=reference_ts,
        window_minutes=tradability_window_minutes,
        min_dollar_volume=min_tradeable_dollar_volume,
        max_spread_pct=max_tradeable_spread_pct,
    )

    return OutcomeMetrics(
        reference_ts_utc=to_utc(reference_ts),
        reference_price=reference_price,
        forward_returns=forward,
        ret_to_open_pct=to_open,
        ret_to_1100_pct=to_1100,
        ret_to_close_pct=to_close,
        mfe_pct=mfe,
        mae_pct=mae,
        minutes_to_high=(
            minutes_since_et_open(high_bar.minute, time(4, 0)) if high_bar is not None else None
        ),
        high_of_day_pct=(
            (day_high / prev_close - 1.0) * 100.0
            if day_high is not None and prev_close is not None and prev_close > _EPSILON
            else None
        ),
        held_above_vwap_1000=held_above_vwap(bars, at=et_datetime(day, vwap_check_et)),
        dollar_volume_in_window=verdict.dollar_volume_in_window,
        est_spread_pct=verdict.est_spread_pct,
        tradeable=verdict.tradeable,
        spans_halt=spans_halt(reference_ts, until, halts),
    )


def _return_to(
    bars: list[Bar], reference_ts: datetime, target: datetime, reference_price: float | None
) -> float | None:
    """Percent change from the reference to a fixed later instant."""
    if to_utc(target) <= to_utc(reference_ts):
        return None
    if reference_price is None or reference_price <= _EPSILON:
        return None
    end_price = price_at(bars, target)
    if end_price is None:
        return None
    return (end_price / reference_price - 1.0) * 100.0


def outcome_row(
    ticker: str,
    metrics: OutcomeMetrics,
    *,
    now: datetime,
) -> dict[str, object]:
    """Build the ``outcomes`` lake row."""
    forward = metrics.forward_returns
    return {
        "ticker": ticker,
        "date": et_trading_date(metrics.reference_ts_utc),
        "reference_ts_utc": metrics.reference_ts_utc,
        "reference_price": metrics.reference_price,
        "ret_5m_pct": forward.get(5),
        "ret_15m_pct": forward.get(15),
        "ret_30m_pct": forward.get(30),
        "ret_60m_pct": forward.get(60),
        "ret_to_open_pct": metrics.ret_to_open_pct,
        "ret_to_1100_pct": metrics.ret_to_1100_pct,
        "ret_to_close_pct": metrics.ret_to_close_pct,
        "mfe_pct": metrics.mfe_pct,
        "mae_pct": metrics.mae_pct,
        "minutes_to_high": metrics.minutes_to_high,
        "high_of_day_pct": metrics.high_of_day_pct,
        "held_above_vwap_1000": metrics.held_above_vwap_1000,
        "dollar_volume_in_window": metrics.dollar_volume_in_window,
        "est_spread_pct": metrics.est_spread_pct,
        "tradeable": metrics.tradeable,
        "spans_halt": metrics.spans_halt,
        "written_at_utc": to_utc(now),
    }


def scope_bar_fetch(
    candidates: dict[str, float],
    *,
    max_tickers: int,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Choose which tickers get bars, highest session change first.

    Returns ``(fetched, skipped)``. Bars are refetchable from Alpaca at any
    time, so a skipped ticker is a deferral rather than a loss — but the
    skipped list still goes to ``data_quality``, because a day that silently
    fetched 300 of 900 interesting names would otherwise look complete.
    """
    ranked = sorted(candidates.items(), key=lambda pair: (-pair[1], pair[0]))
    fetched = tuple(ticker for ticker, _change in ranked[:max_tickers])
    skipped = tuple(ticker for ticker, _change in ranked[max_tickers:])
    if skipped:
        logger.warning(
            "Bar fetch capped at %s tickers; %s skipped (recorded in data_quality)",
            max_tickers,
            len(skipped),
        )
    return fetched, skipped
