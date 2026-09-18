"""The Finnhub fallback: narrow conditions, hard budget, labelled source."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import httpx
import pytest
from app.core.timeutils import UTC
from app.sources import finnhub
from app.sources.retry import RetryPolicy, SourceError

DAY = date(2026, 3, 10)
NOW = datetime(2026, 3, 10, 12, 5, tzinfo=UTC)
POLICY = RetryPolicy(
    max_retries=1,
    base_seconds=0.001,
    max_seconds=0.001,
    timeout_seconds=5.0,
    connect_timeout_seconds=2.0,
)


def record(news_id: int = 1) -> dict:
    return {
        "id": news_id,
        "headline": "Company announces offering",
        "datetime": int((NOW - timedelta(minutes=5)).timestamp()),
        "source": "Reuters",
        "url": "https://example.invalid/1",
    }


def client_with(*responses: httpx.Response) -> httpx.Client:
    queue = list(responses)
    return httpx.Client(transport=httpx.MockTransport(lambda request: queue.pop(0)))


# --- when the fallback applies ----------------------------------------------


@pytest.mark.parametrize(
    ("healthy", "pillars", "key", "expected"),
    [
        (False, True, True, True),
        (True, True, True, False),  # Alpaca is fine; do not double up
        (False, False, True, False),  # ticker has not earned a call
        (False, True, False, False),  # no key configured
    ],
)
def test_fallback_conditions(healthy, pillars, key, expected):
    assert (
        finnhub.should_use_fallback(
            alpaca_healthy=healthy, passed_pillars_1_4_5=pillars, has_api_key=key
        )
        is expected
    )


# --- rate limiting -----------------------------------------------------------


def test_limiter_allows_up_to_the_budget():
    limiter = finnhub.RateLimiter(max_calls=3)
    assert all(limiter.allow(now=NOW) for _ in range(3))
    assert not limiter.allow(now=NOW)
    assert limiter.used == 3


def test_limiter_window_slides():
    limiter = finnhub.RateLimiter(max_calls=1)
    assert limiter.allow(now=NOW)
    assert not limiter.allow(now=NOW + timedelta(seconds=30))
    assert limiter.allow(now=NOW + timedelta(seconds=61))


def test_budget_refusal_returns_empty_not_an_exception(caplog):
    """The caller is mid-window; blocking other tickers is the worse failure."""
    limiter = finnhub.RateLimiter(max_calls=0)
    with caplog.at_level("WARNING"):
        items = finnhub.fetch_company_news(
            "key", "ABCD", day=DAY, now=NOW, policy=POLICY, limiter=limiter
        )
    assert items == ()
    assert "budget" in caplog.text


# --- fetching ----------------------------------------------------------------


def test_fetch_parses_and_labels_the_source():
    with client_with(httpx.Response(200, json=[record()])) as client:
        items = finnhub.fetch_company_news(
            "key",
            "abcd",
            day=DAY,
            now=NOW,
            policy=POLICY,
            limiter=finnhub.RateLimiter(max_calls=60),
            client=client,
        )
    assert len(items) == 1
    assert items[0].symbols == ("ABCD",)
    assert items[0].source.startswith("finnhub:")
    assert items[0].news_id.startswith("finnhub:")


def test_a_malformed_record_is_skipped_not_fatal(caplog):
    with (
        client_with(httpx.Response(200, json=[record(), {"id": 2}])) as client,
        caplog.at_level("ERROR"),
    ):
        items = finnhub.fetch_company_news(
            "key",
            "ABCD",
            day=DAY,
            now=NOW,
            policy=POLICY,
            limiter=finnhub.RateLimiter(max_calls=60),
            client=client,
        )
    assert len(items) == 1


def test_a_missing_key_is_a_permanent_error():
    with pytest.raises(SourceError) as excinfo:
        finnhub.fetch_company_news(
            "", "ABCD", day=DAY, now=NOW, policy=POLICY, limiter=finnhub.RateLimiter(max_calls=60)
        )
    assert excinfo.value.transient is False


def test_a_non_list_payload_is_rejected():
    with (
        client_with(httpx.Response(200, json={"error": "bad"})) as client,
        pytest.raises(SourceError),
    ):
        finnhub.fetch_company_news(
            "key",
            "ABCD",
            day=DAY,
            now=NOW,
            policy=POLICY,
            limiter=finnhub.RateLimiter(max_calls=60),
            client=client,
        )


def test_rate_limit_response_is_retried_then_raises():
    responses = [httpx.Response(429) for _ in range(POLICY.max_retries + 1)]
    with client_with(*responses) as client, pytest.raises(SourceError) as excinfo:
        finnhub.fetch_company_news(
            "key",
            "ABCD",
            day=DAY,
            now=NOW,
            policy=POLICY,
            limiter=finnhub.RateLimiter(max_calls=60),
            client=client,
        )
    assert excinfo.value.transient is True
