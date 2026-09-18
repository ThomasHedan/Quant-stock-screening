"""SQLite migrations, timestamp round-trips and the settings helpers."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from app.core.timeutils import UTC
from app.storage import db

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
