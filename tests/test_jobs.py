"""The daily job chain end to end.

Covers the acceptance criteria that only exist once the jobs actually run
(CLAUDE.md 11): pruning keeps movers and a control sample and writes one
pruned_summary row per dropped ticker, a 30-day lake reports its size and
applies retention, the news WebSocket gap reaches data_quality, and a reverse
split is a corporate action rather than a move.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from app import jobs
from app.core.moves import MoveMetrics
from app.core.timeutils import ET, UTC
from app.core.types import Tier
from app.main import build_state, run_tick
from app.storage import lake
from app.storage.pruning import TickerDaySummary

DAY = date(2026, 3, 10)
EVENING = datetime(2026, 3, 10, 20, 15, tzinfo=ET).astimezone(UTC)
NIGHT = datetime(2026, 3, 10, 20, 45, tzinfo=ET).astimezone(UTC)


@pytest.fixture
def state(tmp_path: Path, monkeypatch):
    """Runtime state on a temporary lake and database, in mock mode."""
    raw = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    raw["storage"]["lake_path"] = str(tmp_path / "lake")
    raw["storage"]["sqlite_path"] = str(tmp_path / "app.db")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    monkeypatch.setenv("MOCK_DATA", "1")
    from app.config import get_secrets, load_config

    get_secrets.cache_clear()
    return build_state(load_config(config_path))


def collect_a_window(state) -> None:
    """Run one alert window so the day has evaluations and snapshots."""
    base = datetime(2026, 3, 10, 8, 0, tzinfo=ET).astimezone(UTC)
    for step in range(4):
        run_tick(state, now=base + timedelta(seconds=30 * step))
    state.lake.flush(now=base)


# --- 20:10 corporate actions -------------------------------------------------


def test_corporate_actions_are_recorded_and_baselines_invalidated(state):
    report = jobs.run_corporate_actions(state, now=EVENING)
    state.lake.flush(now=EVENING)

    assert report.actions
    stored = lake.read_day(state.config.storage.lake_path, "corporate_actions", DAY).to_pylist()
    assert any(row["action_type"] == "reverse_split" for row in stored)
    assert any(row["ratio"] == pytest.approx(0.1) for row in stored)


def test_a_reverse_split_is_an_action_not_a_move(state):
    """Criterion 9: the split is classified, and no suspect_price row appears."""
    jobs.run_corporate_actions(state, now=EVENING)
    state.lake.flush(now=EVENING)
    actions = jobs._actions_for(state, DAY)
    split = next(action for action in actions if action.is_reverse_split)
    assert split.ratio == pytest.approx(0.1)

    from app.core.moves import suspect_price

    assert not suspect_price(
        10.0,
        1.0,
        threshold=state.config.integrity.suspect_price_change,
        volume_surge=False,
        corporate_action_on_record=True,
    )


def test_missing_credentials_warn_rather_than_crash(state):
    from app.config import Secrets

    object.__setattr__(state, "secrets", Secrets(mock_data=False, _env_file=None))
    report = jobs.run_corporate_actions(state, now=EVENING)
    assert report.error == "no credentials"
    assert any("Corporate actions skipped" in warning for warning in state.warnings)


# --- 20:15 outcomes and runners ---------------------------------------------


def test_the_outcome_job_writes_bars_universe_outcomes_and_runners(state):
    collect_a_window(state)
    report = jobs.run_outcomes_and_runners(state, now=EVENING)
    state.lake.flush(now=EVENING)

    assert report.tickers_fetched
    assert report.bar_rows > 0
    assert report.universe_rows > 0
    assert report.outcome_rows > 0
    root = state.config.storage.lake_path
    assert lake.row_count(root, "bars_1m", DAY) == report.bar_rows
    assert lake.row_count(root, "daily_universe", DAY) == report.universe_rows
    assert lake.row_count(root, "outcomes", DAY) == report.outcome_rows


def test_the_bar_scope_follows_the_evaluations(state):
    collect_a_window(state)
    scope = jobs.scope_from_evaluations(state, DAY)
    assert scope
    # The quiet filler names never reached 10% and never tiered, so they are out.
    assert all(not ticker.startswith("MK0") or value >= 10.0 for ticker, value in scope.items())


def test_an_empty_day_scopes_nothing(state):
    assert jobs.scope_from_evaluations(state, DAY) == {}


def test_every_tier_0_row_set_covers_all_three_sessions(state):
    collect_a_window(state)
    jobs.run_outcomes_and_runners(state, now=EVENING)
    state.lake.flush(now=EVENING)
    rows = lake.read_day(state.config.storage.lake_path, "daily_universe", DAY).to_pylist()
    by_ticker: dict[str, set[str]] = {}
    for row in rows:
        by_ticker.setdefault(str(row["ticker"]), set()).add(str(row["session"]))
    assert all(sessions == {"pre", "regular", "post"} for sessions in by_ticker.values())


def test_a_missed_runner_lands_on_the_watchlist(state):
    from app import recent_runners
    from app.storage import db

    collect_a_window(state)
    jobs.run_outcomes_and_runners(state, now=EVENING)
    with db.session(state.sqlite_path) as connection:
        active = recent_runners.active(connection, today=DAY)
    assert active
    assert state.recent_runners


def test_outcome_rows_carry_the_tradability_verdict(state):
    collect_a_window(state)
    jobs.run_outcomes_and_runners(state, now=EVENING)
    state.lake.flush(now=EVENING)
    rows = lake.read_day(state.config.storage.lake_path, "outcomes", DAY).to_pylist()
    assert rows
    assert all(row["tradeable"] in (True, False) for row in rows)
    assert any(row["tradeable"] is False for row in rows)


# --- 20:45 nightly -----------------------------------------------------------


def summaries_for(movers: set[str], others: set[str]) -> dict[str, TickerDaySummary]:
    return {
        ticker: TickerDaySummary(
            ticker=ticker,
            metrics=MoveMetrics(up_move_pct=80.0 if ticker in movers else 1.0, day_high=10.0),
            best_tier=Tier.NONE,
            poll_count=4,
        )
        for ticker in movers | others
    }


def test_nightly_prunes_keeps_movers_and_writes_pruned_summaries(state):
    """Criterion 5, through the real job rather than the module."""
    collect_a_window(state)
    root = state.config.storage.lake_path
    tickers = {str(row["ticker"]) for row in lake.read_day(root, "snapshots", DAY).to_pylist()}
    movers = set(list(tickers)[:3])
    report = jobs.run_nightly(state, now=NIGHT, summaries=summaries_for(movers, tickers - movers))

    assert report.prune is not None
    assert report.prune.mover_count == len(movers)
    kept = lake.read_day(root, "snapshots", DAY).to_pylist()
    kept_movers = {row["ticker"] for row in kept if row["retention_class"] == "mover"}
    assert kept_movers == movers
    assert lake.row_count(root, "pruned_summary", DAY) == report.prune.dropped_count


def test_nightly_without_move_metrics_refuses_to_prune(state, caplog):
    """No metrics means nothing can be classified; discarding would be final."""
    collect_a_window(state)
    root = state.config.storage.lake_path
    before = lake.row_count(root, "snapshots", DAY)
    with caplog.at_level("WARNING"):
        report = jobs.run_nightly(state, now=NIGHT, summaries=None)
    assert report.prune is None
    assert lake.row_count(root, "snapshots", DAY) == before
    assert "unrecoverable" in caplog.text


def test_nightly_writes_one_quality_row_per_table(state):
    collect_a_window(state)
    report = jobs.run_nightly(state, now=NIGHT, summaries={})
    rows = lake.read_day(state.config.storage.lake_path, "data_quality", DAY).to_pylist()
    assert report.quality_rows == len(rows)
    assert {row["table_name"] for row in rows} == {
        "evaluations",
        "outcomes",
        "news",
        "snapshots",
    }


def test_the_quality_row_carries_the_pruning_threshold(state):
    collect_a_window(state)
    root = state.config.storage.lake_path
    tickers = {str(row["ticker"]) for row in lake.read_day(root, "snapshots", DAY).to_pylist()}
    jobs.run_nightly(state, now=NIGHT, summaries=summaries_for(set(), tickers))
    rows = lake.read_day(root, "data_quality", DAY).to_pylist()
    snapshot_row = next(row for row in rows if row["table_name"] == "snapshots")
    assert snapshot_row["move_threshold_pct"] == pytest.approx(25.0)
    assert snapshot_row["control_count"] is not None


def test_a_websocket_gap_reaches_data_quality(state):
    """Criterion 8: the outage is visible in the research data, not just a log."""
    collect_a_window(state)
    state.feed_health.mark_disconnected(now=NIGHT - timedelta(minutes=12))
    jobs.run_nightly(state, now=NIGHT, summaries={})
    rows = lake.read_day(state.config.storage.lake_path, "data_quality", DAY).to_pylist()
    news_row = next(row for row in rows if row["table_name"] == "news")
    assert news_row["ws_disconnect_minutes"] == pytest.approx(12.0, abs=0.5)
    assert any("disconnected" in warning for warning in state.warnings)


def test_nightly_compacts_the_days_partitions(state):
    collect_a_window(state)
    root = state.config.storage.lake_path
    jobs.run_nightly(state, now=NIGHT, summaries={})
    parts = list(lake.partition_path(root, "evaluations", DAY).glob("*.parquet"))
    assert len(parts) == 1


# --- criterion 6: a 30-day lake, size and retention -------------------------


def test_a_thirty_day_lake_reports_its_size_and_applies_retention(state):
    """Criterion 6: size on /research, retention applied, and what it dropped."""
    from app import research
    from app.storage.pruning import RetentionPolicy, apply_retention, size_warning

    root = state.config.storage.lake_path
    writer = state.lake
    for offset in range(30):
        day = DAY - timedelta(days=200 + offset)  # old enough for bar thinning
        for minute in range(10):
            writer.append(
                "bars_1m",
                day,
                {
                    "ticker": "ABCD",
                    "date": day,
                    "minute_utc": EVENING + timedelta(minutes=minute),
                    "open": 5.0,
                    "high": 5.5,
                    "low": 4.5,
                    "close": 5.2,
                    "volume": 1_000.0,
                    "trade_count": 10,
                    "vwap": 5.1,
                    "resolution_minutes": 1,
                    "written_at_utc": EVENING,
                },
            )
    writer.flush(now=EVENING)

    stats = {stat.name: stat for stat in research.table_stats(root)}
    assert stats["bars_1m"].days == 30
    assert stats["bars_1m"].size_bytes > 0

    policy = RetentionPolicy(
        bars_1m_raw_days=90,
        bars_1m_thinned_minutes=5,
        snapshots_months=18,
        max_lake_gb=25,
        warn_fraction=0.8,
    )
    actions = apply_retention(root, today=DAY, policy=policy, now=NIGHT)
    assert len(actions) == 30
    assert all(action.rows_after < action.rows_before for action in actions)
    assert all("thinned to 5m" in action.action for action in actions)

    # And the guardrail fires when the lake is pushed past its cap.
    oversized = {"bars_1m": 24 * 1024**3}
    assert "of 25 GB" in (size_warning(oversized, policy) or "")


def test_retention_leaves_tier_zero_alone_whatever_the_size(state):
    from app.storage.pruning import RetentionPolicy, apply_retention

    root = state.config.storage.lake_path
    ancient = DAY - timedelta(days=3000)
    state.lake.append(
        "daily_universe",
        ancient,
        {
            "ticker": "ABCD",
            "date": ancient,
            "session": "pre",
            "close": 5.0,
            "written_at_utc": EVENING,
        },
    )
    state.lake.flush(now=EVENING)
    apply_retention(
        root,
        today=DAY,
        policy=RetentionPolicy(
            bars_1m_raw_days=1,
            bars_1m_thinned_minutes=5,
            snapshots_months=1,
            max_lake_gb=1,
            warn_fraction=0.1,
        ),
        now=NIGHT,
    )
    assert lake.row_count(root, "daily_universe", ancient) == 1


# --- the digest --------------------------------------------------------------


def test_the_digest_names_the_biggest_missed_runner(state):
    collect_a_window(state)
    jobs.run_outcomes_and_runners(state, now=EVENING)
    state.lake.flush(now=EVENING)
    message = jobs.run_digest_push(state, now=EVENING)
    assert message is not None
    assert "missed today" in message


def test_the_digest_is_silent_when_nothing_was_missed(state):
    assert jobs.run_digest_push(state, now=EVENING) is None


def test_the_digest_respects_the_settings_toggle(state):
    object.__setattr__(state.config.alerts, "daily_digest_enabled", False)
    assert jobs.run_digest_push(state, now=EVENING) is None


# --- previous closes ---------------------------------------------------------


def test_previous_closes_prefer_yesterdays_tier_zero_row(state):
    prior = state.calendar.previous_trading_days(DAY, 1)[0]
    state.lake.append(
        "daily_universe",
        prior,
        {
            "ticker": "ABCD",
            "date": prior,
            "session": "regular",
            "close": 4.20,
            "written_at_utc": EVENING,
        },
    )
    state.lake.flush(now=EVENING)
    assert jobs.previous_closes(state, ("ABCD",), DAY, now=EVENING)["ABCD"] == pytest.approx(4.20)


def test_previous_closes_fall_back_to_the_bar_source_on_day_one(state):
    """Without this, the first day of operation has no gap and no runners."""
    closes = jobs.previous_closes(state, ("MKEARLY", "MKAAA"), DAY, now=EVENING)
    assert closes["MKEARLY"] == pytest.approx(4.00)
    assert closes["MKAAA"] == pytest.approx(3.88)


def test_a_ticker_with_no_previous_close_is_logged_not_silently_dropped(state, caplog):
    with caplog.at_level("WARNING"):
        closes = jobs.previous_closes(state, ("NOSUCH",), DAY, now=EVENING)
    assert "NOSUCH" not in closes
    assert "No previous close" in caplog.text


def test_a_split_puts_the_previous_close_on_todays_basis(state):
    from app.core.integrity import CorporateAction

    split = CorporateAction(
        ticker="ABCD", effective_date=DAY, action_type="reverse_split", ratio=0.1
    )
    # Ten old shares become one, so yesterday's $1.00 is $10.00 on today's basis
    # and the split day reads as flat rather than +900%.
    assert jobs._adjust_prev_close(1.00, split) == pytest.approx(10.0)
    assert jobs._adjust_prev_close(1.00, None) == pytest.approx(1.00)


def test_runners_are_detected_once_previous_closes_are_available(state):
    """The regression this fix exists for: the job produced zero runners."""
    collect_a_window(state)
    report = jobs.run_outcomes_and_runners(state, now=EVENING)
    state.lake.flush(now=EVENING)
    assert report.runner_rows > 0
    rows = lake.read_day(state.config.storage.lake_path, "runners", DAY).to_pylist()
    assert {row["ticker"] for row in rows} >= {"MKEARLY", "MKNEAR"}


def test_a_runner_the_scanner_alerted_on_is_listed_as_caught(state):
    """CAUGHT rows stay on the page for comparison (CLAUDE.md 7.2)."""
    collect_a_window(state)
    jobs.run_outcomes_and_runners(state, now=EVENING)
    state.lake.flush(now=EVENING)
    rows = lake.read_day(state.config.storage.lake_path, "runners", DAY).to_pylist()
    caught = next(row for row in rows if row["ticker"] == "MKEARLY")
    assert caught["miss_reasons"] == ["CAUGHT"]
    assert caught["best_tier"] in {"A", "B"}


def test_a_23m_float_runner_is_diagnosed_through_the_job(state):
    """The 23M-float name never alerts, so the job has to explain why."""
    collect_a_window(state)
    jobs.run_outcomes_and_runners(state, now=EVENING)
    state.lake.flush(now=EVENING)
    rows = lake.read_day(state.config.storage.lake_path, "runners", DAY).to_pylist()
    near = next(row for row in rows if row["ticker"] == "MKNEAR")
    assert "FAILED_PILLAR" in (near["miss_reasons"] or [])
    assert "NEAR_MISS" in (near["miss_reasons"] or [])
    assert "23.0M" in (near["miss_detail"] or "")
