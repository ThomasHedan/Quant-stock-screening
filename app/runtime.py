"""Shared runtime state for the web app and the scheduler.

One object the routes and the jobs both read, so the Live page shows exactly
what the last poll produced rather than a second, slightly different view of
it. Everything mutable lives here and nowhere else, which keeps "what does the
app currently believe?" answerable in one place.

Deliberately plain and synchronous: this is a single-user app on one machine,
and the contention story is one background scheduler thread writing while a
request thread reads. A lock around the few mutable fields is the whole
concurrency design.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from app.alerts import Evaluation
from app.collector import WindowBaselines
from app.config import AppConfig, Secrets
from app.core.news import NewsCache
from app.core.tiering import WindowPushState
from app.core.timeutils import to_utc
from app.market_calendar import MarketCalendar
from app.sources.alpaca_news import FeedHealth
from app.storage.lake import LakeWriter
from app.storage.reference import ReferenceTracker

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SourceStatus:
    """Health of one external source, for the header status dots.

    ``last_success`` rather than a bare boolean because "working" is a claim
    with an age: a green dot next to a feed last heard from at 06:00 is a lie
    by 09:00 (CLAUDE.md 9).
    """

    name: str
    healthy: bool
    last_success_utc: datetime | None = None
    detail: str = ""

    def age_seconds(self, *, now: datetime) -> float | None:
        """Seconds since the last success, or ``None`` if never."""
        if self.last_success_utc is None:
            return None
        return (to_utc(now) - to_utc(self.last_success_utc)).total_seconds()


@dataclass(slots=True)
class RuntimeState:
    """Everything the running app knows right now."""

    config: AppConfig
    secrets: Secrets
    calendar: MarketCalendar
    sqlite_path: Path
    lake: LakeWriter
    news: NewsCache = field(default_factory=NewsCache)
    reference: ReferenceTracker = field(default_factory=ReferenceTracker)
    feed_health: FeedHealth = field(default_factory=FeedHealth)
    baselines: WindowBaselines | None = None
    push_state: WindowPushState | None = None
    last_evaluations: list[Evaluation] = field(default_factory=list)
    last_poll_utc: datetime | None = None
    last_flush_utc: datetime | None = None
    recent_runners: frozenset[str] = frozenset()
    warnings: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # -- mutation ------------------------------------------------------------

    def record_poll(self, evaluations: list[Evaluation], *, now: datetime) -> None:
        """Publish the result of a poll for the UI to read."""
        with self._lock:
            self.last_evaluations = evaluations
            self.last_poll_utc = to_utc(now)

    def open_window(self, window_start_utc: datetime) -> None:
        """Reset the per-window state when a new alert window opens.

        Both the baselines and the push budget are window-scoped, so they are
        reset together: carrying either across a window boundary would make
        ``window_change_pct`` meaningless and silently suppress alerts.
        """
        with self._lock:
            self.baselines = WindowBaselines(window_start=to_utc(window_start_utc))
            self.push_state = WindowPushState(
                max_pushes=self.config.alerts.max_pushes_per_window,
                tier_b_enabled=self.config.alerts.tier_b_push_enabled,
            )
        logger.info("Alert window opened at %s", to_utc(window_start_utc).isoformat())

    def set_recent_runners(self, tickers: frozenset[str]) -> None:
        """Replace the recent-runner watchlist."""
        with self._lock:
            self.recent_runners = tickers

    def warn(self, message: str) -> None:
        """Surface a condition in the dashboard header."""
        with self._lock:
            if message not in self.warnings:
                self.warnings.append(message)
        logger.warning("Dashboard warning: %s", message)

    def clear_warnings(self) -> None:
        """Clear the header warnings, typically after a successful nightly run."""
        with self._lock:
            self.warnings = []

    # -- reads ---------------------------------------------------------------

    def snapshot_evaluations(self) -> list[Evaluation]:
        """A stable copy of the last poll's evaluations."""
        with self._lock:
            return list(self.last_evaluations)

    def statuses(self, *, now: datetime) -> list[SourceStatus]:
        """Header status dots for every external dependency."""
        news_healthy = self.feed_health.disconnected_since is None
        return [
            SourceStatus(
                name="TradingView",
                healthy=self.last_poll_utc is not None,
                last_success_utc=self.last_poll_utc,
                detail="snapshot polls",
            ),
            SourceStatus(
                name="Alpaca news",
                healthy=news_healthy,
                last_success_utc=self.feed_health.connected_since,
                detail=f"{self.feed_health.reconnects} reconnects, "
                f"{self.feed_health.outage_minutes(now=now):.0f} min down",
            ),
            SourceStatus(
                name="Alpaca data",
                healthy=self.secrets.has_alpaca(),
                detail="bars and corporate actions",
            ),
            SourceStatus(
                name="Push",
                healthy=self.secrets.has_vapid(),
                detail="VAPID keys configured" if self.secrets.has_vapid() else "not configured",
            ),
            SourceStatus(
                name="Data quality",
                healthy=not self.warnings,
                detail="; ".join(self.warnings) or "no warnings",
            ),
        ]

    def next_windows(
        self, *, now: datetime, count: int = 3
    ) -> tuple[tuple[datetime, datetime], ...]:
        """The next alert windows, for the header countdown."""
        return self.calendar.next_windows(
            now,
            tuple(w.as_tuple() for w in self.config.schedules.alert_windows.windows),
            count=count,
        )
