"""TradingView scanner snapshots — the only live view of the market this app has.

Two properties of this source shape the whole module:

* **Snapshots are ephemeral.** A TradingView row exists for the length of one
  poll and is then gone forever; unlike Alpaca bars, it cannot be refetched
  later. Losing one is permanent, so parsing is strict but never silently
  discards a row that could be salvaged (CLAUDE.md 6.1).
* **It is an unofficial endpoint.** Columns get renamed without notice. A
  renamed field must surface as a loud :class:`FieldDriftError` and mark the
  affected pillar *unknown*; it must never default to zero, because a zeroed
  float passes pillar 5 (CLAUDE.md 1.1).

The alert pipeline and the broad collector read the *same* snapshot and apply
their own thresholds locally (CLAUDE.md 5.1), so this module deliberately knows
nothing about pillars.
"""

from __future__ import annotations

import hashlib
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.timeutils import to_utc
from app.sources.retry import RetryPolicy, SourceError, call_with_retry

logger = logging.getLogger(__name__)

#: Columns requested from the scanner, in request order.
#:
#: Kept as an explicit tuple rather than built ad hoc per caller: this list is
#: the contract with an endpoint that can change under us, and
#: :func:`validate_columns` compares what came back against exactly this.
COLUMNS: tuple[str, ...] = (
    "name",
    "description",
    "exchange",
    "type",
    "typespecs",
    "close",
    "change",
    "volume",
    "average_volume_10d_calc",
    "average_volume_30d_calc",
    "relative_volume_10d_calc",
    "float_shares_outstanding",
    "total_shares_outstanding",
    "market_cap_basic",
    "sector",
    "industry",
    "premarket_close",
    "premarket_change",
    "premarket_volume",
    "postmarket_close",
    "postmarket_change",
    "postmarket_volume",
)

#: Columns without which a row is not usable at all. Everything else may be
#: missing: the affected metric becomes ``None`` and its pillar ``unknown``.
ESSENTIAL_COLUMNS: frozenset[str] = frozenset({"name", "close", "volume"})

#: Instrument types to keep. The spec excludes ETFs, funds and warrants; OTC is
#: excluded by exchange below.
STOCK_TYPES: frozenset[str] = frozenset({"stock", "dr"})

#: Exchanges considered listed US markets. Anything else (OTC, PINK, grey
#: market) is dropped: those quotes are not comparable and the strategy is not
#: defined on them.
LISTED_EXCHANGES: frozenset[str] = frozenset({"NASDAQ", "NYSE", "AMEX", "NYSE ARCA", "BATS"})

#: Type specialisations that disqualify a row even when ``type`` says "stock".
EXCLUDED_TYPESPECS: frozenset[str] = frozenset({"etf", "etn", "fund", "warrant", "right", "unit"})


class FieldDriftError(ValueError):
    """The scanner returned a column set that does not match what we asked for.

    Fatal for the poll rather than best-effort: a silently missing
    ``float_shares_outstanding`` would make every stock look like it passes the
    supply pillar.
    """


class TradingViewRow(BaseModel):
    """One validated scanner row.

    This is a boundary model (CLAUDE.md 1.1): everything downstream receives
    well-typed values or explicit ``None``. NaNs, which the scanner uses for
    "no data", are converted to ``None`` here so no arithmetic downstream ever
    silently produces a NaN that compares false against every threshold.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str = Field(min_length=1)
    exchange: str
    description: str | None = None
    instrument_type: str | None = None
    typespecs: tuple[str, ...] = ()

    close: float | None = None
    change_pct: float | None = None
    volume: float | None = None

    average_volume_10d: float | None = None
    average_volume_30d: float | None = None
    relative_volume_10d: float | None = None

    float_shares: int | None = None
    shares_outstanding: int | None = None
    market_cap: float | None = None
    sector: str | None = None
    industry: str | None = None

    premarket_close: float | None = None
    premarket_change_pct: float | None = None
    premarket_volume: float | None = None
    postmarket_close: float | None = None
    postmarket_change_pct: float | None = None
    postmarket_volume: float | None = None

    @field_validator(
        "close",
        "change_pct",
        "volume",
        "average_volume_10d",
        "average_volume_30d",
        "relative_volume_10d",
        "market_cap",
        "premarket_close",
        "premarket_change_pct",
        "premarket_volume",
        "postmarket_close",
        "postmarket_change_pct",
        "postmarket_volume",
        mode="before",
    )
    @classmethod
    def _nan_is_missing(cls, value: Any) -> Any:
        """Treat NaN as absent. A NaN threshold comparison is always false."""
        if isinstance(value, float) and math.isnan(value):
            return None
        return value

    @field_validator("float_shares", "shares_outstanding", mode="before")
    @classmethod
    def _share_count(cls, value: Any) -> Any:
        """Share counts arrive as floats; keep them whole and drop nonsense."""
        if value is None:
            return None
        if isinstance(value, float):
            if math.isnan(value) or value <= 0:
                return None
            return int(value)
        return value

    @field_validator("typespecs", mode="before")
    @classmethod
    def _typespecs(cls, value: Any) -> Any:
        if value is None:
            return ()
        if isinstance(value, list):
            return tuple(str(v) for v in value)
        return value

    def is_tradable_common_stock(self) -> bool:
        """Whether this row is a listed common stock or depositary receipt.

        ETFs, funds, warrants and OTC names are excluded here rather than in
        the query, so a change in TradingView's filter semantics cannot quietly
        widen the universe.
        """
        if self.instrument_type is not None and self.instrument_type not in STOCK_TYPES:
            return False
        if EXCLUDED_TYPESPECS & {spec.lower() for spec in self.typespecs}:
            return False
        return self.exchange.upper() in LISTED_EXCHANGES


@dataclass(frozen=True, slots=True)
class SnapshotResult:
    """One poll's worth of rows, with everything that went wrong alongside.

    The rejected counts are not diagnostics for a developer — they go straight
    into ``data_quality``, where a jump in ``unparsable`` is the first sign the
    endpoint changed shape.
    """

    poll_ts_utc: datetime
    rows: tuple[TradingViewRow, ...]
    total_matched: int
    missing_columns: tuple[str, ...] = ()
    unparsable: int = 0
    excluded_non_stock: int = 0
    errors: tuple[str, ...] = ()

    @property
    def degraded(self) -> bool:
        """Whether this poll is missing fields the pillars depend on."""
        return bool(self.missing_columns) or self.unparsable > 0


def validate_columns(returned: list[str], requested: tuple[str, ...] = COLUMNS) -> tuple[str, ...]:
    """Columns that were requested but did not come back.

    Raises when an essential one is missing: without ``close`` or ``volume``
    there is no snapshot to speak of, and continuing would write rows that look
    like a quiet market rather than a broken feed.
    """
    missing = tuple(name for name in requested if name not in set(returned))
    essential_missing = ESSENTIAL_COLUMNS & set(missing)
    if essential_missing:
        msg = (
            f"TradingView response is missing essential columns {sorted(essential_missing)}; "
            f"returned {sorted(returned)}"
        )
        raise FieldDriftError(msg)
    if missing:
        logger.error(
            "TradingView response is missing %s; affected pillars will be marked unknown",
            ", ".join(missing),
        )
    return missing


def parse_row(raw: dict[str, Any]) -> TradingViewRow:
    """Map one raw scanner record onto the validated model.

    The mapping is explicit and one-directional so a renamed upstream column
    fails here, at the boundary, with the offending name in the message.
    """
    symbol = str(raw.get("name") or "")
    exchange = str(raw.get("exchange") or "")
    if ":" in symbol:  # some responses carry "NASDAQ:ABCD" in `name`
        exchange, _, symbol = symbol.partition(":")
    return TradingViewRow(
        ticker=symbol.upper(),
        exchange=exchange.upper(),
        description=raw.get("description"),
        instrument_type=raw.get("type"),
        typespecs=raw.get("typespecs") or (),
        close=raw.get("close"),
        change_pct=raw.get("change"),
        volume=raw.get("volume"),
        average_volume_10d=raw.get("average_volume_10d_calc"),
        average_volume_30d=raw.get("average_volume_30d_calc"),
        relative_volume_10d=raw.get("relative_volume_10d_calc"),
        float_shares=raw.get("float_shares_outstanding"),
        shares_outstanding=raw.get("total_shares_outstanding"),
        market_cap=raw.get("market_cap_basic"),
        sector=raw.get("sector"),
        industry=raw.get("industry"),
        premarket_close=raw.get("premarket_close"),
        premarket_change_pct=raw.get("premarket_change"),
        premarket_volume=raw.get("premarket_volume"),
        postmarket_close=raw.get("postmarket_close"),
        postmarket_change_pct=raw.get("postmarket_change"),
        postmarket_volume=raw.get("postmarket_volume"),
    )


def parse_response(
    records: list[dict[str, Any]],
    *,
    total_matched: int,
    poll_ts_utc: datetime,
    returned_columns: list[str] | None = None,
) -> SnapshotResult:
    """Validate a whole scanner response into a :class:`SnapshotResult`.

    One unparsable row does not discard the poll: the other few hundred rows
    are ephemeral and worth keeping. The count is reported so a systematic
    parsing failure is visible in ``data_quality`` instead of looking like a
    quiet market.
    """
    missing = validate_columns(returned_columns) if returned_columns is not None else ()
    rows: list[TradingViewRow] = []
    errors: list[str] = []
    unparsable = 0
    excluded = 0
    for index, record in enumerate(records):
        try:
            row = parse_row(record)
        except (ValueError, TypeError) as exc:
            unparsable += 1
            if len(errors) < 5:  # enough to diagnose; not enough to flood a log
                errors.append(f"row {index}: {exc}")
            continue
        if not row.is_tradable_common_stock():
            excluded += 1
            continue
        rows.append(row)

    if unparsable:
        logger.error("TradingView: %s of %s rows failed validation", unparsable, len(records))
    return SnapshotResult(
        poll_ts_utc=to_utc(poll_ts_utc),
        rows=tuple(rows),
        total_matched=total_matched,
        missing_columns=missing,
        unparsable=unparsable,
        excluded_non_stock=excluded,
        errors=tuple(errors),
    )


def build_query(*, min_price: float, market: str = "america", limit: int = 20_000) -> Any:
    """Build the scanner query for the broad collector.

    Only the one hard filter of CLAUDE.md 6.2 is applied server-side (price
    floor); every other selection happens locally. That is deliberate: applying
    the loose filter here would mean re-querying whenever a threshold is tuned,
    and applying the *alert* thresholds here would destroy the denominator the
    whole lake design exists to preserve (6.0).
    """
    from tradingview_screener.column import col
    from tradingview_screener.query import Query

    return (
        Query(market)
        .select(*COLUMNS)
        .where(col("close") >= min_price)
        .order_by("volume", ascending=False)
        .limit(limit)
    )


def fetch_snapshot(
    *,
    now: datetime,
    policy: RetryPolicy,
    min_price: float,
    market: str = "america",
    limit: int = 20_000,
) -> SnapshotResult:
    """Poll the scanner once, with timeout, retry and a logged failure mode.

    Raises :class:`SourceError` when the endpoint is unusable; the caller
    records the gap in ``data_quality`` rather than writing an empty poll that
    would later read as "the market was quiet".
    """
    import requests

    query = build_query(min_price=min_price, market=market, limit=limit)

    def attempt() -> dict[str, Any]:
        raw = query.get_scanner_data_raw(
            timeout=(policy.connect_timeout_seconds, policy.timeout_seconds)
        )
        return dict(raw)

    def retryable(exc: Exception) -> bool:
        if isinstance(exc, requests.Timeout | requests.ConnectionError):
            return True
        status = getattr(getattr(exc, "response", None), "status_code", None)
        from app.sources.retry import RETRYABLE_STATUS

        return status in RETRYABLE_STATUS

    payload = call_with_retry(
        attempt,
        policy=policy,
        description="TradingView scanner poll",
        is_retryable=retryable,
    )
    records = _records_from_payload(payload)
    return parse_response(
        records,
        total_matched=int(payload.get("totalCount", len(records))),
        poll_ts_utc=now,
        returned_columns=list(COLUMNS),
    )


def _records_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Zip the scanner's positional ``d`` arrays back onto column names."""
    data = payload.get("data")
    if not isinstance(data, list):
        msg = f"TradingView payload has no 'data' list (keys: {sorted(payload)})"
        raise SourceError(msg, transient=False)
    records: list[dict[str, Any]] = []
    for item in data:
        values = item.get("d", [])
        if len(values) != len(COLUMNS):
            msg = (
                f"TradingView returned {len(values)} values for {len(COLUMNS)} requested columns; "
                "the column contract has changed"
            )
            raise FieldDriftError(msg)
        record = dict(zip(COLUMNS, values, strict=True))
        record.setdefault("name", item.get("s"))
        if item.get("s") and not record.get("name"):
            record["name"] = item["s"]
        records.append(record)
    return records


# --- mock mode ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MockProfile:
    """Shape of the synthetic market generated in ``MOCK_DATA=1`` mode."""

    ticker_count: int = 400
    runner_count: int = 6
    seed: str = "momentum-gap-scanner"


def _deterministic_unit(*parts: str) -> float:
    """A stable pseudo-random number in ``[0, 1)`` for the given parts.

    Hash-derived rather than ``random`` so a mock day is byte-for-byte
    reproducible across processes: an acceptance test that cannot be replayed
    is not much of a test.
    """
    digest = hashlib.sha256("|".join(parts).encode()).digest()
    return int.from_bytes(digest[:8], "big") / float(1 << 64)


@dataclass(slots=True)
class MockSnapshotSource:
    """Synthetic snapshots for testing the app outside market hours.

    Generates a plausible market: mostly flat names, a handful of genuine
    runners with small floats, and a few with missing float data so the
    ``unknown`` paths get exercised rather than only the happy one.
    """

    profile: MockProfile = field(default_factory=MockProfile)

    def snapshot(self, *, now: datetime) -> SnapshotResult:
        """Generate one poll of synthetic rows."""
        stamp = to_utc(now)
        minute = stamp.strftime("%Y-%m-%dT%H:%M")
        day = stamp.strftime("%Y-%m-%d")
        rows: list[TradingViewRow] = []
        for index in range(self.profile.ticker_count):
            ticker = f"MK{index:03d}"
            is_runner = index < self.profile.runner_count
            drift = _deterministic_unit(self.profile.seed, ticker, minute)
            base_price = 1.0 + 19.0 * _deterministic_unit(self.profile.seed, ticker, day)
            change = (80.0 * drift) if is_runner else (6.0 * drift - 3.0)
            volume = (5_000_000 * drift if is_runner else 120_000 * drift) + 1_000
            has_float = _deterministic_unit(self.profile.seed, ticker, "float") > 0.1
            rows.append(
                TradingViewRow(
                    ticker=ticker,
                    exchange="NASDAQ",
                    instrument_type="stock",
                    typespecs=("common",),
                    close=round(base_price * (1 + change / 100), 2),
                    change_pct=round(change, 2),
                    volume=round(volume),
                    average_volume_10d=250_000.0,
                    average_volume_30d=260_000.0,
                    relative_volume_10d=round(volume / 250_000, 2),
                    float_shares=int(2_000_000 + 40_000_000 * drift) if has_float else None,
                    shares_outstanding=60_000_000,
                    market_cap=base_price * 60_000_000,
                    sector="Health Technology" if is_runner else "Finance",
                    industry="Biotechnology" if is_runner else "Regional Banks",
                )
            )
        return SnapshotResult(
            poll_ts_utc=stamp,
            rows=tuple(rows),
            total_matched=len(rows),
        )
