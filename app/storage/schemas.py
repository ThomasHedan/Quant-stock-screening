"""Arrow schemas for every lake table, in one place.

Two reasons this is a module of its own rather than literals scattered through
the writer:

* **Schema drift must fail loudly** (CLAUDE.md 1.1). A TradingView field that
  disappears or gets renamed has to raise here, at the boundary, instead of
  arriving downstream as a silent ``None`` that a threshold check then reads as
  zero.
* The lake is meant to be queried by DuckDB months from now, by someone
  reconstructing what a row meant. One file listing every column and its type
  is the documentation that query needs.

Every table carries ``schema_version``, ``source`` and a write timestamp so a
row can always be traced back to what produced it and when.
"""

from __future__ import annotations

from typing import Final

import pyarrow as pa

#: Bumped whenever a column's meaning changes. Adding a nullable column does
#: not require a bump; changing how an existing one is computed does, because
#: research queries spanning the change have to be able to tell the two apart.
SCHEMA_VERSION: Final[int] = 1

_TS = pa.timestamp("us", tz="UTC")

#: Columns every row in the lake carries.
_PROVENANCE: Final[list[pa.Field]] = [
    pa.field("source", pa.string(), nullable=False),
    pa.field("schema_version", pa.int32(), nullable=False),
]


def _table(*fields: pa.Field) -> pa.Schema:
    """Build a schema with the provenance columns appended."""
    return pa.schema([*fields, *_PROVENANCE])


#: Tier 0 — one row per (ticker, date, session) for every listed common stock.
#: Never pruned, never thinned: this is the denominator for every future
#: statistic, and the only thing that makes "what did I miss, and why"
#: answerable for a stock the collector never looked at (CLAUDE.md 6.1).
DAILY_UNIVERSE = _table(
    pa.field("ticker", pa.string(), nullable=False),
    pa.field("date", pa.date32(), nullable=False),
    pa.field("session", pa.string(), nullable=False),
    pa.field("open", pa.float64()),
    pa.field("high", pa.float64()),
    pa.field("low", pa.float64()),
    pa.field("close", pa.float64()),
    pa.field("volume", pa.float64()),
    pa.field("vwap", pa.float64()),
    pa.field("prev_close", pa.float64()),
    pa.field("float_shares", pa.int64()),
    pa.field("shares_outstanding", pa.int64()),
    pa.field("market_cap", pa.float64()),
    pa.field("sector", pa.string()),
    # Integrity flags (6.4): a reverse split mechanically creates a sub-20M
    # float, so a split day must never be mistaken for a move.
    pa.field("split_flag", pa.bool_()),
    pa.field("split_ratio", pa.float64()),
    pa.field("days_since_reverse_split", pa.int32()),
    pa.field("halt_count", pa.int32()),
    pa.field("halt_minutes", pa.float64()),
    pa.field("suspect_price", pa.bool_()),
    pa.field("ticker_canonical_id", pa.string()),
    pa.field("written_at_utc", _TS, nullable=False),
)

#: Tier 1 — intraday snapshots, fast-moving fields only. Slow fields live in
#: ``reference`` and join on (ticker, date), which is what keeps this table
#: small enough to keep 18 months of it (CLAUDE.md 6.3a).
SNAPSHOTS = _table(
    pa.field("ticker", pa.string(), nullable=False),
    pa.field("date", pa.date32(), nullable=False),
    pa.field("poll_ts_utc", _TS, nullable=False),
    pa.field("price", pa.float64()),
    pa.field("session_volume", pa.float64()),
    pa.field("gap_pct", pa.float64()),
    pa.field("window_change_pct", pa.float64()),
    pa.field("window_volume", pa.float64()),
    pa.field("rvol", pa.float64()),
    pa.field("rvol_source", pa.string()),
    pa.field("dollar_volume", pa.float64()),
    pa.field("bid", pa.float64()),
    pa.field("ask", pa.float64()),
    pa.field("spread_pct", pa.float64()),
    pa.field("spread_source", pa.string()),
    pa.field("halt_status", pa.string()),
    pa.field("inferred_halt", pa.bool_()),
    pa.field("suspect_price", pa.bool_()),
    # Written by the 20:45 pruning job, which rewrites this partition once.
    pa.field("retention_class", pa.string()),
    pa.field("written_at_utc", _TS, nullable=False),
)

#: Slow-moving fields, written once per ticker per day plus on intraday change.
#: Point-in-time: float and averages are stored as they were seen that day and
#: are never backfilled over (CLAUDE.md 6.3a).
REFERENCE = _table(
    pa.field("ticker", pa.string(), nullable=False),
    pa.field("date", pa.date32(), nullable=False),
    pa.field("asof_ts_utc", _TS, nullable=False),
    pa.field("sector", pa.string()),
    pa.field("industry", pa.string()),
    pa.field("float_shares_outstanding", pa.int64()),
    pa.field("total_shares_outstanding", pa.int64()),
    pa.field("average_volume_10d_calc", pa.float64()),
    pa.field("average_volume_30d_calc", pa.float64()),
    pa.field("market_cap", pa.float64()),
    pa.field("float_source", pa.string()),
    pa.field("float_asof", _TS),
    pa.field("float_confidence", pa.string()),
    pa.field("float_turnover", pa.float64()),
    pa.field("written_at_utc", _TS, nullable=False),
)

#: One row per ticker dropped by the nightly pruning, with its final session
#: metrics. Dropped tickers are never silently deleted: the count and
#: distribution of what was thrown away stays knowable (CLAUDE.md 6.1).
PRUNED_SUMMARY = _table(
    pa.field("ticker", pa.string(), nullable=False),
    pa.field("date", pa.date32(), nullable=False),
    pa.field("poll_count", pa.int32(), nullable=False),
    pa.field("first_poll_ts_utc", _TS),
    pa.field("last_poll_ts_utc", _TS),
    pa.field("session_high", pa.float64()),
    pa.field("session_low", pa.float64()),
    pa.field("last_price", pa.float64()),
    pa.field("session_volume", pa.float64()),
    pa.field("max_gap_pct", pa.float64()),
    pa.field("max_rvol", pa.float64()),
    pa.field("up_move_pct", pa.float64()),
    pa.field("down_move_pct", pa.float64()),
    pa.field("best_tier", pa.string()),
    pa.field("drop_reason", pa.string(), nullable=False),
    pa.field("written_at_utc", _TS, nullable=False),
)

#: Every pillar evaluation, not only the ones that alerted. This is what makes
#: the missed-runner analysis and all future research possible (CLAUDE.md 5.6).
EVALUATIONS = _table(
    pa.field("ticker", pa.string(), nullable=False),
    pa.field("date", pa.date32(), nullable=False),
    pa.field("poll_ts_utc", _TS, nullable=False),
    pa.field("window_start_utc", _TS, nullable=False),
    pa.field("price", pa.float64()),
    pa.field("gap_pct", pa.float64()),
    pa.field("window_change_pct", pa.float64()),
    pa.field("rvol", pa.float64()),
    pa.field("rvol_source", pa.string()),
    pa.field("float_shares", pa.int64()),
    pa.field("float_confidence", pa.string()),
    pa.field("news_age_minutes", pa.float64()),
    pa.field("news_id", pa.string()),
    # Raw value, threshold and verdict per pillar, so a past evaluation can be
    # re-judged under a changed threshold without refetching anything.
    pa.field("pillar_1_status", pa.string(), nullable=False),
    pa.field("pillar_2_status", pa.string(), nullable=False),
    pa.field("pillar_3_status", pa.string(), nullable=False),
    pa.field("pillar_4_status", pa.string(), nullable=False),
    pa.field("pillar_5_status", pa.string(), nullable=False),
    pa.field("pillars_passed", pa.int32(), nullable=False),
    pa.field("pillars_unknown", pa.int32(), nullable=False),
    pa.field("tier", pa.string(), nullable=False),
    pa.field("rank_score", pa.float64()),
    pa.field("is_recent_runner", pa.bool_()),
    pa.field("pushed", pa.bool_()),
    pa.field("push_reason", pa.string()),
    pa.field("written_at_utc", _TS, nullable=False),
)

#: News as received. ``received_at_utc`` is kept alongside ``created_at_utc``
#: so feed latency is measurable; ``updated_at_utc`` is stored but must never
#: be used for freshness (CLAUDE.md 6.4.6).
NEWS = _table(
    pa.field("news_id", pa.string(), nullable=False),
    pa.field("date", pa.date32(), nullable=False),
    pa.field("symbols", pa.list_(pa.string()), nullable=False),
    pa.field("headline", pa.string(), nullable=False),
    pa.field("news_source", pa.string(), nullable=False),
    pa.field("url", pa.string()),
    pa.field("created_at_utc", _TS, nullable=False),
    pa.field("updated_at_utc", _TS),
    pa.field("received_at_utc", _TS, nullable=False),
    pa.field("feed_latency_s", pa.float64()),
    pa.field("written_at_utc", _TS, nullable=False),
)

#: Tier 2 — scoped 1-minute bars. Always refetchable from Alpaca, so losing
#: these is never permanent; snapshots are the opposite (CLAUDE.md 6.1).
BARS_1M = _table(
    pa.field("ticker", pa.string(), nullable=False),
    pa.field("date", pa.date32(), nullable=False),
    pa.field("minute_utc", _TS, nullable=False),
    pa.field("open", pa.float64()),
    pa.field("high", pa.float64()),
    pa.field("low", pa.float64()),
    pa.field("close", pa.float64()),
    pa.field("volume", pa.float64()),
    pa.field("trade_count", pa.int64()),
    pa.field("vwap", pa.float64()),
    pa.field("resolution_minutes", pa.int32(), nullable=False),
    pa.field("written_at_utc", _TS, nullable=False),
)

#: Forward returns and excursions per (ticker, date, reference_time), each row
#: carrying its own tradability verdict so research can report tradeable and
#: all-rows figures side by side (CLAUDE.md 6.4.5).
OUTCOMES = _table(
    pa.field("ticker", pa.string(), nullable=False),
    pa.field("date", pa.date32(), nullable=False),
    pa.field("reference_ts_utc", _TS, nullable=False),
    pa.field("reference_price", pa.float64()),
    pa.field("ret_5m_pct", pa.float64()),
    pa.field("ret_15m_pct", pa.float64()),
    pa.field("ret_30m_pct", pa.float64()),
    pa.field("ret_60m_pct", pa.float64()),
    pa.field("ret_to_open_pct", pa.float64()),
    pa.field("ret_to_1100_pct", pa.float64()),
    pa.field("ret_to_close_pct", pa.float64()),
    pa.field("mfe_pct", pa.float64()),
    pa.field("mae_pct", pa.float64()),
    pa.field("minutes_to_high", pa.float64()),
    pa.field("high_of_day_pct", pa.float64()),
    pa.field("held_above_vwap_1000", pa.bool_()),
    pa.field("dollar_volume_in_window", pa.float64()),
    pa.field("est_spread_pct", pa.float64()),
    pa.field("tradeable", pa.bool_()),
    pa.field("spans_halt", pa.bool_()),
    pa.field("written_at_utc", _TS, nullable=False),
)

#: The day's runners and why each was or was not caught (CLAUDE.md 7.2).
RUNNERS = _table(
    pa.field("ticker", pa.string(), nullable=False),
    pa.field("date", pa.date32(), nullable=False),
    pa.field("high_of_day_pct", pa.float64()),
    pa.field("move_start_utc", _TS),
    pa.field("first_news_utc", _TS),
    pa.field("news_lag_minutes", pa.float64()),
    pa.field("best_tier", pa.string()),
    pa.field("best_tier_ts_utc", _TS),
    pa.field("price_at_move_start", pa.float64()),
    pa.field("float_shares", pa.int64()),
    pa.field("dollar_volume", pa.float64()),
    pa.field("miss_reasons", pa.list_(pa.string()), nullable=False),
    pa.field("miss_detail", pa.string()),
    pa.field("qualifying_rule", pa.string(), nullable=False),
    pa.field("written_at_utc", _TS, nullable=False),
)

#: Splits, symbol changes and delistings. Tiny, kept forever, and the only
#: thing standing between a 1:10 reverse split and a -90% "move".
CORPORATE_ACTIONS = _table(
    pa.field("ticker", pa.string(), nullable=False),
    pa.field("date", pa.date32(), nullable=False),
    pa.field("effective_date", pa.date32(), nullable=False),
    pa.field("action_type", pa.string(), nullable=False),
    pa.field("ratio", pa.float64()),
    pa.field("old_symbol", pa.string()),
    pa.field("new_symbol", pa.string()),
    pa.field("cash_amount", pa.float64()),
    pa.field("written_at_utc", _TS, nullable=False),
)

#: first_seen / last_seen per ticker. A ticker that stops appearing has its
#: last_seen set; nothing is ever deleted, because in this population names
#: delist constantly and survivorship bias is the result (CLAUDE.md 6.4.2).
LISTING_STATUS = _table(
    pa.field("ticker", pa.string(), nullable=False),
    pa.field("date", pa.date32(), nullable=False),
    pa.field("first_seen", pa.date32(), nullable=False),
    pa.field("last_seen", pa.date32(), nullable=False),
    pa.field("status", pa.string(), nullable=False),
    pa.field("ticker_canonical_id", pa.string()),
    pa.field("written_at_utc", _TS, nullable=False),
)

#: The trader's own context, exported nightly from SQLite. Never used to
#: auto-tune thresholds: it is evidence, not a training signal (CLAUDE.md 7.5).
JOURNAL = _table(
    pa.field("ticker", pa.string(), nullable=False),
    pa.field("date", pa.date32(), nullable=False),
    pa.field("alert_id", pa.string()),
    pa.field("action", pa.string(), nullable=False),
    pa.field("entry", pa.float64()),
    pa.field("exit", pa.float64()),
    pa.field("size", pa.float64()),
    pa.field("note", pa.string()),
    pa.field("tags", pa.list_(pa.string())),
    pa.field("created_at_utc", _TS, nullable=False),
    pa.field("written_at_utc", _TS, nullable=False),
)

#: One row per table per day, plus the integrity metrics of 6.4. Drift in these
#: is a louder signal than a missed alert.
DATA_QUALITY = _table(
    pa.field("date", pa.date32(), nullable=False),
    pa.field("table_name", pa.string(), nullable=False),
    pa.field("rows_collected", pa.int64(), nullable=False),
    pa.field("polls_expected", pa.int32()),
    pa.field("polls_completed", pa.int32()),
    pa.field("missing_field_count", pa.int64()),
    pa.field("api_errors", pa.int32()),
    pa.field("ws_disconnect_minutes", pa.float64()),
    pa.field("suspect_price_rows", pa.int32()),
    pa.field("corporate_actions_applied", pa.int32()),
    pa.field("rvol_baselines_recomputed", pa.int32()),
    pa.field("halt_count", pa.int32()),
    pa.field("inferred_halt_count", pa.int32()),
    pa.field("pillar5_low_confidence_share", pa.float64()),
    pa.field("news_latency_median_s", pa.float64()),
    pa.field("news_latency_p95_s", pa.float64()),
    pa.field("tradeable_share", pa.float64()),
    pa.field("mover_count", pa.int32()),
    pa.field("control_count", pa.int32()),
    pa.field("dropped_count", pa.int32()),
    pa.field("move_threshold_pct", pa.float64()),
    pa.field("note", pa.string()),
    pa.field("written_at_utc", _TS, nullable=False),
)

#: Table name -> schema. The writer refuses any name absent from this map, so
#: a typo creates a loud error rather than a stray partition nobody queries.
TABLES: Final[dict[str, pa.Schema]] = {
    "daily_universe": DAILY_UNIVERSE,
    "snapshots": SNAPSHOTS,
    "reference": REFERENCE,
    "pruned_summary": PRUNED_SUMMARY,
    "evaluations": EVALUATIONS,
    "news": NEWS,
    "bars_1m": BARS_1M,
    "outcomes": OUTCOMES,
    "runners": RUNNERS,
    "corporate_actions": CORPORATE_ACTIONS,
    "listing_status": LISTING_STATUS,
    "journal": JOURNAL,
    "data_quality": DATA_QUALITY,
}

#: Tables the retention job must never touch, whatever the lake size says
#: (CLAUDE.md 6.3b). Kept here, next to the schemas, so the list cannot drift
#: away from the tables it protects.
NEVER_PRUNED: Final[frozenset[str]] = frozenset(
    {
        "daily_universe",
        "evaluations",
        "outcomes",
        "runners",
        "news",
        "pruned_summary",
        "corporate_actions",
        "listing_status",
        "journal",
        "data_quality",
    }
)


class UnknownTableError(KeyError):
    """Raised when a caller names a table the lake does not define."""


class SchemaDriftError(ValueError):
    """Raised when incoming data does not match the table's declared schema.

    Deliberately fatal for the row in question: the alternative is defaulting a
    missing field to zero, and a zeroed float passes pillar 5 (CLAUDE.md 1.1).
    """


def schema_for(table: str) -> pa.Schema:
    """The schema of ``table``, or raise :class:`UnknownTableError`."""
    try:
        return TABLES[table]
    except KeyError as exc:
        msg = f"unknown lake table {table!r}; known tables: {sorted(TABLES)}"
        raise UnknownTableError(msg) from exc


def required_columns(table: str) -> tuple[str, ...]:
    """Non-nullable columns of ``table``, which every row must supply."""
    schema = schema_for(table)
    return tuple(field.name for field in schema if not field.nullable)
