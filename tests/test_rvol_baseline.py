"""RVOL baselines: caching, the no-lookahead rule, and split invalidation."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest
from app import rvol
from app.core.moves import Bar
from app.core.timeutils import ET, UTC, et_datetime
from app.storage import db

DAY = date(2026, 3, 10)
AS_OF = datetime(2026, 3, 10, 8, 5, tzinfo=ET).astimezone(UTC)
NOW = AS_OF
PRIOR_DAYS = (date(2026, 3, 6), date(2026, 3, 9))


@pytest.fixture
def connection(tmp_path: Path):
    with db.session(tmp_path / "app.db") as conn:
        yield conn


def bars_for(day: date, per_minute: list[float]) -> tuple[Bar, ...]:
    start = et_datetime(day, time(4, 0))
    return tuple(
        Bar(
            minute=start + timedelta(minutes=i),
            open=5.0,
            high=5.1,
            low=4.9,
            close=5.0,
            volume=volume,
        )
        for i, volume in enumerate(per_minute)
    )


# --- offsets -----------------------------------------------------------------


def test_offset_counts_minutes_from_the_session_start():
    assert rvol.minute_offset(AS_OF) == 245  # 04:00 -> 08:05 ET


def test_offset_is_stable_across_the_dst_switch():
    """08:05 ET is 245 minutes into the session in both March and November."""
    winter = datetime(2026, 1, 6, 8, 5, tzinfo=ET).astimezone(UTC)
    summer = datetime(2026, 7, 6, 8, 5, tzinfo=ET).astimezone(UTC)
    assert rvol.minute_offset(winter) == rvol.minute_offset(summer) == 245


# --- computation -------------------------------------------------------------


def test_baseline_averages_cumulative_volume_across_days():
    bars_by_day = {
        PRIOR_DAYS[0]: bars_for(PRIOR_DAYS[0], [100.0, 200.0, 300.0]),
        PRIOR_DAYS[1]: bars_for(PRIOR_DAYS[1], [300.0, 300.0, 400.0]),
    }
    volume, days_used = rvol.compute_baseline(bars_by_day, offset=2)
    assert volume == pytest.approx(450.0)  # (300 + 600) / 2
    assert days_used == 2


def test_a_day_without_bars_is_excluded_from_the_average():
    """Averaging in a zero would halve the baseline and double every RVOL."""
    bars_by_day = {
        PRIOR_DAYS[0]: bars_for(PRIOR_DAYS[0], [100.0, 200.0]),
        PRIOR_DAYS[1]: (),
    }
    volume, days_used = rvol.compute_baseline(bars_by_day, offset=2)
    assert volume == pytest.approx(300.0)
    assert days_used == 1


def test_no_usable_days_yields_none():
    assert rvol.compute_baseline({}, offset=5) == (None, 0)


# --- caching -----------------------------------------------------------------


def fetcher(bars_by_day: dict[date, tuple[Bar, ...]], calls: list[tuple]):
    def fetch(ticker: str, days: tuple[date, ...]) -> dict[date, tuple[Bar, ...]]:
        calls.append((ticker, days))
        return {day: bars_by_day.get(day, ()) for day in days}

    return fetch


def test_a_miss_computes_and_caches(connection):
    calls: list[tuple] = []
    bars_by_day = {day: bars_for(day, [100.0] * 300) for day in PRIOR_DAYS}
    entry = rvol.baseline_for(
        connection,
        "ABCD",
        as_of=AS_OF,
        fetch_bars=fetcher(bars_by_day, calls),
        baseline_days=10,
        trading_days=(*PRIOR_DAYS, DAY),
        now=NOW,
    )
    assert entry is not None
    assert entry.baseline_volume == pytest.approx(24_500.0)
    assert entry.days_used == 2
    assert len(calls) == 1


def test_a_hit_does_not_refetch(connection):
    calls: list[tuple] = []
    bars_by_day = {day: bars_for(day, [100.0] * 300) for day in PRIOR_DAYS}
    fetch = fetcher(bars_by_day, calls)
    for _ in range(3):
        rvol.baseline_for(
            connection,
            "ABCD",
            as_of=AS_OF,
            fetch_bars=fetch,
            baseline_days=10,
            trading_days=(*PRIOR_DAYS, DAY),
            now=NOW,
        )
    assert len(calls) == 1


def test_the_baseline_never_includes_today(connection):
    """Comparing a stock against itself makes every RVOL tend to 1."""
    calls: list[tuple] = []
    bars_by_day = {day: bars_for(day, [100.0] * 300) for day in (*PRIOR_DAYS, DAY)}
    rvol.baseline_for(
        connection,
        "ABCD",
        as_of=AS_OF,
        fetch_bars=fetcher(bars_by_day, calls),
        baseline_days=10,
        trading_days=(*PRIOR_DAYS, DAY),
        now=NOW,
    )
    _ticker, requested_days = calls[0]
    assert DAY not in requested_days


def test_no_prior_days_means_no_baseline(connection, caplog):
    with caplog.at_level("INFO"):
        entry = rvol.baseline_for(
            connection,
            "IPO",
            as_of=AS_OF,
            fetch_bars=fetcher({}, []),
            baseline_days=10,
            trading_days=(DAY,),
            now=NOW,
        )
    assert entry is None
    assert "No prior trading days" in caplog.text


def test_zero_volume_history_yields_no_baseline(connection):
    bars_by_day = {day: bars_for(day, [0.0] * 300) for day in PRIOR_DAYS}
    entry = rvol.baseline_for(
        connection,
        "DEAD",
        as_of=AS_OF,
        fetch_bars=fetcher(bars_by_day, []),
        baseline_days=10,
        trading_days=(*PRIOR_DAYS, DAY),
        now=NOW,
    )
    assert entry is None


def test_only_the_configured_number_of_days_is_used(connection):
    calls: list[tuple] = []
    many = tuple(date(2026, 2, d) for d in range(1, 20))
    rvol.baseline_for(
        connection,
        "ABCD",
        as_of=AS_OF,
        fetch_bars=fetcher({day: bars_for(day, [50.0] * 300) for day in many}, calls),
        baseline_days=10,
        trading_days=(*many, DAY),
        now=NOW,
    )
    _ticker, requested_days = calls[0]
    assert len(requested_days) == 10


# --- split invalidation ------------------------------------------------------


def test_a_split_invalidates_the_cached_baseline(connection, caplog):
    """A baseline straddling a split is wrong by the split ratio."""
    bars_by_day = {day: bars_for(day, [100.0] * 300) for day in PRIOR_DAYS}
    rvol.baseline_for(
        connection,
        "ABCD",
        as_of=AS_OF,
        fetch_bars=fetcher(bars_by_day, []),
        baseline_days=10,
        trading_days=(*PRIOR_DAYS, DAY),
        now=NOW,
    )
    assert rvol.load_cached(connection, "ABCD", DAY, 245) is not None

    with caplog.at_level("INFO"):
        removed = rvol.invalidate(connection, ("ABCD",), DAY)
    assert removed == 1
    assert rvol.load_cached(connection, "ABCD", DAY, 245) is None
    assert "Invalidated 1" in caplog.text


def test_invalidating_nothing_is_a_no_op(connection):
    assert rvol.invalidate(connection, (), DAY) == 0


def test_invalidation_leaves_other_tickers_alone(connection):
    bars_by_day = {day: bars_for(day, [100.0] * 300) for day in PRIOR_DAYS}
    for ticker in ("ABCD", "EFGH"):
        rvol.baseline_for(
            connection,
            ticker,
            as_of=AS_OF,
            fetch_bars=fetcher(bars_by_day, []),
            baseline_days=10,
            trading_days=(*PRIOR_DAYS, DAY),
            now=NOW,
        )
    rvol.invalidate(connection, ("ABCD",), DAY)
    assert rvol.load_cached(connection, "EFGH", DAY, 245) is not None
