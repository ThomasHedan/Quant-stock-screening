"""Time-of-day RVOL baselines: computation, caching and split invalidation.

The baseline answers "how much volume had this stock usually traded by this
minute of the session?", averaged over the last ten trading days from 1-minute
bars including extended hours (CLAUDE.md 5.3). It is cached per ticker per day
because the underlying bars do not change intraday, and recomputing ten days of
bars on every 30-second poll would exhaust the free tier before 08:05.

Two correctness rules the cache has to respect:

* **The baseline never includes today.** Comparing a stock against itself makes
  every RVOL tend to 1 and hides exactly the days worth finding.
* **A split invalidates it.** A reverse split rescales share counts, so a
  baseline averaged across the split is wrong by the split ratio — and wrong in
  the direction that manufactures a pillar-2 pass (CLAUDE.md 6.4.1).
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time

from app.core.metrics import baseline_volume_at
from app.core.moves import Bar
from app.core.timeutils import et_datetime, minutes_between, to_utc
from app.storage.db import utc_text

logger = logging.getLogger(__name__)

#: Supplies historical bars for a ticker over a set of days. Injected so the
#: network-bound half stays outside this module and the caching path is
#: testable offline.
BarFetcher = Callable[[str, tuple[date, ...]], dict[date, tuple[Bar, ...]]]


@dataclass(frozen=True, slots=True)
class BaselineEntry:
    """A cached baseline for one ticker, day and minute offset."""

    ticker: str
    trade_date: date
    minute_offset: int
    baseline_volume: float
    days_used: int


def minute_offset(as_of: datetime, *, day_start_et: time = time(4, 0)) -> int:
    """Whole minutes from the session start to ``as_of``.

    Offsets rather than clock times so the cache key survives the DST switch:
    08:05 ET is 245 minutes into the session in both March and November, while
    the UTC instant differs by an hour.
    """
    start = et_datetime(as_of.date(), day_start_et)
    return max(0, int(minutes_between(start, as_of)))


def compute_baseline(
    bars_by_day: dict[date, tuple[Bar, ...]],
    *,
    offset: int,
    day_start_et: time = time(4, 0),
) -> tuple[float | None, int]:
    """Average cumulative volume by ``offset`` minutes, over the given days.

    Returns ``(volume, days_used)``. A day with no bars contributes nothing and
    is excluded from the denominator: averaging in a zero for a day the stock
    did not trade would halve the baseline and double every RVOL.
    """
    per_day: dict[object, dict[datetime, float]] = {}
    for day, bars in bars_by_day.items():
        if not bars:
            continue
        session_start = et_datetime(day, day_start_et)
        per_day[day] = {to_utc(bar.minute): bar.volume for bar in bars}
        # Align every day on its own session start so the offset means the same
        # ET clock time on each, which is what makes the average comparable.
        per_day[day][session_start] = per_day[day].get(session_start, 0.0)
    if not per_day:
        return None, 0
    return baseline_volume_at(per_day, offset), len(per_day)


def load_cached(
    connection: sqlite3.Connection, ticker: str, day: date, offset: int
) -> BaselineEntry | None:
    """Read a cached baseline, or ``None``."""
    row = connection.execute(
        """
        SELECT baseline_volume, days_used FROM rvol_baseline
        WHERE ticker = ? AND trade_date = ? AND minute_offset = ?
        """,
        (ticker, day.isoformat(), offset),
    ).fetchone()
    if row is None:
        return None
    return BaselineEntry(
        ticker=ticker,
        trade_date=day,
        minute_offset=offset,
        baseline_volume=float(row["baseline_volume"]),
        days_used=int(row["days_used"]),
    )


def store(
    connection: sqlite3.Connection,
    entry: BaselineEntry,
    *,
    now: datetime,
) -> None:
    """Cache a computed baseline."""
    connection.execute(
        """
        INSERT INTO rvol_baseline
            (ticker, trade_date, minute_offset, baseline_volume, days_used, computed_at_utc)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (ticker, trade_date, minute_offset) DO UPDATE SET
            baseline_volume = excluded.baseline_volume,
            days_used = excluded.days_used,
            computed_at_utc = excluded.computed_at_utc
        """,
        (
            entry.ticker,
            entry.trade_date.isoformat(),
            entry.minute_offset,
            entry.baseline_volume,
            entry.days_used,
            utc_text(now),
        ),
    )


def invalidate(connection: sqlite3.Connection, tickers: tuple[str, ...], day: date) -> int:
    """Drop cached baselines for tickers affected by a corporate action.

    Returns how many rows were removed, which goes straight into
    ``data_quality`` as "baselines recomputed" — a number that should track the
    day's split count and is a useful alarm when it does not.
    """
    if not tickers:
        return 0
    placeholders = ",".join("?" for _ in tickers)
    cursor = connection.execute(
        f"DELETE FROM rvol_baseline WHERE trade_date = ? AND ticker IN ({placeholders})",  # noqa: S608
        (day.isoformat(), *tickers),
    )
    removed = cursor.rowcount or 0
    logger.info("Invalidated %s cached RVOL baselines for %s on %s", removed, tickers, day)
    return removed


def baseline_for(
    connection: sqlite3.Connection,
    ticker: str,
    *,
    as_of: datetime,
    fetch_bars: BarFetcher,
    baseline_days: int,
    trading_days: tuple[date, ...],
    now: datetime,
    day_start_et: time = time(4, 0),
) -> BaselineEntry | None:
    """Return the cached baseline, computing it from bars on a miss.

    ``fetch_bars`` supplies the historical bars; see :data:`BarFetcher`.
    """
    day = as_of.date()
    offset = minute_offset(as_of, day_start_et=day_start_et)
    cached = load_cached(connection, ticker, day, offset)
    if cached is not None:
        return cached

    days = tuple(d for d in trading_days if d < day)[-baseline_days:]
    if not days:
        logger.info("No prior trading days available for %s baseline on %s", ticker, day)
        return None

    bars_by_day = fetch_bars(ticker, days)
    volume, days_used = compute_baseline(bars_by_day, offset=offset, day_start_et=day_start_et)
    if volume is None or volume <= 0:
        logger.info("No usable baseline volume for %s at offset %s", ticker, offset)
        return None

    entry = BaselineEntry(
        ticker=ticker,
        trade_date=day,
        minute_offset=offset,
        baseline_volume=volume,
        days_used=days_used,
    )
    store(connection, entry, now=now)
    return entry
