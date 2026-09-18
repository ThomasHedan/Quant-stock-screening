"""Lake invariants: row counts survive, drift is fatal, the past stays put.

These are the tests that protect data that cannot be refetched. A TradingView
snapshot exists for 30 seconds and then is gone forever, so "the flush lost a
few rows" is not recoverable by rerunning anything.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from app.core.timeutils import UTC
from app.storage import lake
from app.storage.schemas import SCHEMA_VERSION, SchemaDriftError, UnknownTableError

DAY = date(2026, 3, 10)
NOW = datetime(2026, 3, 10, 12, 5, tzinfo=UTC)


def snapshot_row(ticker: str, minute: int, price: float = 5.0) -> dict:
    return {
        "ticker": ticker,
        "date": DAY,
        "poll_ts_utc": NOW + timedelta(minutes=minute),
        "price": price,
        "session_volume": 1_000_000.0 + minute,
        "gap_pct": 34.0,
        "written_at_utc": NOW,
    }


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "lake"


@pytest.fixture
def writer(root: Path) -> lake.LakeWriter:
    return lake.LakeWriter(root=root, source="tradingview")


# --- writing and reading -----------------------------------------------------


def test_flush_writes_a_part_and_clears_the_buffer(writer, root):
    writer.extend("snapshots", DAY, [snapshot_row("ABCD", i) for i in range(3)])
    assert writer.pending() == 3
    paths = writer.flush(now=NOW)
    assert len(paths) == 1
    assert writer.pending() == 0
    assert lake.row_count(root, "snapshots", DAY) == 3


def test_empty_flush_writes_nothing(writer, root):
    assert writer.flush(now=NOW) == []
    assert not (root / "snapshots").exists()


def test_provenance_columns_are_stamped(writer, root):
    writer.append("snapshots", DAY, snapshot_row("ABCD", 0))
    writer.flush(now=NOW)
    table = lake.read_day(root, "snapshots", DAY)
    assert table.column("source").to_pylist() == ["tradingview"]
    assert table.column("schema_version").to_pylist() == [SCHEMA_VERSION]


def test_partition_path_layout(root):
    path = lake.partition_path(root, "snapshots", DAY)
    assert path.as_posix().endswith("snapshots/date=2026-03-10")


def test_read_day_of_missing_partition_is_empty(root):
    assert lake.read_day(root, "snapshots", DAY).num_rows == 0


def test_partition_days_lists_dates(writer, root):
    writer.append("snapshots", DAY, snapshot_row("ABCD", 0))
    other = date(2026, 3, 11)
    writer.append("snapshots", other, {**snapshot_row("ABCD", 0), "date": other})
    writer.flush(now=NOW)
    assert lake.partition_days(root, "snapshots") == (DAY, date(2026, 3, 11))


# --- append-only within a day ------------------------------------------------


def test_successive_flushes_append_parts_and_never_rewrite(writer, root):
    writer.append("snapshots", DAY, snapshot_row("ABCD", 0))
    first = writer.flush(now=NOW)[0]
    first_bytes = first.read_bytes()

    writer.append("snapshots", DAY, snapshot_row("EFGH", 1))
    second = writer.flush(now=NOW + timedelta(minutes=5))[0]

    assert second != first
    assert first.read_bytes() == first_bytes  # the earlier part is untouched
    assert lake.row_count(root, "snapshots", DAY) == 2


def test_a_crash_loses_at_most_the_unflushed_buffer(writer, root):
    """Everything flushed survives; only the in-memory tail is lost."""
    writer.extend("snapshots", DAY, [snapshot_row("ABCD", i) for i in range(4)])
    writer.flush(now=NOW)
    writer.extend("snapshots", DAY, [snapshot_row("ABCD", i) for i in range(4, 7)])

    del writer  # the process dies here, buffer and all

    assert lake.row_count(root, "snapshots", DAY) == 4


def test_flush_if_due_respects_the_interval(writer, root):
    writer.append("snapshots", DAY, snapshot_row("ABCD", 0))
    early = writer.flush_if_due(
        now=NOW + timedelta(seconds=60), last_flush=NOW, interval_seconds=300
    )
    assert early == []
    assert writer.pending() == 1

    due = writer.flush_if_due(
        now=NOW + timedelta(seconds=300), last_flush=NOW, interval_seconds=300
    )
    assert len(due) == 1


# --- schema drift ------------------------------------------------------------


def test_unknown_table_is_rejected_at_the_call_site(writer):
    with pytest.raises(UnknownTableError):
        writer.append("snapsohts", DAY, {})


def test_undeclared_column_is_fatal(writer):
    writer.append("snapshots", DAY, {**snapshot_row("ABCD", 0), "gap_percent": 34.0})
    with pytest.raises(SchemaDriftError, match="undeclared columns"):
        writer.flush(now=NOW)


def test_missing_required_column_is_fatal_not_nulled(writer):
    """A renamed upstream field must raise, never default to zero."""
    row = snapshot_row("ABCD", 0)
    del row["ticker"]
    writer.append("snapshots", DAY, row)
    with pytest.raises(SchemaDriftError, match="ticker"):
        writer.flush(now=NOW)


def test_wrong_type_is_fatal(writer):
    writer.append("snapshots", DAY, {**snapshot_row("ABCD", 0), "price": "five dollars"})
    with pytest.raises(SchemaDriftError):
        writer.flush(now=NOW)


def test_failed_flush_keeps_the_rows_buffered(writer):
    """Dropping them would lose the only copy of an ephemeral snapshot."""
    writer.append("snapshots", DAY, {**snapshot_row("ABCD", 0), "bogus": 1})
    with pytest.raises(SchemaDriftError):
        writer.flush(now=NOW)
    assert writer.pending("snapshots") == 1


# --- compaction --------------------------------------------------------------


def test_compaction_preserves_every_row(writer, root):
    for batch in range(4):
        writer.extend("snapshots", DAY, [snapshot_row(f"T{i}", batch) for i in range(25)])
        writer.flush(now=NOW + timedelta(minutes=5 * batch))
    before = lake.row_count(root, "snapshots", DAY)
    assert before == 100

    merged = lake.compact_day(root, "snapshots", DAY, now=NOW + timedelta(hours=8))
    assert merged is not None
    assert lake.row_count(root, "snapshots", DAY) == before
    assert len(list(lake.partition_path(root, "snapshots", DAY).glob("*.parquet"))) == 1


def test_compaction_preserves_content_not_just_counts(writer, root):
    writer.extend("snapshots", DAY, [snapshot_row(f"T{i}", 0, price=float(i)) for i in range(5)])
    writer.flush(now=NOW)
    writer.extend("snapshots", DAY, [snapshot_row(f"U{i}", 1, price=float(i)) for i in range(5)])
    writer.flush(now=NOW + timedelta(minutes=5))

    before = sorted(lake.read_day(root, "snapshots", DAY).column("ticker").to_pylist())
    lake.compact_day(root, "snapshots", DAY, now=NOW + timedelta(hours=8))
    after = sorted(lake.read_day(root, "snapshots", DAY).column("ticker").to_pylist())
    assert after == before


def test_compaction_of_a_single_part_is_a_no_op(writer, root):
    writer.append("snapshots", DAY, snapshot_row("ABCD", 0))
    only = writer.flush(now=NOW)[0]
    assert lake.compact_day(root, "snapshots", DAY, now=NOW) == only


def test_compaction_uses_zstd_and_dictionary_encodes_ticker(writer, root):
    for batch in range(2):
        writer.extend("snapshots", DAY, [snapshot_row("ABCD", batch)])
        writer.flush(now=NOW + timedelta(minutes=5 * batch))
    merged = lake.compact_day(root, "snapshots", DAY, now=NOW + timedelta(hours=8))
    assert merged is not None
    column = pq.read_metadata(merged).row_group(0).column(0)
    assert column.compression.lower() == "zstd"
    assert "RLE_DICTIONARY" in column.encodings or "PLAIN_DICTIONARY" in column.encodings


def test_compaction_leaves_other_days_alone(writer, root):
    other = date(2026, 3, 9)
    writer.append("snapshots", other, {**snapshot_row("ABCD", 0), "date": other})
    writer.flush(now=NOW)
    writer.append("snapshots", DAY, snapshot_row("EFGH", 0))
    writer.flush(now=NOW)

    lake.compact_day(root, "snapshots", DAY, now=NOW)
    assert lake.row_count(root, "snapshots", other) == 1


# --- the one sanctioned rewrite ---------------------------------------------


def test_rewrite_day_replaces_the_partition_once(writer, root):
    writer.extend("snapshots", DAY, [snapshot_row(f"T{i}", 0) for i in range(10)])
    writer.flush(now=NOW)
    kept = [
        {**snapshot_row("T1", 0), "retention_class": "mover"},
        {**snapshot_row("T2", 0), "retention_class": "control"},
    ]
    stamped = [{**r, "source": "tradingview", "schema_version": SCHEMA_VERSION} for r in kept]
    lake.rewrite_day(root, "snapshots", DAY, stamped, now=NOW)
    table = lake.read_day(root, "snapshots", DAY)
    assert table.num_rows == 2
    assert set(table.column("retention_class").to_pylist()) == {"mover", "control"}


# --- size accounting ---------------------------------------------------------


def test_lake_size_reports_per_table(writer, root):
    writer.append("snapshots", DAY, snapshot_row("ABCD", 0))
    writer.append(
        "data_quality",
        DAY,
        {"date": DAY, "table_name": "snapshots", "rows_collected": 1, "written_at_utc": NOW},
    )
    writer.flush(now=NOW)
    sizes = lake.lake_size_bytes(root)
    assert set(sizes) == {"snapshots", "data_quality"}
    assert all(size > 0 for size in sizes.values())


def test_lake_size_of_missing_root_is_empty(root):
    assert lake.lake_size_bytes(root) == {}
