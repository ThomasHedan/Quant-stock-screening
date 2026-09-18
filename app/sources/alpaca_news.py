"""Alpaca news: WebSocket listener plus REST backfill.

The listener subscribes to every symbol and stores each item as it arrives. On
startup *and* on every reconnect it backfills the last 60 minutes over REST,
because the gap between a dropped socket and a restored one is exactly when a
catalyst arrives and is missed (CLAUDE.md 5.5). The overlap that creates is
deliberate and harmless: the cache deduplicates by article id.

Disconnect minutes are counted and handed to ``data_quality``. A feed that was
down is not a market with no news, and the two must never look the same in the
research data.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx

from app.core.news import NewsCache
from app.core.timeutils import to_utc
from app.core.types import NewsItem
from app.sources.alpaca import DATA_BASE_URL, AlpacaCredentials, paginate
from app.sources.retry import RetryPolicy

logger = logging.getLogger(__name__)

NEWS_REST_URL = f"{DATA_BASE_URL}/v1beta1/news"
NEWS_WS_URL = "wss://stream.data.alpaca.markets/v1beta1/news"


def parse_item(raw: dict[str, Any], *, received_at: datetime) -> NewsItem:
    """Validate one Alpaca news record.

    ``received_at`` is supplied by the caller rather than read from a clock
    here, so a backfilled article can be stamped with the moment it actually
    reached us instead of the moment it was parsed.
    """
    news_id = raw.get("id")
    created = raw.get("created_at")
    headline = raw.get("headline")
    if news_id is None or created is None or not headline:
        missing = [
            name
            for name, value in (("id", news_id), ("created_at", created), ("headline", headline))
            if not value
        ]
        msg = f"news record missing {missing}"
        raise ValueError(msg)
    symbols = tuple(str(s).upper() for s in raw.get("symbols", []) if s)
    updated = raw.get("updated_at")
    return NewsItem(
        news_id=str(news_id),
        symbols=symbols,
        headline=str(headline),
        source=str(raw.get("source") or "alpaca"),
        url=raw.get("url"),
        created_at=to_utc(datetime.fromisoformat(str(created).replace("Z", "+00:00"))),
        received_at=to_utc(received_at),
        updated_at=(
            to_utc(datetime.fromisoformat(str(updated).replace("Z", "+00:00"))) if updated else None
        ),
    )


@dataclass(frozen=True, slots=True)
class BackfillResult:
    """What a REST backfill produced."""

    items: tuple[NewsItem, ...]
    unparsable: int = 0
    errors: tuple[str, ...] = ()


def parse_backfill(pages: list[dict[str, Any]], *, received_at: datetime) -> BackfillResult:
    """Validate a REST news response, keeping what parses."""
    items: list[NewsItem] = []
    errors: list[str] = []
    unparsable = 0
    for page in pages:
        for raw in page.get("news", []):
            try:
                items.append(parse_item(raw, received_at=received_at))
            except (ValueError, TypeError) as exc:
                unparsable += 1
                if len(errors) < 5:
                    errors.append(str(exc))
    if unparsable:
        logger.error("Alpaca news backfill: %s records failed validation", unparsable)
    return BackfillResult(tuple(items), unparsable, tuple(errors))


def backfill(
    credentials: AlpacaCredentials,
    *,
    since: datetime,
    until: datetime,
    policy: RetryPolicy,
    client: httpx.Client | None = None,
    limit: int = 50,
) -> BackfillResult:
    """Fetch news published in ``[since, until]`` over REST.

    Called on startup and after every reconnect. ``until`` is explicit so the
    backfill cannot silently pull articles newer than the moment it is
    reconstructing.
    """
    owned = client is None
    http = client or httpx.Client(headers=credentials.headers())
    try:
        pages = paginate(
            http,
            NEWS_REST_URL,
            params={
                "start": to_utc(since).isoformat(),
                "end": to_utc(until).isoformat(),
                "limit": limit,
                "sort": "desc",
            },
            policy=policy,
            description="Alpaca news backfill",
        )
    finally:
        if owned:
            http.close()
    result = parse_backfill(pages, received_at=until)
    logger.info(
        "Backfilled %s news items for %s..%s",
        len(result.items),
        to_utc(since).isoformat(),
        to_utc(until).isoformat(),
    )
    return result


@dataclass(slots=True)
class FeedHealth:
    """Connection bookkeeping handed to ``data_quality`` each night.

    Disconnect minutes matter more than they look: during an outage pillar 3
    has no information, and an evaluation recorded as "no news" then is a false
    negative that research would otherwise read as a real one.
    """

    connected_since: datetime | None = None
    disconnected_since: datetime | None = None
    disconnect_minutes: float = 0.0
    reconnects: int = 0
    messages: int = 0

    def mark_connected(self, *, now: datetime) -> None:
        """Record a successful connection, closing any open outage."""
        if self.disconnected_since is not None:
            gap = (to_utc(now) - to_utc(self.disconnected_since)).total_seconds() / 60.0
            self.disconnect_minutes += gap
            self.reconnects += 1
            logger.warning("News feed reconnected after %.1f minutes", gap)
            self.disconnected_since = None
        self.connected_since = to_utc(now)

    def mark_disconnected(self, *, now: datetime) -> None:
        """Record a dropped connection."""
        if self.disconnected_since is None:
            self.disconnected_since = to_utc(now)
        self.connected_since = None

    def outage_minutes(self, *, now: datetime) -> float:
        """Total outage minutes including one currently in progress."""
        pending = 0.0
        if self.disconnected_since is not None:
            pending = (to_utc(now) - to_utc(self.disconnected_since)).total_seconds() / 60.0
        return self.disconnect_minutes + pending


@dataclass(slots=True)
class NewsListener:
    """Drives the WebSocket, the cache and the reconnect/backfill loop.

    The socket itself is injected as a factory so the whole reconnect path is
    testable without a network: the tests hand it a fake that drops on demand.
    """

    credentials: AlpacaCredentials
    cache: NewsCache
    policy: RetryPolicy
    backfill_minutes: int = 60
    health: FeedHealth = field(default_factory=FeedHealth)
    on_item: Callable[[NewsItem], None] | None = None

    def handle_message(self, payload: str | bytes, *, received_at: datetime) -> list[NewsItem]:
        """Parse one WebSocket frame into cached news items.

        Non-news control frames (subscription acks, errors) are logged and
        skipped rather than raising: a malformed frame must not take down a
        listener that is meant to run unattended for sixteen hours.
        """
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError:
            logger.exception("News frame was not valid JSON; skipping")
            return []
        frames = decoded if isinstance(decoded, list) else [decoded]
        items: list[NewsItem] = []
        for frame in frames:
            if not isinstance(frame, dict):
                continue
            kind = frame.get("T")
            if kind == "error":
                logger.error("News stream error: %s", frame.get("msg", "unknown"))
                continue
            if kind != "n":
                logger.debug("Ignoring news control frame %r", kind)
                continue
            try:
                item = parse_item(frame, received_at=received_at)
            except (ValueError, TypeError):
                logger.exception("News frame failed validation; skipping")
                continue
            self.health.messages += 1
            if self.cache.add(item) and self.on_item is not None:
                self.on_item(item)
            items.append(item)
        return items

    def subscribe_payloads(self) -> list[dict[str, Any]]:
        """The auth and subscribe frames, in order.

        The key is in the payload, so this value must never be logged — which
        is why it is built here and handed straight to the socket rather than
        stored on the listener.
        """
        return [
            {
                "action": "auth",
                "key": self.credentials.key_id,
                "secret": self.credentials.secret_key,
            },
            {"action": "subscribe", "news": ["*"]},
        ]

    def backfill_window(self, *, now: datetime) -> tuple[datetime, datetime]:
        """The REST window to replay after a (re)connection."""
        return to_utc(now) - timedelta(minutes=self.backfill_minutes), to_utc(now)

    async def run(
        self,
        connect: Callable[[], Awaitable[Any]],
        *,
        clock: Callable[[], datetime],
        backfill_fn: Callable[[datetime, datetime], BackfillResult],
        max_cycles: int | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Connect, backfill, then consume frames until cancelled.

        A dropped socket is not an error to propagate — it is the normal
        overnight behaviour of a free feed — so the loop reconnects with
        backoff, backfills the gap, and records the outage. Only cancellation
        ends it.
        """
        cycle = 0
        while max_cycles is None or cycle < max_cycles:
            cycle += 1
            try:
                socket = await connect()
            except OSError:
                self.health.mark_disconnected(now=clock())
                delay = self.policy.delay_for(min(cycle, self.policy.max_retries + 1))
                logger.exception("News socket failed to connect; retrying in %.1fs", delay)
                await sleep(delay)
                continue

            self.health.mark_connected(now=clock())
            start, end = self.backfill_window(now=clock())
            replayed = backfill_fn(start, end)
            added = self.cache.extend(list(replayed.items))
            logger.info("Backfill added %s of %s items after connect", added, len(replayed.items))

            try:
                async for frame in socket:
                    self.handle_message(frame, received_at=clock())
            except (OSError, ConnectionError):
                logger.exception("News socket dropped; will reconnect")
            finally:
                with contextlib.suppress(Exception):
                    await socket.close()
            self.health.mark_disconnected(now=clock())


# --- mock mode ---------------------------------------------------------------


def mock_items(*, now: datetime, tickers: list[str]) -> list[NewsItem]:
    """Synthetic catalysts, one per ticker, staggered over the last hour."""
    return [
        NewsItem(
            news_id=f"mock-{ticker}-{index}",
            symbols=(ticker,),
            headline=f"{ticker} announces positive topline results",
            source="benzinga",
            url=f"https://example.invalid/{ticker}",
            created_at=to_utc(now) - timedelta(minutes=index * 7),
            received_at=to_utc(now) - timedelta(minutes=index * 7) + timedelta(seconds=2),
        )
        for index, ticker in enumerate(tickers)
    ]
