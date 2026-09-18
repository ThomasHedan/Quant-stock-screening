"""Pillar boundaries, unknowns, and the float-confidence override.

Every threshold is tested exactly at its boundary, because that is where a
``>`` that should have been a ``>=`` hides.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from app.core import pillars
from app.core.timeutils import UTC
from app.core.types import FloatConfidence, NewsItem, PillarStatus, PillarThresholds

AS_OF = datetime(2026, 3, 10, 12, 5, tzinfo=UTC)


def news(minutes_old: float, headline: str = "Company announces FDA clearance") -> NewsItem:
    created = AS_OF - timedelta(minutes=minutes_old)
    return NewsItem(
        news_id="n1",
        symbols=("ABCD",),
        headline=headline,
        source="benzinga",
        url=None,
        created_at=created,
        received_at=created + timedelta(seconds=3),
    )


# --- pillar 1: up on the day -------------------------------------------------


@pytest.mark.parametrize(
    ("gap", "expected"),
    [
        (9.99, PillarStatus.FAIL),
        (10.0, PillarStatus.PASS),  # exactly at the threshold passes
        (34.0, PillarStatus.PASS),
        (None, PillarStatus.UNKNOWN),
    ],
)
def test_up_on_day_boundary(gap, expected):
    assert pillars.check_up_on_day(gap, 10.0).status is expected


# --- pillar 2: relative volume ----------------------------------------------


@pytest.mark.parametrize(
    ("rvol", "expected"),
    [(4.999, PillarStatus.FAIL), (5.0, PillarStatus.PASS), (None, PillarStatus.UNKNOWN)],
)
def test_relative_volume_boundary(rvol, expected):
    assert pillars.check_relative_volume(rvol, 5.0).status is expected


# --- pillar 3: news catalyst -------------------------------------------------


def test_news_fresh_within_window():
    result = pillars.check_news_catalyst(news(5), as_of=AS_OF, fresh_minutes=15)
    assert result.status is PillarStatus.PASS


def test_news_exactly_at_freshness_boundary_passes():
    result = pillars.check_news_catalyst(news(15), as_of=AS_OF, fresh_minutes=15)
    assert result.status is PillarStatus.PASS


def test_news_just_stale_fails():
    result = pillars.check_news_catalyst(news(15.1), as_of=AS_OF, fresh_minutes=15)
    assert result.status is PillarStatus.FAIL


def test_no_news_is_a_fail_not_unknown():
    """A connected, quiet feed is information: the catalyst is absent."""
    result = pillars.check_news_catalyst(None, as_of=AS_OF, fresh_minutes=15)
    assert result.status is PillarStatus.FAIL
    assert "no news" in result.detail


def test_future_dated_news_is_not_trusted():
    result = pillars.check_news_catalyst(news(-3), as_of=AS_OF, fresh_minutes=15)
    assert result.status is PillarStatus.FAIL
    assert "future" in result.detail


# --- pillar 4: price range ---------------------------------------------------


@pytest.mark.parametrize(
    ("price", "expected"),
    [
        (1.99, PillarStatus.FAIL),
        (2.00, PillarStatus.PASS),  # inclusive lower bound
        (20.00, PillarStatus.PASS),  # inclusive upper bound
        (20.01, PillarStatus.FAIL),
        (None, PillarStatus.UNKNOWN),
    ],
)
def test_price_range_boundaries(price, expected):
    assert pillars.check_price_range(price, 2.00, 20.00).status is expected


# --- pillar 5: float ---------------------------------------------------------


@pytest.mark.parametrize(
    ("shares", "expected"),
    [
        (19_999_999, PillarStatus.PASS),
        (20_000_000, PillarStatus.FAIL),  # the rule is strictly below
        (None, PillarStatus.UNKNOWN),
    ],
)
def test_float_boundary(shares, expected):
    result = pillars.check_float(shares, FloatConfidence.HIGH, 20_000_000)
    assert result.status is expected


def test_low_confidence_float_is_unknown_never_pass():
    """The guard that keeps a stale 4M float from manufacturing a tier A."""
    result = pillars.check_float(4_000_000, FloatConfidence.LOW, 20_000_000)
    assert result.status is PillarStatus.UNKNOWN
    assert "float_confidence=low" in result.detail


def test_low_confidence_float_is_unknown_never_fail():
    result = pillars.check_float(80_000_000, FloatConfidence.LOW, 20_000_000)
    assert result.status is PillarStatus.UNKNOWN


# --- aggregate ---------------------------------------------------------------


def perfect_inputs() -> pillars.PillarInputs:
    return pillars.PillarInputs(
        gap_pct=34.0,
        rvol=12.0,
        price=5.20,
        float_shares=4_100_000,
        float_confidence=FloatConfidence.HIGH,
        fresh_news=news(2),
    )


def test_evaluate_five_of_five(thresholds: PillarThresholds):
    score = pillars.evaluate(perfect_inputs(), thresholds, as_of=AS_OF)
    assert score.passed_count == 5
    assert score.unknown_count == 0
    assert score.passed(1, 2, 3, 4, 5)


def test_unknown_never_counts_as_passed(thresholds: PillarThresholds):
    inputs = pillars.PillarInputs(
        gap_pct=34.0,
        rvol=12.0,
        price=5.20,
        float_shares=4_100_000,
        float_confidence=FloatConfidence.LOW,
        fresh_news=news(2),
    )
    score = pillars.evaluate(inputs, thresholds, as_of=AS_OF)
    assert score.passed_count == 4
    assert score.unknown_count == 1
    assert not score.passed(5)


def test_by_number_rejects_unknown_pillar(thresholds: PillarThresholds):
    score = pillars.evaluate(perfect_inputs(), thresholds, as_of=AS_OF)
    with pytest.raises(KeyError):
        score.by_number(6)


# --- near miss ---------------------------------------------------------------


def test_near_miss_float_23m_against_20m():
    result = pillars.check_float(23_000_000, FloatConfidence.HIGH, 20_000_000)
    assert result.status is PillarStatus.FAIL
    assert pillars.near_miss(result, 0.20)


def test_far_miss_float_80m_against_20m():
    result = pillars.check_float(80_000_000, FloatConfidence.HIGH, 20_000_000)
    assert not pillars.near_miss(result, 0.20)


def test_near_miss_is_false_for_a_pass():
    result = pillars.check_up_on_day(34.0, 10.0)
    assert not pillars.near_miss(result, 0.20)


def test_near_miss_is_false_without_a_value():
    result = pillars.check_up_on_day(None, 10.0)
    assert not pillars.near_miss(result, 0.20)
