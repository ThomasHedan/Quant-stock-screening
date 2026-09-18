"""Change-only writer for the slow-moving ``reference`` fields.

Sector, float and the average-volume figures barely move, so repeating them on
every 30-second snapshot would multiply Tier 1 by several times for no
information gain. They are written once per ticker per day, plus whenever one
of them actually changes, and ``snapshots`` joins them on ``(ticker, date)``
(CLAUDE.md 6.3a).

The point-in-time rule applies with full force here: a float figure is stored
as it was *seen that day* and is never backfilled over. A later correction
becomes a new row on a later day. Overwriting would make a past evaluation
un-reproducible, and a research result nobody can reproduce is worthless.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from app.core.timeutils import to_utc

logger = logging.getLogger(__name__)

#: Fields whose change triggers a new reference row.
#:
#: ``market_cap`` is deliberately absent: it moves with every price tick, so
#: including it would turn "change-only" into "every poll" and defeat the whole
#: split. It is still stored on each row that gets written, as observed then.
COMPARED_FIELDS: tuple[str, ...] = (
    "sector",
    "industry",
    "float_shares_outstanding",
    "total_shares_outstanding",
    "average_volume_10d_calc",
    "average_volume_30d_calc",
    "float_source",
    "float_asof",
    "float_confidence",
)

ReferenceFields = dict[str, Any]


@dataclass(slots=True)
class ReferenceTracker:
    """Decides which reference rows are worth writing.

    Holds the last values seen per ``(ticker, date)`` for the current session
    only. A restart re-emits one row per ticker, which is the harmless
    direction: a duplicate reference row costs a few bytes, a missing one
    breaks the join for that day.
    """

    _last: dict[tuple[str, date], ReferenceFields] = field(default_factory=dict)

    def observe(
        self,
        ticker: str,
        day: date,
        fields: ReferenceFields,
        *,
        asof: datetime,
    ) -> ReferenceFields | None:
        """Return a row to write, or ``None`` when nothing has changed.

        ``asof`` is the instant the values were observed, not the instant they
        were written: the two differ by up to one flush interval, and research
        that reasons about what was knowable at 08:05 needs the former.
        """
        key = (ticker, day)
        previous = self._last.get(key)
        current = {name: fields.get(name) for name in COMPARED_FIELDS}
        if previous is not None and previous == current:
            return None

        if previous is not None:
            changed = [name for name in COMPARED_FIELDS if previous[name] != current[name]]
            logger.info("Reference change for %s on %s: %s", ticker, day, ", ".join(changed))

        self._last[key] = current
        return {
            "ticker": ticker,
            "date": day,
            "asof_ts_utc": to_utc(asof),
            **fields,
        }

    def forget_day(self, day: date) -> int:
        """Drop the memory of one day. Returns how many entries were dropped.

        Called at the end of a session so a process running for months does not
        accumulate a key per ticker per day.
        """
        stale = [key for key in self._last if key[1] == day]
        for key in stale:
            del self._last[key]
        return len(stale)

    @property
    def tracked(self) -> int:
        """How many (ticker, date) pairs are currently remembered."""
        return len(self._last)


def latest_per_ticker(rows: list[ReferenceFields]) -> dict[str, ReferenceFields]:
    """Reduce a day's reference rows to the last observation per ticker.

    This is the join key resolution ``snapshots`` needs: with change-only
    writes a ticker can have several rows in a day, and a snapshot taken at
    09:00 must join the values known at 09:00 — not the day's final ones.
    Callers that need that stricter behaviour use :func:`as_of_per_ticker`.
    """
    latest: dict[str, ReferenceFields] = {}
    for row in sorted(rows, key=lambda r: to_utc(r["asof_ts_utc"])):
        latest[str(row["ticker"])] = row
    return latest


def as_of_per_ticker(rows: list[ReferenceFields], as_of: datetime) -> dict[str, ReferenceFields]:
    """Reference values as they were known at ``as_of``.

    The point-in-time join: rows observed after ``as_of`` are invisible, so a
    float correction that arrived at 16:00 cannot leak into an 08:05
    evaluation. This is the lookahead guard for every research query that joins
    reference data (CLAUDE.md 1.1).
    """
    cutoff = to_utc(as_of)
    visible = [row for row in rows if to_utc(row["asof_ts_utc"]) <= cutoff]
    return latest_per_ticker(visible)
