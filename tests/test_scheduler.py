"""Tick planning: what is due when, and what must never run on a closed day."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest
from app.config import load_config
from app.core.timeutils import ET, UTC
from app.market_calendar import MarketCalendar
from app.scheduler import TickAction, build_daily_jobs, plan_tick


@pytest.fixture(scope="module")
def config():
    return load_config(Path("config.yaml"))


@pytest.fixture(scope="module")
def calendar() -> MarketCalendar:
    return MarketCalendar("XNYS")


def at(hour: int, minute: int, second: int = 0, *, day: date = date(2026, 3, 10)) -> datetime:
    return datetime.combine(day, time(hour, minute, second), tzinfo=ET).astimezone(UTC)


def test_a_holiday_is_always_idle(config, calendar):
    plan = plan_tick(at(8, 2, day=date(2026, 1, 1)), config, calendar)
    assert plan.action is TickAction.IDLE
    assert "not a trading day" in plan.reason


def test_a_weekend_is_idle(config, calendar):
    plan = plan_tick(at(8, 2, day=date(2026, 3, 14)), config, calendar)
    assert plan.action is TickAction.IDLE


def test_inside_an_alert_window_polls(config, calendar):
    plan = plan_tick(at(8, 2), config, calendar)
    assert plan.action is TickAction.ALERT_POLL
    assert plan.interval_seconds == 30
    assert plan.window_start_utc == at(8, 0)


def test_the_window_is_half_open_at_its_end(config, calendar):
    assert plan_tick(at(8, 4, 59), config, calendar).action is TickAction.ALERT_POLL
    assert plan_tick(at(8, 5), config, calendar).action is not TickAction.ALERT_POLL


def test_the_poll_interval_is_respected_within_a_window(config, calendar):
    now = at(8, 2)
    recent = plan_tick(now, config, calendar, last_poll_utc=now - timedelta(seconds=10))
    assert recent.action is TickAction.IDLE
    assert recent.window_start_utc == at(8, 0)

    due = plan_tick(now, config, calendar, last_poll_utc=now - timedelta(seconds=30))
    assert due.action is TickAction.ALERT_POLL


def test_alert_windows_take_precedence_over_the_collector(config, calendar):
    """Inside a window the collector reuses the 30s snapshots (CLAUDE.md 4)."""
    # 09:00 is inside both an alert window and the hot collector window.
    plan = plan_tick(at(9, 2), config, calendar)
    assert plan.action is TickAction.ALERT_POLL


def test_hot_collection_between_windows(config, calendar):
    plan = plan_tick(at(7, 30), config, calendar)
    assert plan.action is TickAction.COLLECT_HOT
    assert plan.interval_seconds == 60


def test_cold_collection_midday(config, calendar):
    plan = plan_tick(at(12, 0), config, calendar)
    assert plan.action is TickAction.COLLECT_COLD
    assert plan.interval_seconds == 300


def test_collector_cadence_is_respected(config, calendar):
    now = at(12, 0)
    plan = plan_tick(now, config, calendar, last_poll_utc=now - timedelta(seconds=60))
    assert plan.action is TickAction.IDLE
    assert "cadence" in plan.reason


def test_outside_every_window_is_idle(config, calendar):
    plan = plan_tick(at(22, 0), config, calendar)
    assert plan.action is TickAction.IDLE
    assert "outside every configured window" in plan.reason


def test_the_post_market_windows_are_planned_too(config, calendar):
    assert plan_tick(at(16, 2), config, calendar).action is TickAction.ALERT_POLL
    assert plan_tick(at(16, 32), config, calendar).action is TickAction.ALERT_POLL


def test_plan_carries_utc_timestamps(config, calendar):
    plan = plan_tick(at(8, 2), config, calendar)
    assert plan.now_utc.tzinfo is UTC
    assert plan.window_start_utc is not None
    assert plan.window_start_utc.tzinfo is UTC


# --- daily jobs --------------------------------------------------------------


def handlers(names: list[str]) -> dict:
    return {name: (lambda _now: None) for name in names}


ALL_JOBS = [
    "premarket_outcomes",
    "daily_digest_push",
    "corporate_actions",
    "full_day_outcomes",
    "data_quality_and_compaction",
]


def test_daily_jobs_pair_times_with_handlers(config):
    jobs = build_daily_jobs(config, handlers(ALL_JOBS))
    by_name = {job.name: job.at_et for job in jobs}
    assert by_name["corporate_actions"] == time(20, 10)
    assert by_name["full_day_outcomes"] == time(20, 15)
    assert by_name["data_quality_and_compaction"] == time(20, 45)


def test_a_missing_handler_raises_rather_than_skipping(config):
    """A job that silently never runs loses a month of outcomes unnoticed."""
    with pytest.raises(KeyError, match="full_day_outcomes"):
        build_daily_jobs(config, handlers([n for n in ALL_JOBS if n != "full_day_outcomes"]))


def test_corporate_actions_run_before_the_outcome_jobs(config):
    """Splits must be on record before move metrics are computed."""
    jobs = {job.name: job.at_et for job in build_daily_jobs(config, handlers(ALL_JOBS))}
    assert jobs["corporate_actions"] < jobs["full_day_outcomes"]
    assert jobs["full_day_outcomes"] < jobs["data_quality_and_compaction"]
