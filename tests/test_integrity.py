"""Halt inference, the split guard and tradability boundaries.

Acceptance criteria covered here (CLAUDE.md 11.9, 11.11): a synthetic 1:10
reverse split is a corporate action rather than a -90% move and triggers an
RVOL baseline recompute, and a $10k post-signal dollar volume is recorded
tradeable = false.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from app.core import integrity
from app.core.integrity import CorporateAction
from app.core.moves import Bar
from app.core.timeutils import UTC

DAY = date(2026, 3, 10)
OPEN = datetime(2026, 3, 10, 14, 30, tzinfo=UTC)  # 09:30 ET


def bars_at(minutes: list[int], *, close: float = 10.0, volume: float = 1_000.0) -> list[Bar]:
    return [
        Bar(
            minute=OPEN + timedelta(minutes=m),
            open=close,
            high=close * 1.01,
            low=close * 0.99,
            close=close,
            volume=volume,
        )
        for m in minutes
    ]


# --- halts -------------------------------------------------------------------


def test_continuous_bars_infer_no_halt():
    assert integrity.infer_halts(bars_at([0, 1, 2, 3]), min_silent_minutes=3) == ()


def test_a_long_silence_is_inferred_as_a_halt(caplog):
    with caplog.at_level("INFO"):
        halts = integrity.infer_halts(bars_at([0, 1, 10]), min_silent_minutes=3)
    assert len(halts) == 1
    assert halts[0].minutes == pytest.approx(8.0)
    assert halts[0].inferred
    assert "Inferred 1 halt" in caplog.text


def test_a_silence_just_under_the_threshold_is_not_a_halt():
    assert integrity.infer_halts(bars_at([0, 3]), min_silent_minutes=3) == ()


def test_a_silence_exactly_at_the_threshold_is_a_halt():
    assert len(integrity.infer_halts(bars_at([0, 4]), min_silent_minutes=3)) == 1


def test_no_halts_are_inferred_when_the_market_is_closed():
    """A thin small cap is silent for 20 minutes pre-market with nothing halted."""
    quiet = bars_at([0, 30, 60])
    assert integrity.infer_halts(quiet, min_silent_minutes=3, market_active=False) == ()


def test_spans_halt_flags_an_overlapping_window():
    halts = integrity.infer_halts(bars_at([0, 1, 10]), min_silent_minutes=3)
    assert integrity.spans_halt(OPEN, OPEN + timedelta(minutes=12), halts)


def test_spans_halt_is_false_for_a_clean_window():
    halts = integrity.infer_halts(bars_at([0, 1, 10]), min_silent_minutes=3)
    after = OPEN + timedelta(minutes=10)
    assert not integrity.spans_halt(after, after + timedelta(minutes=5), halts)


def test_halt_summary_counts_and_totals():
    halts = integrity.infer_halts(bars_at([0, 1, 10, 30]), min_silent_minutes=3)
    count, minutes = integrity.halt_summary(halts)
    assert count == 2
    assert minutes == pytest.approx(8.0 + 19.0)


# --- corporate actions -------------------------------------------------------


REVERSE_SPLIT = CorporateAction(
    ticker="ABCD", effective_date=DAY, action_type="reverse_split", ratio=0.1
)
FORWARD_SPLIT = CorporateAction(
    ticker="EFGH", effective_date=DAY, action_type="forward_split", ratio=2.0
)


def test_reverse_split_is_recognised():
    assert REVERSE_SPLIT.is_split
    assert REVERSE_SPLIT.is_reverse_split
    assert not FORWARD_SPLIT.is_reverse_split


def test_action_on_finds_the_days_action():
    assert integrity.action_on([REVERSE_SPLIT], "ABCD", DAY) is REVERSE_SPLIT
    assert integrity.action_on([REVERSE_SPLIT], "ABCD", DAY + timedelta(days=1)) is None


def test_a_one_for_ten_reverse_split_is_not_a_ninety_percent_move():
    """The whole point of the split guard, stated as one test."""
    from app.core import moves

    pre_split_close = 1.00
    post_split_price = 10.00
    # Without the guard this looks like +900%; with prices on a common basis it is flat.
    adjusted_prev_close = integrity.adjust_for_split(pre_split_close, REVERSE_SPLIT.ratio)
    assert adjusted_prev_close == pytest.approx(10.0)
    assert moves.compute(
        bars_at([0], close=post_split_price), prev_close=adjusted_prev_close
    ).up_move_pct == pytest.approx(1.0, abs=1.5)


def test_no_suspect_price_row_for_a_split_on_record():
    from app.core import moves

    assert not moves.suspect_price(
        10.0, 1.0, threshold=0.8, volume_surge=False, corporate_action_on_record=True
    )


def test_volume_is_adjusted_the_other_way():
    assert integrity.adjust_volume_for_split(1_000_000.0, 0.1) == pytest.approx(100_000.0)


def test_adjustment_is_a_no_op_without_a_ratio():
    assert integrity.adjust_for_split(5.0, None) == 5.0
    assert integrity.adjust_for_split(None, 0.1) is None


def test_split_days_trigger_a_baseline_recompute():
    affected = integrity.baselines_to_recompute([REVERSE_SPLIT, FORWARD_SPLIT], DAY)
    assert affected == ("ABCD", "EFGH")


def test_no_recompute_on_a_day_without_splits():
    assert integrity.baselines_to_recompute([REVERSE_SPLIT], DAY + timedelta(days=1)) == ()


def test_days_since_reverse_split_is_recorded():
    later = DAY + timedelta(days=3)
    assert integrity.days_since_reverse_split([REVERSE_SPLIT], "ABCD", later) == 3


def test_days_since_reverse_split_ignores_forward_splits():
    assert integrity.days_since_reverse_split([FORWARD_SPLIT], "EFGH", DAY) is None


def test_days_since_reverse_split_beyond_the_horizon_is_none():
    far = DAY + timedelta(days=400)
    assert integrity.days_since_reverse_split([REVERSE_SPLIT], "ABCD", far) is None


# --- tradability -------------------------------------------------------------


def assess(bars: list[Bar], **overrides: object):
    kwargs = {
        "reference_ts": OPEN,
        "window_minutes": 5,
        "min_dollar_volume": 50_000.0,
        "max_spread_pct": 2.0,
    }
    return integrity.assess_tradability(bars, **{**kwargs, **overrides})


def test_a_liquid_move_is_tradeable():
    verdict = assess(bars_at([0, 1, 2, 3, 4], close=10.0, volume=5_000.0))
    assert verdict.tradeable
    assert verdict.dollar_volume_in_window == pytest.approx(250_000.0)
    assert verdict.spread_source == "estimated"


def test_a_ten_thousand_dollar_runner_is_not_tradeable():
    """A +40% move nobody could have taken is not a +40% result."""
    verdict = assess(bars_at([0, 1], close=10.0, volume=500.0))
    assert not verdict.tradeable
    assert verdict.dollar_volume_in_window == pytest.approx(10_000.0)
    assert "below" in verdict.reason


def test_dollar_volume_exactly_at_the_floor_is_tradeable():
    verdict = assess(bars_at([0], close=10.0, volume=5_000.0))
    assert verdict.dollar_volume_in_window == pytest.approx(50_000.0)
    assert verdict.tradeable


def test_a_wide_spread_disqualifies_a_liquid_move():
    wide = [
        Bar(minute=OPEN, open=10.0, high=10.5, low=9.5, close=10.0, volume=100_000.0),
    ]
    verdict = assess(wide, max_spread_pct=2.0)
    assert not verdict.tradeable
    assert "spread" in verdict.reason


def test_a_quoted_spread_is_preferred_over_the_estimate():
    verdict = assess(bars_at([0], volume=100_000.0), quoted_spread_pct=0.4)
    assert verdict.spread_source == "quoted"
    assert verdict.est_spread_pct == 0.4


def test_only_bars_after_the_reference_time_count():
    """The only liquidity a trader reacting to the signal could have used."""
    before = [
        Bar(
            minute=OPEN - timedelta(minutes=m),
            open=10.0,
            high=10.1,
            low=9.9,
            close=10.0,
            volume=1_000_000.0,
        )
        for m in range(1, 5)
    ]
    verdict = assess(before)
    assert not verdict.tradeable
    assert "no bars in the window" in verdict.reason


def test_spread_estimate_of_no_bars_is_none():
    assert integrity.estimate_spread_pct([]) is None
