"""Pruning, the control sample, and the retention job's protected tables.

The acceptance criterion these serve (CLAUDE.md 11.5): after a mock day,
pruning keeps every >=25% mover at full resolution, keeps ~10% of the rest
flagged control, writes one pruned_summary row per dropped ticker, and leaves
daily_universe complete. Re-weighted control rows reproduce the true population
hit rate within tolerance.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from app.core.collection import control_weight
from app.core.moves import MoveMetrics
from app.core.timeutils import UTC
from app.core.types import RetentionClass, Tier
from app.storage import lake, pruning
from app.storage.pruning import RetentionPolicy, TickerDaySummary

DAY = date(2026, 3, 10)
NOW = datetime(2026, 3, 10, 20, 45, tzinfo=UTC)
POLICY = RetentionPolicy(
    bars_1m_raw_days=90,
    bars_1m_thinned_minutes=5,
    snapshots_months=18,
    max_lake_gb=25,
    warn_fraction=0.8,
)


def summary(ticker: str, *, up: float | None = 2.0, tier: Tier = Tier.NONE) -> TickerDaySummary:
    return TickerDaySummary(
        ticker=ticker,
        metrics=MoveMetrics(up_move_pct=up, day_high=10.0, day_low=9.0),
        best_tier=tier,
        poll_count=12,
        session_volume=500_000.0,
        last_price=9.5,
    )


def classify(s: TickerDaySummary):
    return pruning.classify(s, DAY, move_threshold_pct=25.0, control_sample_pct=10)


# --- classification ----------------------------------------------------------


def test_a_mover_is_kept():
    decision = classify(summary("ABCD", up=30.0))
    assert decision.retention_class is RetentionClass.MOVER
    assert decision.kept


def test_the_threshold_is_inclusive():
    assert classify(summary("ABCD", up=25.0)).retention_class is RetentionClass.MOVER
    assert classify(summary("ABCD", up=24.99)).retention_class is not RetentionClass.MOVER


def test_a_dumper_is_kept_at_the_same_threshold():
    """The setup's failure mode is what teaches the scanner to tell them apart."""
    s = TickerDaySummary(
        ticker="DUMP", metrics=MoveMetrics(down_move_pct=-40.0), best_tier=Tier.NONE, poll_count=8
    )
    assert classify(s).retention_class is RetentionClass.MOVER


def test_a_watch_tier_evaluation_is_kept_even_if_it_went_nowhere():
    decision = classify(summary("FLAT", up=1.0, tier=Tier.WATCH))
    assert decision.retention_class is RetentionClass.SIGNAL


def test_everything_else_falls_through_to_the_control_draw():
    tickers = [f"T{i:04d}" for i in range(500)]
    classes = [classify(summary(t)).retention_class for t in tickers]
    controls = sum(1 for c in classes if c is RetentionClass.CONTROL)
    dropped = sum(1 for c in classes if c is None)
    assert controls + dropped == 500
    assert 30 <= controls <= 70  # 10% of 500, with sampling slack


# --- the partition rewrite ---------------------------------------------------


def snapshot_row(ticker: str, minute: int) -> dict:
    return {
        "ticker": ticker,
        "date": DAY,
        "poll_ts_utc": NOW - timedelta(hours=12) + timedelta(minutes=minute),
        "price": 5.0,
        "session_volume": 100_000.0,
        "written_at_utc": NOW,
    }


@pytest.fixture
def populated_lake(tmp_path: Path) -> Path:
    root = tmp_path / "lake"
    writer = lake.LakeWriter(root=root, source="tradingview")
    for i in range(100):
        ticker = f"T{i:04d}"
        writer.extend("snapshots", DAY, [snapshot_row(ticker, m) for m in range(3)])
    writer.flush(now=NOW)
    return root


def summaries_for(movers: set[str]) -> dict[str, TickerDaySummary]:
    return {
        f"T{i:04d}": summary(f"T{i:04d}", up=80.0 if f"T{i:04d}" in movers else 1.0)
        for i in range(100)
    }


def test_every_mover_survives_at_full_resolution(populated_lake):
    movers = {"T0001", "T0002", "T0003"}
    report, _pruned = pruning.prune_day(
        populated_lake,
        DAY,
        summaries_for(movers),
        now=NOW,
        move_threshold_pct=25.0,
        control_sample_pct=10,
    )
    kept = lake.read_day(populated_lake, "snapshots", DAY).to_pylist()
    kept_movers = {r["ticker"] for r in kept if r["retention_class"] == "mover"}
    assert kept_movers == movers
    assert sum(1 for r in kept if r["ticker"] in movers) == len(movers) * 3
    assert report.mover_count == 3


def test_one_pruned_summary_row_per_dropped_ticker(populated_lake):
    report, pruned = pruning.prune_day(
        populated_lake,
        DAY,
        summaries_for(set()),
        now=NOW,
        move_threshold_pct=25.0,
        control_sample_pct=10,
    )
    assert len(pruned) == report.dropped_count
    assert {row["ticker"] for row in pruned}.isdisjoint(
        {r["ticker"] for r in lake.read_day(populated_lake, "snapshots", DAY).to_pylist()}
    )
    assert all(row["drop_reason"] for row in pruned)


def test_the_control_sample_survives_and_is_labelled(populated_lake):
    report, _ = pruning.prune_day(
        populated_lake,
        DAY,
        summaries_for(set()),
        now=NOW,
        move_threshold_pct=25.0,
        control_sample_pct=10,
    )
    kept = lake.read_day(populated_lake, "snapshots", DAY).to_pylist()
    controls = {r["ticker"] for r in kept if r["retention_class"] == "control"}
    assert len(controls) == report.control_count
    assert 3 <= len(controls) <= 20  # ~10 of 100


def test_a_ticker_without_metrics_is_kept_not_dropped(populated_lake, caplog):
    """A failed outcome job must not discard snapshots that cannot be refetched."""
    partial = summaries_for(set())
    del partial["T0007"]
    with caplog.at_level("WARNING"):
        pruning.prune_day(
            populated_lake,
            DAY,
            partial,
            now=NOW,
            move_threshold_pct=25.0,
            control_sample_pct=10,
        )
    kept = {r["ticker"] for r in lake.read_day(populated_lake, "snapshots", DAY).to_pylist()}
    assert "T0007" in kept
    assert "cannot be refetched" in caplog.text


def test_pruning_rewrites_the_partition_exactly_once(populated_lake):
    pruning.prune_day(
        populated_lake,
        DAY,
        summaries_for({"T0001"}),
        now=NOW,
        move_threshold_pct=25.0,
        control_sample_pct=10,
    )
    parts = list(lake.partition_path(populated_lake, "snapshots", DAY).glob("*.parquet"))
    assert len(parts) == 1


def test_reweighted_control_reproduces_the_population_rate(populated_lake):
    """The re-weighting that research depends on, checked end to end."""
    # Population: 100 tickers, 20 of which "hit" (here: are movers).
    hits = {f"T{i:04d}" for i in range(20)}
    report, _ = pruning.prune_day(
        populated_lake,
        DAY,
        summaries_for(hits),
        now=NOW,
        move_threshold_pct=25.0,
        control_sample_pct=10,
    )
    kept = lake.read_day(populated_lake, "snapshots", DAY).to_pylist()
    tickers = {r["ticker"]: r["retention_class"] for r in kept}
    movers = sum(1 for c in tickers.values() if c == "mover")
    controls = sum(1 for c in tickers.values() if c == "control")

    weight = control_weight(10)
    estimated_population = movers + controls * weight
    estimated_rate = movers / estimated_population
    assert estimated_population == pytest.approx(100, abs=35)
    assert estimated_rate == pytest.approx(0.20, abs=0.10)
    assert report.kept_count == movers + controls


# --- thinning ----------------------------------------------------------------


def bar_row(minute: int, *, high: float, low: float) -> dict:
    return {
        "ticker": "ABCD",
        "date": DAY,
        "minute_utc": datetime(2026, 3, 10, 14, minute, tzinfo=UTC),
        "open": 10.0 + minute,
        "high": high,
        "low": low,
        "close": 10.5 + minute,
        "volume": 1_000.0,
        "trade_count": 10,
        "vwap": 10.2,
        "resolution_minutes": 1,
        "written_at_utc": NOW,
        "source": "alpaca",
        "schema_version": 1,
    }


def test_thinning_preserves_ohlcv_correctly():
    rows = [bar_row(m, high=20.0 + m, low=5.0 - m) for m in range(5)]
    thinned = pruning.thin_bars(rows, minutes=5)
    assert len(thinned) == 1
    bucket = thinned[0]
    assert bucket["open"] == rows[0]["open"]
    assert bucket["close"] == rows[-1]["close"]
    assert bucket["high"] == max(r["high"] for r in rows)
    assert bucket["low"] == min(r["low"] for r in rows)
    assert bucket["volume"] == sum(r["volume"] for r in rows)
    assert bucket["resolution_minutes"] == 5


def test_thinning_splits_on_bucket_boundaries():
    rows = [bar_row(m, high=20.0, low=5.0) for m in range(10)]
    thinned = pruning.thin_bars(rows, minutes=5)
    assert len(thinned) == 2
    assert thinned[0]["minute_utc"].minute == 0
    assert thinned[1]["minute_utc"].minute == 5


def test_thinning_rejects_a_zero_bucket():
    with pytest.raises(ValueError, match="minutes"):
        pruning.thin_bars([bar_row(0, high=1.0, low=1.0)], minutes=0)


# --- retention ---------------------------------------------------------------


def test_retention_thins_old_bars_and_leaves_recent_ones(tmp_path: Path):
    root = tmp_path / "lake"
    writer = lake.LakeWriter(root=root, source="alpaca")
    old_day = DAY - timedelta(days=200)
    for m in range(10):
        writer.append("bars_1m", old_day, {**bar_row(m, high=20.0, low=5.0), "date": old_day})
        writer.append("bars_1m", DAY, bar_row(m, high=20.0, low=5.0))
    writer.flush(now=NOW)

    actions = pruning.apply_retention(root, today=DAY, policy=POLICY, now=NOW)
    assert [a.day for a in actions] == [old_day]
    assert lake.row_count(root, "bars_1m", old_day) == 2
    assert lake.row_count(root, "bars_1m", DAY) == 10


def test_retention_never_touches_the_protected_tables(tmp_path: Path):
    root = tmp_path / "lake"
    writer = lake.LakeWriter(root=root, source="alpaca")
    ancient = DAY - timedelta(days=3000)
    writer.append(
        "daily_universe",
        ancient,
        {
            "ticker": "ABCD",
            "date": ancient,
            "session": "pre",
            "close": 5.0,
            "written_at_utc": NOW,
        },
    )
    writer.append(
        "evaluations",
        ancient,
        {
            "ticker": "ABCD",
            "date": ancient,
            "poll_ts_utc": NOW,
            "window_start_utc": NOW,
            "pillar_1_status": "pass",
            "pillar_2_status": "pass",
            "pillar_3_status": "fail",
            "pillar_4_status": "pass",
            "pillar_5_status": "unknown",
            "pillars_passed": 3,
            "pillars_unknown": 1,
            "tier": "watch",
            "written_at_utc": NOW,
        },
    )
    writer.flush(now=NOW)

    pruning.apply_retention(root, today=DAY, policy=POLICY, now=NOW)
    assert lake.row_count(root, "daily_universe", ancient) == 1
    assert lake.row_count(root, "evaluations", ancient) == 1
    assert {"daily_universe", "evaluations"} <= pruning.protected_tables()


def test_retention_drops_snapshots_past_the_horizon(tmp_path: Path):
    root = tmp_path / "lake"
    writer = lake.LakeWriter(root=root, source="tradingview")
    ancient = DAY - timedelta(days=700)
    writer.append("snapshots", ancient, {**snapshot_row("ABCD", 0), "date": ancient})
    writer.append("snapshots", DAY, snapshot_row("ABCD", 0))
    writer.flush(now=NOW)

    actions = pruning.apply_retention(root, today=DAY, policy=POLICY, now=NOW)
    assert any(a.table == "snapshots" and a.day == ancient for a in actions)
    assert lake.row_count(root, "snapshots", ancient) == 0
    assert lake.row_count(root, "snapshots", DAY) == 1


def test_dry_run_changes_nothing(tmp_path: Path):
    root = tmp_path / "lake"
    writer = lake.LakeWriter(root=root, source="tradingview")
    ancient = DAY - timedelta(days=700)
    writer.append("snapshots", ancient, {**snapshot_row("ABCD", 0), "date": ancient})
    writer.flush(now=NOW)

    actions = pruning.apply_retention(root, today=DAY, policy=POLICY, now=NOW, dry_run=True)
    assert actions
    assert lake.row_count(root, "snapshots", ancient) == 1


# --- size guardrail ----------------------------------------------------------


def test_size_warning_is_silent_below_the_threshold():
    assert pruning.size_warning({"snapshots": 5 * 1024**3}, POLICY) is None


def test_size_warning_names_the_largest_tables():
    sizes = {"bars_1m": 15 * 1024**3, "snapshots": 6 * 1024**3, "news": 1 * 1024**3}
    message = pruning.size_warning(sizes, POLICY)
    assert message is not None
    assert "bars_1m" in message
    assert "of 25 GB" in message
