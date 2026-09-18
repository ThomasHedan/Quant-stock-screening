"""The loose live filter and the deterministic control sample.

The filter's job is to be *too* generous. These tests pin that down, because
the natural instinct when reading the code later will be to tighten it, and
tightening it destroys the denominator the whole lake exists to provide.
"""

from __future__ import annotations

from datetime import date

import pytest
from app.core import collection

RULES = collection.CollectionFilter(
    min_abs_change_pct=3.0,
    min_volume_ratio=2.0,
    top_dollar_volume_n=100,
    min_price=0.50,
)
DAY = date(2026, 3, 10)


def candidate(**overrides: object) -> collection.CollectionCandidate:
    base = {
        "ticker": "ABCD",
        "price": 5.0,
        "change_pct": 0.5,
        "volume": 50_000.0,
        "average_volume_10d": 1_000_000.0,
    }
    return collection.CollectionCandidate(**{**base, **overrides})  # type: ignore[arg-type]


def decide(cand: collection.CollectionCandidate, *, top: bool = False):
    return collection.decide(cand, RULES, session_fraction=0.05, in_top_dollar_volume=top)


def test_quiet_stock_is_not_collected():
    assert not decide(candidate()).collected


def test_change_rule_at_the_boundary():
    assert decide(candidate(change_pct=3.0)).rule == "abs_change"
    assert not decide(candidate(change_pct=2.99)).collected


def test_losers_are_collected_too():
    """A low-float stock with news that dumps is the setup's failure mode."""
    decision = decide(candidate(change_pct=-30.0))
    assert decision.collected
    assert decision.rule == "abs_change"


def test_volume_rule_uses_the_expected_volume_so_far():
    # 5% of a 1M-share average day is 50k expected; 2x that is 100k.
    assert not decide(candidate(volume=99_000.0)).collected
    assert decide(candidate(volume=100_000.0)).rule == "volume_ratio"


def test_top_dollar_volume_is_a_rule_of_its_own():
    assert decide(candidate(), top=True).rule == "top_dollar_volume"


def test_price_floor_is_the_only_hard_gate():
    """Sub-penny names where one tick is +50% never enter the lake."""
    decision = decide(candidate(price=0.49, change_pct=90.0), top=True)
    assert not decision.collected
    assert decision.rule == "below_price_floor"


def test_missing_price_is_not_collected():
    assert not decide(candidate(price=None, change_pct=90.0)).collected


def test_missing_average_volume_does_not_crash_the_volume_rule():
    assert not decide(candidate(average_volume_10d=None)).collected


def test_filter_is_looser_than_the_alert_threshold():
    """CLAUDE.md 6.0 in one assertion: a +5% stock is kept, though it alerts on none."""
    assert decide(candidate(change_pct=5.0)).collected


# --- top dollar volume -------------------------------------------------------


def test_top_dollar_volume_picks_the_largest():
    candidates = [
        candidate(ticker="BIG", price=10.0, volume=1_000_000.0),
        candidate(ticker="MID", price=5.0, volume=500_000.0),
        candidate(ticker="SML", price=1.0, volume=1_000.0),
    ]
    assert collection.top_dollar_volume_tickers(candidates, 2) == frozenset({"BIG", "MID"})


def test_top_dollar_volume_breaks_ties_deterministically():
    candidates = [
        candidate(ticker="BBB", price=2.0, volume=1_000.0),
        candidate(ticker="AAA", price=2.0, volume=1_000.0),
    ]
    assert collection.top_dollar_volume_tickers(candidates, 1) == frozenset({"AAA"})


def test_top_dollar_volume_skips_rows_without_price_or_volume():
    candidates = [candidate(ticker="NOPX", price=None), candidate(ticker="OKAY")]
    assert collection.top_dollar_volume_tickers(candidates, 5) == frozenset({"OKAY"})


def test_top_dollar_volume_of_zero_is_empty():
    assert collection.top_dollar_volume_tickers([candidate()], 0) == frozenset()


# --- control sample ----------------------------------------------------------


def test_control_sample_is_deterministic_for_a_ticker_day():
    first = collection.in_control_sample("ABCD", DAY, 10)
    for _ in range(5):
        assert collection.in_control_sample("ABCD", DAY, 10) is first


def test_control_sample_differs_across_days():
    buckets = {collection.control_sample_hash("ABCD", date(2026, 3, d)) for d in range(1, 20)}
    assert len(buckets) > 1


def test_control_sample_rate_matches_the_configured_share():
    """Over 1000 synthetic tickers the realised rate must track the setting."""
    tickers = [f"T{i:04d}" for i in range(1000)]
    sampled = sum(1 for t in tickers if collection.in_control_sample(t, DAY, 10))
    assert 70 <= sampled <= 130  # 10% +/- 3 points


def test_control_sample_of_zero_pct_takes_nothing():
    assert not any(collection.in_control_sample(f"T{i}", DAY, 0) for i in range(200))


def test_control_sample_of_hundred_pct_takes_everything():
    assert all(collection.in_control_sample(f"T{i}", DAY, 100) for i in range(200))


def test_control_sample_rejects_an_impossible_share():
    with pytest.raises(ValueError, match="control_sample_pct"):
        collection.in_control_sample("ABCD", DAY, 101)


def test_control_weight_inverts_the_sample_rate():
    assert collection.control_weight(10) == 10.0
    assert collection.control_weight(25) == 4.0


def test_control_weight_rejects_zero():
    with pytest.raises(ValueError, match="positive"):
        collection.control_weight(0)
