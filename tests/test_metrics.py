"""Metric arithmetic, with the emphasis on missing data and boundaries.

The recurring theme: a metric that cannot be computed must come back as
``None``, never as a zero that would sail through a threshold check.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from app.core import metrics
from app.core.timeutils import UTC
from app.core.types import FloatConfidence, RankWeights, RvolSource


def test_pct_change_basic():
    assert metrics.pct_change(11.0, 10.0) == pytest.approx(10.0)


@pytest.mark.parametrize("reference", [0.0, -1.0])
def test_pct_change_none_on_unusable_reference(reference):
    assert metrics.pct_change(11.0, reference) is None


def test_gap_pct_none_without_prev_close():
    """A missing prev_close must not read as a flat day."""
    assert metrics.gap_pct(5.0, None) is None


def test_window_change_pct_uses_window_open():
    assert metrics.window_change_pct(5.5, 5.0) == pytest.approx(10.0)


def test_window_volume_delta():
    assert metrics.window_volume(1_500_000, 1_000_000) == 500_000


def test_window_volume_clamps_decrease(caplog):
    """Cumulative volume going backwards is a feed fault, not a negative move."""
    with caplog.at_level("WARNING"):
        assert metrics.window_volume(900_000, 1_000_000) == 0.0
    assert "Session volume decreased" in caplog.text


def test_rvol_time_of_day():
    assert metrics.rvol_time_of_day(500_000, 100_000) == pytest.approx(5.0)


def test_rvol_time_of_day_none_on_zero_baseline():
    assert metrics.rvol_time_of_day(500_000, 0.0) is None


def test_rvol_fallback_applies_session_fraction():
    # 5% of a 2M-share average day is 100k expected by 08:05.
    assert metrics.rvol_fallback(500_000, 2_000_000, 0.05) == pytest.approx(5.0)


def test_rvol_fallback_rejects_zero_fraction():
    with pytest.raises(ValueError, match="session_fraction"):
        metrics.rvol_fallback(500_000, 2_000_000, 0.0)


def test_resolve_rvol_prefers_baseline():
    value, source = metrics.resolve_rvol(500_000, 100_000, 2_000_000, 0.05)
    assert source is RvolSource.BASELINE
    assert value == pytest.approx(5.0)


def test_resolve_rvol_degrades_to_fallback():
    value, source = metrics.resolve_rvol(500_000, None, 2_000_000, 0.05)
    assert source is RvolSource.FALLBACK
    assert value == pytest.approx(5.0)


def test_resolve_rvol_unknown_when_nothing_usable():
    value, source = metrics.resolve_rvol(500_000, None, None, 0.05)
    assert (value, source) == (None, RvolSource.UNKNOWN)


def test_baseline_volume_at_averages_cumulative_volume():
    def day(start: datetime, per_minute: list[float]) -> dict[datetime, float]:
        return {start + timedelta(minutes=i): v for i, v in enumerate(per_minute)}

    d1 = datetime(2026, 3, 9, 8, 0, tzinfo=UTC)
    d2 = datetime(2026, 3, 10, 8, 0, tzinfo=UTC)
    by_day: dict[object, dict[datetime, float]] = {
        "2026-03-09": day(d1, [100.0, 200.0, 300.0]),
        "2026-03-10": day(d2, [300.0, 300.0, 400.0]),
    }
    # First two minutes: 300 on day one, 600 on day two -> mean 450.
    assert metrics.baseline_volume_at(by_day, 2) == pytest.approx(450.0)


def test_baseline_volume_at_none_without_days():
    assert metrics.baseline_volume_at({}, 5) is None


def test_percentile_ranks_orders_and_keeps_none():
    ranks = metrics.percentile_ranks([1.0, None, 3.0, 2.0])
    assert ranks[1] is None
    assert ranks[0] == pytest.approx(0.0)
    assert ranks[3] == pytest.approx(0.5)
    assert ranks[2] == pytest.approx(1.0)


def test_percentile_ranks_averages_ties():
    ranks = metrics.percentile_ranks([5.0, 5.0, 9.0])
    assert ranks[0] == ranks[1] == pytest.approx(0.25)
    assert ranks[2] == pytest.approx(1.0)


def test_percentile_ranks_all_none():
    assert metrics.percentile_ranks([None, None]) == [None, None]


def test_rank_score_weights(weights: RankWeights):
    score = metrics.rank_score(1.0, 0.5, 0.0, weights)
    assert score == pytest.approx(0.5 * 1.0 + 0.3 * 0.5 + 0.2 * 0.0)


def test_rank_score_renormalises_when_a_component_is_missing(weights: RankWeights):
    """A ticker with no RVOL yet must not be pushed to the bottom."""
    score = metrics.rank_score(1.0, None, 1.0, weights)
    assert score == pytest.approx(1.0)


def test_rank_score_none_when_nothing_present(weights: RankWeights):
    assert metrics.rank_score(None, None, None, weights) is None


def test_float_turnover():
    assert metrics.float_turnover(40_000_000, 1_000_000) == pytest.approx(40.0)


def test_float_turnover_none_without_float():
    assert metrics.float_turnover(40_000_000, None) is None


def test_float_confidence_low_on_implausible_turnover():
    as_of = datetime(2026, 3, 10, 13, 0, tzinfo=UTC)
    assert (
        metrics.float_confidence(
            float_shares=1_000_000,
            turnover=40.0,
            float_asof=as_of,
            as_of=as_of,
            turnover_low_threshold=10.0,
            max_age_days=90,
        )
        is FloatConfidence.LOW
    )


def test_float_confidence_low_on_stale_asof():
    as_of = datetime(2026, 3, 10, 13, 0, tzinfo=UTC)
    assert (
        metrics.float_confidence(
            float_shares=1_000_000,
            turnover=2.0,
            float_asof=as_of - timedelta(days=120),
            as_of=as_of,
            turnover_low_threshold=10.0,
            max_age_days=90,
        )
        is FloatConfidence.LOW
    )


def test_float_confidence_medium_without_asof():
    as_of = datetime(2026, 3, 10, 13, 0, tzinfo=UTC)
    assert (
        metrics.float_confidence(
            float_shares=1_000_000,
            turnover=2.0,
            float_asof=None,
            as_of=as_of,
            turnover_low_threshold=10.0,
            max_age_days=90,
        )
        is FloatConfidence.MEDIUM
    )


def test_float_confidence_high_when_fresh_and_plausible():
    as_of = datetime(2026, 3, 10, 13, 0, tzinfo=UTC)
    assert (
        metrics.float_confidence(
            float_shares=4_100_000,
            turnover=3.0,
            float_asof=as_of - timedelta(days=5),
            as_of=as_of,
            turnover_low_threshold=10.0,
            max_age_days=90,
        )
        is FloatConfidence.HIGH
    )


def test_float_confidence_at_the_turnover_boundary():
    """Exactly at the threshold is still trusted; the rule is strictly above."""
    as_of = datetime(2026, 3, 10, 13, 0, tzinfo=UTC)
    assert (
        metrics.float_confidence(
            float_shares=1_000_000,
            turnover=10.0,
            float_asof=as_of,
            as_of=as_of,
            turnover_low_threshold=10.0,
            max_age_days=90,
        )
        is FloatConfidence.HIGH
    )
