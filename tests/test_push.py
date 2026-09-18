"""Web Push: storage, delivery, and pruning subscriptions that are gone."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from app.core.timeutils import UTC
from app.notify import push
from app.storage import db

NOW = datetime(2026, 3, 10, 12, 5, tzinfo=UTC)
KEYS = push.VapidKeys(public_key="BPublicKey123456", private_key="privkey", subject="mailto:a@b.c")


def subscription(endpoint: str = "https://push.example.invalid/abc") -> push.PushSubscription:
    return push.PushSubscription(
        endpoint=endpoint, p256dh="p256dh-value", auth="auth-value", user_agent="Firefox"
    )


@pytest.fixture
def connection(tmp_path: Path):
    with db.session(tmp_path / "app.db") as conn:
        yield conn


def sender_returning(*statuses: int) -> push.Sender:
    queue = list(statuses)

    def send(payload: dict, message: str, keys: push.VapidKeys) -> int:
        return queue.pop(0) if queue else 201

    return send


def sender_raising(exc: Exception) -> push.Sender:
    def send(payload: dict, message: str, keys: push.VapidKeys) -> int:
        raise exc

    return send


# --- redaction ---------------------------------------------------------------


def test_subscription_repr_hides_the_endpoint():
    """A push endpoint is a capability: anyone holding it can notify the user."""
    rendered = f"{subscription()!r} {subscription()}"
    assert "push.example.invalid" not in rendered
    assert subscription().fingerprint in rendered


def test_fingerprint_is_stable_and_short():
    assert subscription().fingerprint == subscription().fingerprint
    assert len(subscription().fingerprint) == 12


def test_vapid_repr_hides_the_private_key():
    assert "privkey" not in f"{KEYS!r} {KEYS}"


# --- storage -----------------------------------------------------------------


def test_save_and_load(connection):
    push.save_subscription(connection, subscription(), now=NOW)
    stored = push.load_subscriptions(connection)
    assert len(stored) == 1
    assert stored[0].p256dh == "p256dh-value"


def test_resubscribing_updates_keys_and_clears_failures(connection):
    push.save_subscription(connection, subscription(), now=NOW)
    push.record_failure(connection, subscription().endpoint)
    refreshed = push.PushSubscription(
        endpoint=subscription().endpoint, p256dh="new-p256dh", auth="new-auth"
    )
    push.save_subscription(connection, refreshed, now=NOW)

    row = connection.execute("SELECT * FROM push_subscriptions").fetchone()
    assert row["p256dh"] == "new-p256dh"
    assert row["failure_count"] == 0


def test_delete_removes_the_row(connection):
    push.save_subscription(connection, subscription(), now=NOW)
    push.delete_subscription(connection, subscription().endpoint)
    assert push.load_subscriptions(connection) == []


# --- sending -----------------------------------------------------------------


def test_a_successful_send_is_recorded(connection):
    push.save_subscription(connection, subscription(), now=NOW)
    results = push.broadcast(connection, "hello", KEYS, sender=sender_returning(201), now=NOW)
    assert results[0].delivered
    row = connection.execute("SELECT * FROM push_subscriptions").fetchone()
    assert row["last_success_utc"] is not None
    assert row["failure_count"] == 0


@pytest.mark.parametrize("status", [404, 410])
def test_a_gone_subscription_is_deleted_immediately(connection, status):
    push.save_subscription(connection, subscription(), now=NOW)
    push.broadcast(connection, "hello", KEYS, sender=sender_returning(status), now=NOW)
    assert push.load_subscriptions(connection) == []


def test_a_transient_failure_keeps_the_subscription(connection):
    """A push service having a bad hour is not a reason to re-subscribe."""
    push.save_subscription(connection, subscription(), now=NOW)
    push.broadcast(connection, "hello", KEYS, sender=sender_returning(503), now=NOW)
    assert len(push.load_subscriptions(connection)) == 1
    row = connection.execute("SELECT failure_count FROM push_subscriptions").fetchone()
    assert row["failure_count"] == 1


def test_a_long_failure_streak_eventually_drops_it(connection, caplog):
    push.save_subscription(connection, subscription(), now=NOW)
    with caplog.at_level("WARNING"):
        for _ in range(push.MAX_CONSECUTIVE_FAILURES):
            push.broadcast(connection, "hello", KEYS, sender=sender_returning(500), now=NOW)
    assert push.load_subscriptions(connection) == []
    assert "consecutive failures" in caplog.text


def test_an_exception_never_escapes_the_send(connection):
    """A failed push must not take down the poll that produced the alert."""
    push.save_subscription(connection, subscription(), now=NOW)
    results = push.broadcast(
        connection, "hello", KEYS, sender=sender_raising(RuntimeError("socket died")), now=NOW
    )
    assert not results[0].delivered
    assert "socket died" in (results[0].error or "")


def test_an_exception_carrying_a_gone_status_prunes(connection):
    class Response:
        status_code = 410

    class GoneError(Exception):
        response = Response()

    push.save_subscription(connection, subscription(), now=NOW)
    push.broadcast(connection, "hello", KEYS, sender=sender_raising(GoneError("gone")), now=NOW)
    assert push.load_subscriptions(connection) == []


def test_broadcast_with_no_subscriptions_is_a_no_op(connection, caplog):
    with caplog.at_level("INFO"):
        assert push.broadcast(connection, "hello", KEYS, sender=sender_returning(), now=NOW) == []
    assert "no subscriptions stored" in caplog.text


def test_each_subscription_is_judged_independently(connection):
    push.save_subscription(connection, subscription("https://push.example.invalid/a"), now=NOW)
    push.save_subscription(connection, subscription("https://push.example.invalid/b"), now=NOW)
    results = push.broadcast(connection, "hello", KEYS, sender=sender_returning(201, 410), now=NOW)
    assert sum(1 for r in results if r.delivered) == 1
    assert len(push.load_subscriptions(connection)) == 1


# --- payloads ----------------------------------------------------------------


def test_notification_body_is_json_the_worker_understands():
    payload = json.loads(
        push.notification_body("[A] ABCD +34%", "RVOL 12x", url="/ticker/ABCD", tag="ABCD")
    )
    assert payload["title"] == "[A] ABCD +34%"
    assert payload["url"] == "/ticker/ABCD"
    assert payload["tag"] == "ABCD"


def test_the_tag_collapses_repeats_for_one_ticker():
    first = json.loads(push.notification_body("a", "b", url="/t/ABCD", tag="ABCD"))
    second = json.loads(push.notification_body("c", "d", url="/t/ABCD", tag="ABCD"))
    assert first["tag"] == second["tag"]


def test_test_notification_names_the_time():
    payload = json.loads(push.test_notification(now=NOW))
    assert "12:05:00" in payload["body"]
    assert payload["tag"] == "test"
