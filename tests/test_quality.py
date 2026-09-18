"""Data-quality aggregation and the warnings surfaced on the dashboard."""

from __future__ import annotations

from datetime import date, datetime

import pytest
from app.core.timeutils import UTC
from app.storage import quality

DAY = date(2026, 3, 10)
NOW = datetime(2026, 3, 10, 20, 45, tzinfo=UTC)


def test_percentile_nearest_rank():
    values = [1.0, 2.0, 3.0, 4.0, 100.0]
    assert quality.percentile(values, 0.95) == 100.0
    assert quality.percentile(values, 0.5) == 3.0


def test_percentile_never_invents_a_value():
    """Nearest-rank: the p95 of two observations is one of those two."""
    assert quality.percentile([10.0, 70.0], 0.95) in {10.0, 70.0}


def test_percentile_of_nothing_is_none():
    assert quality.percentile([], 0.95) is None


def test_percentile_rejects_a_bad_fraction():
    with pytest.raises(ValueError, match="fraction"):
        quality.percentile([1.0], 1.5)


def test_build_row_shapes_the_lake_row():
    counters = quality.QualityCounters(
        table_name="snapshots",
        rows_collected=4321,
        polls_expected=120,
        polls_completed=118,
        mover_count=40,
        control_count=46,
        dropped_count=414,
        move_threshold_pct=25.0,
    )
    row = quality.build_row(counters, DAY, now=NOW)
    assert row["date"] == DAY
    assert row["rows_collected"] == 4321
    assert row["move_threshold_pct"] == 25.0
    assert row["written_at_utc"] == NOW


def test_empty_share_is_none_not_zero():
    """No pillar-5 evaluations is not the same as no confidence problems."""
    row = quality.build_row(quality.QualityCounters(table_name="evaluations"), DAY, now=NOW)
    assert row["pillar5_low_confidence_share"] is None
    assert row["tradeable_share"] is None


def test_shares_are_computed_when_there_is_a_denominator():
    counters = quality.QualityCounters(
        table_name="evaluations",
        pillar5_evaluations=200,
        pillar5_low_confidence=120,
        outcome_rows=50,
        tradeable_rows=20,
    )
    row = quality.build_row(counters, DAY, now=NOW)
    assert row["pillar5_low_confidence_share"] == pytest.approx(0.6)
    assert row["tradeable_share"] == pytest.approx(0.4)


def test_notes_are_joined_and_logged(caplog):
    counters = quality.QualityCounters(table_name="snapshots")
    with caplog.at_level("WARNING"):
        counters.note("bar fetch skipped 12 tickers over max_bar_tickers")
    row = quality.build_row(counters, DAY, now=NOW)
    assert "skipped 12 tickers" in row["note"]
    assert "skipped 12 tickers" in caplog.text


# --- warnings ----------------------------------------------------------------


def test_latency_warning_says_what_the_number_now_measures():
    counters = quality.QualityCounters(table_name="news", news_latencies_s=[10.0, 20.0, 400.0])
    row = quality.build_row(counters, DAY, now=NOW)
    warnings = quality.warnings_for(row, latency_p95_warn_s=60.0)
    assert any(w.code == "news_feed_latency" for w in warnings)
    assert "feed lag" in next(w.message for w in warnings if w.code == "news_feed_latency")


def test_no_latency_warning_when_the_feed_is_prompt():
    counters = quality.QualityCounters(table_name="news", news_latencies_s=[1.0, 2.0, 3.0])
    row = quality.build_row(counters, DAY, now=NOW)
    assert quality.warnings_for(row, latency_p95_warn_s=60.0) == ()


def test_float_confidence_warning_is_a_finding_not_a_bug():
    counters = quality.QualityCounters(
        table_name="evaluations", pillar5_evaluations=100, pillar5_low_confidence=80
    )
    row = quality.build_row(counters, DAY, now=NOW)
    warning = next(w for w in quality.warnings_for(row, latency_p95_warn_s=60.0))
    assert warning.code == "float_confidence"
    assert "may not be usable" in warning.message


def test_websocket_gap_is_surfaced():
    counters = quality.QualityCounters(table_name="news", ws_disconnect_minutes=12.0)
    row = quality.build_row(counters, DAY, now=NOW)
    assert any(w.code == "news_feed_gap" for w in quality.warnings_for(row, latency_p95_warn_s=60))


def test_suspect_price_rows_are_surfaced():
    counters = quality.QualityCounters(table_name="snapshots", suspect_price_rows=3)
    row = quality.build_row(counters, DAY, now=NOW)
    assert any(w.code == "suspect_price" for w in quality.warnings_for(row, latency_p95_warn_s=60))


def test_a_clean_day_raises_nothing():
    row = quality.build_row(quality.QualityCounters(table_name="snapshots"), DAY, now=NOW)
    assert quality.warnings_for(row, latency_p95_warn_s=60.0) == ()
