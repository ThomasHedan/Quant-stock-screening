"""The news cache and freshness rules.

Pillar 3 asks a deceptively simple question — "is there a fresh catalyst?" —
and every part of it is a trap:

* Freshness must be judged on ``created_at`` only. Articles get revised hours
  later, and judging on ``updated_at`` would let tomorrow's edit make today's
  news look fresh: lookahead through the back door (CLAUDE.md 6.4.6).
* The cache must answer *as of* a moment, not "now". A backfill that inserts an
  article published at 08:02 into a cache queried for 08:01 must not make that
  article visible at 08:01.
* Feed latency has to be measurable, which is why ``received_at`` is kept
  alongside ``created_at``. If p95 latency exceeds a minute, the 15-minute
  freshness rule is measuring feed lag rather than market reaction, and the UI
  has to say so rather than pretend the number is clean.

Pure and in-memory: the durable copy lives in the lake.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from enum import StrEnum

from app.core.timeutils import ET, et_datetime, et_trading_date, minutes_between, to_et, to_utc
from app.core.types import NewsItem

logger = logging.getLogger(__name__)


class Freshness(StrEnum):
    """How recent an article is relative to the moment being evaluated."""

    FRESH = "fresh"
    TODAY = "today"
    STALE = "stale"
    NONE = "none"


def session_news_start(
    as_of: datetime,
    *,
    day_start: time,
    prev_day_start: time,
    regular_open: time = time(9, 30),
) -> datetime:
    """Start of the "today" news window for the moment being evaluated.

    Before the regular open, the relevant window reaches back to the
    *previous* day's 16:00 ET: a biotech that announced at 16:30 yesterday is
    the catalyst for this morning's gap, and a window starting at 04:00 today
    would miss it entirely (CLAUDE.md 5.5). From the open onwards, the window
    is today's own session start.
    """
    et_now = to_et(as_of)
    if et_now.time() < regular_open:
        previous_day = et_now.date() - timedelta(days=1)
        return et_datetime(previous_day, prev_day_start)
    return et_datetime(et_trading_date(as_of), day_start)


def classify(
    item: NewsItem | None,
    *,
    as_of: datetime,
    fresh_minutes: int,
    day_start: time = time(4, 0),
    prev_day_start: time = time(16, 0),
    regular_open: time = time(9, 30),
) -> Freshness:
    """Classify one article's recency at ``as_of``.

    An article timestamped in the future is ``STALE``, never ``FRESH``: a clock
    skew on the feed's side must not be able to manufacture a catalyst.
    """
    if item is None:
        return Freshness.NONE
    age = minutes_between(item.created_at, as_of)
    if age < 0:
        logger.warning(
            "News %s is timestamped %.1f minutes in the future; treating as stale",
            item.news_id,
            -age,
        )
        return Freshness.STALE
    if age <= fresh_minutes:
        return Freshness.FRESH
    window_start = session_news_start(
        as_of, day_start=day_start, prev_day_start=prev_day_start, regular_open=regular_open
    )
    if to_utc(item.created_at) >= window_start:
        return Freshness.TODAY
    return Freshness.STALE


@dataclass(slots=True)
class NewsCache:
    """In-memory index of recent news, queried point-in-time.

    Keyed by symbol, since every question asked of it is per-ticker. Items are
    deduplicated by ``news_id`` because the WebSocket and the REST backfill
    overlap by design: after a reconnect the last 60 minutes arrive twice, and
    counting an article twice would inflate every catalyst statistic.
    """

    _by_symbol: dict[str, list[NewsItem]] = field(default_factory=lambda: defaultdict(list))
    _seen: set[str] = field(default_factory=set)

    def add(self, item: NewsItem) -> bool:
        """Insert an article. Returns ``False`` if it was already known."""
        if item.news_id in self._seen:
            return False
        self._seen.add(item.news_id)
        for symbol in item.symbols:
            self._by_symbol[symbol.upper()].append(item)
        return True

    def extend(self, items: list[NewsItem]) -> int:
        """Insert many; returns how many were new."""
        return sum(1 for item in items if self.add(item))

    def latest_for(self, ticker: str, *, as_of: datetime) -> NewsItem | None:
        """The most recent article for a ticker that existed at ``as_of``.

        The ``as_of`` filter is the lookahead guard: an article created at
        08:02 is invisible to a query about 08:01, whatever order the cache was
        filled in.
        """
        cutoff = to_utc(as_of)
        visible = [
            item
            for item in self._by_symbol.get(ticker.upper(), [])
            if to_utc(item.created_at) <= cutoff
        ]
        if not visible:
            return None
        return max(visible, key=lambda item: to_utc(item.created_at))

    def fresh_for(
        self,
        ticker: str,
        *,
        as_of: datetime,
        fresh_minutes: int,
        day_start: time = time(4, 0),
        prev_day_start: time = time(16, 0),
    ) -> NewsItem | None:
        """The ticker's latest article, but only if it counts as fresh."""
        item = self.latest_for(ticker, as_of=as_of)
        verdict = classify(
            item,
            as_of=as_of,
            fresh_minutes=fresh_minutes,
            day_start=day_start,
            prev_day_start=prev_day_start,
        )
        return item if verdict is Freshness.FRESH else None

    def first_for(self, ticker: str, *, on: datetime) -> NewsItem | None:
        """The earliest article for a ticker on the ET day of ``on``.

        Used by the missed-runner diagnosis to line the first headline up
        against the moment the move started (CLAUDE.md 7.2).
        """
        day = et_trading_date(on)
        same_day = [
            item
            for item in self._by_symbol.get(ticker.upper(), [])
            if et_trading_date(item.created_at) == day
        ]
        if not same_day:
            return None
        return min(same_day, key=lambda item: to_utc(item.created_at))

    def prune_before(self, cutoff: datetime) -> int:
        """Drop articles older than ``cutoff``; returns how many were dropped.

        The cache only serves the live pillar check, so it needs a few hours at
        most. The lake keeps everything forever.
        """
        limit = to_utc(cutoff)
        dropped = 0
        for symbol, items in list(self._by_symbol.items()):
            kept = [item for item in items if to_utc(item.created_at) >= limit]
            dropped += len(items) - len(kept)
            if kept:
                self._by_symbol[symbol] = kept
            else:
                del self._by_symbol[symbol]
        self._seen = {item.news_id for items in self._by_symbol.values() for item in items}
        return dropped

    @property
    def size(self) -> int:
        """Distinct articles currently cached."""
        return len(self._seen)

    def symbols(self) -> frozenset[str]:
        """Symbols with at least one cached article."""
        return frozenset(self._by_symbol)


def feed_latency_seconds(item: NewsItem) -> float:
    """Seconds between publication and receipt.

    Negative values are kept rather than clamped: a systematically negative
    latency means the feed's clock disagrees with ours, which is worth seeing
    on the dashboard rather than hiding behind a ``max(0, …)``.
    """
    return (to_utc(item.received_at) - to_utc(item.created_at)).total_seconds()


def news_row(item: NewsItem, *, now: datetime) -> dict[str, object]:
    """Build the ``news`` lake row for one article."""
    return {
        "news_id": item.news_id,
        "date": et_trading_date(item.created_at),
        "symbols": list(item.symbols),
        "headline": item.headline,
        "news_source": item.source,
        "url": item.url,
        "created_at_utc": to_utc(item.created_at),
        "updated_at_utc": to_utc(item.updated_at) if item.updated_at else None,
        "received_at_utc": to_utc(item.received_at),
        "feed_latency_s": feed_latency_seconds(item),
        "written_at_utc": to_utc(now),
    }


def display_et(value: datetime) -> str:
    """Format an instant in ET for the UI and for log lines about news."""
    return to_et(value).strftime("%H:%M:%S ET")


__all__ = [
    "ET",
    "Freshness",
    "NewsCache",
    "classify",
    "display_et",
    "feed_latency_seconds",
    "news_row",
    "session_news_start",
]
