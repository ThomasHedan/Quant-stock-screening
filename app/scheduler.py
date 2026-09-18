"""Job scheduling: what should run at this instant, and the APScheduler wiring.

The decision of *what is due* is a pure function (:func:`plan_tick`) that takes
the clock as an argument. The scheduler itself only calls it on a fixed tick
and dispatches. That split matters for two reasons: a whole trading day can be
replayed in a unit test in milliseconds, and the schedule logic cannot
accidentally read the wall clock behind the caller's back.

Alert windows take precedence over the collector: inside a window the app is
already polling every 30 seconds, and the collector reuses those snapshots
rather than making its own calls (CLAUDE.md 4).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum
from zoneinfo import ZoneInfo

from app.config import AppConfig, TimeWindow
from app.core.timeutils import et_datetime, et_trading_date, to_et, to_utc
from app.market_calendar import MarketCalendar, log_next_windows

logger = logging.getLogger(__name__)


class TickAction(StrEnum):
    """What a tick should do."""

    ALERT_POLL = "alert_poll"
    COLLECT_HOT = "collect_hot"
    COLLECT_COLD = "collect_cold"
    IDLE = "idle"


@dataclass(frozen=True, slots=True)
class TickPlan:
    """The decision for one tick.

    ``window_start_utc`` is carried because window-relative metrics
    (``window_change_pct``, ``window_volume``) and the push budget are all
    scoped to it, and a poll that does not know which window it belongs to
    cannot compute any of them.
    """

    action: TickAction
    now_utc: datetime
    window_start_utc: datetime | None = None
    interval_seconds: int = 0
    reason: str = ""

    @property
    def is_alert_poll(self) -> bool:
        """Whether this tick is inside an alert window."""
        return self.action is TickAction.ALERT_POLL


def _matching_window(now: datetime, windows: tuple[TimeWindow, ...]) -> TimeWindow | None:
    """The configured window containing ``now``, if any (half-open)."""
    wall = to_et(now).time()
    for window in windows:
        if window.start <= wall < window.end:
            return window
    return None


def _due(now: datetime, anchor: datetime, interval_seconds: int) -> bool:
    """Whether ``interval_seconds`` have elapsed since ``anchor``.

    Uses elapsed time from the window's own start rather than a modulo of the
    wall clock, so a window that opens at 08:00:07 still polls every 30 seconds
    from then rather than waiting for the next :30 boundary.
    """
    elapsed = (to_utc(now) - to_utc(anchor)).total_seconds()
    return elapsed >= 0 and (elapsed % interval_seconds) < 1.0


def plan_tick(
    now: datetime,
    config: AppConfig,
    calendar: MarketCalendar,
    *,
    last_poll_utc: datetime | None = None,
) -> TickPlan:
    """Decide what this instant calls for.

    Returns ``IDLE`` on non-trading days and outside every configured window,
    which is the common case: the app spends most of its life doing nothing,
    and it should do that cheaply and without touching a free API.
    """
    day: date = et_trading_date(now)
    if not calendar.is_trading_day(day):
        return TickPlan(TickAction.IDLE, to_utc(now), reason=f"{day} is not a trading day")

    alert_windows = config.schedules.alert_windows
    window = _matching_window(now, alert_windows.windows)
    if window is not None:
        start = et_datetime(day, window.start)
        if last_poll_utc is None or (
            (to_utc(now) - to_utc(last_poll_utc)).total_seconds() >= alert_windows.interval_seconds
        ):
            return TickPlan(
                TickAction.ALERT_POLL,
                to_utc(now),
                window_start_utc=start,
                interval_seconds=alert_windows.interval_seconds,
                reason=f"alert window {window.start:%H:%M}-{window.end:%H:%M} ET",
            )
        return TickPlan(
            TickAction.IDLE,
            to_utc(now),
            window_start_utc=start,
            reason="alert window open, but the poll interval has not elapsed",
        )

    for action, cadence in (
        (TickAction.COLLECT_HOT, config.schedules.collector.hot),
        (TickAction.COLLECT_COLD, config.schedules.collector.cold),
    ):
        matched = _matching_window(now, cadence.windows)
        if matched is None:
            continue
        if last_poll_utc is not None and (
            (to_utc(now) - to_utc(last_poll_utc)).total_seconds() < cadence.interval_seconds
        ):
            return TickPlan(
                TickAction.IDLE,
                to_utc(now),
                reason=f"{action} cadence has not elapsed",
            )
        return TickPlan(
            action,
            to_utc(now),
            interval_seconds=cadence.interval_seconds,
            reason=f"{action} window {matched.start:%H:%M}-{matched.end:%H:%M} ET",
        )

    return TickPlan(TickAction.IDLE, to_utc(now), reason="outside every configured window")


@dataclass(frozen=True, slots=True)
class DailyJob:
    """A once-a-day job and the ET time it runs at."""

    name: str
    at_et: time
    run: Callable[[datetime], None]


def build_daily_jobs(
    config: AppConfig,
    handlers: dict[str, Callable[[datetime], None]],
) -> list[DailyJob]:
    """Pair each configured daily time with its handler.

    A handler named in the config but absent here raises rather than being
    skipped: a job that silently never runs is how a month of outcomes ends up
    missing without anyone noticing.
    """
    jobs = config.schedules.jobs
    times = {
        "premarket_outcomes": jobs.premarket_outcomes,
        "daily_digest_push": jobs.daily_digest_push,
        "corporate_actions": jobs.corporate_actions,
        "full_day_outcomes": jobs.full_day_outcomes,
        "data_quality_and_compaction": jobs.data_quality_and_compaction,
    }
    missing = sorted(set(times) - set(handlers))
    if missing:
        msg = f"no handler registered for daily job(s) {missing}"
        raise KeyError(msg)
    return [DailyJob(name=name, at_et=at, run=handlers[name]) for name, at in sorted(times.items())]


def start(
    config: AppConfig,
    calendar: MarketCalendar,
    *,
    tick: Callable[[datetime], None],
    daily_jobs: list[DailyJob],
    now: datetime,
    tick_seconds: int = 5,
) -> object:
    """Start APScheduler with the tick loop and the daily jobs.

    Returns the scheduler so the caller (the FastAPI lifespan) can shut it
    down. Misfires are given a grace period rather than being dropped: a
    laptop that slept through 20:10 should still pull corporate actions when it
    wakes, because that table is what keeps split days out of the move metrics.
    """
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    market_tz = ZoneInfo(config.timezones.market)
    scheduler = BackgroundScheduler(timezone=market_tz)

    scheduler.add_job(
        lambda: tick(datetime.now(tz=market_tz)),
        IntervalTrigger(seconds=tick_seconds),
        id="tick",
        name="poll dispatcher",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=tick_seconds,
    )

    for job in daily_jobs:
        scheduler.add_job(
            lambda job=job: job.run(datetime.now(tz=market_tz)),
            CronTrigger(hour=job.at_et.hour, minute=job.at_et.minute, timezone=market_tz),
            id=job.name,
            name=job.name,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )
        logger.info("Scheduled %s at %s ET", job.name, job.at_et.strftime("%H:%M"))

    log_next_windows(
        calendar,
        now,
        tuple(w.as_tuple() for w in config.schedules.alert_windows.windows),
        count=5,
        display_tz=config.timezones.display,
    )
    scheduler.start()
    logger.info("Scheduler started with a %ss tick in %s", tick_seconds, config.timezones.market)
    return scheduler
