"""The integrity layer: halts, corporate actions and tradability.

Free data on small caps is wrong in specific, predictable ways, and each of
them corrupts research silently rather than loudly (CLAUDE.md 6.4). This module
holds the pure half of the defences:

* **Halts.** LULD volatility halts are routine on these names. A halted stock's
  last price is stale, a ``window_change_pct`` computed across a halt is
  meaningless, and a stock can reopen 40% higher in one print.
* **Corporate actions.** A reverse split mechanically creates a sub-20M float
  and frequently precedes the exact setup being scanned, while breaking
  ``prev_close``, ``gap_pct`` and every RVOL baseline at once.
* **Tradability.** A +40% move in a 2M-float stock with a 30-cent spread is not
  +40% in an account. Outcomes that ignore this overstate everything.

Built before any research page on purpose: retrofitting these invalidates
everything collected beforehand.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from itertools import pairwise

from app.core.moves import Bar
from app.core.timeutils import to_utc

logger = logging.getLogger(__name__)

_EPSILON = 1e-9


# --- halts -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HaltWindow:
    """A period during which a ticker is believed not to have traded."""

    start: datetime
    end: datetime
    inferred: bool

    @property
    def minutes(self) -> float:
        """Length of the halt in minutes."""
        return (to_utc(self.end) - to_utc(self.start)).total_seconds() / 60.0

    def covers(self, moment: datetime) -> bool:
        """Whether an instant falls inside the halt, half-open."""
        return to_utc(self.start) <= to_utc(moment) < to_utc(self.end)


def infer_halts(
    bars: list[Bar],
    *,
    min_silent_minutes: int,
    market_active: bool = True,
) -> tuple[HaltWindow, ...]:
    """Infer halts from gaps in the minute bars.

    Alpaca's free tier omits minutes with no trades rather than emitting empty
    bars, so a halt shows up as missing minutes. Anything at least
    ``min_silent_minutes`` long during an active session is treated as a halt.

    ``market_active`` is passed in by the caller, which knows whether the broad
    market was trading: outside regular hours, a thin small cap can be silent
    for twenty minutes with nothing halted at all, and inferring halts there
    would label most of pre-market as halted.
    """
    if not market_active or len(bars) < 2:
        return ()
    ordered = sorted(bars, key=lambda bar: to_utc(bar.minute))
    halts: list[HaltWindow] = []
    for previous, current in pairwise(ordered):
        gap_start = to_utc(previous.minute) + timedelta(minutes=1)
        gap_end = to_utc(current.minute)
        silent = (gap_end - gap_start).total_seconds() / 60.0
        if silent >= min_silent_minutes:
            halts.append(HaltWindow(start=gap_start, end=gap_end, inferred=True))
    if halts:
        logger.info(
            "Inferred %s halt(s) totalling %.0f minutes from gaps in the bars",
            len(halts),
            sum(halt.minutes for halt in halts),
        )
    return tuple(halts)


def spans_halt(start: datetime, end: datetime, halts: tuple[HaltWindow, ...]) -> bool:
    """Whether a metric window overlaps any halt.

    Flagged rather than corrected: a change measured across a halt is not
    wrong by a knowable amount, it is meaningless, and research needs to be
    able to exclude it rather than trust a patched-up number.
    """
    window_start, window_end = to_utc(start), to_utc(end)
    return any(
        to_utc(halt.start) < window_end and window_start < to_utc(halt.end) for halt in halts
    )


def halt_summary(halts: tuple[HaltWindow, ...]) -> tuple[int, float]:
    """``(count, total minutes)`` for a ticker-day.

    Halt behaviour is itself a strong momentum feature worth studying, so these
    go on the Tier 0 row rather than only into a log line.
    """
    return len(halts), sum(halt.minutes for halt in halts)


# --- corporate actions -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CorporateAction:
    """A split, symbol change or delisting as recorded in the lake."""

    ticker: str
    effective_date: date
    action_type: str
    ratio: float | None = None
    old_symbol: str | None = None
    new_symbol: str | None = None

    @property
    def is_split(self) -> bool:
        """Whether this action rescales prices and share counts."""
        return self.action_type in {"forward_split", "reverse_split", "split"}

    @property
    def is_reverse_split(self) -> bool:
        """A ratio below 1 means fewer shares at a higher price."""
        return self.is_split and self.ratio is not None and self.ratio < 1.0


def action_on(actions: list[CorporateAction], ticker: str, day: date) -> CorporateAction | None:
    """The corporate action effective for a ticker on a given day, if any."""
    for action in actions:
        if action.ticker == ticker and action.effective_date == day:
            return action
    return None


def days_since_reverse_split(
    actions: list[CorporateAction], ticker: str, day: date, *, horizon_days: int = 365
) -> int | None:
    """Sessions since this ticker's most recent reverse split, if recent.

    Recorded on every Tier 0 row because a reverse split is a confound, not a
    curiosity: it manufactures the small float the scanner is looking for, so
    research has to be able to condition on "how long ago".
    """
    candidates = [
        action.effective_date
        for action in actions
        if action.ticker == ticker
        and action.is_reverse_split
        and 0 <= (day - action.effective_date).days <= horizon_days
    ]
    if not candidates:
        return None
    return (day - max(candidates)).days


def adjust_for_split(value: float | None, ratio: float | None) -> float | None:
    """Rescale a pre-split price or volume onto the post-split basis.

    A 1:10 reverse split has ``ratio = 0.1``: ten old shares become one, so the
    old price is divided by the ratio and the old volume multiplied by it. Used
    to repair an RVOL baseline whose history straddles the split, never to
    rewrite a stored row (CLAUDE.md 6.3a: history is not overwritten).
    """
    if value is None or ratio is None or ratio <= _EPSILON:
        return value
    return value / ratio


def adjust_volume_for_split(volume: float | None, ratio: float | None) -> float | None:
    """The volume counterpart of :func:`adjust_for_split`."""
    if volume is None or ratio is None or ratio <= _EPSILON:
        return volume
    return volume * ratio


def baselines_to_recompute(actions: list[CorporateAction], day: date) -> tuple[str, ...]:
    """Tickers whose RVOL baseline is invalid because of a split today.

    The baseline averages ten days of volume at the same clock time; a split
    rescales share counts, so mixing pre- and post-split days produces a
    baseline off by the split ratio — and therefore an RVOL off by the same
    factor, in the direction that manufactures a pillar-2 pass.
    """
    affected = {
        action.ticker for action in actions if action.is_split and action.effective_date == day
    }
    return tuple(sorted(affected))


# --- tradability -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TradabilityVerdict:
    """Whether a move was actually takeable, and the figures behind it."""

    tradeable: bool
    dollar_volume_in_window: float | None
    est_spread_pct: float | None
    spread_source: str
    reason: str


def estimate_spread_pct(bars: list[Bar]) -> float | None:
    """Estimate a spread from bar high/low dispersion when no quote exists.

    The free data tier gives no bid/ask, so the dispersion within one-minute
    bars stands in for it. It is a crude proxy and is labelled
    ``spread_source="estimated"`` precisely so that no research output can
    quietly treat it as a measured quote.
    """
    usable = [bar for bar in bars if bar.high > _EPSILON and bar.low > _EPSILON]
    if not usable:
        return None
    spreads = [(bar.high - bar.low) / ((bar.high + bar.low) / 2) * 100.0 for bar in usable]
    return sum(spreads) / len(spreads)


def assess_tradability(
    bars: list[Bar],
    *,
    reference_ts: datetime,
    window_minutes: int,
    min_dollar_volume: float,
    max_spread_pct: float,
    quoted_spread_pct: float | None = None,
) -> TradabilityVerdict:
    """Judge whether a signal could have been acted on.

    Measures the dollar volume actually traded in the minutes *after* the
    reference time — the only liquidity a trader reacting to that signal could
    have used — and pairs it with a spread figure. Every outcome row carries
    this so research can report tradeable and all-rows numbers side by side
    instead of quietly reporting only the flattering one (CLAUDE.md 6.4.5).
    """
    start = to_utc(reference_ts)
    end = start + timedelta(minutes=window_minutes)
    in_window = [bar for bar in bars if start <= to_utc(bar.minute) < end]
    if not in_window:
        return TradabilityVerdict(
            tradeable=False,
            dollar_volume_in_window=None,
            est_spread_pct=quoted_spread_pct,
            spread_source="quoted" if quoted_spread_pct is not None else "unavailable",
            reason="no bars in the window after the reference time",
        )

    traded = sum(bar.close * bar.volume for bar in in_window)
    spread = quoted_spread_pct if quoted_spread_pct is not None else estimate_spread_pct(in_window)
    source = "quoted" if quoted_spread_pct is not None else "estimated"

    if traded < min_dollar_volume:
        reason = f"${traded:,.0f} traded in {window_minutes}m is below ${min_dollar_volume:,.0f}"
        return TradabilityVerdict(False, traded, spread, source, reason)
    if spread is None:
        return TradabilityVerdict(False, traded, None, source, "no spread figure available")
    if spread > max_spread_pct:
        reason = f"spread {spread:.2f}% exceeds {max_spread_pct:.2f}%"
        return TradabilityVerdict(False, traded, spread, source, reason)
    return TradabilityVerdict(True, traded, spread, source, "liquid enough to act on")
