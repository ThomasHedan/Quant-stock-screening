"""The trade journal: the trader's own context, captured while watching anyway.

The highest-value data in this system is not a price field — it is why the
trader took one setup and skipped another. It costs nothing to capture at the
moment of the decision and is impossible to reconstruct afterwards
(CLAUDE.md 7.5).

Two rules the design follows:

* **Two clicks maximum.** Anything heavier will not get used mid-window, and
  an unused journal is worth less than no journal because it looks like data.
  ``Traded`` / ``Skipped`` / ``Note`` are one tap; everything else is optional.
* **Never a training signal.** Journal entries join to evaluations and outcomes
  for the trader to read; they never feed threshold tuning. A scanner that
  learned from its user's hesitation would quietly converge on that hesitation.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from app.core.timeutils import to_utc
from app.storage.db import utc_text

logger = logging.getLogger(__name__)


class JournalAction(StrEnum):
    """What the trader did about a setup."""

    TRADED = "traded"
    SKIPPED = "skipped"
    WATCHED = "watched"


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """One decision, with whatever context the trader chose to add."""

    ticker: str
    trade_date: date
    action: JournalAction
    alert_id: str | None = None
    entry: float | None = None
    exit: float | None = None
    size: float | None = None
    note: str | None = None
    tags: tuple[str, ...] = ()
    created_at_utc: datetime | None = None
    entry_id: int | None = None


def record(connection: sqlite3.Connection, entry: JournalEntry, *, now: datetime) -> int:
    """Store one entry and return its id.

    Entries are append-only. A trader who changes their mind adds a second
    entry rather than overwriting the first: what they thought at 08:05 is the
    interesting part, and an edit would erase exactly that.
    """
    cursor = connection.execute(
        """
        INSERT INTO journal
            (ticker, trade_date, alert_id, action, entry, exit, size, note, tags, created_at_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entry.ticker.upper(),
            entry.trade_date.isoformat(),
            entry.alert_id,
            entry.action.value,
            entry.entry,
            entry.exit,
            entry.size,
            entry.note,
            json.dumps(list(entry.tags)) if entry.tags else None,
            utc_text(entry.created_at_utc or now),
        ),
    )
    entry_id = int(cursor.lastrowid or 0)
    logger.info(
        "Journal: %s %s on %s (id %s)", entry.action, entry.ticker, entry.trade_date, entry_id
    )
    return entry_id


def for_day(connection: sqlite3.Connection, day: date) -> list[JournalEntry]:
    """Every entry for one trading day, newest first."""
    rows = connection.execute(
        """
        SELECT id, ticker, trade_date, alert_id, action, entry, exit, size, note, tags,
               created_at_utc
        FROM journal WHERE trade_date = ?
        ORDER BY created_at_utc DESC, id DESC
        """,
        (day.isoformat(),),
    ).fetchall()
    return [_from_row(row) for row in rows]


def for_ticker(
    connection: sqlite3.Connection, ticker: str, *, limit: int = 50
) -> list[JournalEntry]:
    """Recent entries for one ticker, newest first."""
    rows = connection.execute(
        """
        SELECT id, ticker, trade_date, alert_id, action, entry, exit, size, note, tags,
               created_at_utc
        FROM journal WHERE ticker = ?
        ORDER BY created_at_utc DESC, id DESC
        LIMIT ?
        """,
        (ticker.upper(), limit),
    ).fetchall()
    return [_from_row(row) for row in rows]


def actions_for_day(connection: sqlite3.Connection, day: date) -> dict[str, str]:
    """The latest action per ticker, for rendering button states.

    Latest rather than first: the row should show what the trader most
    recently decided, while the history keeps both.
    """
    latest: dict[str, str] = {}
    for entry in reversed(for_day(connection, day)):
        latest[entry.ticker] = entry.action.value
    return latest


def _from_row(row: sqlite3.Row) -> JournalEntry:
    """Rebuild an entry from its stored row."""
    return JournalEntry(
        entry_id=int(row["id"]),
        ticker=row["ticker"],
        trade_date=date.fromisoformat(row["trade_date"]),
        alert_id=row["alert_id"],
        action=JournalAction(row["action"]),
        entry=row["entry"],
        exit=row["exit"],
        size=row["size"],
        note=row["note"],
        tags=tuple(json.loads(row["tags"])) if row["tags"] else (),
        created_at_utc=datetime.fromisoformat(row["created_at_utc"]),
    )


def export_rows(entries: list[JournalEntry], *, now: datetime) -> list[dict[str, object]]:
    """Build ``journal`` lake rows for the nightly export.

    Exported to the lake so the notebook can join journal entries to
    evaluations and outcomes — "setups I skipped that ran" and "setups I took
    that faded" are one query each (CLAUDE.md 7.5).
    """
    return [
        {
            "ticker": entry.ticker,
            "date": entry.trade_date,
            "alert_id": entry.alert_id,
            "action": entry.action.value,
            "entry": entry.entry,
            "exit": entry.exit,
            "size": entry.size,
            "note": entry.note,
            "tags": list(entry.tags),
            "created_at_utc": to_utc(entry.created_at_utc or now),
            "written_at_utc": to_utc(now),
        }
        for entry in entries
    ]


def realised_pnl(entry: JournalEntry) -> float | None:
    """Realised profit for a closed trade, or ``None``.

    Deliberately simple and free of fees: it is a memory aid in the journal,
    not an accounting figure, and presenting it as a P&L the trader might rely
    on would be a different (and out-of-scope) product.
    """
    if entry.action is not JournalAction.TRADED:
        return None
    if entry.entry is None or entry.exit is None or entry.size is None:
        return None
    return (entry.exit - entry.entry) * entry.size
