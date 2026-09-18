"""The news listener: frame handling, backfill, and reconnect bookkeeping."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta

import httpx
import pytest
from app.core.news import NewsCache
from app.core.timeutils import UTC
from app.sources import alpaca_news
from app.sources.alpaca import AlpacaCredentials
from app.sources.retry import RetryPolicy

NOW = datetime(2026, 3, 10, 12, 5, tzinfo=UTC)
CREDS = AlpacaCredentials(key_id="AKTEST", secret_key="secret")
POLICY = RetryPolicy(
    max_retries=1,
    base_seconds=0.001,
    max_seconds=0.001,
    timeout_seconds=5.0,
    connect_timeout_seconds=2.0,
)


def raw(news_id: str = "1", *, created: str = "2026-03-10T12:03:00Z", **extra: object) -> dict:
    return {
        "T": "n",
        "id": news_id,
        "headline": "Company announces FDA clearance",
        "symbols": ["abcd"],
        "source": "benzinga",
        "url": "https://example.invalid/1",
        "created_at": created,
        **extra,
    }


def listener() -> alpaca_news.NewsListener:
    return alpaca_news.NewsListener(credentials=CREDS, cache=NewsCache(), policy=POLICY)


# --- parsing -----------------------------------------------------------------


def test_parse_item_normalises_symbols_and_times():
    item = alpaca_news.parse_item(raw(), received_at=NOW)
    assert item.symbols == ("ABCD",)
    assert item.created_at.tzinfo is UTC
    assert item.received_at == NOW


def test_parse_item_keeps_updated_at_when_present():
    item = alpaca_news.parse_item(raw(updated_at="2026-03-10T13:00:00Z"), received_at=NOW)
    assert item.updated_at is not None


def test_parse_item_rejects_a_record_without_a_headline():
    with pytest.raises(ValueError, match="headline"):
        alpaca_news.parse_item({"id": "1", "created_at": "2026-03-10T12:00:00Z"}, received_at=NOW)


def test_received_at_is_supplied_not_read_from_a_clock():
    """A backfilled article is stamped when it reached us, not when parsed."""
    earlier = NOW - timedelta(minutes=30)
    assert alpaca_news.parse_item(raw(), received_at=earlier).received_at == earlier


# --- websocket frames --------------------------------------------------------


def test_a_news_frame_is_cached():
    lst = listener()
    items = lst.handle_message(json.dumps([raw()]), received_at=NOW)
    assert len(items) == 1
    assert lst.cache.size == 1
    assert lst.health.messages == 1


def test_a_duplicate_frame_does_not_double_count_the_article():
    lst = listener()
    lst.handle_message(json.dumps([raw()]), received_at=NOW)
    lst.handle_message(json.dumps([raw()]), received_at=NOW)
    assert lst.cache.size == 1


def test_control_frames_are_ignored():
    lst = listener()
    frame = json.dumps([{"T": "subscription", "news": ["*"]}])
    assert lst.handle_message(frame, received_at=NOW) == []


def test_an_error_frame_is_logged_not_raised(caplog):
    lst = listener()
    with caplog.at_level("ERROR"):
        lst.handle_message(json.dumps([{"T": "error", "msg": "auth failed"}]), received_at=NOW)
    assert "auth failed" in caplog.text


def test_invalid_json_does_not_kill_the_listener(caplog):
    """A malformed frame must not take down a listener meant to run 16 hours."""
    lst = listener()
    with caplog.at_level("ERROR"):
        assert lst.handle_message("{not json", received_at=NOW) == []
    assert "not valid JSON" in caplog.text


def test_a_malformed_news_frame_is_skipped(caplog):
    lst = listener()
    with caplog.at_level("ERROR"):
        assert lst.handle_message(json.dumps([{"T": "n", "id": "1"}]), received_at=NOW) == []
    assert lst.cache.size == 0


def test_on_item_callback_fires_once_per_new_article():
    seen = []
    lst = alpaca_news.NewsListener(
        credentials=CREDS, cache=NewsCache(), policy=POLICY, on_item=seen.append
    )
    lst.handle_message(json.dumps([raw()]), received_at=NOW)
    lst.handle_message(json.dumps([raw()]), received_at=NOW)
    assert len(seen) == 1


def test_subscribe_payloads_auth_then_subscribe():
    payloads = listener().subscribe_payloads()
    assert payloads[0]["action"] == "auth"
    assert payloads[1] == {"action": "subscribe", "news": ["*"]}


# --- backfill ----------------------------------------------------------------


def test_backfill_parses_and_reports_bad_records():
    pages = [{"news": [raw("1"), {"id": "2"}]}]
    result = alpaca_news.parse_backfill(pages, received_at=NOW)
    assert len(result.items) == 1
    assert result.unparsable == 1


def test_backfill_over_http():
    payload = {"news": [raw("1"), raw("2")]}
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        result = alpaca_news.backfill(
            CREDS,
            since=NOW - timedelta(minutes=60),
            until=NOW,
            policy=POLICY,
            client=client,
        )
    assert len(result.items) == 2
    assert all(item.received_at == NOW for item in result.items)


def test_backfill_window_is_the_configured_lookback():
    start, end = listener().backfill_window(now=NOW)
    assert (end - start) == timedelta(minutes=60)
    assert end == NOW


# --- reconnect ---------------------------------------------------------------


class FakeSocket:
    """A socket that yields some frames and then drops."""

    def __init__(self, frames: list[str], *, drop: bool = True) -> None:
        self.frames = frames
        self.drop = drop
        self.closed = False

    def __aiter__(self) -> AsyncIterator[str]:
        async def gen() -> AsyncIterator[str]:
            for frame in self.frames:
                yield frame
            if self.drop:
                raise ConnectionError("socket dropped")

        return gen()

    async def close(self) -> None:
        self.closed = True


def test_reconnect_backfills_the_gap_and_records_the_outage():
    """The gap between a dropped socket and a restored one is exactly when a
    catalyst arrives and is missed."""
    lst = listener()
    clock = {"t": NOW}
    backfills: list[tuple[datetime, datetime]] = []

    def fake_backfill(start: datetime, end: datetime) -> alpaca_news.BackfillResult:
        backfills.append((start, end))
        return alpaca_news.BackfillResult(items=())

    sockets = [FakeSocket([json.dumps([raw("1")])]), FakeSocket([json.dumps([raw("2")])])]

    async def connect() -> FakeSocket:
        clock["t"] += timedelta(minutes=2)  # time passes while reconnecting
        return sockets.pop(0)

    asyncio.run(
        lst.run(
            connect,
            clock=lambda: clock["t"],
            backfill_fn=fake_backfill,
            max_cycles=2,
            sleep=lambda _: asyncio.sleep(0),
        )
    )

    assert len(backfills) == 2
    assert lst.cache.size == 2
    assert lst.health.reconnects == 1
    assert lst.health.disconnect_minutes > 0


def test_a_failed_connection_is_retried_and_counted():
    lst = listener()
    clock = {"t": NOW}
    attempts = {"n": 0}

    async def connect() -> FakeSocket:
        attempts["n"] += 1
        clock["t"] += timedelta(minutes=1)
        if attempts["n"] == 1:
            raise OSError("connection refused")
        return FakeSocket([], drop=True)

    asyncio.run(
        lst.run(
            connect,
            clock=lambda: clock["t"],
            backfill_fn=lambda s, e: alpaca_news.BackfillResult(items=()),
            max_cycles=2,
            sleep=lambda _: asyncio.sleep(0),
        )
    )
    assert attempts["n"] == 2
    assert lst.health.outage_minutes(now=clock["t"]) > 0


def test_outage_minutes_include_an_open_gap():
    health = alpaca_news.FeedHealth()
    health.mark_disconnected(now=NOW)
    assert health.outage_minutes(now=NOW + timedelta(minutes=7)) == pytest.approx(7.0)


def test_mock_items_are_staggered():
    items = alpaca_news.mock_items(now=NOW, tickers=["MK001", "MK002"])
    assert items[0].created_at > items[1].created_at
    assert items[0].symbols == ("MK001",)
