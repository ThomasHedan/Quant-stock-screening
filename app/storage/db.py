"""SQLite app state.

What lives here is operational state the app needs to run: push subscriptions,
the RVOL baseline cache, recent runners, the trade journal, and the settings the
trader edits in the UI. Research data does **not** live here — it goes to the
Parquet lake, which is built for columnar scans over months of history.

The schema is applied by numbered migrations rather than ``CREATE TABLE IF NOT
EXISTS`` sprinkled around, so an upgrade on a machine that has been collecting
for months is a known, ordered operation.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from app.core.timeutils import to_utc

logger = logging.getLogger(__name__)

#: Ordered schema migrations. Append only — never edit a statement that has
#: already shipped, or two installs end up with silently different schemas.
MIGRATIONS: tuple[tuple[int, str], ...] = (
    (
        1,
        """
        CREATE TABLE push_subscriptions (
            endpoint        TEXT PRIMARY KEY,
            p256dh          TEXT NOT NULL,
            auth            TEXT NOT NULL,
            user_agent      TEXT,
            created_at_utc  TEXT NOT NULL,
            last_success_utc TEXT,
            failure_count   INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE rvol_baseline (
            ticker          TEXT NOT NULL,
            trade_date      TEXT NOT NULL,
            minute_offset   INTEGER NOT NULL,
            baseline_volume REAL NOT NULL,
            days_used       INTEGER NOT NULL,
            computed_at_utc TEXT NOT NULL,
            PRIMARY KEY (ticker, trade_date, minute_offset)
        );

        CREATE TABLE recent_runners (
            ticker          TEXT NOT NULL,
            run_date        TEXT NOT NULL,
            high_pct        REAL NOT NULL,
            float_shares    INTEGER,
            headline        TEXT,
            expires_on      TEXT NOT NULL,
            pinned          INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (ticker, run_date)
        );

        CREATE TABLE journal (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker          TEXT NOT NULL,
            trade_date      TEXT NOT NULL,
            alert_id        TEXT,
            action          TEXT NOT NULL CHECK (action IN ('traded','skipped','watched')),
            entry           REAL,
            exit            REAL,
            size            REAL,
            note            TEXT,
            tags            TEXT,
            created_at_utc  TEXT NOT NULL
        );

        CREATE TABLE settings (
            key             TEXT PRIMARY KEY,
            value           TEXT NOT NULL,
            updated_at_utc  TEXT NOT NULL
        );

        CREATE TABLE alerts (
            alert_id        TEXT PRIMARY KEY,
            ticker          TEXT NOT NULL,
            trade_date      TEXT NOT NULL,
            window_start_utc TEXT NOT NULL,
            tier            TEXT NOT NULL,
            price           REAL,
            gap_pct         REAL,
            rvol            REAL,
            rvol_source     TEXT,
            float_shares    INTEGER,
            headline        TEXT,
            pushed          INTEGER NOT NULL DEFAULT 0,
            push_reason     TEXT,
            created_at_utc  TEXT NOT NULL
        );

        CREATE INDEX idx_alerts_date ON alerts (trade_date, tier);
        CREATE INDEX idx_journal_ticker_date ON journal (ticker, trade_date);
        CREATE INDEX idx_recent_runners_expiry ON recent_runners (expires_on);
        """,
    ),
)


def _apply_pragmas(connection: sqlite3.Connection) -> None:
    """Durability and concurrency settings for a long-running scanner.

    WAL matters here: the scheduler writes alerts while the web process reads
    them for the Live page, and the default rollback journal would have the two
    blocking each other every poll.
    """
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")


def connect(path: Path) -> sqlite3.Connection:
    """Open (and create) the state database with rows as mappings."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    _apply_pragmas(connection)
    return connection


def current_version(connection: sqlite3.Connection) -> int:
    """The schema version this database is at (0 for an empty file)."""
    row = connection.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def migrate(connection: sqlite3.Connection) -> int:
    """Apply every pending migration in order; return the resulting version.

    Each migration runs in its own transaction together with the version bump,
    so an interrupted upgrade leaves the database at the last fully applied
    version rather than half-way through one.
    """
    version = current_version(connection)
    for target, statements in MIGRATIONS:
        if target <= version:
            continue
        logger.info("Applying SQLite migration %s", target)
        # The transaction markers go inside the script because executescript
        # commits any transaction opened outside it before running.
        script = f"BEGIN;\n{statements}\nPRAGMA user_version = {target};\nCOMMIT;"
        try:
            connection.executescript(script)
        except sqlite3.DatabaseError:
            connection.executescript("ROLLBACK;")
            logger.exception("Migration %s failed; database left at version %s", target, version)
            raise
        version = target
    return version


@contextmanager
def session(path: Path) -> Iterator[sqlite3.Connection]:
    """Open a migrated connection and close it afterwards."""
    connection = connect(path)
    try:
        migrate(connection)
        yield connection
    finally:
        connection.close()


def utc_text(value: datetime) -> str:
    """Serialise an aware datetime for storage.

    SQLite has no datetime type, so everything is stored as an ISO-8601 string
    in UTC with an explicit offset — readable in a plain ``sqlite3`` shell and
    unambiguous when read back.
    """
    return to_utc(value).isoformat()


def read_utc(value: str) -> datetime:
    """Parse a timestamp written by :func:`utc_text` back into UTC."""
    return to_utc(datetime.fromisoformat(value))


def get_setting(connection: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    """Read one UI-editable setting."""
    row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return default if row is None else str(row["value"])


def set_setting(connection: sqlite3.Connection, key: str, value: str, *, now: datetime) -> None:
    """Write one UI-editable setting. ``now`` is passed in, never read here."""
    connection.execute(
        """
        INSERT INTO settings (key, value, updated_at_utc) VALUES (?, ?, ?)
        ON CONFLICT (key) DO UPDATE SET value = excluded.value,
                                        updated_at_utc = excluded.updated_at_utc
        """,
        (key, value, utc_text(now)),
    )
