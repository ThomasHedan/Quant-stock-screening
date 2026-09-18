"""The recent-runner watchlist: trading-day expiry, pins, badges and the digest."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from app import recent_runners
from app.recent_runners import RecentRunner
from app.storage import db

FRIDAY = date(2026, 3, 6)
MONDAY = date(2026, 3, 9)
TRADING_DAYS = (
    date(2026, 3, 5),
    FRIDAY,
    MONDAY,
    date(2026, 3, 10),
    date(2026, 3, 11),
    date(2026, 3, 12),
    date(2026, 3, 13),
)


@pytest.fixture
def connection(tmp_path: Path):
    with db.session(tmp_path / "app.db") as conn:
        yield conn


def runner(
    ticker: str = "ABCD",
    *,
    run_date: date = FRIDAY,
    high_pct: float = 84.0,
    expires_on: date | None = None,
    pinned: bool = False,
) -> RecentRunner:
    return RecentRunner(
        ticker=ticker,
        run_date=run_date,
        high_pct=high_pct,
        float_shares=4_100_000,
        headline="Phase 3 data",
        expires_on=expires_on or recent_runners.expiry_for(run_date, TRADING_DAYS, 5),
        pinned=pinned,
    )


# --- expiry ------------------------------------------------------------------


def test_expiry_counts_trading_days_not_calendar_days():
    """A Friday runner is still recent on Monday, and the weekend does not count."""
    expiry = recent_runners.expiry_for(FRIDAY, TRADING_DAYS, 5)
    assert expiry == date(2026, 3, 13)
    assert (expiry - FRIDAY).days == 7  # five trading days span a weekend


def test_expiry_clamps_to_the_last_known_trading_day():
    assert recent_runners.expiry_for(date(2026, 3, 12), TRADING_DAYS, 5) == date(2026, 3, 13)


def test_expiry_of_the_last_day_is_itself():
    assert recent_runners.expiry_for(date(2026, 3, 13), TRADING_DAYS, 5) == date(2026, 3, 13)


# --- storage -----------------------------------------------------------------


def test_add_and_list(connection):
    recent_runners.add(connection, runner())
    active = recent_runners.active(connection, today=MONDAY)
    assert [r.ticker for r in active] == ["ABCD"]
    assert active[0].high_pct == 84.0


def test_a_repeat_run_refreshes_rather_than_duplicates(connection):
    recent_runners.add(connection, runner(run_date=FRIDAY, high_pct=84.0))
    recent_runners.add(connection, runner(run_date=FRIDAY, high_pct=120.0))
    active = recent_runners.active(connection, today=MONDAY)
    assert len(active) == 1
    assert active[0].high_pct == 120.0


def test_two_runs_on_different_days_are_separate_entries(connection):
    recent_runners.add(connection, runner(run_date=FRIDAY))
    recent_runners.add(connection, runner(run_date=MONDAY))
    assert len(recent_runners.active(connection, today=MONDAY)) == 2


def test_tickers_returns_the_symbols_for_tiering(connection):
    recent_runners.add(connection, runner("ABCD"))
    recent_runners.add(connection, runner("EFGH"))
    assert recent_runners.tickers(connection, today=MONDAY) == frozenset({"ABCD", "EFGH"})


def test_expired_entries_drop_out_of_the_active_list(connection):
    recent_runners.add(connection, runner(expires_on=date(2026, 3, 8)))
    assert recent_runners.active(connection, today=MONDAY) == []


def test_pruning_removes_expired_entries(connection):
    recent_runners.add(connection, runner(expires_on=date(2026, 3, 8)))
    assert recent_runners.prune(connection, today=MONDAY) == 1
    assert recent_runners.active(connection, today=MONDAY) == []


def test_pruning_never_removes_a_pinned_entry(connection, caplog):
    """A cleanup job must not overrule the trader's own 'keep watching this'."""
    recent_runners.add(connection, runner(expires_on=date(2026, 3, 8), pinned=True))
    assert recent_runners.prune(connection, today=MONDAY) == 0
    assert [r.ticker for r in recent_runners.active(connection, today=MONDAY)] == ["ABCD"]


def test_a_pinned_entry_stays_active_past_its_expiry(connection):
    recent_runners.add(connection, runner(expires_on=date(2026, 3, 8)))
    assert recent_runners.pin(connection, "ABCD") == 1
    assert len(recent_runners.active(connection, today=date(2026, 4, 1))) == 1


def test_unpinning_restores_the_expiry(connection):
    recent_runners.add(connection, runner(expires_on=date(2026, 3, 8), pinned=True))
    recent_runners.pin(connection, "ABCD", pinned=False)
    assert recent_runners.active(connection, today=MONDAY) == []


def test_remove_deletes_the_entry(connection):
    recent_runners.add(connection, runner())
    assert recent_runners.remove(connection, "ABCD") == 1
    assert recent_runners.active(connection, today=MONDAY) == []


# --- presentation ------------------------------------------------------------


def test_badge_text_reads_naturally():
    assert runner().badge(today=FRIDAY) == "Ran +84% today"
    assert runner().badge(today=date(2026, 3, 7)) == "Ran +84% yesterday"
    assert runner().badge(today=MONDAY) == "Ran +84% 3 days ago"


def test_digest_leads_with_the_biggest_name():
    line = recent_runners.digest_line(
        [runner("SMALL", high_pct=55.0), runner("BIG", high_pct=120.0)], missed=3
    )
    assert line.startswith("3 runners missed today")
    assert "BIG +120%" in line


def test_digest_is_singular_for_one_runner():
    assert "1 runner missed" in recent_runners.digest_line([runner()], missed=1)


def test_digest_says_so_when_nothing_was_missed():
    assert recent_runners.digest_line([], missed=0) == "No runners missed today."
