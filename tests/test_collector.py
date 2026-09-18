"""The collection path end to end: snapshot -> filtered rows -> lake."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from app.collector import WindowBaselines, snapshot_rows
from app.core.collection import CollectionFilter
from app.core.timeutils import UTC, et_trading_date
from app.sources.tradingview import MockSnapshotSource, SnapshotResult, TradingViewRow
from app.storage import lake
from app.storage.reference import ReferenceTracker

NOW = datetime(2026, 3, 10, 12, 5, tzinfo=UTC)  # 08:05 ET
RULES = CollectionFilter(
    min_abs_change_pct=3.0, min_volume_ratio=2.0, top_dollar_volume_n=100, min_price=0.50
)


def row(**overrides: object) -> TradingViewRow:
    base = {
        "ticker": "ABCD",
        "exchange": "NASDAQ",
        "instrument_type": "stock",
        "typespecs": ("common",),
        "close": 5.20,
        "change_pct": 34.0,
        "volume": 8_200_000.0,
        "average_volume_10d": 900_000.0,
        "average_volume_30d": 850_000.0,
        "float_shares": 4_100_000,
        "shares_outstanding": 12_000_000,
        "market_cap": 62_400_000.0,
        "sector": "Health Technology",
        "industry": "Biotechnology",
    }
    return TradingViewRow(**{**base, **overrides})  # type: ignore[arg-type]


def result_of(*rows: TradingViewRow, at: datetime = NOW) -> SnapshotResult:
    return SnapshotResult(poll_ts_utc=at, rows=rows, total_matched=len(rows))


@pytest.fixture
def baselines() -> WindowBaselines:
    return WindowBaselines(window_start=NOW)


@pytest.fixture
def tracker() -> ReferenceTracker:
    return ReferenceTracker()


def collect(
    result: SnapshotResult,
    baselines: WindowBaselines,
    tracker: ReferenceTracker,
    **kw: object,
):
    return snapshot_rows(
        result,
        rules=RULES,
        session_fraction=0.05,
        baselines=baselines,
        reference_tracker=tracker,
        float_turnover_low_confidence=10.0,
        **kw,
    )


def test_a_mover_is_collected_with_its_reference_row(baselines, tracker):
    out = collect(result_of(row()), baselines, tracker)
    assert len(out.snapshots) == 1
    assert len(out.reference) == 1
    assert out.snapshots[0]["ticker"] == "ABCD"
    assert out.reference[0]["float_shares_outstanding"] == 4_100_000


def test_a_quiet_stock_outside_the_top_dollar_volume_is_dropped(baselines, tracker):
    quiet = row(ticker="QUIET", change_pct=0.5, volume=10_000.0, close=1.0)
    loud = row(ticker="LOUD", close=50.0, volume=20_000_000.0)
    out = snapshot_rows(
        result_of(quiet, loud),
        rules=CollectionFilter(
            min_abs_change_pct=3.0,
            min_volume_ratio=2.0,
            top_dollar_volume_n=1,
            min_price=0.50,
        ),
        session_fraction=0.05,
        baselines=baselines,
        reference_tracker=tracker,
        float_turnover_low_confidence=10.0,
    )
    assert [r["ticker"] for r in out.snapshots] == ["LOUD"]
    assert out.stats.rules["no_rule_matched"] == 1


def test_rows_are_dated_by_et_trading_date(baselines, tracker):
    out = collect(result_of(row()), baselines, tracker)
    assert out.snapshots[0]["date"] == et_trading_date(NOW)


def test_first_sight_is_its_own_window_baseline(baselines, tracker):
    """Zero is honest for a ticker first seen mid-window; a stale base is not."""
    out = collect(result_of(row()), baselines, tracker)
    assert out.snapshots[0]["window_change_pct"] == pytest.approx(0.0)
    assert out.snapshots[0]["window_volume"] == pytest.approx(0.0)


def test_window_change_is_measured_against_the_window_open(baselines, tracker):
    collect(result_of(row(close=5.00, volume=8_000_000.0)), baselines, tracker)
    later = collect(
        result_of(row(close=5.50, volume=8_600_000.0), at=NOW + timedelta(minutes=1)),
        baselines,
        tracker,
    )
    assert later.snapshots[0]["window_change_pct"] == pytest.approx(10.0)
    assert later.snapshots[0]["window_volume"] == pytest.approx(600_000.0)


def test_resetting_the_window_rebaselines(baselines, tracker):
    collect(result_of(row(close=5.00)), baselines, tracker)
    baselines.reset(NOW + timedelta(minutes=25))
    out = collect(result_of(row(close=5.50), at=NOW + timedelta(minutes=25)), baselines, tracker)
    assert out.snapshots[0]["window_change_pct"] == pytest.approx(0.0)
    assert baselines.tracked == 1


def test_gap_prefers_the_daily_prev_close_over_the_feed_change(baselines, tracker):
    """Snapshots are unadjusted on split days; the daily bar source is not."""
    snapshot = result_of(row(close=5.20, change_pct=34.0))
    out = collect(snapshot, baselines, tracker, prev_closes={"ABCD": 4.00})
    assert out.snapshots[0]["gap_pct"] == pytest.approx(30.0)


def test_gap_falls_back_to_the_feed_change_without_a_prev_close(baselines, tracker):
    out = collect(result_of(row(close=5.20, change_pct=34.0)), baselines, tracker)
    assert out.snapshots[0]["gap_pct"] == pytest.approx(34.0)


def test_reference_is_written_once_per_day_not_per_poll(baselines, tracker):
    collect(result_of(row()), baselines, tracker)
    again = collect(result_of(row(close=6.00), at=NOW + timedelta(minutes=1)), baselines, tracker)
    assert again.reference == []
    assert len(again.snapshots) == 1


def test_low_float_confidence_is_recorded_on_the_reference_row(baselines, tracker):
    # 8.2M shares traded against a 100k float is 82x turnover: not credible.
    out = collect(result_of(row(float_shares=100_000)), baselines, tracker)
    assert out.reference[0]["float_confidence"] == "low"


def test_stats_report_degradation(baselines, tracker):
    degraded = SnapshotResult(
        poll_ts_utc=NOW,
        rows=(row(),),
        total_matched=1,
        missing_columns=("float_shares_outstanding",),
        unparsable=3,
    )
    out = collect(degraded, baselines, tracker)
    assert out.stats.missing_columns == ("float_shares_outstanding",)
    assert out.stats.unparsable == 3


def test_a_mock_poll_writes_to_the_lake(tmp_path: Path, baselines, tracker):
    """The whole path: mock source, filter, lake, read back."""
    root = tmp_path / "lake"
    writer = lake.LakeWriter(root=root, source="tradingview")
    out = collect(MockSnapshotSource().snapshot(now=NOW), baselines, tracker)
    day = et_trading_date(NOW)
    writer.extend("snapshots", day, out.snapshots)
    writer.extend("reference", day, out.reference)
    writer.flush(now=NOW)

    assert out.stats.collected > 0
    assert lake.row_count(root, "snapshots", day) == len(out.snapshots)
    assert lake.row_count(root, "reference", day) == len(out.reference)
