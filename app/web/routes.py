"""HTTP routes.

Server-rendered Jinja templates with HTMX polling for the live table — no JS
build step (CLAUDE.md 3). The rule the routes follow throughout: **the UI
states what is known and what is not.** A pillar with no data renders ``?``, a
figure from the fallback says so, and a stale status dot shows its age. A
screen that quietly rounds uncertainty away would be worse than no screen,
because the trader would act on it.

Nothing here places an order, and nothing here gives advice.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app.alerts import Evaluation
from app.core.timeutils import UTC, to_et, to_utc
from app.core.types import PillarStatus, Tier
from app.notify import push
from app.runtime import RuntimeState
from app.storage import db

logger = logging.getLogger(__name__)

router = APIRouter()

#: Set by :func:`configure` at app startup.
_templates: Jinja2Templates | None = None


def configure(templates: Jinja2Templates) -> None:
    """Install the template environment and its filters."""
    global _templates
    templates.env.filters["et"] = lambda value: to_et(value).strftime("%H:%M:%S") if value else "—"
    templates.env.filters["pillar"] = _pillar_glyph
    templates.env.filters["num"] = _number
    templates.env.filters["pct"] = _percent
    _templates = templates


def templates() -> Jinja2Templates:
    """The configured template environment."""
    if _templates is None:
        msg = "templates are not configured; call app.web.routes.configure() first"
        raise RuntimeError(msg)
    return _templates


def state_of(request: Request) -> RuntimeState:
    """The runtime state attached to the app."""
    state: RuntimeState | None = getattr(request.app.state, "runtime", None)
    if state is None:
        raise HTTPException(status_code=503, detail="Application is still starting")
    return state


# --- display helpers ---------------------------------------------------------


def _pillar_glyph(status: PillarStatus | str) -> str:
    """Render a pillar verdict.

    Unknown is ``?`` and never a cross: the difference between "this failed"
    and "we could not tell" is the difference between a rule and a data gap.
    """
    value = status.value if isinstance(status, PillarStatus) else str(status)
    return {"pass": "●", "fail": "○", "unknown": "?"}.get(value, "?")


def _number(value: float | int | None, digits: int = 2) -> str:
    """Format a number, or an em dash when it is missing."""
    if value is None:
        return "—"
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if abs(value) >= 1_000:
        return f"{value / 1_000:.1f}k"
    return f"{value:.{digits}f}"


def _percent(value: float | None, digits: int = 1) -> str:
    """Format a percentage with an explicit sign."""
    return "—" if value is None else f"{value:+.{digits}f}%"


def evaluation_view(evaluation: Evaluation) -> dict[str, Any]:
    """Flatten an evaluation into what the template needs."""
    return {
        "ticker": evaluation.ticker,
        "price": evaluation.price,
        "gap_pct": evaluation.gap_pct,
        "window_change_pct": evaluation.window_change_pct,
        "rvol": evaluation.rvol,
        "rvol_source": evaluation.rvol_source.value,
        "float_shares": evaluation.float_shares,
        "float_confidence": evaluation.float_confidence.value,
        "pillars": [
            {"number": result.number, "status": result.status.value, "detail": result.detail}
            for result in evaluation.score.results
        ],
        "tier": evaluation.tier.value,
        "rank_score": evaluation.rank_score,
        "is_recent_runner": evaluation.is_recent_runner,
        "headline": evaluation.news.headline if evaluation.news else None,
        "news_url": evaluation.news.url if evaluation.news else None,
        "pushed": evaluation.pushed,
    }


def header_context(state: RuntimeState, *, now: datetime) -> dict[str, Any]:
    """Clocks, status dots and the next-window countdown."""
    display_zone = ZoneInfo(state.config.timezones.display)
    upcoming = state.next_windows(now=now, count=3)
    next_start = upcoming[0][0] if upcoming else None
    return {
        "now_et": to_et(now).strftime("%H:%M:%S"),
        "now_local": to_utc(now).astimezone(display_zone).strftime("%H:%M:%S"),
        "display_tz": state.config.timezones.display,
        "next_window_et": to_et(next_start).strftime("%a %H:%M") if next_start else None,
        "next_window_in_minutes": (
            (to_utc(next_start) - to_utc(now)).total_seconds() / 60 if next_start else None
        ),
        "statuses": [
            {
                "name": status.name,
                "healthy": status.healthy,
                "detail": status.detail,
                "age_seconds": status.age_seconds(now=now),
            }
            for status in state.statuses(now=now)
        ],
        "warnings": list(state.warnings),
    }


# --- pages -------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
def live_page(request: Request) -> HTMLResponse:
    """The Live page: the current window's table, or the last one seen."""
    state = state_of(request)
    now = datetime.now(tz=UTC)
    rows = [evaluation_view(e) for e in state.snapshot_evaluations()]
    return templates().TemplateResponse(
        request,
        "live.html",
        {
            "rows": rows,
            "last_poll_et": to_et(state.last_poll_utc).strftime("%H:%M:%S")
            if state.last_poll_utc
            else None,
            "in_window": state.push_state is not None,
            **header_context(state, now=now),
        },
    )


@router.get("/api/live", response_class=HTMLResponse)
def live_fragment(request: Request) -> HTMLResponse:
    """The table fragment HTMX polls every 10 seconds."""
    state = state_of(request)
    rows = [evaluation_view(e) for e in state.snapshot_evaluations()]
    return templates().TemplateResponse(
        request,
        "_live_table.html",
        {
            "rows": rows,
            "last_poll_et": to_et(state.last_poll_utc).strftime("%H:%M:%S")
            if state.last_poll_utc
            else None,
        },
    )


@router.get("/history", response_class=HTMLResponse)
def history_page(request: Request, day: str | None = None) -> HTMLResponse:
    """Past alerts for one day, with their outcomes once computed."""
    state = state_of(request)
    now = datetime.now(tz=UTC)
    trade_date = day or to_et(now).date().isoformat()
    with db.session(state.sqlite_path) as connection:
        rows = connection.execute(
            """
            SELECT alert_id, ticker, tier, price, gap_pct, rvol, rvol_source,
                   float_shares, headline, pushed, created_at_utc, window_start_utc
            FROM alerts WHERE trade_date = ?
            ORDER BY created_at_utc DESC
            """,
            (trade_date,),
        ).fetchall()
    return templates().TemplateResponse(
        request,
        "history.html",
        {
            "trade_date": trade_date,
            "alerts": [dict(row) for row in rows],
            **header_context(state, now=now),
        },
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    """Thresholds, toggles and the test-push button.

    Read-only for the configured thresholds in this step: they are shown so the
    trader can see exactly what the scanner is applying, which is the part that
    matters most. Editing lands with the settings-write route.
    """
    state = state_of(request)
    now = datetime.now(tz=UTC)
    pillars = state.config.pillars
    return templates().TemplateResponse(
        request,
        "settings.html",
        {
            "pillars": {
                "gap_pct_min": pillars.gap_pct_min,
                "rvol_min": pillars.rvol_min,
                "news_fresh_minutes": pillars.news_fresh_minutes,
                "price_min": pillars.price_min,
                "price_max": pillars.price_max,
                "float_shares_max": pillars.float_shares_max,
            },
            "alerts": {
                "tier_b_push_enabled": state.config.alerts.tier_b_push_enabled,
                "max_pushes_per_window": state.config.alerts.max_pushes_per_window,
                "daily_digest_enabled": state.config.alerts.daily_digest_enabled,
            },
            "vapid_public_key": state.secrets.vapid_public_key,
            "push_configured": state.secrets.has_vapid(),
            **header_context(state, now=now),
        },
    )


@router.get("/runners", response_class=HTMLResponse)
def runners_page(request: Request) -> HTMLResponse:
    """Missed Runners — arrives with build-order step 9.

    A stub rather than a missing route: the navigation is part of the app's
    shape, and a 404 from your own menu reads as a bug rather than as work in
    progress.
    """
    state = state_of(request)
    now = datetime.now(tz=UTC)
    return templates().TemplateResponse(
        request,
        "placeholder.html",
        {
            "page_title": "Missed Runners",
            "explanation": (
                "End-of-day runner detection and miss-reason diagnosis land in build step 9. "
                "Every evaluation is already being recorded, so the analysis will cover the "
                "days collected before the page exists."
            ),
            **header_context(state, now=now),
        },
    )


@router.get("/research", response_class=HTMLResponse)
def research_page(request: Request) -> HTMLResponse:
    """Research — arrives with build-order step 11."""
    state = state_of(request)
    now = datetime.now(tz=UTC)
    return templates().TemplateResponse(
        request,
        "placeholder.html",
        {
            "page_title": "Research",
            "explanation": (
                "The DuckDB query box, table downloads and the holdout guard land in build "
                "step 11. The lake is already being written, so nothing is lost in the "
                "meantime."
            ),
            **header_context(state, now=now),
        },
    )


@router.get("/healthz")
def healthz(request: Request) -> JSONResponse:
    """Liveness probe, also handy from a phone to check the tunnel is up."""
    state = state_of(request)
    now = datetime.now(tz=UTC)
    return JSONResponse(
        {
            "status": "ok",
            "last_poll_utc": state.last_poll_utc.isoformat() if state.last_poll_utc else None,
            "sources": [{"name": s.name, "healthy": s.healthy} for s in state.statuses(now=now)],
            "warnings": state.warnings,
        }
    )


# --- push API ----------------------------------------------------------------


class SubscriptionKeys(BaseModel):
    """The encryption keys a browser hands over on subscribe."""

    p256dh: str = Field(min_length=1)
    auth: str = Field(min_length=1)


class SubscriptionPayload(BaseModel):
    """A ``PushSubscription.toJSON()`` body, validated at the boundary."""

    endpoint: str = Field(min_length=1)
    keys: SubscriptionKeys

    def to_subscription(self, user_agent: str | None) -> push.PushSubscription:
        """Convert to the internal type."""
        return push.PushSubscription(
            endpoint=self.endpoint,
            p256dh=self.keys.p256dh,
            auth=self.keys.auth,
            user_agent=user_agent,
        )


class UnsubscribePayload(BaseModel):
    """The endpoint to forget."""

    endpoint: str = Field(min_length=1)


@router.post("/api/push/subscribe")
def subscribe(request: Request, payload: SubscriptionPayload) -> JSONResponse:
    """Store a browser's push subscription."""
    state = state_of(request)
    now = datetime.now(tz=UTC)
    subscription = payload.to_subscription(request.headers.get("user-agent"))
    with db.session(state.sqlite_path) as connection:
        push.save_subscription(connection, subscription, now=now)
    return JSONResponse({"stored": True, "id": subscription.fingerprint})


@router.post("/api/push/unsubscribe")
def unsubscribe(request: Request, payload: UnsubscribePayload) -> JSONResponse:
    """Forget a subscription the browser has revoked."""
    state = state_of(request)
    with db.session(state.sqlite_path) as connection:
        push.delete_subscription(connection, payload.endpoint)
    return JSONResponse({"removed": True})


@router.post("/api/push/test")
def send_test_push(request: Request) -> JSONResponse:
    """Send a test notification to every stored subscription.

    Returns the per-subscription outcome rather than a bare 200: "I pressed
    the button and nothing arrived" needs an answer, and the usual one is that
    there are no subscriptions or the keys are missing.
    """
    state = state_of(request)
    if not state.secrets.has_vapid():
        raise HTTPException(status_code=503, detail="VAPID keys are not configured")
    now = datetime.now(tz=UTC)
    keys = push.VapidKeys(
        public_key=state.secrets.vapid_public_key,
        private_key=state.secrets.vapid_private_key,
        subject=state.secrets.vapid_subject,
    )
    sender = getattr(request.app.state, "push_sender", None) or push.pywebpush_sender()
    with db.session(state.sqlite_path) as connection:
        results = push.broadcast(
            connection, push.test_notification(now=now), keys, sender=sender, now=now
        )
    return JSONResponse(
        {
            "subscriptions": len(results),
            "delivered": sum(1 for r in results if r.delivered),
            "pruned": sum(1 for r in results if r.gone),
        }
    )


TIER_LABELS: dict[Tier, str] = {
    Tier.A: "A — all five pillars with fresh news",
    Tier.B: "B — pillars 1, 2, 4, 5, no fresh catalyst",
    Tier.WATCH: "Watch — worth eyes, not a push",
    Tier.NONE: "—",
}
