"""Shared Alpaca HTTP plumbing: auth headers, base URLs, bounded requests.

One place for the two things every Alpaca call needs — credentials that are
never logged, and a request that cannot hang — so the bars, news and corporate
actions modules each stay about their own data.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.sources.retry import RETRYABLE_STATUS, RetryPolicy, SourceError, call_with_retry

logger = logging.getLogger(__name__)

DATA_BASE_URL = "https://data.alpaca.markets"
TRADING_BASE_URL = "https://api.alpaca.markets"

#: Alpaca's free tier only serves market data older than this. Requesting
#: anything fresher returns a subscription error, so every caller here must
#: stay behind it (CLAUDE.md 3).
FREE_TIER_DELAY_MINUTES = 15


@dataclass(frozen=True, slots=True)
class AlpacaCredentials:
    """API key pair. Never logged, never included in an error message."""

    key_id: str
    secret_key: str

    def headers(self) -> dict[str, str]:
        """Auth headers for a request."""
        return {
            "APCA-API-KEY-ID": self.key_id,
            "APCA-API-SECRET-KEY": self.secret_key,
            "accept": "application/json",
        }

    def __repr__(self) -> str:
        """Redacted, so a traceback cannot leak the key."""
        return f"AlpacaCredentials(key_id=***{self.key_id[-4:] if self.key_id else ''})"

    __str__ = __repr__


def is_retryable(exc: Exception) -> bool:
    """Whether an Alpaca failure is worth another attempt."""
    if isinstance(exc, httpx.TimeoutException | httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRYABLE_STATUS
    return False


def get_json(
    client: httpx.Client,
    url: str,
    *,
    params: dict[str, Any],
    policy: RetryPolicy,
    description: str,
) -> dict[str, Any]:
    """GET one JSON document with timeout, retry and a logged failure mode.

    Raises :class:`SourceError` on failure rather than returning an empty
    payload: an empty result and a failed request must not look the same, or a
    feed outage gets written to the lake as a quiet market.
    """

    def attempt() -> dict[str, Any]:
        response = client.get(url, params=params, timeout=policy.timeout_seconds)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            msg = f"{description}: expected a JSON object, got {type(payload).__name__}"
            raise SourceError(msg, transient=False)
        return payload

    return call_with_retry(
        attempt, policy=policy, description=description, is_retryable=is_retryable
    )


def paginate(
    client: httpx.Client,
    url: str,
    *,
    params: dict[str, Any],
    policy: RetryPolicy,
    description: str,
    max_pages: int = 100,
) -> list[dict[str, Any]]:
    """Follow Alpaca's ``next_page_token`` cursor, with a hard page cap.

    The cap is a guard against an endpoint that keeps handing back a token:
    an unbounded loop against a free API is how an account gets rate-limited
    into uselessness.
    """
    pages: list[dict[str, Any]] = []
    cursor: str | None = None
    for page in range(max_pages):
        query = dict(params)
        if cursor:
            query["page_token"] = cursor
        payload = get_json(
            client, url, params=query, policy=policy, description=f"{description} page {page + 1}"
        )
        pages.append(payload)
        cursor = payload.get("next_page_token")
        if not cursor:
            return pages
    logger.warning("%s stopped at the %s-page cap with a cursor still open", description, max_pages)
    return pages
