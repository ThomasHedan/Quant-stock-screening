"""Change-only reference writes and the point-in-time join."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from app.core.timeutils import UTC
from app.storage import lake, reference
from app.storage.schemas import SCHEMA_VERSION

DAY = date(2026, 3, 10)
OPEN = datetime(2026, 3, 10, 11, 0, tzinfo=UTC)


def fields(**overrides: object) -> dict:
    base = {
        "sector": "Biotechnology",
        "industry": "Pharma",
        "float_shares_outstanding": 4_100_000,
        "total_shares_outstanding": 12_000_000,
        "average_volume_10d_calc": 900_000.0,
        "average_volume_30d_calc": 850_000.0,
        "market_cap": 62_000_000.0,
        "float_source": "tradingview",
        "float_asof": None,
        "float_confidence": "high",
        "float_turnover": 2.0,
    }
    return {**base, **overrides}


@pytest.fixture
def tracker() -> reference.ReferenceTracker:
    return reference.ReferenceTracker()


def test_first_observation_always_writes(tracker):
    row = tracker.observe("ABCD", DAY, fields(), asof=OPEN)
    assert row is not None
    assert row["ticker"] == "ABCD"
    assert row["asof_ts_utc"] == OPEN


def test_unchanged_fields_write_nothing(tracker):
    tracker.observe("ABCD", DAY, fields(), asof=OPEN)
    assert tracker.observe("ABCD", DAY, fields(), asof=OPEN + timedelta(minutes=1)) is None


def test_market_cap_drift_alone_writes_nothing(tracker):
    """Market cap moves with every tick; including it would defeat the split."""
    tracker.observe("ABCD", DAY, fields(), asof=OPEN)
    later = fields(market_cap=99_000_000.0)
    assert tracker.observe("ABCD", DAY, later, asof=OPEN + timedelta(minutes=1)) is None


def test_float_change_writes_a_new_row(tracker, caplog):
    tracker.observe("ABCD", DAY, fields(), asof=OPEN)
    with caplog.at_level("INFO"):
        row = tracker.observe(
            "ABCD", DAY, fields(float_shares_outstanding=9_000_000), asof=OPEN + timedelta(hours=1)
        )
    assert row is not None
    assert row["float_shares_outstanding"] == 9_000_000
    assert "float_shares_outstanding" in caplog.text


def test_a_new_day_writes_again(tracker):
    tracker.observe("ABCD", DAY, fields(), asof=OPEN)
    next_day = date(2026, 3, 11)
    assert tracker.observe("ABCD", next_day, fields(), asof=OPEN + timedelta(days=1)) is not None


def test_forget_day_bounds_memory(tracker):
    tracker.observe("ABCD", DAY, fields(), asof=OPEN)
    tracker.observe("EFGH", DAY, fields(), asof=OPEN)
    tracker.observe("ABCD", date(2026, 3, 11), fields(), asof=OPEN + timedelta(days=1))
    assert tracker.forget_day(DAY) == 2
    assert tracker.tracked == 1


# --- point-in-time join ------------------------------------------------------


def rows_for_a_day() -> list[dict]:
    return [
        {"ticker": "ABCD", "asof_ts_utc": OPEN, **fields()},
        {
            "ticker": "ABCD",
            "asof_ts_utc": OPEN + timedelta(hours=5),
            **fields(float_shares_outstanding=30_000_000),
        },
    ]


def test_as_of_join_hides_later_corrections():
    """A 16:00 float correction must not leak into an 08:05 evaluation."""
    at_open = reference.as_of_per_ticker(rows_for_a_day(), OPEN + timedelta(minutes=5))
    assert at_open["ABCD"]["float_shares_outstanding"] == 4_100_000


def test_as_of_join_sees_the_correction_afterwards():
    later = reference.as_of_per_ticker(rows_for_a_day(), OPEN + timedelta(hours=6))
    assert later["ABCD"]["float_shares_outstanding"] == 30_000_000


def test_as_of_join_is_empty_before_the_first_observation():
    assert reference.as_of_per_ticker(rows_for_a_day(), OPEN - timedelta(hours=1)) == {}


def test_latest_per_ticker_takes_the_last_observation():
    latest = reference.latest_per_ticker(rows_for_a_day())
    assert latest["ABCD"]["float_shares_outstanding"] == 30_000_000


# --- the join reconstructs a full row ---------------------------------------


def test_snapshots_join_reference_reconstructs_the_full_row(tmp_path: Path):
    """Acceptance criterion: the split is lossless when joined back."""
    root = tmp_path / "lake"
    writer = lake.LakeWriter(root=root, source="tradingview")
    tracker = reference.ReferenceTracker()

    reference_row = tracker.observe("ABCD", DAY, fields(), asof=OPEN)
    assert reference_row is not None
    writer.append("reference", DAY, {**reference_row, "written_at_utc": OPEN})
    writer.append(
        "snapshots",
        DAY,
        {
            "ticker": "ABCD",
            "date": DAY,
            "poll_ts_utc": OPEN + timedelta(minutes=5),
            "price": 5.20,
            "session_volume": 8_200_000.0,
            "gap_pct": 34.0,
            "written_at_utc": OPEN,
        },
    )
    writer.flush(now=OPEN)

    snapshots = lake.read_day(root, "snapshots", DAY).to_pylist()
    references = lake.read_day(root, "reference", DAY).to_pylist()
    as_of = reference.as_of_per_ticker(references, OPEN + timedelta(minutes=5))

    joined = {**as_of[snapshots[0]["ticker"]], **snapshots[0]}
    assert joined["price"] == 5.20
    assert joined["float_shares_outstanding"] == 4_100_000
    assert joined["sector"] == "Biotechnology"
    assert joined["schema_version"] == SCHEMA_VERSION
