"""Market calendar access — holidays, early closes and the next alert windows.

Wraps ``exchange_calendars`` (XNYS) so the rest of the app never has to reason
about holidays itself. Encoding the NYSE calendar by hand is a recurring source
of silent bugs: the scanner would simply run on a closed day and write a day of
empty polls that later reads as a market with no movers.

Early closes matter as much as holidays here. On a half day the market closes
at 13:00 ET and post-market ends at 17:00, so the 16:00 and 16:30 alert windows
either do not exist or mean something different — and an outcome measured "to
the close" on such a day is measured to a different clock time.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import exchange_calendars as xcals

from app.core.timeutils import ET, et_datetime, to_et, to_utc

logger = logging.getLogger(__name__)

#: A regular NYSE session closes at 16:00 ET; anything earlier is a half day.
REGULAR_CLOSE = time(16, 0)


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """One trading day's open and close, and whether it is a half day."""

    day: date
    open_utc: datetime
    close_utc: datetime

    @property
    def close_et(self) -> time:
        """Closing wall-clock time in ET."""
        return to_et(self.close_utc).time()

    @property
    def is_early_close(self) -> bool:
        """Whether the session closes before the usual 16:00 ET."""
        return self.close_et < REGULAR_CLOSE


@lru_cache(maxsize=4)
def _calendar(name: str) -> xcals.ExchangeCalendar:
    """Load and cache an exchange calendar.

    Cached because building one parses years of holiday rules, and the
    scheduler asks for the next windows on every tick.
    """
    return xcals.get_calendar(name)


class MarketCalendar:
    """Trading days and session times for one exchange."""

    def __init__(self, name: str = "XNYS") -> None:
        self.name = name
        self._calendar = _calendar(name)

    def is_trading_day(self, day: date) -> bool:
        """Whether the exchange is open on ``day``."""
        return bool(self._calendar.is_session(day.isoformat()))

    def trading_days(self, start: date, end: date) -> tuple[date, ...]:
        """Every trading day in ``[start, end]``, ascending."""
        if end < start:
            return ()
        sessions = self._calendar.sessions_in_range(start.isoformat(), end.isoformat())
        return tuple(stamp.date() for stamp in sessions)

    def previous_trading_days(self, day: date, count: int) -> tuple[date, ...]:
        """The ``count`` trading days strictly before ``day``, oldest first.

        Strictly before, because an RVOL baseline that includes the day it is
        measuring compares a stock against itself.
        """
        if count <= 0:
            return ()
        # Reach back generously: 10 trading days can span a fortnight with
        # holidays, and asking for too few is a silent under-count.
        window_start = day - timedelta(days=count * 3 + 14)
        earlier = [d for d in self.trading_days(window_start, day) if d < day]
        return tuple(earlier[-count:])

    def session_info(self, day: date) -> SessionInfo | None:
        """Open and close for ``day``, or ``None`` when the market is closed."""
        if not self.is_trading_day(day):
            return None
        iso = day.isoformat()
        return SessionInfo(
            day=day,
            open_utc=to_utc(self._calendar.session_open(iso).to_pydatetime()),
            close_utc=to_utc(self._calendar.session_close(iso).to_pydatetime()),
        )

    def is_early_close(self, day: date) -> bool:
        """Whether ``day`` is a half day."""
        info = self.session_info(day)
        return info is not None and info.is_early_close

    def next_windows(
        self,
        as_of: datetime,
        windows: tuple[tuple[time, time], ...],
        *,
        count: int = 5,
        horizon_days: int = 14,
    ) -> tuple[tuple[datetime, datetime], ...]:
        """The next ``count`` alert windows after ``as_of``, skipping holidays.

        Windows are returned as UTC instants; the caller converts for display.
        A window whose start falls after that day's close is skipped, which is
        what removes the 16:00 and 16:30 windows from a half day.
        """
        start_day = to_et(as_of).date()
        found: list[tuple[datetime, datetime]] = []
        for day in self.trading_days(start_day, start_day + timedelta(days=horizon_days)):
            info = self.session_info(day)
            if info is None:
                continue
            for window_start, window_end in windows:
                begins = et_datetime(day, window_start)
                if begins <= to_utc(as_of):
                    continue
                if info.is_early_close and window_start >= info.close_et:
                    logger.debug(
                        "Skipping the %s window on %s: early close at %s",
                        window_start,
                        day,
                        info.close_et,
                    )
                    continue
                found.append((begins, et_datetime(day, window_end)))
                if len(found) >= count:
                    return tuple(sorted(found))
        return tuple(sorted(found))


def describe_windows(
    windows: tuple[tuple[datetime, datetime], ...], *, display_tz: str = "Europe/Paris"
) -> list[str]:
    """Render upcoming windows in ET and the trader's own timezone.

    Both zones on every line on purpose: the trader is in Paris, the market is
    in New York, and every scheduling mistake in this project will come from
    reading one and thinking the other.
    """
    zone = ZoneInfo(display_tz)
    lines = []
    for start, end in windows:
        et_start, et_end = to_et(start), to_et(end)
        local_start = start.astimezone(zone)
        lines.append(
            f"{et_start:%a %d %b} {et_start:%H:%M}-{et_end:%H:%M} ET "
            f"({local_start:%H:%M} {display_tz})"
        )
    return lines


def log_next_windows(
    calendar: MarketCalendar,
    as_of: datetime,
    windows: tuple[tuple[time, time], ...],
    *,
    count: int = 5,
    display_tz: str = "Europe/Paris",
) -> list[str]:
    """Log and return the next windows, as the app does on startup."""
    upcoming = calendar.next_windows(as_of, windows, count=count)
    lines = describe_windows(upcoming, display_tz=display_tz)
    for line in lines:
        logger.info("Next alert window: %s", line)
    if not lines:
        logger.warning("No alert windows found in the scheduling horizon")
    return lines


__all__ = [
    "ET",
    "MarketCalendar",
    "SessionInfo",
    "describe_windows",
    "log_next_windows",
]
