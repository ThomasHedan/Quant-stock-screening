"""Outcome metrics on hand-built bars: no lookahead, honest about liquidity."""

from __future__ import annotations

from datetime import datetime, time, timedelta

import pytest
from app.core import outcomes
from app.core.integrity import HaltWindow
from app.core.moves import Bar
from app.core.timeutils import ET, UTC

REF = datetime(2026, 3, 10, 8, 5, tzinfo=ET).astimezone(UTC)


def series(closes: list[float], *, start: datetime = REF, volume: float = 20_000.0) -> list[Bar]:
    return [
        Bar(
            minute=start + timedelta(minutes=i),
            open=close,
            high=close * 1.005,
            low=close * 0.995,
            close=close,
            volume=volume,
            vwap=close,
        )
        for i, close in enumerate(closes)
    ]


# --- price lookup ------------------------------------------------------------


def test_price_at_uses_the_covering_bar():
    assert outcomes.price_at(series([10.0, 11.0, 12.0]), REF + timedelta(minutes=1)) == 11.0


def test_price_at_falls_back_to_the_last_print():
    """A thin small cap often has no print in the reference minute at all."""
    bars = [series([10.0])[0], series([13.0], start=REF + timedelta(minutes=5))[0]]
    assert outcomes.price_at(bars, REF + timedelta(minutes=2)) == 10.0


def test_price_at_before_any_bar_is_none():
    assert outcomes.price_at(series([10.0]), REF - timedelta(minutes=1)) is None


# --- forward returns ---------------------------------------------------------


def test_forward_return_at_five_minutes():
    bars = series([10.0] * 5 + [12.0])
    assert outcomes.forward_return_pct(bars, reference_ts=REF, minutes=5) == pytest.approx(20.0)


def test_forward_return_never_reaches_past_its_tolerance():
    """A '+60 min' return taken from a bar four hours later is not that number."""
    bars = series([10.0]) + series([30.0], start=REF + timedelta(hours=4))
    assert outcomes.forward_return_pct(bars, reference_ts=REF, minutes=60) is None


def test_forward_return_tolerates_a_couple_of_silent_minutes():
    bars = series([10.0]) + series([11.0], start=REF + timedelta(minutes=6))
    assert outcomes.forward_return_pct(bars, reference_ts=REF, minutes=5) == pytest.approx(10.0)


def test_forward_return_without_a_reference_price_is_none():
    assert outcomes.forward_return_pct([], reference_ts=REF, minutes=5) is None


# --- excursions --------------------------------------------------------------


def test_mfe_and_mae_bracket_the_path():
    bars = series([10.0, 14.0, 7.0, 11.0])
    mfe, mae = outcomes.excursions(bars, reference_ts=REF, until=REF + timedelta(minutes=10))
    assert mfe == pytest.approx(14.0 * 1.005 / 10.0 * 100 - 100, rel=1e-3)
    assert mae == pytest.approx(7.0 * 0.995 / 10.0 * 100 - 100, rel=1e-3)


def test_excursions_ignore_bars_before_the_reference():
    """Lookahead in reverse: a spike before entry is not the trade's excursion."""
    before = series([50.0], start=REF - timedelta(minutes=5))
    bars = before + series([10.0, 11.0])
    mfe, _mae = outcomes.excursions(bars, reference_ts=REF, until=REF + timedelta(minutes=10))
    assert mfe is not None
    assert mfe < 20.0


def test_excursions_of_an_empty_window():
    assert outcomes.excursions([], reference_ts=REF, until=REF) == (None, None)


# --- vwap --------------------------------------------------------------------


def test_held_above_vwap_true_on_a_rising_series():
    bars = series([10.0, 11.0, 12.0, 13.0])
    assert outcomes.held_above_vwap(bars, at=REF + timedelta(minutes=3)) is True


def test_held_above_vwap_false_on_a_fading_series():
    bars = series([13.0, 12.0, 11.0, 9.0])
    assert outcomes.held_above_vwap(bars, at=REF + timedelta(minutes=3)) is False


def test_held_above_vwap_unknown_without_bars():
    """'No bars' is not the same finding as 'it failed'."""
    assert outcomes.held_above_vwap([], at=REF) is None


# --- the whole row -----------------------------------------------------------


def full_day() -> list[Bar]:
    rise = series([4.0 + i * 0.1 for i in range(30)], volume=60_000.0)
    fade = series(
        [7.0 - i * 0.05 for i in range(60)],
        start=REF + timedelta(minutes=30),
        volume=30_000.0,
    )
    return rise + fade


def test_compute_produces_every_metric():
    metrics = outcomes.compute(
        full_day(),
        reference_ts=REF,
        forward_minutes=(5, 15, 30, 60),
        prev_close=3.00,
    )
    assert metrics.reference_price == 4.0
    assert set(metrics.forward_returns) == {5, 15, 30, 60}
    assert metrics.mfe_pct is not None and metrics.mfe_pct > 0
    assert metrics.high_of_day_pct is not None
    assert metrics.minutes_to_high is not None
    assert metrics.tradeable is True


def test_a_thin_runner_is_recorded_untradeable():
    """A +40% move nobody could take is not a +40% result."""
    thin = series([4.0, 5.6], volume=100.0)
    metrics = outcomes.compute(thin, reference_ts=REF, forward_minutes=(5,), prev_close=3.00)
    assert metrics.tradeable is False
    assert metrics.dollar_volume_in_window is not None
    assert metrics.dollar_volume_in_window < 50_000


def test_a_reference_spanning_a_halt_is_flagged():
    halt = HaltWindow(
        start=REF + timedelta(minutes=2), end=REF + timedelta(minutes=20), inferred=True
    )
    metrics = outcomes.compute(
        full_day(), reference_ts=REF, forward_minutes=(5,), prev_close=3.00, halts=(halt,)
    )
    assert metrics.spans_halt is True


def test_high_of_day_needs_a_prev_close():
    metrics = outcomes.compute(full_day(), reference_ts=REF, forward_minutes=(5,), prev_close=None)
    assert metrics.high_of_day_pct is None


def test_outcome_row_shape():
    metrics = outcomes.compute(
        full_day(), reference_ts=REF, forward_minutes=(5, 15, 30, 60), prev_close=3.00
    )
    row = outcomes.outcome_row("ABCD", metrics, now=REF)
    assert row["ticker"] == "ABCD"
    assert str(row["date"]) == "2026-03-10"
    assert row["ret_5m_pct"] is not None
    assert row["tradeable"] in (True, False)
    assert row["spans_halt"] in (True, False)


def test_post_market_reference_measures_to_the_session_end():
    """An 16:05 reference cannot have its excursion measured to 11:00."""
    late_ref = datetime(2026, 3, 10, 16, 5, tzinfo=ET).astimezone(UTC)
    bars = series([10.0, 12.0], start=late_ref)
    metrics = outcomes.compute(
        bars,
        reference_ts=late_ref,
        forward_minutes=(5,),
        prev_close=9.0,
        mfe_until_et=time(11, 0),
    )
    assert metrics.mfe_pct is not None


# --- scoping -----------------------------------------------------------------


def test_scope_takes_the_largest_movers_first():
    fetched, skipped = outcomes.scope_bar_fetch({"A": 12.0, "B": 80.0, "C": 45.0}, max_tickers=2)
    assert fetched == ("B", "C")
    assert skipped == ("A",)


def test_scope_logs_what_it_skipped(caplog):
    with caplog.at_level("WARNING"):
        outcomes.scope_bar_fetch({f"T{i}": float(i) for i in range(10)}, max_tickers=3)
    assert "7 skipped" in caplog.text


def test_scope_under_the_cap_skips_nothing():
    fetched, skipped = outcomes.scope_bar_fetch({"A": 1.0}, max_tickers=300)
    assert fetched == ("A",)
    assert skipped == ()
