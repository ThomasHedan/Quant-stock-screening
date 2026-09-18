"""TradingView parsing: drift is loud, NaN is absent, non-stocks are excluded."""

from __future__ import annotations

import math
from datetime import datetime

import pytest
from app.core.timeutils import UTC
from app.sources import tradingview as tv

NOW = datetime(2026, 3, 10, 12, 5, tzinfo=UTC)


def raw_row(**overrides: object) -> dict:
    base = {
        "name": "ABCD",
        "description": "Abcd Therapeutics",
        "exchange": "NASDAQ",
        "type": "stock",
        "typespecs": ["common"],
        "close": 5.20,
        "change": 34.0,
        "volume": 8_200_000.0,
        "average_volume_10d_calc": 900_000.0,
        "average_volume_30d_calc": 850_000.0,
        "relative_volume_10d_calc": 9.1,
        "float_shares_outstanding": 4_100_000.0,
        "total_shares_outstanding": 12_000_000.0,
        "market_cap_basic": 62_400_000.0,
        "sector": "Health Technology",
        "industry": "Biotechnology",
        "premarket_close": 5.20,
        "premarket_change": 34.0,
        "premarket_volume": 8_200_000.0,
        "postmarket_close": None,
        "postmarket_change": None,
        "postmarket_volume": None,
    }
    return {**base, **overrides}


# --- row parsing -------------------------------------------------------------


def test_parse_row_maps_every_field():
    row = tv.parse_row(raw_row())
    assert row.ticker == "ABCD"
    assert row.close == 5.20
    assert row.float_shares == 4_100_000
    assert row.relative_volume_10d == 9.1


def test_parse_row_splits_an_exchange_prefixed_symbol():
    row = tv.parse_row(raw_row(name="NASDAQ:ABCD", exchange=""))
    assert (row.ticker, row.exchange) == ("ABCD", "NASDAQ")


def test_nan_becomes_none_not_zero():
    """A NaN compares false against every threshold; None is at least honest."""
    row = tv.parse_row(raw_row(float_shares_outstanding=math.nan, close=math.nan))
    assert row.float_shares is None
    assert row.close is None


def test_nonpositive_float_is_treated_as_missing():
    assert tv.parse_row(raw_row(float_shares_outstanding=0.0)).float_shares is None


def test_missing_ticker_is_rejected():
    with pytest.raises(ValueError, match="ticker"):
        tv.parse_row(raw_row(name=""))


# --- instrument filtering ----------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "keep"),
    [
        ({}, True),
        ({"type": "fund"}, False),
        ({"typespecs": ["etf"]}, False),
        ({"typespecs": ["warrant"]}, False),
        ({"exchange": "OTC"}, False),
        ({"type": "dr", "typespecs": ["common"]}, True),
    ],
)
def test_only_listed_common_stocks_are_kept(overrides, keep):
    assert tv.parse_row(raw_row(**overrides)).is_tradable_common_stock() is keep


# --- column drift ------------------------------------------------------------


def test_missing_essential_column_raises():
    columns = [c for c in tv.COLUMNS if c != "volume"]
    with pytest.raises(tv.FieldDriftError, match="volume"):
        tv.validate_columns(columns)


def test_missing_optional_column_is_reported_not_fatal(caplog):
    columns = [c for c in tv.COLUMNS if c != "float_shares_outstanding"]
    with caplog.at_level("ERROR"):
        missing = tv.validate_columns(columns)
    assert missing == ("float_shares_outstanding",)
    assert "unknown" in caplog.text


def test_column_count_mismatch_is_drift():
    payload = {"totalCount": 1, "data": [{"s": "NASDAQ:ABCD", "d": [1.0, 2.0]}]}
    with pytest.raises(tv.FieldDriftError, match="column contract"):
        tv._records_from_payload(payload)


def test_payload_without_data_is_a_source_error():
    from app.sources.retry import SourceError

    with pytest.raises(SourceError):
        tv._records_from_payload({"totalCount": 0})


def test_records_are_zipped_back_onto_column_names():
    values = [None] * len(tv.COLUMNS)
    values[tv.COLUMNS.index("name")] = "NASDAQ:ABCD"
    values[tv.COLUMNS.index("close")] = 5.2
    payload = {"totalCount": 1, "data": [{"s": "NASDAQ:ABCD", "d": values}]}
    records = tv._records_from_payload(payload)
    assert records[0]["close"] == 5.2
    assert records[0]["name"] == "NASDAQ:ABCD"


# --- whole responses ---------------------------------------------------------


def test_parse_response_separates_kept_and_excluded():
    result = tv.parse_response(
        [raw_row(), raw_row(name="ETFX", typespecs=["etf"])],
        total_matched=2,
        poll_ts_utc=NOW,
        returned_columns=list(tv.COLUMNS),
    )
    assert [row.ticker for row in result.rows] == ["ABCD"]
    assert result.excluded_non_stock == 1
    assert not result.degraded


def test_one_bad_row_does_not_discard_the_poll(caplog):
    """The other rows are ephemeral; throwing them away loses them forever."""
    with caplog.at_level("ERROR"):
        result = tv.parse_response(
            [raw_row(), raw_row(name="")],
            total_matched=2,
            poll_ts_utc=NOW,
            returned_columns=list(tv.COLUMNS),
        )
    assert len(result.rows) == 1
    assert result.unparsable == 1
    assert result.degraded
    assert result.errors


def test_error_samples_are_capped():
    bad = [raw_row(name="") for _ in range(20)]
    result = tv.parse_response(bad, total_matched=20, poll_ts_utc=NOW)
    assert result.unparsable == 20
    assert len(result.errors) == 5


def test_missing_optional_column_marks_the_poll_degraded():
    columns = [c for c in tv.COLUMNS if c != "float_shares_outstanding"]
    result = tv.parse_response(
        [raw_row()], total_matched=1, poll_ts_utc=NOW, returned_columns=columns
    )
    assert result.missing_columns == ("float_shares_outstanding",)
    assert result.degraded


def test_poll_timestamp_is_utc():
    from zoneinfo import ZoneInfo

    paris = NOW.astimezone(ZoneInfo("Europe/Paris"))
    result = tv.parse_response([raw_row()], total_matched=1, poll_ts_utc=paris)
    assert result.poll_ts_utc == NOW
    assert result.poll_ts_utc.tzinfo is UTC


# --- mock mode ---------------------------------------------------------------


def test_mock_snapshot_is_deterministic():
    """A mock day that cannot be replayed is not much of an acceptance test."""
    first = tv.MockSnapshotSource().snapshot(now=NOW)
    second = tv.MockSnapshotSource().snapshot(now=NOW)
    assert [r.ticker for r in first.rows] == [r.ticker for r in second.rows]
    assert [r.close for r in first.rows] == [r.close for r in second.rows]


def test_mock_snapshot_contains_runners_and_missing_floats():
    result = tv.MockSnapshotSource().snapshot(now=NOW)
    assert len(result.rows) == 400
    assert any(r.change_pct is not None and r.change_pct >= 10 for r in result.rows)
    assert any(r.float_shares is None for r in result.rows)


def test_mock_snapshot_moves_between_polls():
    from datetime import timedelta

    first = tv.MockSnapshotSource().snapshot(now=NOW)
    later = tv.MockSnapshotSource().snapshot(now=NOW + timedelta(minutes=1))
    assert [r.close for r in first.rows] != [r.close for r in later.rows]
