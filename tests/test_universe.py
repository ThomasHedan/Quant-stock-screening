"""Tier 0 rollups and listing status — the survivorship guards."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest
from app.core import universe
from app.core.moves import Bar
from app.core.timeutils import ET, UTC
from app.core.types import MarketSession

DAY = date(2026, 3, 10)
BOUNDS = {
    MarketSession.PRE: (time(4, 0), time(9, 30)),
    MarketSession.REGULAR: (time(9, 30), time(16, 0)),
    MarketSession.POST: (time(16, 0), time(20, 0)),
}


def bar_at(hour: int, minute: int, *, close: float = 10.0, volume: float = 1_000.0) -> Bar:
    stamp = datetime(2026, 3, 10, hour, minute, tzinfo=ET).astimezone(UTC)
    return Bar(
        minute=stamp,
        open=close - 0.5,
        high=close + 1.0,
        low=close - 1.0,
        close=close,
        volume=volume,
        vwap=close,
    )


# --- session splitting -------------------------------------------------------


def test_bars_are_attributed_by_their_opening_minute():
    grouped = universe.split_by_session([bar_at(9, 29), bar_at(9, 30), bar_at(16, 0)], BOUNDS)
    assert len(grouped[MarketSession.PRE]) == 1
    assert len(grouped[MarketSession.REGULAR]) == 1
    assert len(grouped[MarketSession.POST]) == 1


def test_bars_outside_every_session_are_dropped():
    grouped = universe.split_by_session([bar_at(3, 59), bar_at(20, 0)], BOUNDS)
    assert all(not bars for bars in grouped.values())


# --- rollups -----------------------------------------------------------------


def test_rollup_takes_first_open_and_last_close():
    bars = [bar_at(8, 0, close=10.0), bar_at(8, 1, close=12.0), bar_at(8, 2, close=11.0)]
    rollup = universe.session_rollup(MarketSession.PRE, bars)
    assert rollup.open == bars[0].open
    assert rollup.close == 11.0
    assert rollup.high == 13.0
    assert rollup.low == 9.0
    assert rollup.volume == 3_000.0


def test_rollup_vwap_is_volume_weighted_not_copied():
    """A session vwap copied off the last bar is that bar's vwap, not the session's."""
    bars = [bar_at(8, 0, close=10.0, volume=1_000.0), bar_at(8, 1, close=20.0, volume=3_000.0)]
    rollup = universe.session_rollup(MarketSession.PRE, bars)
    assert rollup.vwap == pytest.approx((10.0 * 1000 + 20.0 * 3000) / 4000)


def test_an_untraded_session_still_yields_a_row():
    """'Did not trade pre-market' is information, not an absence to infer."""
    rollup = universe.session_rollup(MarketSession.PRE, [])
    assert rollup.volume == 0.0
    assert rollup.close is None
    assert rollup.bar_count == 0


def test_rollup_of_zero_volume_bars_has_no_vwap():
    bars = [bar_at(8, 0, volume=0.0)]
    assert universe.session_rollup(MarketSession.PRE, bars).vwap is None


# --- daily rows --------------------------------------------------------------


def test_every_ticker_day_gets_three_rows():
    rows = universe.daily_rows("ABCD", DAY, [bar_at(8, 0)], BOUNDS, prev_close=9.0)
    assert len(rows) == 3
    assert {row["session"] for row in rows} == {"pre", "regular", "post"}


def test_untraded_sessions_are_present_with_zero_volume():
    rows = universe.daily_rows("ABCD", DAY, [bar_at(8, 0)], BOUNDS, prev_close=9.0)
    by_session = {row["session"]: row for row in rows}
    assert by_session["regular"]["volume"] == 0.0
    assert by_session["pre"]["volume"] == 1_000.0


def test_reference_fields_are_carried_onto_the_rows():
    rows = universe.daily_rows(
        "ABCD",
        DAY,
        [bar_at(8, 0)],
        BOUNDS,
        prev_close=9.0,
        reference={
            "float_shares_outstanding": 4_100_000,
            "total_shares_outstanding": 12_000_000,
            "sector": "Health Technology",
            "market_cap": 62_000_000.0,
        },
    )
    assert rows[0]["float_shares"] == 4_100_000
    assert rows[0]["sector"] == "Health Technology"


def test_integrity_flags_default_safely():
    row = universe.daily_rows("ABCD", DAY, [], BOUNDS, prev_close=None)[0]
    assert row["split_flag"] is False
    assert row["suspect_price"] is False
    assert row["ticker_canonical_id"] == "ABCD"


def test_split_metadata_is_recorded():
    row = universe.daily_rows(
        "ABCD",
        DAY,
        [],
        BOUNDS,
        prev_close=10.0,
        split_flag=True,
        split_ratio=0.1,
        days_since_reverse_split=0,
    )[0]
    assert row["split_flag"] is True
    assert row["split_ratio"] == 0.1


# --- listing status ----------------------------------------------------------


def test_a_new_ticker_is_recorded_active():
    records = universe.listing_transitions({}, {"ABCD"}, DAY)
    assert records["ABCD"].first_seen == DAY
    assert records["ABCD"].status == "active"


def test_first_seen_is_preserved_on_later_days():
    day_one = universe.listing_transitions({}, {"ABCD"}, DAY)
    day_two = universe.listing_transitions(day_one, {"ABCD"}, DAY + timedelta(days=1))
    assert day_two["ABCD"].first_seen == DAY
    assert day_two["ABCD"].last_seen == DAY + timedelta(days=1)


def test_a_single_absence_does_not_delist():
    """One missing day is far more often a feed hiccup than a delisting."""
    known = universe.listing_transitions({}, {"ABCD"}, DAY)
    after = universe.listing_transitions(known, set(), DAY + timedelta(days=1))
    assert after["ABCD"].status == "active"


def test_a_sustained_absence_delists_without_deleting(caplog):
    known = universe.listing_transitions({}, {"ABCD"}, DAY)
    with caplog.at_level("INFO"):
        after = universe.listing_transitions(known, set(), DAY + timedelta(days=6))
    assert after["ABCD"].status == "delisted"
    assert after["ABCD"].last_seen == DAY  # history is kept, not rewritten
    assert "delisted" in caplog.text


def test_a_symbol_change_links_both_histories():
    known = universe.listing_transitions({}, {"OLDX"}, DAY)
    renamed = universe.apply_symbol_change(known, "OLDX", "NEWX")
    assert renamed["OLDX"].status == "renamed"
    assert renamed["NEWX"].ticker_canonical_id == "OLDX"
    assert renamed["OLDX"].ticker_canonical_id == "OLDX"


def test_a_second_rename_keeps_the_original_canonical_id():
    known = universe.listing_transitions({}, {"OLDX"}, DAY)
    once = universe.apply_symbol_change(known, "OLDX", "MIDX")
    twice = universe.apply_symbol_change(once, "MIDX", "NEWX")
    assert twice["NEWX"].ticker_canonical_id == "OLDX"
