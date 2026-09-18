"""Timezone and session-boundary behaviour.

These are boundary tests on purpose: every bug this module can have shows up
exactly at a session edge, a DST switch, or on a naive datetime slipping in.
"""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from app.core import timeutils as tu
from app.core.types import MarketSession

ET = ZoneInfo("America/New_York")


def test_require_aware_rejects_naive():
    with pytest.raises(tu.NaiveDatetimeError):
        tu.require_aware(datetime(2026, 3, 10, 8, 0))  # noqa: DTZ001


def test_require_aware_accepts_utc():
    value = datetime(2026, 3, 10, 12, 0, tzinfo=tu.UTC)
    assert tu.require_aware(value) is value


def test_et_datetime_converts_to_utc_in_winter():
    # 08:05 ET in March before the DST switch is UTC-5.
    assert tu.et_datetime(date(2026, 3, 5), time(8, 5)) == datetime(
        2026, 3, 5, 13, 5, tzinfo=tu.UTC
    )


def test_et_datetime_converts_to_utc_in_summer():
    # After the 2026-03-08 switch the same wall clock is UTC-4.
    assert tu.et_datetime(date(2026, 3, 10), time(8, 5)) == datetime(
        2026, 3, 10, 12, 5, tzinfo=tu.UTC
    )


def test_et_trading_date_uses_et_not_utc():
    # A 19:30 ET post-market print in winter is already the next UTC day; the
    # trading date must stay on the ET day it traded.
    instant = datetime(2026, 1, 6, 19, 30, tzinfo=ET)
    assert instant.astimezone(tu.UTC).date() == date(2026, 1, 7)
    assert tu.et_trading_date(instant) == date(2026, 1, 6)


@pytest.mark.parametrize(
    ("wall", "expected"),
    [
        (time(3, 59), None),
        (time(4, 0), MarketSession.PRE),
        (time(9, 29), MarketSession.PRE),
        (time(9, 30), MarketSession.REGULAR),
        (time(15, 59), MarketSession.REGULAR),
        (time(16, 0), MarketSession.POST),
        (time(19, 59), MarketSession.POST),
        (time(20, 0), None),
    ],
)
def test_session_of_boundaries(wall, expected, session_bounds, trading_day):
    instant = datetime.combine(trading_day, wall, tzinfo=ET)
    assert tu.session_of(instant, session_bounds) is expected


def test_is_within_window_is_half_open(trading_day):
    start, end = time(8, 0), time(8, 5)
    at_start = datetime.combine(trading_day, start, tzinfo=ET)
    at_end = datetime.combine(trading_day, end, tzinfo=ET)
    assert tu.is_within_window(at_start, start, end)
    assert not tu.is_within_window(at_end, start, end)


def test_minutes_since_et_open_measures_from_0400(trading_day):
    instant = datetime.combine(trading_day, time(7, 20), tzinfo=ET)
    assert tu.minutes_since_et_open(instant, time(4, 0)) == pytest.approx(200.0)


def test_next_window_start_skips_holidays():
    # 2026-01-01 is a holiday, so it is absent from the calendar's trading days.
    as_of = datetime(2025, 12, 31, 21, 0, tzinfo=ET)
    windows = ((time(8, 0), time(8, 5)), (time(8, 30), time(8, 35)))
    days = (date(2025, 12, 31), date(2026, 1, 2))
    nxt = tu.next_window_start(as_of, windows, days)
    assert nxt is not None
    assert tu.to_et(nxt) == datetime(2026, 1, 2, 8, 0, tzinfo=ET)


def test_next_window_start_returns_none_when_exhausted(trading_day):
    as_of = datetime.combine(trading_day, time(23, 0), tzinfo=ET)
    windows = ((time(8, 0), time(8, 5)),)
    assert tu.next_window_start(as_of, windows, (trading_day,)) is None


def test_trading_days_before_is_strict():
    calendar = (date(2026, 3, 4), date(2026, 3, 5), date(2026, 3, 6), date(2026, 3, 9))
    assert tu.trading_days_before(date(2026, 3, 9), calendar, 2) == (
        date(2026, 3, 5),
        date(2026, 3, 6),
    )


def test_trading_days_before_excludes_the_day_itself():
    """The baseline must never include today — that is lookahead."""
    calendar = (date(2026, 3, 5), date(2026, 3, 6), date(2026, 3, 9))
    assert date(2026, 3, 9) not in tu.trading_days_before(date(2026, 3, 9), calendar, 10)


def test_minute_range_is_half_open():
    start = datetime(2026, 3, 10, 12, 0, tzinfo=tu.UTC)
    end = datetime(2026, 3, 10, 12, 3, tzinfo=tu.UTC)
    assert tu.minute_range(start, end) == (
        datetime(2026, 3, 10, 12, 0, tzinfo=tu.UTC),
        datetime(2026, 3, 10, 12, 1, tzinfo=tu.UTC),
        datetime(2026, 3, 10, 12, 2, tzinfo=tu.UTC),
    )
