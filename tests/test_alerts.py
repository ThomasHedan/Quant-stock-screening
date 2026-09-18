"""The alert pipeline: evaluation, ranking, push budget and stored rows."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from app import alerts
from app.core import tiering
from app.core.news import NewsCache
from app.core.timeutils import UTC, et_trading_date
from app.core.types import FloatConfidence, NewsItem, PillarStatus, RvolSource, Tier
from app.sources.tradingview import TradingViewRow
from app.storage import lake

WINDOW_START = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)  # 08:00 ET
POLL = WINDOW_START + timedelta(minutes=5)


def row(**overrides: object) -> TradingViewRow:
    base = {
        "ticker": "ABCD",
        "exchange": "NASDAQ",
        "instrument_type": "stock",
        "typespecs": ("common",),
        "close": 5.20,
        "change_pct": 34.0,
        "volume": 8_200_000.0,
        "average_volume_10d": 900_000.0,
        "float_shares": 4_100_000,
        "shares_outstanding": 12_000_000,
    }
    return TradingViewRow(**{**base, **overrides})  # type: ignore[arg-type]


def news_for(ticker: str, *, minutes_old: float = 2.0) -> NewsItem:
    created = POLL - timedelta(minutes=minutes_old)
    return NewsItem(
        news_id=f"n-{ticker}",
        symbols=(ticker,),
        headline="Phase 3 data beats endpoint",
        source="benzinga",
        url=None,
        created_at=created,
        received_at=created,
    )


def cache_with_news(*, minutes_old: float = 2.0, tickers: tuple[str, ...] = ("ABCD",)) -> NewsCache:
    cache = NewsCache()
    cache.extend([news_for(ticker, minutes_old=minutes_old) for ticker in tickers])
    return cache


def context(thresholds, weights, **overrides: object) -> alerts.WindowContext:
    base = {
        "window_start_utc": WINDOW_START,
        "thresholds": thresholds,
        "weights": weights,
        "float_turnover_low_confidence": 10.0,
        "float_asof_max_age_days": 90,
    }
    return alerts.WindowContext(**{**base, **overrides})  # type: ignore[arg-type]


def run(rows, ctx, cache, *, max_pushes: int = 5, tier_b: bool = True, **overrides: object):
    state = tiering.WindowPushState(max_pushes=max_pushes, tier_b_enabled=tier_b)
    kwargs = {
        "context": ctx,
        "poll_ts_utc": POLL,
        "news_cache": cache,
        "rvol_for": {},
        "window_open_prices": {},
        "prev_closes": {},
        "push_state": state,
    }
    return alerts.run_window_poll(rows, **{**kwargs, **overrides}), state


# --- single evaluations ------------------------------------------------------


def test_a_five_pillar_setup_is_tier_a(thresholds, weights):
    rvol = alerts.RvolInputs(
        baseline_volume=500_000.0, average_volume_10d=900_000.0, session_fraction=0.05
    )
    evaluation = alerts.evaluate_row(
        row(),
        context=context(thresholds, weights),
        poll_ts_utc=POLL,
        news_cache=cache_with_news(),
        rvol_inputs=rvol,
        window_open_price=5.00,
        prev_close=3.88,
    )
    assert evaluation.tier is Tier.A
    assert evaluation.rvol_source is RvolSource.BASELINE
    assert evaluation.score.passed_count == 5


def test_no_fresh_news_makes_it_tier_b(thresholds, weights):
    rvol = alerts.RvolInputs(500_000.0, 900_000.0, 0.05)
    evaluation = alerts.evaluate_row(
        row(),
        context=context(thresholds, weights),
        poll_ts_utc=POLL,
        news_cache=NewsCache(),
        rvol_inputs=rvol,
        window_open_price=5.00,
        prev_close=3.88,
    )
    assert evaluation.tier is Tier.B
    assert evaluation.score.by_number(3).status is PillarStatus.FAIL


def test_an_unhealthy_feed_makes_pillar_three_unknown_not_failed(thresholds, weights):
    """A feed outage recorded as 'no catalyst' is a false negative in research."""
    rvol = alerts.RvolInputs(500_000.0, 900_000.0, 0.05)
    evaluation = alerts.evaluate_row(
        row(),
        context=context(thresholds, weights, news_feed_healthy=False),
        poll_ts_utc=POLL,
        news_cache=cache_with_news(),
        rvol_inputs=rvol,
        window_open_price=5.00,
        prev_close=3.88,
    )
    assert evaluation.score.by_number(3).status is PillarStatus.UNKNOWN
    assert "unavailable" in evaluation.score.by_number(3).detail
    assert evaluation.tier is not Tier.A


def test_the_fallback_rvol_is_labelled(thresholds, weights):
    evaluation = alerts.evaluate_row(
        row(),
        context=context(thresholds, weights),
        poll_ts_utc=POLL,
        news_cache=cache_with_news(),
        rvol_inputs=alerts.RvolInputs(None, 900_000.0, 0.05),
        window_open_price=5.00,
        prev_close=3.88,
    )
    assert evaluation.rvol_source is RvolSource.FALLBACK


def test_low_float_confidence_blocks_tier_a(thresholds, weights):
    evaluation = alerts.evaluate_row(
        row(float_shares=100_000),  # 82x turnover: not credible
        context=context(thresholds, weights),
        poll_ts_utc=POLL,
        news_cache=cache_with_news(),
        rvol_inputs=alerts.RvolInputs(500_000.0, 900_000.0, 0.05),
        window_open_price=5.00,
        prev_close=3.88,
    )
    assert evaluation.float_confidence is FloatConfidence.LOW
    assert evaluation.score.by_number(5).status is PillarStatus.UNKNOWN
    assert evaluation.tier is not Tier.A


def test_a_recent_runner_reaches_watch_on_pillar_one_alone(thresholds, weights):
    evaluation = alerts.evaluate_row(
        row(volume=1_000.0, float_shares=900_000_000),
        context=context(thresholds, weights, recent_runners=frozenset({"ABCD"})),
        poll_ts_utc=POLL,
        news_cache=NewsCache(),
        rvol_inputs=alerts.RvolInputs(None, None, 0.05),
        window_open_price=5.00,
        prev_close=3.88,
    )
    assert evaluation.is_recent_runner
    assert evaluation.tier is Tier.WATCH


# --- whole polls -------------------------------------------------------------


def test_every_evaluation_is_recorded_not_only_the_alerts(thresholds, weights):
    """CLAUDE.md 5.6: the rows that never fired are the research denominator."""
    rows = [row(ticker=f"T{i}", change_pct=1.0, volume=1_000.0) for i in range(20)]
    poll, _state = run(rows, context(thresholds, weights), NewsCache())
    assert len(poll.evaluations) == 20
    assert poll.pushes == []
    assert poll.by_tier[Tier.NONE] == 20


def test_pushes_respect_the_window_budget(thresholds, weights):
    rows = [row(ticker=f"T{i}") for i in range(8)]
    cache = cache_with_news(tickers=tuple(f"T{i}" for i in range(8)))
    rvol = {f"T{i}": alerts.RvolInputs(500_000.0, 900_000.0, 0.05) for i in range(8)}
    poll, state = run(
        rows,
        context(thresholds, weights),
        cache,
        max_pushes=5,
        rvol_for=rvol,
        prev_closes={f"T{i}": 3.88 for i in range(8)},
    )
    assert len(poll.pushes) == 5
    assert state.budget_left == 0
    # The other three are still evaluated and still reach the UI.
    assert len(poll.evaluations) == 8
    assert sum(1 for e in poll.evaluations if e.tier is Tier.A) == 8


def test_the_budget_is_spent_on_the_highest_ranked(thresholds, weights):
    rows = [
        row(ticker="WEAK", change_pct=11.0, volume=4_600_000.0),
        row(ticker="STRONG", change_pct=90.0, volume=20_000_000.0),
    ]
    cache = cache_with_news(tickers=("WEAK", "STRONG"))
    rvol = {t: alerts.RvolInputs(500_000.0, 900_000.0, 0.05) for t in ("WEAK", "STRONG")}
    poll, _state = run(rows, context(thresholds, weights), cache, max_pushes=1, rvol_for=rvol)
    assert [e.ticker for e in poll.pushes] == ["STRONG"]


def test_a_b_to_a_upgrade_pushes_again_within_a_window(thresholds, weights):
    state = tiering.WindowPushState(max_pushes=5, tier_b_enabled=True)
    ctx = context(thresholds, weights)
    rvol = {"ABCD": alerts.RvolInputs(500_000.0, 900_000.0, 0.05)}
    first = alerts.run_window_poll(
        [row()],
        context=ctx,
        poll_ts_utc=POLL,
        news_cache=NewsCache(),
        rvol_for=rvol,
        window_open_prices={},
        prev_closes={},
        push_state=state,
    )
    assert first.pushes[0].tier is Tier.B

    second = alerts.run_window_poll(
        [row()],
        context=ctx,
        poll_ts_utc=POLL + timedelta(seconds=30),
        news_cache=cache_with_news(),
        rvol_for=rvol,
        window_open_prices={},
        prev_closes={},
        push_state=state,
    )
    assert second.pushes[0].tier is Tier.A
    assert "upgrade" in second.pushes[0].push_decision.reason


# --- stored rows -------------------------------------------------------------


def test_evaluation_rows_write_to_the_lake(tmp_path: Path, thresholds, weights):
    rows = [row(ticker=f"T{i}") for i in range(5)]
    poll, _state = run(rows, context(thresholds, weights), cache_with_news())
    root = tmp_path / "lake"
    writer = lake.LakeWriter(root=root, source="tradingview")
    day = et_trading_date(POLL)
    writer.extend(
        "evaluations", day, [alerts.evaluation_row(e, now=POLL) for e in poll.evaluations]
    )
    writer.flush(now=POLL)

    stored = lake.read_day(root, "evaluations", day).to_pylist()
    assert len(stored) == 5
    assert {r["tier"] for r in stored} == {"B"}
    assert all(r["pillar_1_status"] == "pass" for r in stored)
    assert all(r["window_start_utc"] == WINDOW_START for r in stored)


def test_evaluation_row_records_the_push_refusal_reason(thresholds, weights):
    rows = [row(ticker=f"T{i}") for i in range(7)]
    poll, _state = run(rows, context(thresholds, weights), NewsCache(), max_pushes=2)
    refused = [alerts.evaluation_row(e, now=POLL) for e in poll.evaluations if not e.pushed]
    assert refused
    assert all("budget" in r["push_reason"] for r in refused)


# --- push payload ------------------------------------------------------------


def test_push_payload_carries_every_checkable_number(thresholds, weights):
    evaluation = alerts.evaluate_row(
        row(),
        context=context(thresholds, weights),
        poll_ts_utc=POLL,
        news_cache=cache_with_news(),
        rvol_inputs=alerts.RvolInputs(500_000.0, 900_000.0, 0.05),
        window_open_price=5.00,
        prev_close=3.88,
    )
    payload = alerts.push_payload(evaluation)
    assert payload.startswith("[A] ABCD")
    assert "RVOL" in payload
    assert "Float 4.1M" in payload
    assert "$5.20" in payload
    assert "Phase 3" in payload


def test_push_payload_marks_missing_values_rather_than_inventing_them(thresholds, weights):
    evaluation = alerts.evaluate_row(
        row(float_shares=None, volume=None),
        context=context(thresholds, weights),
        poll_ts_utc=POLL,
        news_cache=NewsCache(),
        rvol_inputs=alerts.RvolInputs(None, None, 0.05),
        window_open_price=5.00,
        prev_close=3.88,
    )
    payload = alerts.push_payload(evaluation)
    assert "Float ?" in payload
    assert "no fresh catalyst" in payload
