"""Runner detection and miss diagnosis.

A runner is a day the scanner *should* have caught. Finding them after the
close and explaining why each was missed is what turns a static threshold set
into something that improves: a rule that misses 40% of runners for one
identifiable reason is a rule with a fixable flaw, and a rule that misses them
for forty different reasons is a rule that does not work (CLAUDE.md 7).

The diagnosis is deliberately multi-labelled. A runner is rarely missed for one
reason — it moved outside the windows *and* its float was 23M *and* the news
came late — and collapsing that to a single cause would hide the pattern worth
seeing.

Pure: bars, evaluations and news are all passed in.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import StrEnum

from app.core.moves import Bar, sorted_bars
from app.core.pillars import near_miss
from app.core.timeutils import et_datetime, et_trading_date, minutes_between, to_et, to_utc
from app.core.types import PillarResult, PillarStatus, Tier

logger = logging.getLogger(__name__)

_EPSILON = 1e-9


class MissReason(StrEnum):
    """Why a runner was not alerted (a runner can carry several)."""

    OUTSIDE_WINDOW = "OUTSIDE_WINDOW"
    FAILED_PILLAR = "FAILED_PILLAR"
    NEAR_MISS = "NEAR_MISS"
    NEWS_LATE = "NEWS_LATE"
    NEWS_STALE = "NEWS_STALE"
    NO_NEWS = "NO_NEWS"
    DATA_MISSING = "DATA_MISSING"
    NOT_IN_UNIVERSE = "NOT_IN_UNIVERSE"
    CAUGHT = "CAUGHT"


@dataclass(frozen=True, slots=True)
class RunnerRules:
    """The configurable definition of a runner (CLAUDE.md 7.1)."""

    high_of_day_pct_min: float
    intraday_move_pct_min: float
    intraday_lookback_minutes: int
    intraday_window: tuple[time, time]
    postmarket_move_pct_min: float
    min_price: float
    min_dollar_volume: float
    move_start_trigger_pct: float


@dataclass(frozen=True, slots=True)
class RunnerDetection:
    """Whether a ticker-day qualifies, and under which rule."""

    is_runner: bool
    rule: str
    high_of_day_pct: float | None = None
    move_start_utc: datetime | None = None
    price_at_move_start: float | None = None
    dollar_volume: float | None = None


def _dollar_volume(bars: list[Bar]) -> float:
    """Session traded value."""
    return sum(bar.close * bar.volume for bar in bars)


def detect(
    bars: list[Bar],
    *,
    prev_close: float | None,
    rules: RunnerRules,
    postmarket_reference: float | None = None,
) -> RunnerDetection:
    """Decide whether a ticker-day is a runner.

    The liquidity and price floors come first: a 300% move in a $0.40 stock on
    $40k of volume is not the setup being traded, and letting it into the
    runner set would drown the genuine ones in noise.
    """
    ordered = sorted_bars(bars)
    if not ordered:
        return RunnerDetection(is_runner=False, rule="no bars")

    last_price = ordered[-1].close
    traded = _dollar_volume(ordered)
    if last_price < rules.min_price or traded < rules.min_dollar_volume:
        return RunnerDetection(
            is_runner=False,
            rule=f"below the ${rules.min_price:g} / ${rules.min_dollar_volume:,.0f} floors",
            dollar_volume=traded,
        )

    day_high = max(bar.high for bar in ordered)
    high_pct = (
        (day_high / prev_close - 1.0) * 100.0
        if prev_close is not None and prev_close > _EPSILON
        else None
    )
    start = move_start(ordered, rules=rules)

    if high_pct is not None and high_pct >= rules.high_of_day_pct_min:
        return RunnerDetection(
            is_runner=True,
            rule=f"high of day +{high_pct:.0f}% vs prev close",
            high_of_day_pct=high_pct,
            move_start_utc=start,
            price_at_move_start=_price_at(ordered, start),
            dollar_volume=traded,
        )

    intraday = largest_intraday_move(ordered, rules=rules)
    if intraday is not None and intraday >= rules.intraday_move_pct_min:
        return RunnerDetection(
            is_runner=True,
            rule=f"intraday +{intraday:.0f}% from a 15-minute low",
            high_of_day_pct=high_pct,
            move_start_utc=start,
            price_at_move_start=_price_at(ordered, start),
            dollar_volume=traded,
        )

    if postmarket_reference is not None and postmarket_reference > _EPSILON:
        post_bars = [
            bar
            for bar in ordered
            if to_utc(bar.minute) >= et_datetime(et_trading_date(ordered[0].minute), time(16, 0))
        ]
        if post_bars:
            post_high = max(bar.high for bar in post_bars)
            post_move = (post_high / postmarket_reference - 1.0) * 100.0
            if post_move >= rules.postmarket_move_pct_min:
                return RunnerDetection(
                    is_runner=True,
                    rule=f"post-market +{post_move:.0f}% from the 16:00 price",
                    high_of_day_pct=high_pct,
                    move_start_utc=start,
                    price_at_move_start=_price_at(ordered, start),
                    dollar_volume=traded,
                )

    return RunnerDetection(
        is_runner=False,
        rule="no runner rule met",
        high_of_day_pct=high_pct,
        dollar_volume=traded,
    )


def _price_at(bars: list[Bar], moment: datetime | None) -> float | None:
    """Close of the bar at ``moment``."""
    if moment is None:
        return None
    for bar in bars:
        if to_utc(bar.minute) == to_utc(moment):
            return bar.close
    return None


def largest_intraday_move(bars: list[Bar], *, rules: RunnerRules) -> float | None:
    """Largest rise from a rolling low to a later high inside the window.

    Rolling rather than session-wide: the rule is about a move that *starts*
    somewhere, and a stock grinding up all morning is a different animal from
    one that doubles in fifteen minutes off a base.
    """
    start, end = rules.intraday_window
    if not bars:
        return None
    day = et_trading_date(bars[0].minute)
    window_start, window_end = et_datetime(day, start), et_datetime(day, end)
    window = [bar for bar in sorted_bars(bars) if window_start <= to_utc(bar.minute) < window_end]
    if len(window) < 2:
        return None

    best: float | None = None
    lookback = timedelta(minutes=rules.intraday_lookback_minutes)
    for index, bar in enumerate(window):
        recent = [
            candidate
            for candidate in window[: index + 1]
            if to_utc(bar.minute) - to_utc(candidate.minute) <= lookback
        ]
        base = min(candidate.low for candidate in recent)
        if base <= _EPSILON:
            continue
        move = (bar.high / base - 1.0) * 100.0
        best = move if best is None else max(best, move)
    return best


def move_start(bars: list[Bar], *, rules: RunnerRules) -> datetime | None:
    """First minute the move from the prior 15-minute low reached the trigger.

    This is the number the whole Missed Runners page hangs on: it says *when*
    the opportunity appeared, and therefore whether any alert window could ever
    have caught it (CLAUDE.md 7.2).
    """
    ordered = sorted_bars(bars)
    if not ordered:
        return None
    lookback = timedelta(minutes=rules.intraday_lookback_minutes)
    for index, bar in enumerate(ordered):
        recent = [
            candidate
            for candidate in ordered[: index + 1]
            if to_utc(bar.minute) - to_utc(candidate.minute) <= lookback
        ]
        base = min(candidate.low for candidate in recent)
        if base <= _EPSILON:
            continue
        if (bar.high / base - 1.0) * 100.0 >= rules.move_start_trigger_pct:
            return to_utc(bar.minute)
    return None


@dataclass(frozen=True, slots=True)
class EvaluationSummary:
    """What the scanner saw for one ticker on one day.

    Built from the ``evaluations`` table, which is why every evaluation is
    stored: without the rows that never alerted there is nothing to diagnose.
    """

    ticker: str
    best_tier: Tier = Tier.NONE
    best_tier_ts_utc: datetime | None = None
    pillar_results: tuple[PillarResult, ...] = ()
    evaluated: bool = False


@dataclass(frozen=True, slots=True)
class Diagnosis:
    """Why a runner was or was not caught."""

    ticker: str
    reasons: tuple[MissReason, ...]
    detail: str
    move_start_utc: datetime | None = None
    first_news_utc: datetime | None = None
    news_lag_minutes: float | None = None
    best_tier: Tier = Tier.NONE
    caught: bool = False


@dataclass(frozen=True, slots=True)
class DiagnosisInputs:
    """Everything the diagnosis needs, gathered by the caller."""

    detection: RunnerDetection
    summary: EvaluationSummary
    alert_windows: tuple[tuple[time, time], ...]
    first_news_utc: datetime | None = None
    near_miss_fraction: float = 0.20
    news_fresh_minutes: int = 15
    reasons: list[MissReason] = field(default_factory=list)


def in_any_window(moment: datetime, windows: tuple[tuple[time, time], ...]) -> bool:
    """Whether an instant falls inside any configured alert window."""

    wall = to_et(moment).time()
    return any(start <= wall < end for start, end in windows)


def diagnose(inputs: DiagnosisInputs) -> Diagnosis:
    """Explain one runner.

    Order of checks reflects what the trader would ask first: was it even in
    the universe, did it move when nobody was looking, and only then which
    pillar stopped it.
    """
    detection = inputs.detection
    summary = inputs.summary
    reasons: list[MissReason] = []
    details: list[str] = []

    if summary.best_tier in (Tier.A, Tier.B):
        lag = _news_lag(detection.move_start_utc, inputs.first_news_utc)
        return Diagnosis(
            ticker=summary.ticker,
            reasons=(MissReason.CAUGHT,),
            detail=f"alerted at tier {summary.best_tier}",
            move_start_utc=detection.move_start_utc,
            first_news_utc=inputs.first_news_utc,
            news_lag_minutes=lag,
            best_tier=summary.best_tier,
            caught=True,
        )

    if not summary.evaluated:
        reasons.append(MissReason.NOT_IN_UNIVERSE)
        details.append("never appeared in a snapshot: the collector filter did not select it")

    if detection.move_start_utc is not None and not in_any_window(
        detection.move_start_utc, inputs.alert_windows
    ):
        from app.core.timeutils import to_et

        reasons.append(MissReason.OUTSIDE_WINDOW)
        started = to_et(detection.move_start_utc)
        details.append(f"move started {started:%H:%M} ET, outside every alert window")

    for result in summary.pillar_results:
        if result.status is PillarStatus.UNKNOWN:
            if MissReason.DATA_MISSING not in reasons:
                reasons.append(MissReason.DATA_MISSING)
            details.append(f"pillar {result.number} ({result.name}) unknown: {result.detail}")
            continue
        if result.status is not PillarStatus.FAIL:
            continue
        if result.number == 3:
            continue  # news reasons are reported separately below
        reasons.append(MissReason.FAILED_PILLAR)
        details.append(
            f"pillar {result.number} ({result.name}) failed: "
            f"{_format(result.value)} vs {_format(result.threshold)}"
        )
        if near_miss(result, inputs.near_miss_fraction):
            reasons.append(MissReason.NEAR_MISS)
            details.append(
                f"pillar {result.number} was within "
                f"{inputs.near_miss_fraction:.0%} of its threshold"
            )

    lag = _news_lag(detection.move_start_utc, inputs.first_news_utc)
    if inputs.first_news_utc is None:
        reasons.append(MissReason.NO_NEWS)
        details.append("no news recorded for this ticker that day")
    elif lag is not None and lag > inputs.news_fresh_minutes:
        reasons.append(MissReason.NEWS_LATE)
        details.append(f"first news arrived {lag:.0f} min after the move started")
    elif lag is not None and lag < -inputs.news_fresh_minutes:
        reasons.append(MissReason.NEWS_STALE)
        details.append(f"news predated the move by {-lag:.0f} min: stale by the freshness rule")

    return Diagnosis(
        ticker=summary.ticker,
        reasons=tuple(dict.fromkeys(reasons)),  # de-duplicated, order preserved
        detail="; ".join(details),
        move_start_utc=detection.move_start_utc,
        first_news_utc=inputs.first_news_utc,
        news_lag_minutes=lag,
        best_tier=summary.best_tier,
        caught=False,
    )


def _news_lag(move_start_utc: datetime | None, first_news_utc: datetime | None) -> float | None:
    """Minutes from the move start to the first headline (negative = before)."""
    if move_start_utc is None or first_news_utc is None:
        return None
    return minutes_between(move_start_utc, first_news_utc)


def _format(value: float | None) -> str:
    """Render a pillar value for the diagnosis text."""
    if value is None:
        return "?"
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    return f"{value:.2f}"


def runner_row(
    detection: RunnerDetection,
    diagnosis: Diagnosis,
    day: date,
    *,
    float_shares: int | None,
    now: datetime,
) -> dict[str, object]:
    """Build the ``runners`` lake row."""
    return {
        "ticker": diagnosis.ticker,
        "date": day,
        "high_of_day_pct": detection.high_of_day_pct,
        "move_start_utc": detection.move_start_utc,
        "first_news_utc": diagnosis.first_news_utc,
        "news_lag_minutes": diagnosis.news_lag_minutes,
        "best_tier": diagnosis.best_tier.value,
        "best_tier_ts_utc": None,
        "price_at_move_start": detection.price_at_move_start,
        "float_shares": float_shares,
        "dollar_volume": detection.dollar_volume,
        "miss_reasons": [reason.value for reason in diagnosis.reasons],
        "miss_detail": diagnosis.detail,
        "qualifying_rule": detection.rule,
        "written_at_utc": to_utc(now),
    }
