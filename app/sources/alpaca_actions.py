"""Corporate actions and listing status from Alpaca.

Pulled daily at 20:10 for the previous day and the next five sessions, into a
tiny table kept forever. This is the biggest confound in the whole dataset: a
reverse split mechanically creates the sub-20M float the scanner looks for, and
it breaks ``prev_close``, ``gap_pct`` and every RVOL baseline on the same day
(CLAUDE.md 6.4.1). Without this table, those days look like enormous moves and
quietly poison every statistic computed from them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from app.core.integrity import CorporateAction
from app.core.timeutils import to_utc
from app.sources.alpaca import TRADING_BASE_URL, AlpacaCredentials, paginate
from app.sources.retry import RetryPolicy

logger = logging.getLogger(__name__)

ACTIONS_URL = f"{TRADING_BASE_URL}/v1/corporate-actions"

#: Alpaca's action types mapped onto the vocabulary used in the lake. Anything
#: unmapped is kept with its own name rather than dropped: an unknown action
#: type is still evidence that something happened to that ticker that day.
ACTION_TYPES: dict[str, str] = {
    "forward_split": "forward_split",
    "reverse_split": "reverse_split",
    "unit_split": "forward_split",
    "stock_dividend": "stock_dividend",
    "cash_dividend": "cash_dividend",
    "name_change": "symbol_change",
    "symbol_change": "symbol_change",
    "worthless_removal": "delisting",
    "delisting": "delisting",
}


@dataclass(frozen=True, slots=True)
class ActionsResult:
    """A day's corporate actions, plus what could not be parsed."""

    actions: tuple[CorporateAction, ...]
    unparsable: int = 0
    errors: tuple[str, ...] = ()


def _ratio_from(record: dict[str, Any]) -> float | None:
    """Derive a split ratio as new-shares-per-old-share.

    Alpaca reports ``old_rate`` and ``new_rate``: a 1:10 reverse split is
    ``old_rate=10, new_rate=1``, giving 0.1 — fewer shares at a higher price,
    which is what :func:`app.core.integrity.adjust_for_split` expects.
    """
    old_rate = record.get("old_rate")
    new_rate = record.get("new_rate")
    if old_rate in (None, 0) or new_rate is None:
        return None
    try:
        return float(new_rate) / float(old_rate)
    except (TypeError, ValueError, ZeroDivisionError):
        logger.warning("Unusable split rates old=%r new=%r", old_rate, new_rate)
        return None


def parse_action(kind: str, record: dict[str, Any]) -> CorporateAction:
    """Map one Alpaca record onto a :class:`CorporateAction`."""
    effective = record.get("ex_date") or record.get("process_date") or record.get("effective_date")
    if effective is None:
        msg = f"corporate action without an effective date: {sorted(record)}"
        raise ValueError(msg)
    symbol = record.get("symbol") or record.get("new_symbol") or record.get("old_symbol")
    if not symbol:
        msg = f"corporate action without a symbol: {sorted(record)}"
        raise ValueError(msg)
    return CorporateAction(
        ticker=str(symbol).upper(),
        effective_date=date.fromisoformat(str(effective)),
        action_type=ACTION_TYPES.get(kind, kind),
        ratio=_ratio_from(record),
        old_symbol=record.get("old_symbol"),
        new_symbol=record.get("new_symbol"),
    )


def parse_payload(payload: dict[str, Any]) -> ActionsResult:
    """Validate a whole corporate-actions response.

    One malformed record never discards the rest: the others are what stand
    between a reverse split and a -90% "move" in the research data.
    """
    container = payload.get("corporate_actions", payload)
    actions: list[CorporateAction] = []
    errors: list[str] = []
    unparsable = 0
    for kind, records in container.items():
        if not isinstance(records, list):
            continue
        for record in records:
            try:
                actions.append(parse_action(str(kind), record))
            except (ValueError, TypeError) as exc:
                unparsable += 1
                if len(errors) < 5:
                    errors.append(f"{kind}: {exc}")
    if unparsable:
        logger.error("Alpaca corporate actions: %s records failed validation", unparsable)
    return ActionsResult(actions=tuple(actions), unparsable=unparsable, errors=tuple(errors))


def fetch_actions(
    credentials: AlpacaCredentials,
    *,
    start: date,
    end: date,
    policy: RetryPolicy,
    client: httpx.Client | None = None,
) -> ActionsResult:
    """Fetch corporate actions effective between ``start`` and ``end``.

    The window deliberately reaches into the future: knowing about tomorrow's
    reverse split *before* it happens is what lets the scanner treat the next
    morning's price jump as an adjustment rather than a signal.
    """
    if end < start:
        msg = f"end ({end}) precedes start ({start})"
        raise ValueError(msg)
    owned_client = client is None
    http = client or httpx.Client(headers=credentials.headers())
    try:
        pages = paginate(
            http,
            ACTIONS_URL,
            params={
                "start": start.isoformat(),
                "end": end.isoformat(),
                "types": "reverse_split,forward_split,name_change,worthless_removal,unit_split",
                "limit": 1000,
            },
            policy=policy,
            description="Alpaca corporate actions",
        )
    finally:
        if owned_client:
            http.close()

    merged: list[CorporateAction] = []
    unparsable = 0
    errors: list[str] = []
    for page in pages:
        result = parse_payload(page)
        merged.extend(result.actions)
        unparsable += result.unparsable
        errors.extend(result.errors)
    logger.info("Fetched %s corporate actions for %s..%s", len(merged), start, end)
    return ActionsResult(tuple(merged), unparsable, tuple(errors[:5]))


def action_rows(
    actions: tuple[CorporateAction, ...], day: date, *, now: datetime
) -> list[dict[str, Any]]:
    """Build ``corporate_actions`` lake rows for one collection day."""
    return [
        {
            "ticker": action.ticker,
            "date": day,
            "effective_date": action.effective_date,
            "action_type": action.action_type,
            "ratio": action.ratio,
            "old_symbol": action.old_symbol,
            "new_symbol": action.new_symbol,
            "cash_amount": None,
            "written_at_utc": to_utc(now),
        }
        for action in actions
    ]


def default_window(
    today: date, *, lookback_days: int = 1, lookahead_days: int = 5
) -> tuple[date, date]:
    """The daily job's fetch window: yesterday through the next five sessions."""
    return today - timedelta(days=lookback_days), today + timedelta(days=lookahead_days)


# --- mock mode ---------------------------------------------------------------


def mock_actions(day: date) -> ActionsResult:
    """A synthetic 1:10 reverse split, for the acceptance test of 11.9."""
    return ActionsResult(
        actions=(
            CorporateAction(
                ticker="MK001",
                effective_date=day,
                action_type="reverse_split",
                ratio=0.1,
            ),
            CorporateAction(
                ticker="MK002",
                effective_date=day,
                action_type="symbol_change",
                old_symbol="MK002",
                new_symbol="MK002W",
            ),
        )
    )
