"""Tier assignment, the B-to-A upgrade, dedup and the push budget."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from app.core import pillars, tiering
from app.core.timeutils import UTC
from app.core.types import FloatConfidence, NewsItem, PillarThresholds, Tier

AS_OF = datetime(2026, 3, 10, 12, 5, tzinfo=UTC)


def fresh_news() -> NewsItem:
    created = AS_OF - timedelta(minutes=2)
    return NewsItem(
        news_id="n1",
        symbols=("ABCD",),
        headline="Phase 3 data",
        source="benzinga",
        url=None,
        created_at=created,
        received_at=created,
    )


def score_for(
    thresholds: PillarThresholds,
    *,
    gap: float | None = 34.0,
    rvol: float | None = 12.0,
    price: float | None = 5.20,
    float_shares: int | None = 4_100_000,
    confidence: FloatConfidence = FloatConfidence.HIGH,
    with_news: bool = True,
) -> pillars.PillarScore:
    return pillars.evaluate(
        pillars.PillarInputs(
            gap_pct=gap,
            rvol=rvol,
            price=price,
            float_shares=float_shares,
            float_confidence=confidence,
            fresh_news=fresh_news() if with_news else None,
        ),
        thresholds,
        as_of=AS_OF,
    )


def test_tier_a_needs_all_five(thresholds):
    assert tiering.classify(score_for(thresholds)) is Tier.A


def test_tier_b_is_a_without_fresh_news(thresholds):
    assert tiering.classify(score_for(thresholds, with_news=False)) is Tier.B


def test_watch_on_three_pillars_including_the_first(thresholds):
    # Up 34% and in range, but no news and a too-large float.
    score = score_for(thresholds, with_news=False, float_shares=80_000_000)
    assert score.passed_count == 3
    assert tiering.classify(score) is Tier.WATCH


def test_no_tier_without_pillar_one(thresholds):
    """Three pillars are not enough when the stock is not up on the day."""
    score = score_for(thresholds, gap=1.0, with_news=False)
    assert tiering.classify(score) is Tier.NONE


def test_recent_runner_reaches_watch_on_pillar_one_alone(thresholds):
    score = score_for(thresholds, rvol=0.5, with_news=False, float_shares=900_000_000)
    assert score.passed_count < 3
    assert tiering.classify(score, is_recent_runner=True) is Tier.WATCH


def test_unknown_float_cannot_reach_tier_a(thresholds):
    score = score_for(thresholds, confidence=FloatConfidence.LOW)
    assert tiering.classify(score) is Tier.WATCH


# --- push decisions ----------------------------------------------------------


@pytest.fixture
def state() -> tiering.WindowPushState:
    return tiering.WindowPushState(max_pushes=5, tier_b_enabled=True)


def test_watch_tier_never_pushes(state):
    assert not tiering.decide_push(state, "ABCD", Tier.WATCH).should_push


def test_tier_b_suppressed_when_disabled():
    state = tiering.WindowPushState(max_pushes=5, tier_b_enabled=False)
    decision = tiering.decide_push(state, "ABCD", Tier.B)
    assert not decision.should_push
    assert "disabled" in decision.reason


def test_same_tier_pushes_once_per_window(state):
    assert tiering.decide_push(state, "ABCD", Tier.A).should_push
    tiering.record_push(state, "ABCD", Tier.A)
    repeat = tiering.decide_push(state, "ABCD", Tier.A)
    assert not repeat.should_push
    assert "already pushed" in repeat.reason


def test_b_to_a_upgrade_pushes_again(state):
    tiering.record_push(state, "ABCD", Tier.B)
    decision = tiering.decide_push(state, "ABCD", Tier.A)
    assert decision.should_push
    assert "upgrade" in decision.reason


def test_a_to_b_downgrade_does_not_push(state):
    tiering.record_push(state, "ABCD", Tier.A)
    assert not tiering.decide_push(state, "ABCD", Tier.B).should_push


def test_budget_caps_pushes_and_extras_go_to_ui(state):
    for i in range(5):
        ticker = f"T{i}"
        assert tiering.decide_push(state, ticker, Tier.A).should_push
        tiering.record_push(state, ticker, Tier.A)
    decision = tiering.decide_push(state, "T5", Tier.A)
    assert not decision.should_push
    assert "budget" in decision.reason


def test_failed_send_does_not_consume_budget(state):
    """decide_push is separate from record_push precisely for this case."""
    assert tiering.decide_push(state, "ABCD", Tier.A).should_push
    assert state.budget_left == 5
    assert tiering.decide_push(state, "ABCD", Tier.A).should_push
