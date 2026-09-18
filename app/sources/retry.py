"""Timeout and retry policy shared by every external call.

CLAUDE.md 1.1 requires every external call to have a timeout, a retry policy
and a logged failure mode. Putting that in one place means a new source cannot
accidentally ship without one, and means the backoff behaviour is tested once
rather than trusted five times.

Retries are deliberately narrow: only transport failures and the status codes
that mean "try again later". A 400 or a 401 is a bug or a bad key, and
hammering the endpoint four more times neither fixes it nor is polite to a free
service.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: Status codes worth retrying: throttling and transient server-side failures.
RETRYABLE_STATUS: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})


class SourceError(RuntimeError):
    """A source failed in a way the caller has to handle.

    Carries whether the failure looked transient, so a caller can tell "the
    feed is down right now" (mark the pillar unknown, note it in data_quality)
    from "this request will never work" (a config or credential problem).
    """

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounded exponential backoff with jitter.

    Jitter matters even for a single-user app: the scheduler fires several
    sources on the same 60-second tick, and without it a shared outage makes
    all of them retry in lockstep.
    """

    max_retries: int
    base_seconds: float
    max_seconds: float
    timeout_seconds: float
    connect_timeout_seconds: float

    def delay_for(self, attempt: int, *, jitter: float = 1.0) -> float:
        """Seconds to wait before ``attempt`` (1-based), capped.

        ``jitter`` is injected rather than drawn here so the backoff curve can
        be tested exactly.
        """
        if attempt < 1:
            msg = f"attempt must be >= 1, got {attempt}"
            raise ValueError(msg)
        raw: float = self.base_seconds * float(2 ** (attempt - 1))
        return min(raw, self.max_seconds) * jitter


def call_with_retry[T](
    operation: Callable[[], T],
    *,
    policy: RetryPolicy,
    description: str,
    is_retryable: Callable[[Exception], bool],
    sleep: Callable[[float], None] = time.sleep,
    jitter: Callable[[], float] = lambda: random.uniform(0.5, 1.0),  # noqa: S311
) -> T:
    """Run ``operation``, retrying transient failures with backoff.

    ``sleep`` and ``jitter`` are injected so the tests can assert the exact
    delay sequence without actually waiting. The last failure is re-raised as a
    :class:`SourceError` marked transient, because by then the caller's question
    is "is this source usable right now?", not "which exception type was it".
    """
    last: Exception | None = None
    for attempt in range(1, policy.max_retries + 2):
        try:
            return operation()
        except Exception as exc:  # narrowed immediately by is_retryable
            if not is_retryable(exc):
                logger.exception("%s failed permanently on attempt %s", description, attempt)
                raise SourceError(f"{description} failed: {exc}", transient=False) from exc
            last = exc
            if attempt > policy.max_retries:
                break
            delay = policy.delay_for(attempt, jitter=jitter())
            logger.warning(
                "%s failed (attempt %s/%s): %s; retrying in %.1fs",
                description,
                attempt,
                policy.max_retries + 1,
                exc,
                delay,
            )
            sleep(delay)

    logger.error("%s exhausted %s retries", description, policy.max_retries)
    raise SourceError(
        f"{description} failed after {policy.max_retries + 1} attempts: {last}",
        transient=True,
    ) from last
