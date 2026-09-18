"""SQLite migrations, timestamp round-trips and the settings helpers."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from app.core.timeutils import UTC
from app.storage import db

DAY = date(2026, 3, 10)

NOW = datetime(2026, 3, 10, 12, 5, tzinfo=UTC)


@pytest.fixture
def connection(tmp_path: Path):
    with db.session(tmp_path / "app.db") as conn:
        yield conn


def test_migrate_creates_every_table(connection):
    names = {
        row["name"]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "push_subscriptions",
        "rvol_baseline",
        "recent_runners",
        "journal",
        "settings",
        "alerts",
    } <= names


def test_migrate_sets_user_version(connection):
    assert db.current_version(connection) == db.MIGRATIONS[-1][0]


def test_migrate_is_idempotent(tmp_path: Path):
    path = tmp_path / "app.db"
    with db.session(path) as first:
        db.set_setting(first, "tier_b_push_enabled", "false", now=NOW)
    with db.session(path) as second:
        # A second run applies nothing and must not disturb existing rows.
        assert db.migrate(second) == db.MIGRATIONS[-1][0]
        assert db.get_setting(second, "tier_b_push_enabled") == "false"


def test_connect_creates_parent_directory(tmp_path: Path):
    path = tmp_path / "nested" / "dir" / "app.db"
    with db.session(path):
        pass
    assert path.exists()


def test_wal_is_enabled(connection):
    mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_journal_action_is_constrained(connection):
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO journal (ticker, trade_date, action, created_at_utc)
            VALUES ('ABCD', '2026-03-10', 'yolo', ?)
            """,
            (db.utc_text(NOW),),
        )


def test_settings_upsert(connection):
    db.set_setting(connection, "max_pushes_per_window", "5", now=NOW)
    db.set_setting(connection, "max_pushes_per_window", "3", now=NOW + timedelta(minutes=1))
    assert db.get_setting(connection, "max_pushes_per_window") == "3"


def test_get_setting_default(connection):
    assert db.get_setting(connection, "never_set", default="fallback") == "fallback"


def test_utc_text_roundtrip_preserves_the_instant():
    text = db.utc_text(NOW)
    assert text.endswith("+00:00")
    assert db.read_utc(text) == NOW


def test_utc_text_normalises_a_non_utc_offset():
    from zoneinfo import ZoneInfo

    paris = NOW.astimezone(ZoneInfo("Europe/Paris"))
    assert db.read_utc(db.utc_text(paris)) == NOW


def test_utc_text_rejects_naive():
    from app.core.timeutils import NaiveDatetimeError

    with pytest.raises(NaiveDatetimeError):
        db.utc_text(datetime(2026, 3, 10, 12, 5))  # noqa: DTZ001


# --- alerts ------------------------------------------------------------------


def alert(alert_id: str = "2026-03-10-0800-ABCD-A", **overrides: object) -> db.AlertRecord:
    base = {
        "alert_id": alert_id,
        "ticker": "ABCD",
        "trade_date": DAY,
        "window_start_utc": NOW,
        "tier": "A",
        "price": 5.20,
        "gap_pct": 34.0,
        "rvol": 12.0,
        "rvol_source": "baseline",
        "float_shares": 4_100_000,
        "headline": "Phase 3 data",
        "pushed": True,
        "push_reason": "first A alert for ABCD",
    }
    return db.AlertRecord(**{**base, **overrides})


def test_alerts_round_trip(connection):
    db.record_alert(connection, alert(), now=NOW)
    rows = db.alerts_for_day(connection, DAY)
    assert len(rows) == 1
    assert rows[0]["ticker"] == "ABCD"
    assert rows[0]["pushed"] == 1


def test_replaying_a_window_updates_rather_than_duplicates(connection):
    """History shows one line per alert, not one per poll that produced it."""
    db.record_alert(connection, alert(price=5.20), now=NOW)
    db.record_alert(connection, alert(price=6.40), now=NOW + timedelta(seconds=30))
    rows = db.alerts_for_day(connection, DAY)
    assert len(rows) == 1
    assert rows[0]["price"] == 6.40


def test_a_push_flag_is_never_cleared_by_a_later_poll(connection):
    """The push happened; a later suppressed poll must not rewrite history."""
    db.record_alert(connection, alert(pushed=True), now=NOW)
    db.record_alert(connection, alert(pushed=False), now=NOW + timedelta(seconds=30))
    assert db.alerts_for_day(connection, DAY)[0]["pushed"] == 1


def test_alerts_are_scoped_by_day(connection):
    db.record_alert(connection, alert(), now=NOW)
    assert db.alerts_for_day(connection, date(2026, 3, 11)) == []
