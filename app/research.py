"""Research access over the lake: DuckDB queries, exports and the holdout guard.

The query box is read-only, time-limited and row-capped, but the guard that
matters most is the **holdout**. Testing fifty threshold combinations against
five hundred observations throws up two or three that look excellent purely by
chance; the defence is to hold the most recent slice of history untouched,
settle on a rule using the rest, and test it on the holdout exactly once
(CLAUDE.md 6.8).

So this module refuses queries that reach past the cutoff unless the caller
explicitly overrides, and the override is logged. It cannot stop a determined
user — nothing can — but it makes "I peeked" a deliberate act rather than an
accident.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app.storage.lake import lake_size_bytes, partition_days
from app.storage.schemas import TABLES

logger = logging.getLogger(__name__)

#: Statements the read-only connection must never see. DuckDB's own read-only
#: mode is the real guard; this is a second, clearer one that fails with a
#: message a human can act on instead of a driver error.
_FORBIDDEN = re.compile(
    r"\b(attach|copy|create|delete|drop|export|insert|install|load|pragma|update|vacuum)\b",
    re.IGNORECASE,
)

#: Date literals in a query, used to spot reads past the holdout cutoff.
_DATE_LITERAL = re.compile(r"'(\d{4}-\d{2}-\d{2})'")


class QueryRejectedError(ValueError):
    """A query was refused before it reached the database."""


@dataclass(frozen=True, slots=True)
class ResearchLimits:
    """Guardrails from ``config.yaml``."""

    timeout_seconds: int
    row_cap: int
    holdout_fraction: float


@dataclass(frozen=True, slots=True)
class TableStats:
    """What one lake table currently holds."""

    name: str
    size_bytes: int
    days: int
    first_day: date | None
    last_day: date | None

    @property
    def size_mb(self) -> float:
        """Size in megabytes, for display."""
        return self.size_bytes / 1024**2


def table_stats(root: Path) -> list[TableStats]:
    """Size and coverage per table, for the /research header."""
    sizes = lake_size_bytes(root)
    stats: list[TableStats] = []
    for name in sorted(TABLES):
        days = partition_days(root, name)
        stats.append(
            TableStats(
                name=name,
                size_bytes=sizes.get(name, 0),
                days=len(days),
                first_day=days[0] if days else None,
                last_day=days[-1] if days else None,
            )
        )
    return stats


def holdout_cutoff(days: tuple[date, ...], fraction: float) -> date | None:
    """The first day of the untouched holdout slice.

    Computed from the days actually collected rather than the calendar, so the
    cutoff moves forward as history accumulates and the holdout stays the most
    recent ``fraction`` of real observations.
    """
    if not days:
        return None
    if not 0 < fraction < 1:
        msg = f"holdout_fraction must be in (0, 1), got {fraction}"
        raise ValueError(msg)
    ordered = sorted(days)
    index = int(len(ordered) * (1 - fraction))
    index = min(max(index, 0), len(ordered) - 1)
    return ordered[index]


def dates_referenced(sql: str) -> tuple[date, ...]:
    """Date literals appearing in a query."""
    found: list[date] = []
    for match in _DATE_LITERAL.findall(sql):
        try:
            found.append(date.fromisoformat(match))
        except ValueError:
            continue
    return tuple(sorted(found))


def check_query(
    sql: str,
    *,
    cutoff: date | None,
    allow_holdout: bool = False,
) -> None:
    """Refuse a query that writes, or that reads into the holdout.

    A query with no date literal at all is allowed: most exploratory queries
    are aggregate and unfiltered, and refusing them would make the page
    unusable. The holdout guard targets the specific act of pointing at recent
    dates while tuning a rule.
    """
    statement = sql.strip().rstrip(";")
    if not statement:
        msg = "empty query"
        raise QueryRejectedError(msg)
    forbidden = _FORBIDDEN.search(statement)
    if forbidden:
        msg = f"{forbidden.group(0).upper()} is not allowed: the research connection is read-only"
        raise QueryRejectedError(msg)
    if not re.match(r"^\s*(select|with)\b", statement, re.IGNORECASE):
        msg = "only SELECT and WITH queries are allowed"
        raise QueryRejectedError(msg)

    if cutoff is None or allow_holdout:
        if allow_holdout and cutoff is not None:
            logger.warning(
                "Holdout override: query reaches past %s. Every look costs some of the "
                "holdout's value as an out-of-sample test.",
                cutoff,
            )
        return

    reaching = [day for day in dates_referenced(statement) if day >= cutoff]
    if reaching:
        msg = (
            f"query references {reaching[0]}, inside the holdout that starts {cutoff}. "
            "Settle the rule on earlier data, then test it on the holdout once — "
            "or tick the override if that is what you are doing."
        )
        raise QueryRejectedError(msg)


def lake_views(root: Path) -> dict[str, str]:
    """SQL that exposes each lake table as a DuckDB view.

    Hive-partitioned reads, so a query can filter on ``date`` without opening
    every file in the table.
    """
    return {
        name: f"read_parquet('{root / name}/**/*.parquet', hive_partitioning = true)"
        for name in sorted(TABLES)
        if (root / name).exists()
    }


def run_query(
    root: Path,
    sql: str,
    *,
    limits: ResearchLimits,
    cutoff: date | None,
    allow_holdout: bool = False,
) -> tuple[list[str], list[tuple[Any, ...]]]:
    """Run a read-only query against the lake.

    Returns ``(columns, rows)`` with at most ``row_cap`` rows. The cap is
    applied by wrapping the query rather than trusting the user to add a
    LIMIT: a stray ``SELECT *`` over a year of snapshots would otherwise hang
    the page.
    """
    import duckdb

    check_query(sql, cutoff=cutoff, allow_holdout=allow_holdout)
    views = lake_views(root)
    if not views:
        msg = "the lake is empty: nothing has been collected yet"
        raise QueryRejectedError(msg)

    connection = duckdb.connect(database=":memory:", read_only=False)
    # DuckDB has no statement_timeout setting, so the limit is enforced by
    # interrupting the connection from a timer. Without it, one careless join
    # over a year of snapshots would hold the page open indefinitely.
    watchdog = threading.Timer(limits.timeout_seconds, connection.interrupt)
    watchdog.daemon = True
    watchdog.start()
    try:
        for name, source in views.items():
            connection.execute(f"CREATE VIEW {name} AS SELECT * FROM {source}")  # noqa: S608
        wrapped = f"SELECT * FROM ({sql.strip().rstrip(';')}) AS q LIMIT {limits.row_cap}"  # noqa: S608
        cursor = connection.execute(wrapped)
        columns = [description[0] for description in cursor.description or []]
        rows = cursor.fetchall()
    except duckdb.InterruptException as exc:
        msg = f"query exceeded the {limits.timeout_seconds}s limit and was cancelled"
        raise QueryRejectedError(msg) from exc
    finally:
        watchdog.cancel()
        connection.close()

    if len(rows) >= limits.row_cap:
        logger.info("Query hit the %s-row cap; results are truncated", limits.row_cap)
    return columns, rows


def to_csv(columns: list[str], rows: list[tuple[Any, ...]]) -> str:
    """Render a result set as CSV for download."""
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    writer.writerows(rows)
    return buffer.getvalue()


CONTROL_REWEIGHT_NOTE = (
    "Rows with retention_class = 'control' are a {pct}% sample of everything that was "
    "neither a mover nor a signal. Any rate estimated from this lake must weight them by "
    "{weight:.0f}x, or the denominator is wrong and every hit rate looks far better than it is."
)


def reweight_note(control_sample_pct: int) -> str:
    """The warning shown above every /research result set."""
    from app.core.collection import control_weight

    return CONTROL_REWEIGHT_NOTE.format(
        pct=control_sample_pct, weight=control_weight(control_sample_pct)
    )


def starter_queries() -> dict[str, str]:
    """Worked examples for the query box.

    Ordered as the research roadmap of CLAUDE.md 6.8 says to work: descriptives
    before rules, single pillars before combinations.
    """
    return {
        "When do moves actually start?": (
            "SELECT date_trunc('hour', move_start_utc) AS hour_utc, count(*) AS runners\n"
            "FROM runners\nWHERE move_start_utc IS NOT NULL\n"
            "GROUP BY 1 ORDER BY 1"
        ),
        "Miss reasons over the collected history": (
            "SELECT unnest(miss_reasons) AS reason, count(*) AS n\n"
            "FROM runners\nGROUP BY 1 ORDER BY n DESC"
        ),
        "Pillar 5 low-confidence share by day": (
            "SELECT date,\n"
            "       avg(CASE WHEN float_confidence = 'low' THEN 1.0 ELSE 0 END) AS low_share\n"
            "FROM evaluations\nGROUP BY 1 ORDER BY 1"
        ),
        "Tradeable vs all outcomes": (
            "SELECT tradeable, count(*) AS n, avg(ret_30m_pct) AS avg_ret_30m\n"
            "FROM outcomes\nWHERE NOT spans_halt\nGROUP BY 1"
        ),
        "Movers vs control per day": (
            "SELECT date, retention_class, count(DISTINCT ticker) AS tickers\n"
            "FROM snapshots\nGROUP BY 1, 2 ORDER BY 1, 2"
        ),
    }


def cutoff_for(root: Path, table: str, fraction: float) -> date | None:
    """The holdout cutoff implied by one table's collected days."""
    return holdout_cutoff(partition_days(root, table), fraction)


def describe_cutoff(cutoff: date | None, *, now: datetime) -> str:
    """Human-readable holdout status for the page header."""
    if cutoff is None:
        return "No history collected yet, so no holdout has been set aside."
    days_held = (now.date() - cutoff).days
    return (
        f"Holdout starts {cutoff} ({days_held} days held back). Queries that name a date "
        "on or after it are refused unless you override."
    )
