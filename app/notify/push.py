"""Web Push delivery over VAPID.

Three things decide the shape of this module:

* **Subscriptions rot.** Browsers discard them when a profile is cleared, a
  device is reset, or the user simply revokes permission. The push service
  answers 404 or 410 for those, and the only correct response is to delete the
  row — a scanner that keeps retrying dead endpoints spends its alert window
  talking to nobody (CLAUDE.md 8).
* **A failed push must not break the poll.** Notification is the last step of a
  window poll; an exception there would lose the evaluations that had not been
  flushed yet. Every send is therefore reported, never raised.
* **Nothing here logs a key or an endpoint.** A push endpoint URL is a
  capability: anyone holding it can send that browser notifications. Logs carry
  a short fingerprint instead.

The actual sending is injected, so the whole retry-and-prune path is tested
without a network or a browser.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.core.timeutils import to_utc
from app.storage.db import utc_text

logger = logging.getLogger(__name__)

#: Status codes meaning "this subscription is gone" rather than "try later".
GONE_STATUS: frozenset[int] = frozenset({404, 410})

#: Consecutive failures after which a subscription is dropped even though the
#: service never said it was gone. Without this, an endpoint that answers 500
#: forever stays in the table and is retried every window until the trader
#: notices, which they never will.
MAX_CONSECUTIVE_FAILURES = 10


@dataclass(frozen=True, slots=True)
class PushSubscription:
    """A browser's push endpoint and its encryption keys."""

    endpoint: str
    p256dh: str
    auth: str
    user_agent: str | None = None

    @property
    def fingerprint(self) -> str:
        """A short, stable, non-reversible id for logs.

        The endpoint itself is a capability: printing it in a log file hands
        anyone who reads that file the ability to notify the trader's browser.
        """
        return hashlib.sha256(self.endpoint.encode()).hexdigest()[:12]

    def to_payload(self) -> dict[str, Any]:
        """The structure ``pywebpush`` expects."""
        return {
            "endpoint": self.endpoint,
            "keys": {"p256dh": self.p256dh, "auth": self.auth},
        }

    def __repr__(self) -> str:
        """Redacted: the endpoint never appears in a traceback."""
        return f"PushSubscription({self.fingerprint}, ua={self.user_agent!r})"

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class VapidKeys:
    """VAPID key pair and subject, read from the environment."""

    public_key: str
    private_key: str
    subject: str

    def claims(self) -> dict[str, str]:
        """JWT claims identifying this sender to the push service."""
        return {"sub": self.subject}

    def __repr__(self) -> str:
        """Redacted; the private key must never reach a log."""
        return f"VapidKeys(public=***{self.public_key[-6:] if self.public_key else ''})"

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class PushResult:
    """Outcome of one send attempt."""

    subscription: PushSubscription
    delivered: bool
    status_code: int | None = None
    gone: bool = False
    error: str | None = None


# --- storage -----------------------------------------------------------------


def save_subscription(
    connection: sqlite3.Connection, subscription: PushSubscription, *, now: datetime
) -> None:
    """Insert or refresh a subscription.

    Re-subscribing resets the failure count: the browser has just told us the
    endpoint is live, which outranks whatever the push service said last week.
    """
    connection.execute(
        """
        INSERT INTO push_subscriptions
            (endpoint, p256dh, auth, user_agent, created_at_utc, failure_count)
        VALUES (?, ?, ?, ?, ?, 0)
        ON CONFLICT (endpoint) DO UPDATE SET
            p256dh = excluded.p256dh,
            auth = excluded.auth,
            user_agent = excluded.user_agent,
            failure_count = 0
        """,
        (
            subscription.endpoint,
            subscription.p256dh,
            subscription.auth,
            subscription.user_agent,
            utc_text(now),
        ),
    )
    logger.info("Stored push subscription %s", subscription.fingerprint)


def load_subscriptions(connection: sqlite3.Connection) -> list[PushSubscription]:
    """Every stored subscription."""
    rows = connection.execute(
        "SELECT endpoint, p256dh, auth, user_agent FROM push_subscriptions"
    ).fetchall()
    return [
        PushSubscription(
            endpoint=row["endpoint"],
            p256dh=row["p256dh"],
            auth=row["auth"],
            user_agent=row["user_agent"],
        )
        for row in rows
    ]


def delete_subscription(connection: sqlite3.Connection, endpoint: str) -> None:
    """Remove a subscription the push service has declared gone."""
    connection.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))


def record_success(connection: sqlite3.Connection, endpoint: str, *, now: datetime) -> None:
    """Mark a delivery, clearing the failure streak."""
    connection.execute(
        "UPDATE push_subscriptions SET last_success_utc = ?, failure_count = 0 WHERE endpoint = ?",
        (utc_text(now), endpoint),
    )


def record_failure(connection: sqlite3.Connection, endpoint: str) -> int:
    """Increment and return a subscription's consecutive failure count."""
    connection.execute(
        "UPDATE push_subscriptions SET failure_count = failure_count + 1 WHERE endpoint = ?",
        (endpoint,),
    )
    row = connection.execute(
        "SELECT failure_count FROM push_subscriptions WHERE endpoint = ?", (endpoint,)
    ).fetchone()
    return int(row["failure_count"]) if row else 0


# --- sending -----------------------------------------------------------------

#: Signature of the transport: takes the subscription payload, the message and
#: the VAPID keys, returns an HTTP status code, and raises on transport errors.
Sender = Callable[[dict[str, Any], str, VapidKeys], int]


def pywebpush_sender(timeout_seconds: float = 10.0) -> Sender:
    """Build the real sender, bound to ``pywebpush``."""

    def send(payload: dict[str, Any], message: str, keys: VapidKeys) -> int:
        from pywebpush import webpush

        response = webpush(
            subscription_info=payload,
            data=message,
            vapid_private_key=keys.private_key,
            vapid_claims=dict(keys.claims()),
            timeout=timeout_seconds,
        )
        return int(getattr(response, "status_code", 201))

    return send


def send_one(
    subscription: PushSubscription,
    message: str,
    keys: VapidKeys,
    *,
    sender: Sender,
) -> PushResult:
    """Send one notification, converting every failure into a result.

    Never raises. A push is the last step of a window poll, and an exception
    here would take down the poll that produced the evaluations.
    """
    try:
        status = sender(subscription.to_payload(), message, keys)
    except Exception as exc:
        failed: int | None = getattr(getattr(exc, "response", None), "status_code", None)
        gone = failed in GONE_STATUS
        logger.warning("Push to %s failed (status %s): %s", subscription.fingerprint, failed, exc)
        return PushResult(
            subscription, delivered=False, status_code=failed, gone=gone, error=str(exc)
        )

    if status in GONE_STATUS:
        logger.info("Push endpoint %s is gone (status %s)", subscription.fingerprint, status)
        return PushResult(subscription, delivered=False, status_code=status, gone=True)
    if status >= 400:
        return PushResult(
            subscription, delivered=False, status_code=status, error=f"status {status}"
        )
    return PushResult(subscription, delivered=True, status_code=status)


def broadcast(
    connection: sqlite3.Connection,
    message: str,
    keys: VapidKeys,
    *,
    sender: Sender,
    now: datetime,
) -> list[PushResult]:
    """Send to every subscription, pruning the ones that are gone.

    Subscriptions declared gone are deleted immediately; ones failing for other
    reasons are dropped only after a long streak, since a push service having a
    bad hour is not a reason to make the trader re-subscribe.
    """
    results: list[PushResult] = []
    for subscription in load_subscriptions(connection):
        result = send_one(subscription, message, keys, sender=sender)
        results.append(result)
        if result.delivered:
            record_success(connection, subscription.endpoint, now=to_utc(now))
            continue
        if result.gone:
            delete_subscription(connection, subscription.endpoint)
            continue
        failures = record_failure(connection, subscription.endpoint)
        if failures >= MAX_CONSECUTIVE_FAILURES:
            logger.warning(
                "Dropping push subscription %s after %s consecutive failures",
                subscription.fingerprint,
                failures,
            )
            delete_subscription(connection, subscription.endpoint)

    delivered = sum(1 for result in results if result.delivered)
    if results:
        logger.info("Push: %s of %s subscriptions delivered", delivered, len(results))
    else:
        logger.info("Push skipped: no subscriptions stored")
    return results


def notification_body(title: str, body: str, *, url: str, tag: str) -> str:
    """The JSON the service worker renders.

    ``tag`` collapses repeated notifications for the same ticker into one entry
    on the lock screen instead of a stack of five.
    """
    return json.dumps({"title": title, "body": body, "url": url, "tag": tag})


def test_notification(*, now: datetime) -> str:
    """Payload for the settings page's "send test notification" button."""
    return notification_body(
        title="Momentum Gap Scanner",
        body=f"Test notification sent at {to_utc(now):%H:%M:%S} UTC. Push is working.",
        url="/",
        tag="test",
    )
