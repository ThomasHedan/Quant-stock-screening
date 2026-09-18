"""Timezone helpers for the pure core.

Every datetime in this codebase is timezone-aware and stored in UTC; ET is a
display and scheduling concern only. These helpers make that explicit and give
market-hours logic a single, tested place to live. Nothing here reads the
clock — ``as_of`` is always passed in (CLAUDE.md 1.1).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.core.types import MarketSession

UTC = ZoneInfo("UTC")
ET = ZoneInfo("America/New_York")


class NaiveDatetimeError(ValueError):
    """Raised when a naive datetime reaches a function that requires an offset.

    A naive datetime is never merely inconvenient here: silently assuming a zone
    for it is how pre-market bars end up attributed to the wrong session.
    """


def require_aware(value: datetime, *, name: str = "datetime") -> datetime:
    """Return ``value`` unchanged, or raise if it carries no timezone."""
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        msg = f"{name} must be timezone-aware, got naive {value!r}"
        raise NaiveDatetimeError(msg)
    return value


def to_utc(value: datetime) -> datetime:
    """Convert an aware datetime to UTC."""
    return require_aware(value).astimezone(UTC)


def to_et(value: datetime) -> datetime:
    """Convert an aware datetime to America/New_York."""
    return require_aware(value).astimezone(ET)


def et_datetime(day: date, wall: time) -> datetime:
    """Build the aware UTC instant of an ET wall-clock time on ``day``.

    DST transitions are resolved by ``zoneinfo``'s default fold rules. The
    market never opens inside the ambiguous 01:00–02:00 window, so the two
    transition days need no special handling for our schedules.
    """
    return datetime.combine(day, wall, tzinfo=ET).astimezone(UTC)


def et_trading_date(value: datetime) -> date:
    """The ET calendar date a timestamp belongs to.

    Sessions run 04:00–20:00 ET, entirely inside one ET calendar day, so the ET
    date is the trading date. Using the UTC date instead would push every
    post-market print after 20:00 ET onto the next day.
    """
    return to_et(value).date()


def session_of(
    value: datetime, bounds: dict[MarketSession, tuple[time, time]]
) -> MarketSession | None:
    """Which session an instant falls in, or ``None`` outside all of them.

    Bounds are half-open ``[start, end)`` so that 09:30 belongs to ``regular``
    and 16:00 to ``post``, never to both.
    """
    wall = to_et(value).time()
    for session, (start, end) in bounds.items():
        if start <= wall < end:
            return session
    return None


def minutes_between(earlier: datetime, later: datetime) -> float:
    """Signed minutes from ``earlier`` to ``later``, both aware."""
    delta = to_utc(later) - to_utc(earlier)
    return delta.total_seconds() / 60.0


def minutes_since_et_open(value: datetime, day_start: time) -> float:
    """Minutes from ``day_start`` ET on the instant's own trading date.

    Used for ``minutes_to_high`` (CLAUDE.md 6.3), which is measured from 04:00
    ET rather than from the regular open.
    """
    start = et_datetime(et_trading_date(value), day_start)
    return minutes_between(start, value)


def is_within_window(value: datetime, start: time, end: time) -> bool:
    """Whether an instant falls inside an ET wall-clock window ``[start, end)``."""
    wall = to_et(value).time()
    return start <= wall < end


def next_window_start(
    as_of: datetime,
    windows: tuple[tuple[time, time], ...],
    trading_days: tuple[date, ...],
) -> datetime | None:
    """First window start strictly after ``as_of``, over the given trading days.

    ``trading_days`` comes from the exchange calendar, so holidays and weekends
    simply are not in the list — the calendar stays an I/O concern and this
    function stays pure.
    """
    require_aware(as_of, name="as_of")
    candidates = [
        et_datetime(day, start) for day in sorted(trading_days) for start, _end in windows
    ]
    future = [candidate for candidate in sorted(candidates) if candidate > to_utc(as_of)]
    return future[0] if future else None


def trading_days_before(day: date, calendar_days: tuple[date, ...], count: int) -> tuple[date, ...]:
    """The ``count`` trading days strictly before ``day``, oldest first.

    Strictly before, because an RVOL baseline that includes today would compare
    a stock against itself — the textbook lookahead bug in this codebase.
    """
    if count < 0:
        msg = f"count must be non-negative, got {count}"
        raise ValueError(msg)
    earlier = sorted(d for d in calendar_days if d < day)
    return tuple(earlier[-count:]) if count else ()


def floor_to_minute(value: datetime) -> datetime:
    """Truncate an aware datetime to the start of its minute, in UTC."""
    utc = to_utc(value)
    return utc.replace(second=0, microsecond=0)


def minute_range(start: datetime, end: datetime) -> tuple[datetime, ...]:
    """Every minute mark in ``[start, end)``, UTC, ascending."""
    first = floor_to_minute(start)
    last = floor_to_minute(end)
    out: list[datetime] = []
    cursor = first
    while cursor < last:
        out.append(cursor)
        cursor += timedelta(minutes=1)
    return tuple(out)
