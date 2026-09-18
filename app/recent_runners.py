"""The recent-runner watchlist: yesterday's movers feeding back into today.

A stock that ran 84% two days ago and is up again this morning deserves eyes
even when it passes only pillar 1 — the float is already known to be small and
the crowd already knows the name (CLAUDE.md 7.4). Entries expire after a
configurable number of *trading* days, not calendar days: a runner from Friday
is still recent on Monday.

Manual pins never expire, because "keep an eye on this one" is the trader's
judgement and no cleanup job should overrule it.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import date

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RecentRunner:
    """A ticker on the watchlist and why it is there."""

    ticker: str
    run_date: date
    high_pct: float
    float_shares: int | None
    headline: str | None
    expires_on: date
    pinned: bool = False

    def badge(self, *, today: date) -> str:
        """The UI badge text, e.g. "Ran +84% 2 days ago"."""
        days = (today - self.run_date).days
        when = "today" if days == 0 else ("yesterday" if days == 1 else f"{days} days ago")
        return f"Ran +{self.high_pct:.0f}% {when}"


def expiry_for(run_date: date, trading_days: tuple[date, ...], keep_days: int) -> date:
    """The date this entry stops counting as recent.

    Counted in trading days from the exchange calendar, so a Friday runner is
    still recent on Monday rather than expiring over a weekend nobody traded.
    """
    later = sorted(day for day in trading_days if day > run_date)
    if len(later) >= keep_days:
        return later[keep_days - 1]
    return later[-1] if later else run_date


def add(
    connection: sqlite3.Connection,
    runner: RecentRunner,
) -> None:
    """Add or refresh a watchlist entry.

    A repeat run refreshes the entry rather than duplicating it: the same
    ticker running twice in a week is one name to watch, with the newer move as
    its headline.
    """
    connection.execute(
        """
        INSERT INTO recent_runners
            (ticker, run_date, high_pct, float_shares, headline, expires_on, pinned)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (ticker, run_date) DO UPDATE SET
            high_pct = excluded.high_pct,
            float_shares = excluded.float_shares,
            headline = excluded.headline,
            expires_on = excluded.expires_on
        """,
        (
            runner.ticker,
            runner.run_date.isoformat(),
            runner.high_pct,
            runner.float_shares,
            runner.headline,
            runner.expires_on.isoformat(),
            int(runner.pinned),
        ),
    )
    logger.info(
        "Watchlist: %s ran +%.0f%% on %s, recent until %s",
        runner.ticker,
        runner.high_pct,
        runner.run_date,
        runner.expires_on,
    )


def active(connection: sqlite3.Connection, *, today: date) -> list[RecentRunner]:
    """Entries still counting as recent on ``today``."""
    rows = connection.execute(
        """
        SELECT ticker, run_date, high_pct, float_shares, headline, expires_on, pinned
        FROM recent_runners
        WHERE pinned = 1 OR expires_on >= ?
        ORDER BY run_date DESC, high_pct DESC
        """,
        (today.isoformat(),),
    ).fetchall()
    return [
        RecentRunner(
            ticker=row["ticker"],
            run_date=date.fromisoformat(row["run_date"]),
            high_pct=float(row["high_pct"]),
            float_shares=row["float_shares"],
            headline=row["headline"],
            expires_on=date.fromisoformat(row["expires_on"]),
            pinned=bool(row["pinned"]),
        )
        for row in rows
    ]


def tickers(connection: sqlite3.Connection, *, today: date) -> frozenset[str]:
    """Just the symbols, for the tiering rule."""
    return frozenset(runner.ticker for runner in active(connection, today=today))


def pin(connection: sqlite3.Connection, ticker: str, *, pinned: bool = True) -> int:
    """Pin or unpin a ticker. Returns how many rows changed."""
    cursor = connection.execute(
        "UPDATE recent_runners SET pinned = ? WHERE ticker = ?", (int(pinned), ticker)
    )
    return cursor.rowcount or 0


def remove(connection: sqlite3.Connection, ticker: str) -> int:
    """Drop a ticker from the watchlist entirely."""
    cursor = connection.execute("DELETE FROM recent_runners WHERE ticker = ?", (ticker,))
    return cursor.rowcount or 0


def prune(connection: sqlite3.Connection, *, today: date) -> int:
    """Delete expired, unpinned entries. Returns how many were removed.

    Pinned entries survive: a cleanup job must not overrule the trader's own
    "keep watching this".
    """
    cursor = connection.execute(
        "DELETE FROM recent_runners WHERE pinned = 0 AND expires_on < ?", (today.isoformat(),)
    )
    removed = cursor.rowcount or 0
    if removed:
        logger.info("Watchlist: %s entries expired on %s", removed, today)
    return removed


def digest_line(runners: list[RecentRunner], *, missed: int) -> str:
    """The optional 11:10 digest push (CLAUDE.md 8).

    Leads with the count and the single biggest name: a notification read on a
    lock screen has room for one fact, and "how much did I miss" is the one
    worth having.
    """
    if not runners:
        return "No runners missed today."
    top = max(runners, key=lambda runner: runner.high_pct)
    return (
        f"{missed} runner{'s' if missed != 1 else ''} missed today — "
        f"top: {top.ticker} +{top.high_pct:.0f}%"
    )
