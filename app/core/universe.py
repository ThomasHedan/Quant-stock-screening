"""Tier 0 rollups: one row per (ticker, date, session) for every listed stock.

This is the unbiased backbone of the lake (CLAUDE.md 6.1). It is written from
*that day's* live listing and is never regenerated from a later ticker list:
names in this population delist constantly, and rebuilding a past partition
from today's universe would quietly erase exactly the stocks that blew up and
disappeared — the textbook survivorship bias, and in this population not a
small one.

Pure functions over bars the caller has already fetched. ``session_rollup``
knows nothing about where bars come from; ``listing_transitions`` knows nothing
about the lake. Both are driven by explicit dates so a backfill cannot
accidentally stamp today's date on last month's data.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, time

from app.core.moves import Bar
from app.core.timeutils import to_et
from app.core.types import MarketSession

logger = logging.getLogger(__name__)

_EPSILON = 1e-9


@dataclass(frozen=True, slots=True)
class SessionRollup:
    """OHLCV for one ticker in one session.

    ``vwap`` is volume-weighted from the bars rather than taken from any single
    one: a session vwap copied off the last bar would be that bar's vwap, which
    is a different number and a subtly wrong one.
    """

    session: MarketSession
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float
    vwap: float | None
    bar_count: int


def split_by_session(
    bars: list[Bar], bounds: dict[MarketSession, tuple[time, time]]
) -> dict[MarketSession, list[Bar]]:
    """Group bars into the three ET sessions, dropping anything outside them.

    A bar is attributed by its opening minute, so the 09:29 bar is pre-market
    and the 09:30 bar is regular — matching the half-open convention used
    everywhere else in this codebase.
    """
    grouped: dict[MarketSession, list[Bar]] = {session: [] for session in bounds}
    for bar in sorted(bars, key=lambda b: b.minute):
        wall = to_et(bar.minute).time()
        for session, (start, end) in bounds.items():
            if start <= wall < end:
                grouped[session].append(bar)
                break
    return grouped


def session_rollup(session: MarketSession, bars: list[Bar]) -> SessionRollup:
    """Roll one session's bars into a single OHLCV row.

    An empty session yields a row with zero volume and ``None`` prices rather
    than no row at all: "this stock did not trade pre-market" is information
    the research lake should be able to state, not infer from absence.
    """
    if not bars:
        return SessionRollup(
            session=session,
            open=None,
            high=None,
            low=None,
            close=None,
            volume=0.0,
            vwap=None,
            bar_count=0,
        )
    ordered = sorted(bars, key=lambda b: b.minute)
    volume = sum(bar.volume for bar in ordered)
    weighted = sum(
        (bar.vwap if bar.vwap is not None else bar.close) * bar.volume for bar in ordered
    )
    return SessionRollup(
        session=session,
        open=ordered[0].open,
        high=max(bar.high for bar in ordered),
        low=min(bar.low for bar in ordered),
        close=ordered[-1].close,
        volume=volume,
        vwap=(weighted / volume) if volume > _EPSILON else None,
        bar_count=len(ordered),
    )


def daily_rows(
    ticker: str,
    day: date,
    bars: list[Bar],
    bounds: dict[MarketSession, tuple[time, time]],
    *,
    prev_close: float | None,
    reference: dict[str, object] | None = None,
    halt_count: int = 0,
    halt_minutes: float = 0.0,
    split_flag: bool = False,
    split_ratio: float | None = None,
    days_since_reverse_split: int | None = None,
    suspect_price: bool = False,
    ticker_canonical_id: str | None = None,
) -> list[dict[str, object]]:
    """Build the three Tier 0 rows for one ticker-day.

    Always three rows, one per session, even when the stock never traded in
    one: the whole point of Tier 0 is that a question about any stock on any
    past day has an answer.
    """
    ref = reference or {}
    grouped = split_by_session(bars, bounds)
    rows: list[dict[str, object]] = []
    for session in bounds:
        rollup = session_rollup(session, grouped[session])
        rows.append(
            {
                "ticker": ticker,
                "date": day,
                "session": session.value,
                "open": rollup.open,
                "high": rollup.high,
                "low": rollup.low,
                "close": rollup.close,
                "volume": rollup.volume,
                "vwap": rollup.vwap,
                "prev_close": prev_close,
                "float_shares": ref.get("float_shares_outstanding"),
                "shares_outstanding": ref.get("total_shares_outstanding"),
                "market_cap": ref.get("market_cap"),
                "sector": ref.get("sector"),
                "split_flag": split_flag,
                "split_ratio": split_ratio,
                "days_since_reverse_split": days_since_reverse_split,
                "halt_count": halt_count,
                "halt_minutes": halt_minutes,
                "suspect_price": suspect_price,
                "ticker_canonical_id": ticker_canonical_id or ticker,
            }
        )
    return rows


@dataclass(frozen=True, slots=True)
class ListingRecord:
    """A ticker's observed lifetime. Nothing here is ever deleted."""

    ticker: str
    first_seen: date
    last_seen: date
    status: str
    ticker_canonical_id: str | None = None


def listing_transitions(
    known: dict[str, ListingRecord],
    seen_today: set[str],
    day: date,
    *,
    absent_days_before_delisted: int = 5,
) -> dict[str, ListingRecord]:
    """Update listing status from today's live universe.

    A ticker that stops appearing has its ``last_seen`` left where it was and
    its status moved to ``delisted`` only after several absent sessions — a
    single missing day is far more often a feed hiccup than a delisting, and
    flipping status on one absence would fill the table with noise.
    """
    updated = dict(known)
    for ticker in sorted(seen_today):
        record = updated.get(ticker)
        if record is None:
            updated[ticker] = ListingRecord(
                ticker=ticker, first_seen=day, last_seen=day, status="active"
            )
            continue
        updated[ticker] = ListingRecord(
            ticker=ticker,
            first_seen=record.first_seen,
            last_seen=day,
            status="active",
            ticker_canonical_id=record.ticker_canonical_id,
        )

    for ticker, record in known.items():
        if ticker in seen_today:
            continue
        absent = (day - record.last_seen).days
        if absent >= absent_days_before_delisted and record.status == "active":
            logger.info("%s absent for %s sessions; marking delisted", ticker, absent)
            updated[ticker] = ListingRecord(
                ticker=ticker,
                first_seen=record.first_seen,
                last_seen=record.last_seen,
                status="delisted",
                ticker_canonical_id=record.ticker_canonical_id,
            )
    return updated


def apply_symbol_change(
    known: dict[str, ListingRecord], old_symbol: str, new_symbol: str
) -> dict[str, ListingRecord]:
    """Link a renamed ticker's history to its new symbol.

    Both records keep the *old* symbol as ``ticker_canonical_id``, so a join on
    that column spans the rename and a year of history does not silently split
    in two (CLAUDE.md 6.4.2).
    """
    updated = dict(known)
    old = updated.get(old_symbol)
    canonical = (old.ticker_canonical_id if old else None) or old_symbol
    if old is not None:
        updated[old_symbol] = ListingRecord(
            ticker=old_symbol,
            first_seen=old.first_seen,
            last_seen=old.last_seen,
            status="renamed",
            ticker_canonical_id=canonical,
        )
    new = updated.get(new_symbol)
    updated[new_symbol] = ListingRecord(
        ticker=new_symbol,
        first_seen=new.first_seen if new else (old.first_seen if old else date.min),
        last_seen=new.last_seen if new else (old.last_seen if old else date.min),
        status="active",
        ticker_canonical_id=canonical,
    )
    return updated
