"""Market calendar: holidays, early closes and the next-window listing.

Acceptance criterion 11.7: the scheduler logs the next five windows in ET and
Paris time, skipping a known market holiday.
"""

from __future__ import annotations

from datetime import date, datetime, time

import pytest
from app.core.timeutils import ET, UTC
from app.market_calendar import MarketCalendar, describe_windows, log_next_windows

WINDOWS = (
    (time(8, 0), time(8, 5)),
    (time(8, 30), time(8, 35)),
    (time(9, 0), time(9, 5)),
    (time(16, 0), time(16, 5)),
    (time(16, 30), time(16, 35)),
)


@pytest.fixture(scope="module")
def calendar() -> MarketCalendar:
    return MarketCalendar("XNYS")


def test_new_years_day_is_not_a_trading_day(calendar):
    assert not calendar.is_trading_day(date(2026, 1, 1))
    assert calendar.is_trading_day(date(2026, 1, 2))


def test_weekends_are_not_trading_days(calendar):
    assert not calendar.is_trading_day(date(2026, 3, 14))  # Saturday


def test_trading_days_skip_the_holiday(calendar):
    days = calendar.trading_days(date(2025, 12, 31), date(2026, 1, 5))
    assert date(2026, 1, 1) not in days
    assert days == (date(2025, 12, 31), date(2026, 1, 2), date(2026, 1, 5))


def test_previous_trading_days_is_strictly_exclusive(calendar):
    days = calendar.previous_trading_days(date(2026, 1, 5), 3)
    assert date(2026, 1, 5) not in days
    assert len(days) == 3
    assert days[-1] == date(2026, 1, 2)


def test_previous_trading_days_spans_a_holiday_week(calendar):
    """Ten trading days can span more than a fortnight around the holidays."""
    days = calendar.previous_trading_days(date(2026, 1, 5), 10)
    assert len(days) == 10
    assert date(2026, 1, 1) not in days


def test_previous_trading_days_of_zero_is_empty(calendar):
    assert calendar.previous_trading_days(date(2026, 1, 5), 0) == ()


def test_session_info_reports_the_regular_close(calendar):
    info = calendar.session_info(date(2026, 3, 10))
    assert info is not None
    assert info.close_et == time(16, 0)
    assert not info.is_early_close


def test_a_closed_day_has_no_session_info(calendar):
    assert calendar.session_info(date(2026, 1, 1)) is None


def test_the_day_after_thanksgiving_is_an_early_close(calendar):
    assert calendar.is_early_close(date(2025, 11, 28))
    info = calendar.session_info(date(2025, 11, 28))
    assert info is not None
    assert info.close_et == time(13, 0)


# --- next windows ------------------------------------------------------------


def test_next_windows_skip_a_holiday(calendar):
    as_of = datetime(2025, 12, 31, 20, 0, tzinfo=ET).astimezone(UTC)
    upcoming = calendar.next_windows(as_of, WINDOWS, count=5)
    days = {start.astimezone(ET).date() for start, _end in upcoming}
    assert date(2026, 1, 1) not in days
    assert date(2026, 1, 2) in days


def test_next_windows_are_in_order_and_after_the_reference(calendar):
    as_of = datetime(2026, 3, 10, 8, 10, tzinfo=ET).astimezone(UTC)
    upcoming = calendar.next_windows(as_of, WINDOWS, count=5)
    assert len(upcoming) == 5
    assert all(start > as_of for start, _ in upcoming)
    assert list(upcoming) == sorted(upcoming)
    first_et = upcoming[0][0].astimezone(ET)
    assert (first_et.hour, first_et.minute) == (8, 30)


def test_post_market_windows_are_skipped_on_a_half_day(calendar):
    """A 13:00 close means the 16:00 and 16:30 windows do not exist that day."""
    as_of = datetime(2025, 11, 28, 7, 0, tzinfo=ET).astimezone(UTC)
    upcoming = calendar.next_windows(as_of, WINDOWS, count=5)
    same_day = [s for s, _ in upcoming if s.astimezone(ET).date() == date(2025, 11, 28)]
    assert all(s.astimezone(ET).hour < 13 for s in same_day)
    assert len(same_day) == 3  # the three morning windows only


def test_windows_are_returned_in_utc(calendar):
    as_of = datetime(2026, 3, 10, 7, 0, tzinfo=ET).astimezone(UTC)
    start, end = calendar.next_windows(as_of, WINDOWS, count=1)[0]
    assert start.tzinfo is UTC
    assert end > start


# --- rendering ---------------------------------------------------------------


def test_windows_are_described_in_both_timezones(calendar):
    as_of = datetime(2026, 3, 10, 7, 0, tzinfo=ET).astimezone(UTC)
    lines = describe_windows(calendar.next_windows(as_of, WINDOWS, count=1))
    assert "08:00-08:05 ET" in lines[0]
    assert "Europe/Paris" in lines[0]
    assert "13:00" in lines[0]  # 08:00 ET in March is 13:00 in Paris


def test_log_next_windows_logs_five(calendar, caplog):
    as_of = datetime(2025, 12, 31, 20, 0, tzinfo=ET).astimezone(UTC)
    with caplog.at_level("INFO"):
        lines = log_next_windows(calendar, as_of, WINDOWS, count=5)
    assert len(lines) == 5
    assert caplog.text.count("Next alert window") == 5


def test_no_windows_configured_warns(calendar, caplog):
    as_of = datetime(2026, 3, 10, 7, 0, tzinfo=ET).astimezone(UTC)
    with caplog.at_level("WARNING"):
        assert log_next_windows(calendar, as_of, (), count=5) == []
    assert "No alert windows" in caplog.text
