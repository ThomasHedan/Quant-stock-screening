"""Runner detection and miss-reason diagnosis.

Acceptance criterion 11.3: a synthetic runner starting at 07:20 ET shows
OUTSIDE_WINDOW, and one with a 23M float shows FAILED_PILLAR + NEAR_MISS.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest
from app.core import runners
from app.core.moves import Bar
from app.core.runners import (
    DiagnosisInputs,
    EvaluationSummary,
    MissReason,
    RunnerRules,
    detect,
    diagnose,
)
from app.core.timeutils import ET, UTC, et_datetime
from app.core.types import FloatConfidence, PillarResult, PillarStatus, Tier

DAY = date(2026, 3, 10)
RULES = RunnerRules(
    high_of_day_pct_min=50.0,
    intraday_move_pct_min=30.0,
    intraday_lookback_minutes=15,
    intraday_window=(time(4, 0), time(11, 0)),
    postmarket_move_pct_min=30.0,
    min_price=1.00,
    min_dollar_volume=1_000_000.0,
    move_start_trigger_pct=10.0,
)
WINDOWS = (
    (time(8, 0), time(8, 5)),
    (time(8, 30), time(8, 35)),
    (time(9, 0), time(9, 5)),
    (time(16, 0), time(16, 5)),
    (time(16, 30), time(16, 35)),
)


def bars_from(start_et: time, closes: list[float], *, volume: float = 200_000.0) -> list[Bar]:
    start = et_datetime(DAY, start_et)
    return [
        Bar(
            minute=start + timedelta(minutes=i),
            open=close,
            high=close * 1.01,
            low=close * 0.99,
            close=close,
            volume=volume,
        )
        for i, close in enumerate(closes)
    ]


# --- detection ---------------------------------------------------------------


def test_a_fifty_percent_high_of_day_is_a_runner():
    bars = bars_from(time(7, 20), [10.0, 12.0, 16.0])
    result = detect(bars, prev_close=10.0, rules=RULES)
    assert result.is_runner
    assert "high of day" in result.rule
    assert result.high_of_day_pct == pytest.approx(61.6, rel=0.01)


def test_a_thirty_percent_intraday_move_is_a_runner_without_the_gap():
    bars = bars_from(time(7, 20), [10.0] * 5 + [13.5])
    result = detect(bars, prev_close=10.5, rules=RULES)
    assert result.is_runner
    assert "intraday" in result.rule


def test_a_grind_below_every_threshold_is_not_a_runner():
    bars = bars_from(time(7, 20), [10.0, 10.2, 10.5])
    assert not detect(bars, prev_close=10.0, rules=RULES).is_runner


def test_a_penny_stock_is_excluded_by_the_price_floor():
    """A 300% move in a $0.40 stock is not the setup being traded."""
    bars = bars_from(time(7, 20), [0.10, 0.20, 0.40], volume=10_000_000.0)
    result = detect(bars, prev_close=0.10, rules=RULES)
    assert not result.is_runner
    assert "floors" in result.rule


def test_an_illiquid_mover_is_excluded_by_the_dollar_volume_floor():
    bars = bars_from(time(7, 20), [10.0, 20.0], volume=100.0)
    assert not detect(bars, prev_close=10.0, rules=RULES).is_runner


def test_a_post_market_move_qualifies():
    bars = bars_from(time(16, 0), [10.0, 11.0, 14.0])
    result = detect(bars, prev_close=10.0, rules=RULES, postmarket_reference=10.0)
    assert result.is_runner
    assert "post-market" in result.rule


def test_no_bars_is_not_a_runner():
    assert not detect([], prev_close=10.0, rules=RULES).is_runner


# --- move start --------------------------------------------------------------


def test_move_start_is_the_first_ten_percent_minute():
    bars = bars_from(time(7, 20), [10.0, 10.2, 11.5, 16.0])
    start = runners.move_start(bars, rules=RULES)
    assert start is not None
    assert start.astimezone(ET).strftime("%H:%M") == "07:22"


def test_move_start_is_none_for_a_flat_day():
    assert runners.move_start(bars_from(time(7, 20), [10.0, 10.1]), rules=RULES) is None


def test_intraday_move_uses_a_rolling_low_not_the_session_low():
    """A stock grinding up all morning is not a stock doubling off a base."""
    grind = bars_from(time(4, 0), [10.0 + i * 0.05 for i in range(120)])
    move = runners.largest_intraday_move(grind, rules=RULES)
    assert move is not None
    assert move < RULES.intraday_move_pct_min


# --- diagnosis ---------------------------------------------------------------


def summary_with(*results: PillarResult, evaluated: bool = True, tier: Tier = Tier.NONE):
    return EvaluationSummary(
        ticker="ABCD", best_tier=tier, pillar_results=results, evaluated=evaluated
    )


def test_a_runner_that_moved_at_0720_is_outside_every_window():
    bars = bars_from(time(7, 20), [10.0, 12.0, 16.0])
    detection = detect(bars, prev_close=10.0, rules=RULES)
    diagnosis = diagnose(
        DiagnosisInputs(detection=detection, summary=summary_with(), alert_windows=WINDOWS)
    )
    assert MissReason.OUTSIDE_WINDOW in diagnosis.reasons
    assert "07:2" in diagnosis.detail


def test_a_move_inside_a_window_is_not_flagged_outside_window():
    bars = bars_from(time(8, 1), [10.0, 12.0, 16.0])
    detection = detect(bars, prev_close=10.0, rules=RULES)
    diagnosis = diagnose(
        DiagnosisInputs(detection=detection, summary=summary_with(), alert_windows=WINDOWS)
    )
    assert MissReason.OUTSIDE_WINDOW not in diagnosis.reasons


def test_a_23m_float_is_a_failed_pillar_and_a_near_miss():
    from app.core.pillars import check_float

    bars = bars_from(time(8, 1), [10.0, 12.0, 16.0])
    detection = detect(bars, prev_close=10.0, rules=RULES)
    result = check_float(23_000_000, FloatConfidence.HIGH, 20_000_000)
    diagnosis = diagnose(
        DiagnosisInputs(
            detection=detection,
            summary=summary_with(result),
            alert_windows=WINDOWS,
            first_news_utc=detection.move_start_utc,
        )
    )
    assert MissReason.FAILED_PILLAR in diagnosis.reasons
    assert MissReason.NEAR_MISS in diagnosis.reasons
    assert "23.0M" in diagnosis.detail


def test_an_80m_float_fails_without_a_near_miss():
    from app.core.pillars import check_float

    bars = bars_from(time(8, 1), [10.0, 16.0])
    detection = detect(bars, prev_close=10.0, rules=RULES)
    diagnosis = diagnose(
        DiagnosisInputs(
            detection=detection,
            summary=summary_with(check_float(80_000_000, FloatConfidence.HIGH, 20_000_000)),
            alert_windows=WINDOWS,
            first_news_utc=detection.move_start_utc,
        )
    )
    assert MissReason.FAILED_PILLAR in diagnosis.reasons
    assert MissReason.NEAR_MISS not in diagnosis.reasons


def test_a_ticker_never_evaluated_is_not_in_universe():
    bars = bars_from(time(8, 1), [10.0, 16.0])
    diagnosis = diagnose(
        DiagnosisInputs(
            detection=detect(bars, prev_close=10.0, rules=RULES),
            summary=summary_with(evaluated=False),
            alert_windows=WINDOWS,
        )
    )
    assert MissReason.NOT_IN_UNIVERSE in diagnosis.reasons


def test_unknown_pillars_are_data_missing_not_failed_pillars():
    unknown = PillarResult(
        number=5,
        name="float",
        status=PillarStatus.UNKNOWN,
        value=None,
        threshold=20_000_000.0,
        detail="float_confidence=low",
    )
    bars = bars_from(time(8, 1), [10.0, 16.0])
    diagnosis = diagnose(
        DiagnosisInputs(
            detection=detect(bars, prev_close=10.0, rules=RULES),
            summary=summary_with(unknown),
            alert_windows=WINDOWS,
            first_news_utc=None,
        )
    )
    assert MissReason.DATA_MISSING in diagnosis.reasons
    assert MissReason.FAILED_PILLAR not in diagnosis.reasons


def test_news_after_the_move_is_news_late():
    bars = bars_from(time(8, 1), [10.0, 12.0, 16.0])
    detection = detect(bars, prev_close=10.0, rules=RULES)
    assert detection.move_start_utc is not None
    diagnosis = diagnose(
        DiagnosisInputs(
            detection=detection,
            summary=summary_with(),
            alert_windows=WINDOWS,
            first_news_utc=detection.move_start_utc + timedelta(minutes=40),
        )
    )
    assert MissReason.NEWS_LATE in diagnosis.reasons
    assert diagnosis.news_lag_minutes == pytest.approx(40.0)


def test_news_long_before_the_move_is_news_stale():
    bars = bars_from(time(8, 1), [10.0, 12.0, 16.0])
    detection = detect(bars, prev_close=10.0, rules=RULES)
    assert detection.move_start_utc is not None
    diagnosis = diagnose(
        DiagnosisInputs(
            detection=detection,
            summary=summary_with(),
            alert_windows=WINDOWS,
            first_news_utc=detection.move_start_utc - timedelta(hours=3),
        )
    )
    assert MissReason.NEWS_STALE in diagnosis.reasons


def test_no_news_at_all():
    bars = bars_from(time(8, 1), [10.0, 16.0])
    diagnosis = diagnose(
        DiagnosisInputs(
            detection=detect(bars, prev_close=10.0, rules=RULES),
            summary=summary_with(),
            alert_windows=WINDOWS,
            first_news_utc=None,
        )
    )
    assert MissReason.NO_NEWS in diagnosis.reasons


def test_an_alerted_runner_is_caught_and_still_listed():
    bars = bars_from(time(8, 1), [10.0, 16.0])
    diagnosis = diagnose(
        DiagnosisInputs(
            detection=detect(bars, prev_close=10.0, rules=RULES),
            summary=summary_with(tier=Tier.A),
            alert_windows=WINDOWS,
        )
    )
    assert diagnosis.reasons == (MissReason.CAUGHT,)
    assert diagnosis.caught


def test_a_runner_can_carry_several_reasons():
    """Collapsing multi-cause misses to one reason hides the pattern."""
    from app.core.pillars import check_float

    bars = bars_from(time(7, 20), [10.0, 12.0, 16.0])
    detection = detect(bars, prev_close=10.0, rules=RULES)
    diagnosis = diagnose(
        DiagnosisInputs(
            detection=detection,
            summary=summary_with(check_float(23_000_000, FloatConfidence.HIGH, 20_000_000)),
            alert_windows=WINDOWS,
            first_news_utc=None,
        )
    )
    assert {
        MissReason.OUTSIDE_WINDOW,
        MissReason.FAILED_PILLAR,
        MissReason.NEAR_MISS,
        MissReason.NO_NEWS,
    } <= set(diagnosis.reasons)


def test_runner_row_shape():
    bars = bars_from(time(7, 20), [10.0, 16.0])
    detection = detect(bars, prev_close=10.0, rules=RULES)
    diagnosis = diagnose(
        DiagnosisInputs(detection=detection, summary=summary_with(), alert_windows=WINDOWS)
    )
    now = datetime(2026, 3, 10, 20, 15, tzinfo=UTC)
    row = runners.runner_row(detection, diagnosis, DAY, float_shares=4_100_000, now=now)
    assert row["ticker"] == "ABCD"
    assert row["miss_reasons"]
    assert row["qualifying_rule"]
