"""Finnhub news — the fallback used only when Alpaca is down.

Narrow by design (CLAUDE.md 5.5): it runs only while the primary feed is
unavailable, only for tickers that already passed pillars 1, 4 and 5, and
within a hard 60-calls-per-minute budget. A fallback that quietly becomes the
primary source would change what "fresh news" means mid-history without anyone
noticing, so every row it produces is labelled with its source.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from app.core.timeutils import to_utc
from app.core.types import NewsItem
from app.sources.retry import RetryPolicy, SourceError, call_with_retry

logger = logging.getLogger(__name__)

NEWS_URL = "https://finnhub.io/api/v1/company-news"


@dataclass(slots=True)
class RateLimiter:
    """A sliding-window call budget.

    Free tiers ban rather than throttle, and a banned key means no news at all
    for the rest of the session, so the limiter refuses calls instead of
    sleeping through them: the caller can decide to skip a ticker, which is a
    much better outcome than blocking a 30-second alert window.
    """

    max_calls: int
    window_seconds: float = 60.0
    _calls: deque[datetime] = field(default_factory=deque)

    def allow(self, *, now: datetime) -> bool:
        """Whether a call may be made, recording it when it may."""
        cutoff = to_utc(now) - timedelta(seconds=self.window_seconds)
        while self._calls and self._calls[0] < cutoff:
            self._calls.popleft()
        if len(self._calls) >= self.max_calls:
            return False
        self._calls.append(to_utc(now))
        return True

    @property
    def used(self) -> int:
        """Calls currently counted inside the window."""
        return len(self._calls)


def parse_item(raw: dict[str, Any], *, ticker: str, received_at: datetime) -> NewsItem:
    """Validate one Finnhub record into the shared :class:`NewsItem` shape."""
    news_id = raw.get("id")
    headline = raw.get("headline")
    stamp = raw.get("datetime")
    if news_id is None or not headline or not stamp:
        msg = f"finnhub record missing id/headline/datetime: {sorted(raw)}"
        raise ValueError(msg)
    created = datetime.fromtimestamp(int(stamp), tz=to_utc(received_at).tzinfo)
    return NewsItem(
        news_id=f"finnhub:{news_id}",
        symbols=(ticker.upper(),),
        headline=str(headline),
        source=f"finnhub:{raw.get('source', 'unknown')}",
        url=raw.get("url"),
        created_at=created,
        received_at=to_utc(received_at),
    )


def fetch_company_news(
    api_key: str,
    ticker: str,
    *,
    day: date,
    now: datetime,
    policy: RetryPolicy,
    limiter: RateLimiter,
    client: httpx.Client | None = None,
) -> tuple[NewsItem, ...]:
    """Fetch one ticker's news for one day, or return empty if over budget.

    Returning empty rather than raising on a budget refusal is deliberate: the
    caller is mid-window and needs to keep evaluating other tickers. The
    refusal is logged, and the resulting pillar-3 verdict is unknown rather
    than a false negative.
    """
    if not api_key:
        msg = "Finnhub fallback requested without an API key"
        raise SourceError(msg, transient=False)
    if not limiter.allow(now=now):
        logger.warning(
            "Finnhub budget of %s calls/min exhausted; skipping %s", limiter.max_calls, ticker
        )
        return ()

    owned = client is None
    http = client or httpx.Client()

    def attempt() -> list[dict[str, Any]]:
        response = http.get(
            NEWS_URL,
            params={
                "symbol": ticker.upper(),
                "from": day.isoformat(),
                "to": day.isoformat(),
                "token": api_key,
            },
            timeout=policy.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            msg = f"finnhub returned {type(payload).__name__}, expected a list"
            raise SourceError(msg, transient=False)
        return payload

    def retryable(exc: Exception) -> bool:
        from app.sources.alpaca import is_retryable

        return is_retryable(exc)

    try:
        records = call_with_retry(
            attempt,
            policy=policy,
            description=f"Finnhub news for {ticker}",
            is_retryable=retryable,
        )
    finally:
        if owned:
            http.close()

    items: list[NewsItem] = []
    for record in records:
        try:
            items.append(parse_item(record, ticker=ticker, received_at=now))
        except (ValueError, TypeError):
            logger.exception("Finnhub record failed validation for %s; skipping", ticker)
    return tuple(items)


def should_use_fallback(
    *, alpaca_healthy: bool, passed_pillars_1_4_5: bool, has_api_key: bool
) -> bool:
    """Whether the Finnhub fallback is warranted for one ticker right now.

    All three conditions are required. Widening any of them turns a fallback
    into a second primary source with different latency and coverage, which
    would silently change what pillar 3 means across the history.
    """
    return (not alpaca_healthy) and passed_pillars_1_4_5 and has_api_key
