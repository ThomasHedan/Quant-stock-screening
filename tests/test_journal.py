"""The trade journal: append-only, one tap, never a training signal."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from app import journal
from app.core.timeutils import UTC
from app.journal import JournalAction, JournalEntry
from app.storage import db

DAY = date(2026, 3, 10)
NOW = datetime(2026, 3, 10, 13, 5, tzinfo=UTC)


@pytest.fixture
def connection(tmp_path: Path):
    with db.session(tmp_path / "app.db") as conn:
        yield conn


def entry(
    ticker: str = "ABCD",
    action: JournalAction = JournalAction.TRADED,
    **overrides: object,
) -> JournalEntry:
    base = {
        "ticker": ticker,
        "trade_date": DAY,
        "action": action,
        "entry": 5.20,
        "exit": 6.10,
        "size": 500.0,
        "note": "held through the 09:30 open",
        "tags": ("gap-and-go",),
    }
    return JournalEntry(**{**base, **overrides})  # type: ignore[arg-type]


# --- recording ---------------------------------------------------------------


def test_a_one_tap_entry_needs_only_ticker_and_action(connection):
    """Anything heavier than one tap will not get used mid-window."""
    minimal = JournalEntry(ticker="ABCD", trade_date=DAY, action=JournalAction.SKIPPED)
    assert journal.record(connection, minimal, now=NOW) > 0
    stored = journal.for_day(connection, DAY)
    assert stored[0].action is JournalAction.SKIPPED
    assert stored[0].entry is None


def test_a_full_entry_round_trips(connection):
    journal.record(connection, entry(), now=NOW)
    stored = journal.for_day(connection, DAY)[0]
    assert stored.ticker == "ABCD"
    assert stored.entry == 5.20
    assert stored.tags == ("gap-and-go",)
    assert stored.note is not None


def test_tickers_are_normalised(connection):
    journal.record(connection, entry(ticker="abcd"), now=NOW)
    assert journal.for_day(connection, DAY)[0].ticker == "ABCD"


def test_entries_are_append_only(connection):
    """What the trader thought at 08:05 is the interesting part."""
    journal.record(connection, entry(action=JournalAction.SKIPPED), now=NOW)
    journal.record(connection, entry(action=JournalAction.TRADED), now=NOW + timedelta(minutes=30))
    stored = journal.for_day(connection, DAY)
    assert len(stored) == 2
    assert [e.action for e in stored] == [JournalAction.TRADED, JournalAction.SKIPPED]


def test_latest_action_per_ticker_drives_the_buttons(connection):
    journal.record(connection, entry(action=JournalAction.SKIPPED), now=NOW)
    journal.record(connection, entry(action=JournalAction.TRADED), now=NOW + timedelta(minutes=30))
    assert journal.actions_for_day(connection, DAY) == {"ABCD": "traded"}


def test_entries_are_scoped_by_day(connection):
    journal.record(connection, entry(), now=NOW)
    assert journal.for_day(connection, date(2026, 3, 11)) == []


def test_for_ticker_returns_recent_history(connection):
    for day in (date(2026, 3, 9), DAY):
        journal.record(connection, entry(trade_date=day), now=NOW)
    assert len(journal.for_ticker(connection, "ABCD")) == 2


def test_an_invalid_action_cannot_be_stored(connection):
    import sqlite3

    with pytest.raises((sqlite3.IntegrityError, ValueError)):
        connection.execute(
            "INSERT INTO journal (ticker, trade_date, action, created_at_utc) VALUES (?,?,?,?)",
            ("ABCD", DAY.isoformat(), "yolo", db.utc_text(NOW)),
        )


# --- derived figures ---------------------------------------------------------


def test_realised_pnl_of_a_closed_trade():
    assert journal.realised_pnl(entry()) == pytest.approx((6.10 - 5.20) * 500)


def test_realised_pnl_of_a_loss_is_negative():
    assert journal.realised_pnl(entry(exit=4.80)) < 0


def test_no_pnl_for_a_skip():
    assert journal.realised_pnl(entry(action=JournalAction.SKIPPED)) is None


def test_no_pnl_for_an_open_position():
    assert journal.realised_pnl(entry(exit=None)) is None


# --- export ------------------------------------------------------------------


def test_export_rows_match_the_lake_schema(tmp_path: Path):
    from app.core.timeutils import et_trading_date
    from app.storage import lake

    root = tmp_path / "lake"
    writer = lake.LakeWriter(root=root, source="app")
    rows = journal.export_rows([entry(), entry(ticker="EFGH")], now=NOW)
    writer.extend("journal", et_trading_date(NOW), rows)
    writer.flush(now=NOW)

    stored = lake.read_day(root, "journal", et_trading_date(NOW)).to_pylist()
    assert {row["ticker"] for row in stored} == {"ABCD", "EFGH"}
    assert stored[0]["tags"] == ["gap-and-go"]


def test_export_preserves_the_decision_time_not_the_export_time():
    """A join to evaluations needs when the decision was made, not when exported."""
    decided = NOW - timedelta(hours=6)
    rows = journal.export_rows([entry(created_at_utc=decided)], now=NOW)
    assert rows[0]["created_at_utc"] == decided
    assert rows[0]["written_at_utc"] == NOW
