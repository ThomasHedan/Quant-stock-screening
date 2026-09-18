"""Retry policy: what is retried, what is not, and the exact backoff curve."""

from __future__ import annotations

import pytest
from app.sources.retry import RetryPolicy, SourceError, call_with_retry

POLICY = RetryPolicy(
    max_retries=3,
    base_seconds=1.0,
    max_seconds=8.0,
    timeout_seconds=10.0,
    connect_timeout_seconds=5.0,
)


class TransientError(Exception):
    pass


class PermanentError(Exception):
    pass


def is_transient(exc: Exception) -> bool:
    return isinstance(exc, TransientError)


def test_backoff_doubles_and_caps():
    delays = [POLICY.delay_for(attempt, jitter=1.0) for attempt in range(1, 6)]
    assert delays == [1.0, 2.0, 4.0, 8.0, 8.0]


def test_jitter_scales_the_delay():
    assert POLICY.delay_for(2, jitter=0.5) == 1.0


def test_delay_for_rejects_attempt_zero():
    with pytest.raises(ValueError, match="attempt"):
        POLICY.delay_for(0)


def test_success_on_first_attempt_never_sleeps():
    slept: list[float] = []
    result = call_with_retry(
        lambda: "ok",
        policy=POLICY,
        description="test",
        is_retryable=is_transient,
        sleep=slept.append,
    )
    assert result == "ok"
    assert slept == []


def test_transient_failure_is_retried_then_succeeds():
    slept: list[float] = []
    attempts = {"n": 0}

    def flaky() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise TransientError("502")
        return "ok"

    result = call_with_retry(
        flaky,
        policy=POLICY,
        description="test",
        is_retryable=is_transient,
        sleep=slept.append,
        jitter=lambda: 1.0,
    )
    assert result == "ok"
    assert slept == [1.0, 2.0]


def test_permanent_failure_is_not_retried():
    """A 401 is a bad key; four more attempts neither fix it nor are polite."""
    slept: list[float] = []
    with pytest.raises(SourceError) as excinfo:
        call_with_retry(
            lambda: (_ for _ in ()).throw(PermanentError("401")),
            policy=POLICY,
            description="test",
            is_retryable=is_transient,
            sleep=slept.append,
        )
    assert slept == []
    assert excinfo.value.transient is False


def test_exhausted_retries_raise_transient():
    slept: list[float] = []

    def always_fails() -> str:
        raise TransientError("503")

    with pytest.raises(SourceError) as excinfo:
        call_with_retry(
            always_fails,
            policy=POLICY,
            description="test",
            is_retryable=is_transient,
            sleep=slept.append,
            jitter=lambda: 1.0,
        )
    assert len(slept) == POLICY.max_retries
    assert excinfo.value.transient is True
    assert "4 attempts" in str(excinfo.value)


def test_zero_retries_means_one_attempt():
    policy = RetryPolicy(
        max_retries=0,
        base_seconds=1.0,
        max_seconds=8.0,
        timeout_seconds=10.0,
        connect_timeout_seconds=5.0,
    )
    attempts = {"n": 0}

    def counting() -> str:
        attempts["n"] += 1
        raise TransientError("503")

    with pytest.raises(SourceError):
        call_with_retry(
            counting,
            policy=policy,
            description="test",
            is_retryable=is_transient,
            sleep=lambda _: None,
        )
    assert attempts["n"] == 1
