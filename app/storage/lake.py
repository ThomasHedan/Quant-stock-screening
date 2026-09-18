"""Buffered Parquet writer and compaction for the research lake.

Shape of the lake (CLAUDE.md 6.3a)::

    data/lake/<table>/date=YYYY-MM-DD/part-<utc-stamp>-<seq>.parquet

The writer buffers rows in memory and flushes on a fixed interval, because one
Parquet file per 30-second poll would produce tens of thousands of tiny files a
month and make every research query slow. The buffer is the only thing a crash
can lose, which bounds the loss to one flush interval — and snapshots are the
one kind of data in this system that cannot be refetched, so that bound is the
design constraint, not an implementation detail.

Two invariants the tests pin down:

* **Append-only within a day.** Flushing adds part files; it never rewrites
  one. ``compact_day`` merges a day's parts into a single file and is the only
  operation that rewrites, exactly once per day per table.
* **Past partitions are never touched.** ``compact_day`` refuses a date it was
  not explicitly given, and the retention job refuses the tables in
  ``NEVER_PRUNED``.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from app.core.timeutils import to_utc
from app.storage.schemas import (
    SCHEMA_VERSION,
    SchemaDriftError,
    schema_for,
)

logger = logging.getLogger(__name__)

#: Dictionary-encoding ticker is worth it: the column repeats a few hundred
#: distinct values across millions of rows (CLAUDE.md 6.3a).
_DICTIONARY_COLUMNS: tuple[str, ...] = ("ticker", "source", "session", "retention_class")

LakeRow = dict[str, Any]


def partition_path(root: Path, table: str, day: date) -> Path:
    """Directory holding one day of one table."""
    return root / table / f"date={day.isoformat()}"


def _part_name(now: datetime, sequence: int) -> str:
    """Name a part file after the instant it was flushed.

    Sortable by name, and the timestamp makes it obvious which flush a file
    came from when reconciling a crash.
    """
    stamp = to_utc(now).strftime("%Y%m%dT%H%M%S%f")
    return f"part-{stamp}-{sequence:04d}.parquet"


def _coerce_rows(table: str, rows: list[LakeRow], schema: pa.Schema) -> pa.Table:
    """Build an Arrow table, raising on any drift from the declared schema.

    Unknown columns and missing non-nullable columns both raise: silently
    dropping a new field loses data the source went to the trouble of sending,
    and silently nulling a required one is how a zeroed float ends up passing
    a pillar check.
    """
    declared = {f.name for f in schema}
    columns: dict[str, list[Any]] = {name: [] for name in declared}
    for index, row in enumerate(rows):
        unknown = set(row) - declared
        if unknown:
            msg = f"{table}: row {index} has undeclared columns {sorted(unknown)}"
            raise SchemaDriftError(msg)
        for name in declared:
            columns[name].append(row.get(name))

    for field_ in schema:
        if field_.nullable:
            continue
        missing = [i for i, value in enumerate(columns[field_.name]) if value is None]
        if missing:
            msg = (
                f"{table}: required column {field_.name!r} is missing in "
                f"{len(missing)} of {len(rows)} rows (first at index {missing[0]})"
            )
            raise SchemaDriftError(msg)

    try:
        return pa.Table.from_pydict(columns, schema=schema)
    except (pa.ArrowInvalid, pa.ArrowTypeError) as exc:
        msg = f"{table}: rows do not fit the declared schema: {exc}"
        raise SchemaDriftError(msg) from exc


def write_part(
    root: Path,
    table: str,
    day: date,
    rows: list[LakeRow],
    *,
    now: datetime,
    compression: str = "zstd",
    sequence: int = 0,
) -> Path | None:
    """Write one part file. Returns its path, or ``None`` for an empty batch.

    The write goes to a temporary name and is renamed into place, so a reader
    or a crash never sees a half-written part file as a valid partition member.
    """
    if not rows:
        return None
    schema = schema_for(table)
    arrow_table = _coerce_rows(table, rows, schema)
    directory = partition_path(root, table, day)
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / _part_name(now, sequence)
    staging = final.with_suffix(".parquet.tmp")
    pq.write_table(
        arrow_table,
        staging,
        compression=compression,
        use_dictionary=[c for c in _DICTIONARY_COLUMNS if c in schema.names],
        version="2.6",
    )
    staging.replace(final)
    logger.debug("Wrote %s rows to %s", len(rows), final)
    return final


def read_day(root: Path, table: str, day: date) -> pa.Table:
    """Read one day of one table, across however many part files it has."""
    schema = schema_for(table)
    directory = partition_path(root, table, day)
    parts = sorted(directory.glob("*.parquet"))
    if not parts:
        return schema.empty_table()
    return pa.concat_tables([pq.read_table(part, schema=schema) for part in parts])


def row_count(root: Path, table: str, day: date) -> int:
    """Rows stored for one table-day, read from part metadata only.

    Metadata-only so a size check over months of history does not have to
    decompress the whole lake.
    """
    directory = partition_path(root, table, day)
    return sum(pq.read_metadata(part).num_rows for part in sorted(directory.glob("*.parquet")))


def compact_day(
    root: Path,
    table: str,
    day: date,
    *,
    now: datetime,
    compression: str = "zstd",
) -> Path | None:
    """Merge a day's part files into one, preserving every row.

    Writes the merged file under a temporary name, verifies its row count
    against the sum of the parts, and only then deletes the parts. A compaction
    that loses rows must fail loudly with the originals still on disk — this is
    the one operation in the lake that can destroy data.
    """
    directory = partition_path(root, table, day)
    parts = sorted(directory.glob("*.parquet"))
    if len(parts) <= 1:
        logger.debug("Nothing to compact for %s/%s (%s parts)", table, day, len(parts))
        return parts[0] if parts else None

    expected = sum(pq.read_metadata(part).num_rows for part in parts)
    schema = schema_for(table)
    merged = pa.concat_tables([pq.read_table(part, schema=schema) for part in parts])
    if merged.num_rows != expected:
        msg = f"{table}/{day}: compaction would change row count {expected} -> {merged.num_rows}"
        raise SchemaDriftError(msg)

    staging = directory / f"compact-{to_utc(now).strftime('%Y%m%dT%H%M%S')}.parquet.tmp"
    pq.write_table(
        merged,
        staging,
        compression=compression,
        use_dictionary=[c for c in _DICTIONARY_COLUMNS if c in schema.names],
        version="2.6",
    )
    written = pq.read_metadata(staging).num_rows
    if written != expected:
        staging.unlink(missing_ok=True)
        msg = f"{table}/{day}: compacted file holds {written} rows, expected {expected}"
        raise SchemaDriftError(msg)

    final = directory / _part_name(now, 0)
    staging.replace(final)
    for part in parts:
        part.unlink()
    logger.info("Compacted %s parts into %s (%s rows)", len(parts), final, expected)
    return final


def rewrite_day(
    root: Path,
    table: str,
    day: date,
    rows: list[LakeRow],
    *,
    now: datetime,
    compression: str = "zstd",
) -> Path | None:
    """Replace a day's partition wholesale. Used only by the pruning job.

    The single sanctioned rewrite in the lake (CLAUDE.md 6.3a): the 20:45 job
    rewrites that day's ``snapshots`` partition exactly once and never again.
    The new partition is staged in full before anything is removed, so an
    interruption leaves the old partition intact.
    """
    directory = partition_path(root, table, day)
    old_parts = sorted(directory.glob("*.parquet"))
    new_part = write_part(root, table, day, rows, now=now, compression=compression, sequence=9999)
    for part in old_parts:
        part.unlink()
    logger.info(
        "Rewrote %s/%s: %s parts replaced by %s rows", table, day, len(old_parts), len(rows)
    )
    return new_part


def table_size_bytes(root: Path, table: str) -> int:
    """Bytes on disk for one table, across every partition."""
    directory = root / table
    if not directory.exists():
        return 0
    return sum(f.stat().st_size for f in directory.rglob("*.parquet"))


def lake_size_bytes(root: Path) -> dict[str, int]:
    """Bytes on disk per table, for the size guardrail on /research."""
    if not root.exists():
        return {}
    return {
        child.name: table_size_bytes(root, child.name)
        for child in sorted(root.iterdir())
        if child.is_dir()
    }


def partition_days(root: Path, table: str) -> tuple[date, ...]:
    """Every day present for a table, ascending."""
    directory = root / table
    if not directory.exists():
        return ()
    days = []
    for child in directory.iterdir():
        if child.is_dir() and child.name.startswith("date="):
            days.append(date.fromisoformat(child.name.removeprefix("date=")))
    return tuple(sorted(days))


@dataclass(slots=True)
class LakeWriter:
    """In-memory row buffer that flushes whole table-days to Parquet.

    The caller drives flushing (on the 5-minute cadence of §6.3a, and on
    shutdown). Nothing here reads the clock: ``now`` is passed in, which is
    also what lets the tests exercise a crash between two flushes.
    """

    root: Path
    compression: str = "zstd"
    source: str = "unknown"
    _buffer: dict[tuple[str, date], list[LakeRow]] = field(
        default_factory=lambda: defaultdict(list)
    )
    _sequence: int = 0

    def append(self, table: str, day: date, row: LakeRow, *, source: str | None = None) -> None:
        """Buffer one row, stamping provenance if the caller has not.

        Validates the table name immediately so a typo surfaces at the call
        site rather than five minutes later inside a flush.
        """
        schema_for(table)
        enriched = dict(row)
        enriched.setdefault("source", source or self.source)
        enriched.setdefault("schema_version", SCHEMA_VERSION)
        self._buffer[(table, day)].append(enriched)

    def extend(
        self, table: str, day: date, rows: list[LakeRow], *, source: str | None = None
    ) -> None:
        """Buffer many rows at once."""
        for row in rows:
            self.append(table, day, row, source=source)

    def pending(self, table: str | None = None) -> int:
        """Rows currently buffered, optionally for one table."""
        return sum(
            len(rows)
            for (name, _day), rows in self._buffer.items()
            if table is None or name == table
        )

    def flush(self, *, now: datetime) -> list[Path]:
        """Write every buffered table-day and clear the buffer.

        A table-day that fails validation keeps its rows buffered and re-raises:
        dropping them would silently lose the only copy of an ephemeral
        TradingView snapshot.
        """
        written: list[Path] = []
        for key in sorted(self._buffer, key=lambda k: (k[0], k[1])):
            table, day = key
            rows = self._buffer[key]
            if not rows:
                continue
            self._sequence += 1
            path = write_part(
                self.root,
                table,
                day,
                rows,
                now=now,
                compression=self.compression,
                sequence=self._sequence,
            )
            if path is not None:
                written.append(path)
            self._buffer[key] = []
        self._buffer = defaultdict(list, {k: v for k, v in self._buffer.items() if v})
        if written:
            logger.info("Flushed %s part files to %s", len(written), self.root)
        return written

    def flush_if_due(
        self, *, now: datetime, last_flush: datetime, interval_seconds: int
    ) -> list[Path]:
        """Flush only when the interval has elapsed. Returns written paths."""
        elapsed = (to_utc(now) - to_utc(last_flush)).total_seconds()
        if elapsed < interval_seconds:
            return []
        return self.flush(now=now)


def fsync_directory(path: Path) -> None:
    """Flush a directory entry to disk.

    Called after a flush on the shutdown path: ``rename`` is atomic but the
    directory entry itself can still be in the page cache when the machine
    loses power.
    """
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
