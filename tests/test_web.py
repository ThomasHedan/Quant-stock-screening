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


# --- research ----------------------------------------------------------------


def test_research_page_reports_the_lake_and_the_holdout(client):
    body = client.get("/research").text
    assert "Holdout" in body
    assert "GB of 25 GB" in body
    assert "re-weight" in body or "weight them by" in body


def test_research_page_offers_descriptive_starter_queries(client):
    assert "When do moves actually start?" in client.get("/research").text


def test_research_refuses_a_write_query(client):
    body = client.get("/research", params={"sql": "DELETE FROM evaluations"}).text
    assert "read-only" in body


def test_research_reports_an_empty_lake_rather_than_crashing(client):
    body = client.get("/research", params={"sql": "SELECT 1"}).text
    assert "nothing has been collected yet" in body


def test_research_runs_a_query_over_written_data(app, client):
    from datetime import date

    from app.storage import lake

    state = app.state.runtime
    day = date(2026, 3, 10)
    writer = lake.LakeWriter(root=state.config.storage.lake_path, source="tradingview")
    writer.append(
        "evaluations",
        day,
        {
            "ticker": "ABCD",
            "date": day,
            "poll_ts_utc": NOW,
            "window_start_utc": NOW,
            "pillar_1_status": "pass",
            "pillar_2_status": "pass",
            "pillar_3_status": "pass",
            "pillar_4_status": "pass",
            "pillar_5_status": "pass",
            "pillars_passed": 5,
            "pillars_unknown": 0,
            "tier": "A",
            "written_at_utc": NOW,
        },
    )
    writer.flush(now=NOW)

    body = client.get("/research", params={"sql": "SELECT ticker, tier FROM evaluations"}).text
    assert "ABCD" in body
    assert "1 row(s)" in body


def test_research_csv_export(app, client):
    from datetime import date

    from app.storage import lake

    state = app.state.runtime
    day = date(2026, 3, 10)
    writer = lake.LakeWriter(root=state.config.storage.lake_path, source="tradingview")
    writer.append(
        "runners",
        day,
        {
            "ticker": "RUNNR",
            "date": day,
            "high_of_day_pct": 84.0,
            "miss_reasons": ["OUTSIDE_WINDOW"],
            "qualifying_rule": "high of day +84%",
            "written_at_utc": NOW,
        },
    )
    writer.flush(now=NOW)

    response = client.get(
        "/api/research/export", params={"sql": "SELECT ticker, high_of_day_pct FROM runners"}
    )
    assert response.status_code == 200
    assert response.text.splitlines()[0] == "ticker,high_of_day_pct"
    assert "RUNNR" in response.text


def test_research_export_refuses_a_write_query(client):
    response = client.get("/api/research/export", params={"sql": "DROP VIEW runners"})
    assert response.status_code == 400


# --- missed runners ----------------------------------------------------------


def test_runners_page_distinguishes_no_job_from_no_runners(client):
    """'Nothing ran' and 'the job has not run' are opposite findings."""
    body = client.get("/runners?day=2026-03-10").text
    assert "No runner analysis" in body
    assert "20:15 ET job" in body


def test_runners_page_lists_a_written_partition(app, client):
    from datetime import date

    from app.storage import lake

    state = app.state.runtime
    day = date(2026, 3, 10)
    writer = lake.LakeWriter(root=state.config.storage.lake_path, source="alpaca")
    writer.append(
        "runners",
        day,
        {
            "ticker": "RUNNR",
            "date": day,
            "high_of_day_pct": 84.0,
            "move_start_utc": NOW,
            "first_news_utc": None,
            "news_lag_minutes": None,
            "best_tier": "none",
            "best_tier_ts_utc": None,
            "price_at_move_start": 4.10,
            "float_shares": 23_000_000,
            "dollar_volume": 8_000_000.0,
            "miss_reasons": ["OUTSIDE_WINDOW", "FAILED_PILLAR", "NEAR_MISS"],
            "miss_detail": "move started 07:20 ET, outside every alert window",
            "qualifying_rule": "high of day +84% vs prev close",
            "written_at_utc": NOW,
        },
    )
    writer.flush(now=NOW)

    body = client.get("/runners?day=2026-03-10").text
    assert "RUNNR" in body
    assert "OUTSIDE_WINDOW" in body
    assert "NEAR_MISS" in body
    assert "1 runner(s): 0 caught, 1 missed" in body


def test_watchlist_pin_and_remove_round_trip(app, client):
    from datetime import date

    from app import recent_runners
    from app.recent_runners import RecentRunner
    from app.storage import db

    state = app.state.runtime
    with db.session(state.sqlite_path) as connection:
        recent_runners.add(
            connection,
            RecentRunner(
                ticker="ABCD",
                run_date=date(2026, 3, 6),
                high_pct=84.0,
                float_shares=4_100_000,
                headline="Phase 3 data",
                expires_on=date(2026, 3, 13),
            ),
        )

    assert client.post("/api/watchlist/pin", json={"ticker": "abcd"}).json()["pinned"] is True
    assert client.post("/api/watchlist/remove", json={"ticker": "ABCD"}).json()["removed"] is True


def test_watchlist_rejects_a_bogus_ticker(client):
    assert client.post("/api/watchlist/pin", json={"ticker": ""}).status_code == 422


# --- journal -----------------------------------------------------------------


def test_journal_entry_takes_one_post(client):
    response = client.post("/api/journal", json={"ticker": "abcd", "action": "traded"})
    assert response.status_code == 200
    assert response.json()["ticker"] == "ABCD"


def test_journal_rejects_an_unknown_action(client):
    assert client.post("/api/journal", json={"ticker": "ABCD", "action": "yolo"}).status_code == 422


def test_journal_page_lists_the_days_entries(client):
    client.post(
        "/api/journal",
        json={
            "ticker": "ABCD",
            "action": "traded",
            "entry": 5.2,
            "exit": 6.1,
            "size": 500,
            "note": "held through the open",
        },
    )
    from datetime import datetime as _dt

    from app.core.timeutils import to_et

    today = to_et(_dt.now(tz=UTC)).date().isoformat()
    body = client.get(f"/journal?day={today}").text
    assert "ABCD" in body
    assert "held through the open" in body
    assert "450.00" in body  # (6.10 - 5.20) * 500


def test_journal_page_says_how_to_log_when_empty(client):
    body = client.get("/journal?day=2026-03-10").text
    assert "one tap each" in body


def test_journal_page_disclaims_the_pnl_figure(client):
    client.post("/api/journal", json={"ticker": "ABCD", "action": "traded"})
    from datetime import datetime as _dt

    from app.core.timeutils import to_et

    today = to_et(_dt.now(tz=UTC)).date().isoformat()
    body = client.get(f"/journal?day={today}").text
    assert "no fees or slippage" in body
    assert "never used to tune thresholds" in body


def test_live_rows_carry_one_tap_journal_buttons(app, client):
    seed_evaluation(app)
    body = client.get("/api/live").text
    assert 'data-journal="traded"' in body
    assert 'data-journal="skipped"' in body


def test_history_shows_an_alert_the_pipeline_recorded(app, client):
    """The History page was reading a table nothing ever wrote to."""
    from datetime import date

    from app.storage import db

    state = app.state.runtime
    day = date(2026, 3, 10)
    with db.session(state.sqlite_path) as connection:
        db.record_alert(
            connection,
            db.AlertRecord(
                alert_id="2026-03-10-0800-ABCD-A",
                ticker="ABCD",
                trade_date=day,
                window_start_utc=NOW,
                tier="A",
                price=5.20,
                gap_pct=34.0,
                rvol=12.0,
                rvol_source="baseline",
                float_shares=4_100_000,
                headline="Phase 3 data beats endpoint",
                pushed=True,
            ),
            now=NOW,
        )
    body = client.get("/history?day=2026-03-10").text
    assert "ABCD" in body
    assert "Phase 3 data" in body
    assert "+34.0%" in body
