"""Alpaca 1-minute bars: batched, delayed, and scoped.

Three constraints from the free tier shape this module (CLAUDE.md 3, 6.5):

* **Only data older than 15 minutes.** Asking for anything fresher returns a
  subscription error, so requests are clamped and the clamp is logged rather
  than left for a confusing 403.
* **Multi-symbol requests.** One request per ticker for 300 tickers would blow
  through any rate limit; the API takes a comma-separated list, so symbols are
  batched.
* **Extended hours must be requested explicitly.** The whole strategy lives in
  pre- and post-market, so a default request that silently returned only
  regular-hours bars would make every pre-market RVOL baseline wrong. The
  ``feed`` and time bounds are therefore always explicit.

Bars are refetchable at any later date, which is what makes them Tier 2: a
failed fetch is a deferral, not a loss.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from app.core.moves import Bar
from app.core.timeutils import et_datetime, to_utc
from app.sources.alpaca import (
    DATA_BASE_URL,
    FREE_TIER_DELAY_MINUTES,
    AlpacaCredentials,
    paginate,
)
from app.sources.retry import RetryPolicy

logger = logging.getLogger(__name__)

BARS_URL = f"{DATA_BASE_URL}/v2/stocks/bars"

#: Symbols per request. Alpaca accepts more, but a shorter list keeps one
#: failure from costing the whole batch and keeps URLs well under any limit.
BATCH_SIZE = 100


@dataclass(frozen=True, slots=True)
class BarsResult:
    """Bars per ticker, plus what did not come back."""

    bars: dict[str, tuple[Bar, ...]]
    requested: tuple[str, ...]
    unparsable: int = 0
    errors: tuple[str, ...] = ()

    @property
    def missing(self) -> tuple[str, ...]:
        """Requested tickers that returned no bars at all.

        Not an error by itself — a stock can genuinely not trade for a whole
        session — but the count belongs in ``data_quality``, because a sudden
        jump means the request shape changed rather than the market went quiet.
        """
        return tuple(sorted(set(self.requested) - set(self.bars)))


def clamp_end(end: datetime, *, now: datetime) -> datetime:
    """Pull ``end`` back behind the free tier's delay if needed."""
    latest = to_utc(now) - timedelta(minutes=FREE_TIER_DELAY_MINUTES)
    if to_utc(end) <= latest:
        return to_utc(end)
    logger.info(
        "Clamping bar request end from %s to %s (free tier serves data older than %s minutes)",
        to_utc(end).isoformat(),
        latest.isoformat(),
        FREE_TIER_DELAY_MINUTES,
    )
    return latest


def batches(symbols: tuple[str, ...], size: int = BATCH_SIZE) -> list[tuple[str, ...]]:
    """Split symbols into request-sized batches."""
    if size <= 0:
        msg = f"batch size must be positive, got {size}"
        raise ValueError(msg)
    return [tuple(symbols[i : i + size]) for i in range(0, len(symbols), size)]


def parse_bar(raw: dict[str, Any]) -> Bar:
    """Validate one Alpaca bar record.

    Alpaca's field names are single letters; mapping them here, once, means a
    renamed field raises at the boundary instead of producing a bar full of
    ``None`` that silently flattens a move.
    """
    required = ("t", "o", "h", "l", "c", "v")
    missing = [key for key in required if raw.get(key) is None]
    if missing:
        msg = f"bar record missing {missing}"
        raise ValueError(msg)
    stamp = str(raw["t"]).replace("Z", "+00:00")
    return Bar(
        minute=to_utc(datetime.fromisoformat(stamp)),
        open=float(raw["o"]),
        high=float(raw["h"]),
        low=float(raw["l"]),
        close=float(raw["c"]),
        volume=float(raw["v"]),
        vwap=float(raw["vw"]) if raw.get("vw") is not None else None,
        trade_count=int(raw["n"]) if raw.get("n") is not None else None,
    )


def parse_pages(pages: list[dict[str, Any]], requested: tuple[str, ...]) -> BarsResult:
    """Merge paged responses into bars per ticker."""
    collected: dict[str, list[Bar]] = {}
    errors: list[str] = []
    unparsable = 0
    for page in pages:
        for symbol, records in (page.get("bars") or {}).items():
            for raw in records or []:
                try:
                    collected.setdefault(str(symbol).upper(), []).append(parse_bar(raw))
                except (ValueError, TypeError, KeyError) as exc:
                    unparsable += 1
                    if len(errors) < 5:
                        errors.append(f"{symbol}: {exc}")
    if unparsable:
        logger.error("Alpaca bars: %s records failed validation", unparsable)
    return BarsResult(
        bars={
            ticker: tuple(sorted(bars, key=lambda bar: bar.minute))
            for ticker, bars in collected.items()
        },
        requested=requested,
        unparsable=unparsable,
        errors=tuple(errors),
    )


def fetch_bars(
    credentials: AlpacaCredentials,
    symbols: tuple[str, ...],
    *,
    start: datetime,
    end: datetime,
    now: datetime,
    policy: RetryPolicy,
    client: httpx.Client | None = None,
    feed: str = "iex",
) -> BarsResult:
    """Fetch 1-minute bars for many symbols over one time range.

    ``start`` and ``end`` are explicit UTC instants covering extended hours;
    the caller builds them from ET session bounds. ``end`` is clamped behind
    the free tier's delay.
    """
    if not symbols:
        return BarsResult(bars={}, requested=())
    clamped_end = clamp_end(end, now=now)
    if clamped_end <= to_utc(start):
        logger.warning(
            "Bar request window collapsed after clamping (%s..%s); nothing to fetch",
            to_utc(start).isoformat(),
            clamped_end.isoformat(),
        )
        return BarsResult(bars={}, requested=symbols)

    owned = client is None
    http = client or httpx.Client(headers=credentials.headers())
    pages: list[dict[str, Any]] = []
    try:
        for batch in batches(symbols):
            pages.extend(
                paginate(
                    http,
                    BARS_URL,
                    params={
                        "symbols": ",".join(batch),
                        "timeframe": "1Min",
                        "start": to_utc(start).isoformat(),
                        "end": clamped_end.isoformat(),
                        "limit": 10000,
                        "adjustment": "split",
                        "feed": feed,
                    },
                    policy=policy,
                    description=f"Alpaca bars ({len(batch)} symbols)",
                )
            )
    finally:
        if owned:
            http.close()

    result = parse_pages(pages, symbols)
    logger.info(
        "Fetched bars for %s of %s requested tickers", len(result.bars), len(result.requested)
    )
    return result


def session_bounds_utc(day: date, *, start_et: Any, end_et: Any) -> tuple[datetime, datetime]:
    """The UTC instants of an ET session window on ``day``."""
    return et_datetime(day, start_et), et_datetime(day, end_et)


def bar_rows(
    ticker: str, day: date, bars: tuple[Bar, ...], *, now: datetime, resolution_minutes: int = 1
) -> list[dict[str, Any]]:
    """Build ``bars_1m`` lake rows."""
    return [
        {
            "ticker": ticker,
            "date": day,
            "minute_utc": to_utc(bar.minute),
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
            "trade_count": bar.trade_count,
            "vwap": bar.vwap,
            "resolution_minutes": resolution_minutes,
            "written_at_utc": to_utc(now),
        }
        for bar in bars
    ]


# --- mock mode ---------------------------------------------------------------


def mock_bars(
    ticker: str,
    day: date,
    *,
    start_et: Any,
    minutes: int = 240,
    open_price: float = 4.00,
    peak_multiple: float = 1.8,
) -> tuple[Bar, ...]:
    """A synthetic intraday run: rise to a peak, then fade.

    Shaped rather than random so the acceptance tests can assert specific
    outcomes — a runner that peaks and fades exercises MFE, MAE and fade_pct
    in one series.
    """
    start = et_datetime(day, start_et)
    peak_at = minutes // 3
    bars: list[Bar] = []
    for index in range(minutes):
        if index <= peak_at:
            factor = 1 + (peak_multiple - 1) * (index / max(peak_at, 1))
        else:
            decay = (index - peak_at) / max(minutes - peak_at, 1)
            factor = peak_multiple - (peak_multiple - 1.1) * decay
        close = round(open_price * factor, 2)
        bars.append(
            Bar(
                minute=start + timedelta(minutes=index),
                open=round(close * 0.995, 2),
                high=round(close * 1.01, 2),
                low=round(close * 0.99, 2),
                close=close,
                volume=50_000.0 if index <= peak_at else 20_000.0,
                vwap=close,
                trade_count=200,
            )
        )
    return tuple(bars)
