"""What-if counts: both numbers, per ticker-day, never auto-tuning."""

from __future__ import annotations

from dataclasses import replace

from app.core import whatif
from app.core.whatif import EvaluationRecord, Thresholds

BASELINE = Thresholds(
    gap_pct_min=10.0,
    rvol_min=5.0,
    price_min=2.0,
    price_max=20.0,
    float_shares_max=20_000_000,
)


def record(
    ticker: str = "ABCD",
    *,
    day: str = "2026-03-10",
    gap: float | None = 34.0,
    rvol: float | None = 12.0,
    price: float | None = 5.2,
    float_shares: int | None = 4_100_000,
    news: bool = True,
    runner: bool = False,
    alerted: bool = False,
) -> EvaluationRecord:
    return EvaluationRecord(
        ticker=ticker,
        day=day,
        gap_pct=gap,
        rvol=rvol,
        price=price,
        float_shares=float_shares,
        had_fresh_news=news,
        was_runner=runner,
        already_alerted=alerted,
    )


# --- the pass predicate ------------------------------------------------------


def test_a_full_setup_passes():
    assert whatif.passes(record(), BASELINE)


def test_missing_data_never_passes():
    """An unknown float is not evidence of a small one."""
    assert not whatif.passes(record(float_shares=None), BASELINE)
    assert not whatif.passes(record(rvol=None), BASELINE)
    assert not whatif.passes(record(gap=None), BASELINE)
    assert not whatif.passes(record(price=None), BASELINE)


def test_the_float_rule_is_strictly_below():
    assert whatif.passes(record(float_shares=19_999_999), BASELINE)
    assert not whatif.passes(record(float_shares=20_000_000), BASELINE)


def test_news_can_be_made_optional():
    assert not whatif.passes(record(news=False), BASELINE)
    assert whatif.passes(record(news=False), replace(BASELINE, require_news=False))


# --- counting ----------------------------------------------------------------


def test_loosening_the_float_catches_a_runner_and_costs_extra_alerts():
    records = [
        record("RUNNR", float_shares=23_000_000, runner=True),
        record("DUD1", float_shares=23_000_000),
        record("DUD2", float_shares=25_000_000),
    ]
    result = whatif.evaluate_change(
        records,
        baseline=BASELINE,
        candidate=replace(BASELINE, float_shares_max=30_000_000),
        label="float < 30M instead of 20M",
    )
    assert result.extra_runners_caught == 1
    assert result.extra_alerts_that_did_not == 2
    assert "+1 runners caught" in result.summary
    assert "+2 extra alerts that did not run" in result.summary


def test_a_change_that_catches_nothing_reports_zero_not_nothing():
    records = [record("DUD", float_shares=23_000_000)]
    result = whatif.evaluate_change(
        records,
        baseline=BASELINE,
        candidate=replace(BASELINE, float_shares_max=30_000_000),
        label="looser float",
    )
    assert result.extra_runners_caught == 0
    assert result.extra_alerts_that_did_not == 1


def test_counts_are_per_ticker_day_not_per_poll():
    """Counting polls would multiply every number by the poll rate."""
    polls = [record("ABCD", runner=True) for _ in range(20)]
    result = whatif.evaluate_change(polls, baseline=BASELINE, candidate=BASELINE, label="unchanged")
    assert result.candidate_alerts == 1
    assert result.runners_caught == 1


def test_the_same_ticker_on_two_days_counts_twice():
    records = [
        record("ABCD", day="2026-03-10", runner=True),
        record("ABCD", day="2026-03-11", runner=True),
    ]
    result = whatif.evaluate_change(
        records, baseline=BASELINE, candidate=BASELINE, label="unchanged"
    )
    assert result.runners_caught == 2


def test_a_runner_the_candidate_still_misses_is_counted_missed():
    records = [record("RUNNR", float_shares=80_000_000, runner=True)]
    result = whatif.evaluate_change(
        records,
        baseline=BASELINE,
        candidate=replace(BASELINE, float_shares_max=30_000_000),
        label="looser float",
    )
    assert result.runners_missed == 1
    assert result.runners_caught == 0


def test_tightening_a_threshold_reduces_alerts():
    records = [record("ABCD", gap=12.0), record("EFGH", gap=30.0)]
    result = whatif.evaluate_change(
        records,
        baseline=BASELINE,
        candidate=replace(BASELINE, gap_pct_min=20.0),
        label="gap >= 20%",
    )
    assert result.baseline_alerts == 2
    assert result.candidate_alerts == 1


# --- sweeps ------------------------------------------------------------------


def test_sweep_returns_one_result_per_variation_unranked():
    """Ranking would nominate a 'best' threshold; with this sample size that is chance."""
    records = [record("ABCD", runner=True), record("EFGH")]
    results = whatif.sweep(
        records,
        baseline=BASELINE,
        variations={
            "float < 30M": lambda t: replace(t, float_shares_max=30_000_000),
            "rvol >= 3": lambda t: replace(t, rvol_min=3.0),
            "no news required": lambda t: replace(t, require_news=False),
        },
    )
    assert [r.label for r in results] == ["float < 30M", "rvol >= 3", "no news required"]


def test_an_empty_record_set_yields_zeroes():
    result = whatif.evaluate_change([], baseline=BASELINE, candidate=BASELINE, label="none")
    assert result.runners_caught == 0
    assert result.candidate_alerts == 0
