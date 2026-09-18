"""Move metrics on hand-built bars, including the split and fade cases."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from app.core import moves
from app.core.moves import Bar
from app.core.timeutils import ET, UTC

OPEN_0400_ET = datetime(2026, 3, 10, 4, 0, tzinfo=ET).astimezone(UTC)


def bars(
    prices: list[tuple[float, float, float, float]], *, start: datetime = OPEN_0400_ET
) -> list[Bar]:
    """Build minute bars from (open, high, low, close) tuples."""
    return [
        Bar(minute=start + timedelta(minutes=i), open=o, high=h, low=lo, close=c, volume=1_000.0)
        for i, (o, h, lo, c) in enumerate(prices)
    ]


def test_no_bars_yields_all_none():
    assert moves.compute([], prev_close=5.0) == moves.MoveMetrics()


def test_up_and_down_move_are_measured_from_prev_close():
    day = bars([(10.0, 12.0, 9.0, 11.0)])
    m = moves.compute(day, prev_close=10.0)
    assert m.up_move_pct == pytest.approx(20.0)
    assert m.down_move_pct == pytest.approx(-10.0)
    assert m.range_pct == pytest.approx(30.0)


def test_a_faded_runner_is_still_a_move():
    """Opens flat, runs +70%, closes +4%: the close-based view misses it."""
    day = bars([(10.0, 10.1, 9.9, 10.0), (10.0, 17.0, 10.0, 16.0), (16.0, 16.0, 10.2, 10.4)])
    m = moves.compute(day, prev_close=10.0)
    assert m.up_move_pct == pytest.approx(70.0)
    assert m.fade_pct == pytest.approx(10.4 / 17.0 * 100 - 100)
    assert m.session_close == 10.4


def test_runup_separates_a_gapper_from_an_intraday_mover():
    gapped = bars([(14.0, 15.0, 14.0, 15.0)])  # already +40%, grinds to +50%
    intraday = bars([(9.5, 9.5, 9.5, 9.5), (9.5, 14.5, 9.5, 14.5)])  # -5% to +45%
    gapped_m = moves.compute(gapped, prev_close=10.0)
    intraday_m = moves.compute(intraday, prev_close=10.0)
    assert gapped_m.up_move_pct == pytest.approx(50.0)
    assert intraday_m.up_move_pct == pytest.approx(45.0)
    # up_move_pct rates the gapper higher; max_runup_pct tells the truth.
    assert gapped_m.max_runup_pct == pytest.approx(7.142857, rel=1e-4)
    assert intraday_m.max_runup_pct == pytest.approx(52.6315, rel=1e-4)


def test_runup_counts_an_intrabar_move():
    assert moves.max_runup_pct(bars([(2.0, 3.4, 2.0, 3.0)])) == pytest.approx(70.0)


def test_drawdown_is_the_mirror_of_runup():
    day = bars([(10.0, 12.0, 10.0, 12.0), (12.0, 12.0, 6.0, 7.0)])
    assert moves.max_drawdown_pct(day) == pytest.approx(-50.0)


def test_drawdown_of_a_monotonic_rise_is_at_most_intrabar():
    day = bars([(10.0, 11.0, 10.0, 11.0), (11.0, 12.0, 11.0, 12.0)])
    # The worst point is the first bar's own low against its own high.
    assert moves.max_drawdown_pct(day) == pytest.approx(10.0 / 11.0 * 100 - 100)


def test_minutes_to_high_is_measured_from_0400_et():
    day = bars([(10.0, 10.0, 10.0, 10.0)] * 3 + [(10.0, 20.0, 10.0, 20.0)])
    assert moves.compute(day, prev_close=10.0).minutes_to_high == pytest.approx(3.0)


def test_pre_to_post_drift_needs_both_ends():
    day = bars([(10.0, 10.0, 10.0, 10.0)])
    assert moves.compute(day, prev_close=10.0, pre_open=10.0, post_close=13.0).pre_to_post_pct == (
        pytest.approx(30.0)
    )
    assert moves.compute(day, prev_close=10.0, pre_open=10.0).pre_to_post_pct is None


def test_metrics_are_none_without_a_prev_close():
    m = moves.compute(bars([(10.0, 12.0, 9.0, 11.0)]), prev_close=None)
    assert m.up_move_pct is None
    assert m.max_runup_pct is not None  # runup needs no reference price


def test_as_of_truncation_prevents_lookahead():
    """A metric described as 'at 08:05' must not see the 09:30 bar."""
    day = bars([(10.0, 10.0, 10.0, 10.0), (10.0, 30.0, 10.0, 30.0)])
    early = moves.compute(day, prev_close=10.0, as_of=day[0].minute)
    assert early.up_move_pct == pytest.approx(0.0)
    late = moves.compute(day, prev_close=10.0)
    assert late.up_move_pct == pytest.approx(200.0)


def test_bars_out_of_order_are_sorted():
    day = list(reversed(bars([(10.0, 11.0, 10.0, 11.0), (11.0, 11.0, 5.0, 5.0)])))
    assert moves.compute(day, prev_close=10.0).session_close == 5.0


# --- mover classification ----------------------------------------------------


@pytest.mark.parametrize(
    ("metrics", "expected"),
    [
        (moves.MoveMetrics(up_move_pct=25.0), True),  # exactly at the threshold
        (moves.MoveMetrics(up_move_pct=24.99), False),
        (moves.MoveMetrics(down_move_pct=-25.0), True),  # dumps count the same
        (moves.MoveMetrics(down_move_pct=-24.99), False),
        (moves.MoveMetrics(max_runup_pct=30.0), True),
        (moves.MoveMetrics(), False),
    ],
)
def test_is_mover_boundaries(metrics, expected):
    assert moves.is_mover(metrics, 25.0) is expected


def test_unknown_metrics_never_qualify_as_a_mover():
    """An unknown day is not a quiet day; a data outage must not prune the week."""
    assert not moves.is_mover(moves.MoveMetrics(), 25.0)


# --- suspect price guard -----------------------------------------------------


def test_suspect_price_flags_an_unexplained_jump(caplog):
    with caplog.at_level("WARNING"):
        flagged = moves.suspect_price(
            19.0, 10.0, threshold=0.8, volume_surge=False, corporate_action_on_record=False
        )
    assert flagged
    assert "Suspect price" in caplog.text


def test_a_reverse_split_is_not_suspect():
    """A 1:10 reverse split is a corporate action, not a -90% move."""
    assert not moves.suspect_price(
        1.0, 10.0, threshold=0.8, volume_surge=False, corporate_action_on_record=True
    )


def test_a_real_move_with_volume_is_not_suspect():
    assert not moves.suspect_price(
        19.0, 10.0, threshold=0.8, volume_surge=True, corporate_action_on_record=False
    )


def test_a_normal_move_is_not_suspect():
    assert not moves.suspect_price(
        13.0, 10.0, threshold=0.8, volume_surge=False, corporate_action_on_record=False
    )


def test_suspect_price_needs_a_prev_close():
    assert not moves.suspect_price(
        13.0, None, threshold=0.8, volume_surge=False, corporate_action_on_record=False
    )
