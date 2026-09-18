"""The web layer: pages render, the push API round-trips, nothing leaks."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from app import alerts as alert_pipeline
from app.core.news import NewsCache
from app.core.tiering import WindowPushState
from app.core.timeutils import UTC
from app.core.types import NewsItem
from app.main import create_app
from app.sources.tradingview import TradingViewRow
from fastapi.testclient import TestClient

NOW = datetime(2026, 3, 10, 12, 5, tzinfo=UTC)


@pytest.fixture
def app(tmp_path: Path, monkeypatch):
    """An app on a temporary lake and database, with push sending stubbed."""
    import yaml

    raw = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    raw["storage"]["lake_path"] = str(tmp_path / "lake")
    raw["storage"]["sqlite_path"] = str(tmp_path / "app.db")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    monkeypatch.setenv("MOCK_DATA", "1")
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "test-public")
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "test-private")
    monkeypatch.setenv("VAPID_SUBJECT", "mailto:trader@example.invalid")
    from app.config import get_secrets

    get_secrets.cache_clear()

    application = create_app(config_path)
    sent: list[tuple[dict, str]] = []

    def sender(payload: dict, message: str, keys) -> int:
        sent.append((payload, message))
        return 201

    application.state.push_sender = sender
    application.state.sent = sent
    return application


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


def seed_evaluation(app, *, ticker: str = "ABCD") -> None:
    """Put one tier-A evaluation into the runtime state."""
    state = app.state.runtime
    cache = NewsCache()
    created = NOW - timedelta(minutes=2)
    cache.add(
        NewsItem(
            news_id="n1",
            symbols=(ticker,),
            headline="Phase 3 data beats endpoint",
            source="benzinga",
            url="https://example.invalid/n1",
            created_at=created,
            received_at=created,
        )
    )
    context = alert_pipeline.WindowContext(
        window_start_utc=NOW,
        thresholds=state.config.pillars.to_thresholds(),
        weights=state.config.ranking.to_weights(),
        float_turnover_low_confidence=10.0,
        float_asof_max_age_days=90,
    )
    row = TradingViewRow(
        ticker=ticker,
        exchange="NASDAQ",
        instrument_type="stock",
        typespecs=("common",),
        close=5.20,
        change_pct=34.0,
        volume=8_200_000.0,
        average_volume_10d=900_000.0,
        float_shares=4_100_000,
    )
    run = alert_pipeline.run_window_poll(
        [row],
        context=context,
        poll_ts_utc=NOW,
        news_cache=cache,
        rvol_for={ticker: alert_pipeline.RvolInputs(500_000.0, 900_000.0, 0.05)},
        window_open_prices={ticker: 5.00},
        prev_closes={ticker: 3.88},
        push_state=WindowPushState(max_pushes=5, tier_b_enabled=True),
    )
    state.record_poll(run.evaluations, now=NOW)


# --- pages -------------------------------------------------------------------


def test_live_page_renders_empty(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Nothing evaluated yet" in response.text


def test_live_page_shows_an_evaluation(app, client):
    seed_evaluation(app)
    body = client.get("/").text
    assert "ABCD" in body
    assert "+34.0%" in body
    assert "Phase 3 data" in body


def test_unknown_pillars_render_as_a_question_mark(app, client):
    """A data gap must not look like a failed rule."""
    state = app.state.runtime
    seed_evaluation(app)
    evaluation = state.snapshot_evaluations()[0]
    assert evaluation.score.unknown_count == 0
    # A low-confidence float makes pillar 5 unknown; check the glyph mapping.
    from app.web.routes import _pillar_glyph

    assert _pillar_glyph("unknown") == "?"
    assert _pillar_glyph("fail") != _pillar_glyph("unknown")


def test_live_fragment_is_swappable(app, client):
    seed_evaluation(app)
    response = client.get("/api/live")
    assert response.status_code == 200
    assert response.text.lstrip().startswith('<div id="live-table"')
    assert "<html" not in response.text


def test_history_page_renders_for_an_empty_day(client):
    response = client.get("/history?day=2026-03-10")
    assert response.status_code == 200
    assert "No alerts recorded" in response.text


def test_settings_page_shows_the_live_thresholds(client):
    body = client.get("/settings").text
    assert "gap ≥ 10.0%" in body
    assert "RVOL ≥ 5.0" in body
    assert "$2.0 – $20.0" in body


def test_settings_page_explains_the_ios_requirement(client):
    assert "home screen" in client.get("/settings").text


def test_header_shows_both_clocks_and_status_dots(client):
    body = client.get("/").text
    assert "ET" in body
    assert "Paris" in body
    assert "TradingView" in body


def test_footer_states_the_app_gives_no_advice(client):
    assert "does not provide financial advice" in client.get("/").text


def test_healthz_reports_sources(client):
    payload = client.get("/healthz").json()
    assert payload["status"] == "ok"
    assert {s["name"] for s in payload["sources"]} >= {"TradingView", "Push"}


# --- push API ----------------------------------------------------------------


SUBSCRIPTION = {
    "endpoint": "https://push.example.invalid/abc",
    "keys": {"p256dh": "p256dh-value", "auth": "auth-value"},
}


def test_subscribe_stores_and_returns_a_fingerprint(client):
    response = client.post("/api/push/subscribe", json=SUBSCRIPTION)
    assert response.status_code == 200
    body = response.json()
    assert body["stored"] is True
    # The endpoint itself is a capability and must not come back in the body.
    assert "push.example.invalid" not in response.text
    assert len(body["id"]) == 12


def test_subscribe_rejects_a_payload_without_keys(client):
    response = client.post("/api/push/subscribe", json={"endpoint": "https://x.invalid/a"})
    assert response.status_code == 422


def test_test_push_reports_zero_subscriptions_rather_than_succeeding_silently(client):
    payload = client.post("/api/push/test").json()
    assert payload == {"subscriptions": 0, "delivered": 0, "pruned": 0}


def test_test_push_delivers_to_a_stored_subscription(app, client):
    client.post("/api/push/subscribe", json=SUBSCRIPTION)
    payload = client.post("/api/push/test").json()
    assert payload["delivered"] == 1
    _sub, message = app.state.sent[0]
    assert "Push is working" in message


def test_unsubscribe_forgets_the_endpoint(client):
    client.post("/api/push/subscribe", json=SUBSCRIPTION)
    removal = client.post("/api/push/unsubscribe", json={"endpoint": SUBSCRIPTION["endpoint"]})
    assert removal.status_code == 200
    assert client.post("/api/push/test").json()["subscriptions"] == 0


def test_static_assets_are_served(client):
    assert client.get("/static/sw.js").status_code == 200
    assert client.get("/static/manifest.json").status_code == 200
    assert client.get("/static/app.css").status_code == 200


def test_navigation_links_all_resolve(client):
    """A 404 from your own menu reads as a bug, not as work in progress."""
    for path in ("/", "/runners", "/history", "/research", "/settings"):
        assert client.get(path).status_code == 200, path


def test_placeholder_pages_say_what_is_coming(client):
    assert "build step 9" in client.get("/runners").text
    assert "build step 11" in client.get("/research").text
