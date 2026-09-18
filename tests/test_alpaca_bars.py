"""Alpaca bar fetching: batching, the free-tier clamp, and field validation."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import httpx
import pytest
from app.core.timeutils import ET, UTC
from app.sources import alpaca_bars
from app.sources.alpaca import AlpacaCredentials
from app.sources.retry import RetryPolicy, SourceError

DAY = date(2026, 3, 10)
NOW = datetime(2026, 3, 10, 21, 0, tzinfo=UTC)
CREDS = AlpacaCredentials(key_id="AKTEST", secret_key="secret")
POLICY = RetryPolicy(
    max_retries=1,
    base_seconds=0.001,
    max_seconds=0.001,
    timeout_seconds=5.0,
    connect_timeout_seconds=2.0,
)


def raw_bar(minute: str = "2026-03-10T12:05:00Z", close: float = 5.2) -> dict:
    return {"t": minute, "o": 5.0, "h": 5.3, "l": 4.9, "c": close, "v": 12_000, "vw": 5.1, "n": 42}


def client_with(*responses: httpx.Response) -> httpx.Client:
    queue = list(responses)
    return httpx.Client(transport=httpx.MockTransport(lambda request: queue.pop(0)))


# --- the free-tier clamp -----------------------------------------------------


def test_a_recent_end_is_clamped_behind_the_delay(caplog):
    with caplog.at_level("INFO"):
        clamped = alpaca_bars.clamp_end(NOW, now=NOW)
    assert clamped == NOW - timedelta(minutes=15)
    assert "Clamping" in caplog.text


def test_an_old_end_is_left_alone():
    old = NOW - timedelta(hours=2)
    assert alpaca_bars.clamp_end(old, now=NOW) == old


def test_a_window_that_collapses_after_clamping_fetches_nothing(caplog):
    start = NOW - timedelta(minutes=5)
    with caplog.at_level("WARNING"):
        result = alpaca_bars.fetch_bars(
            CREDS, ("ABCD",), start=start, end=NOW, now=NOW, policy=POLICY, client=None
        )
    assert result.bars == {}
    assert "collapsed after clamping" in caplog.text


# --- batching ----------------------------------------------------------------


def test_symbols_are_batched():
    symbols = tuple(f"T{i:03d}" for i in range(250))
    grouped = alpaca_bars.batches(symbols, size=100)
    assert [len(batch) for batch in grouped] == [100, 100, 50]


def test_batching_rejects_a_zero_size():
    with pytest.raises(ValueError, match="batch size"):
        alpaca_bars.batches(("A",), size=0)


def test_an_empty_request_does_not_call_out():
    assert (
        alpaca_bars.fetch_bars(
            CREDS, (), start=NOW - timedelta(hours=5), end=NOW, now=NOW, policy=POLICY
        ).bars
        == {}
    )


# --- parsing -----------------------------------------------------------------


def test_bar_fields_are_mapped_from_alpacas_single_letters():
    bar = alpaca_bars.parse_bar(raw_bar())
    assert (bar.open, bar.high, bar.low, bar.close) == (5.0, 5.3, 4.9, 5.2)
    assert bar.volume == 12_000
    assert bar.vwap == 5.1
    assert bar.trade_count == 42
    assert bar.minute.tzinfo is UTC


def test_a_bar_missing_a_price_raises():
    """A renamed field must raise here, not flatten a move downstream."""
    broken = raw_bar()
    del broken["h"]
    with pytest.raises(ValueError, match="missing"):
        alpaca_bars.parse_bar(broken)


def test_optional_fields_may_be_absent():
    minimal = {"t": "2026-03-10T12:05:00Z", "o": 1, "h": 2, "l": 1, "c": 2, "v": 10}
    bar = alpaca_bars.parse_bar(minimal)
    assert bar.vwap is None
    assert bar.trade_count is None


def test_one_bad_record_does_not_lose_the_others(caplog):
    pages = [{"bars": {"ABCD": [raw_bar(), {"t": "2026-03-10T12:06:00Z"}]}}]
    with caplog.at_level("ERROR"):
        result = alpaca_bars.parse_pages(pages, ("ABCD",))
    assert len(result.bars["ABCD"]) == 1
    assert result.unparsable == 1


def test_bars_come_back_sorted():
    pages = [
        {
            "bars": {
                "ABCD": [
                    raw_bar("2026-03-10T12:07:00Z"),
                    raw_bar("2026-03-10T12:05:00Z"),
                ]
            }
        }
    ]
    minutes = [bar.minute for bar in alpaca_bars.parse_pages(pages, ("ABCD",)).bars["ABCD"]]
    assert minutes == sorted(minutes)


def test_missing_tickers_are_reported():
    result = alpaca_bars.parse_pages([{"bars": {"ABCD": [raw_bar()]}}], ("ABCD", "EFGH"))
    assert result.missing == ("EFGH",)


# --- fetching ----------------------------------------------------------------


def test_fetch_merges_pages():
    first = {"bars": {"ABCD": [raw_bar("2026-03-10T12:05:00Z")]}, "next_page_token": "x"}
    second = {"bars": {"ABCD": [raw_bar("2026-03-10T12:06:00Z")]}}
    with client_with(httpx.Response(200, json=first), httpx.Response(200, json=second)) as client:
        result = alpaca_bars.fetch_bars(
            CREDS,
            ("ABCD",),
            start=NOW - timedelta(hours=10),
            end=NOW - timedelta(hours=1),
            now=NOW,
            policy=POLICY,
            client=client,
        )
    assert len(result.bars["ABCD"]) == 2


def test_a_server_error_raises_a_transient_source_error():
    responses = [httpx.Response(503) for _ in range(POLICY.max_retries + 1)]
    with client_with(*responses) as client, pytest.raises(SourceError) as excinfo:
        alpaca_bars.fetch_bars(
            CREDS,
            ("ABCD",),
            start=NOW - timedelta(hours=10),
            end=NOW - timedelta(hours=1),
            now=NOW,
            policy=POLICY,
            client=client,
        )
    assert excinfo.value.transient is True


# --- lake rows and mock bars -------------------------------------------------


def test_bar_rows_carry_the_resolution():
    bars = alpaca_bars.mock_bars("MK001", DAY, start_et=time(4, 0), minutes=10)
    rows = alpaca_bars.bar_rows("MK001", DAY, bars, now=NOW)
    assert len(rows) == 10
    assert all(row["resolution_minutes"] == 1 for row in rows)
    assert rows[0]["minute_utc"].tzinfo is UTC


def test_mock_bars_rise_then_fade():
    """Shaped, not random, so the acceptance tests can assert on the path."""
    bars = alpaca_bars.mock_bars("MK001", DAY, start_et=time(4, 0), minutes=90)
    closes = [bar.close for bar in bars]
    peak = max(closes)
    assert closes[0] < peak
    assert closes[-1] < peak
    assert peak / closes[0] == pytest.approx(1.8, rel=0.05)


def test_mock_bars_start_at_the_requested_et_time():
    bars = alpaca_bars.mock_bars("MK001", DAY, start_et=time(7, 20), minutes=5)
    assert bars[0].minute.astimezone(ET).strftime("%H:%M") == "07:20"
