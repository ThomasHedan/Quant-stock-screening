"""FastAPI application: wiring, lifespan, and the tick that drives everything.

The lifespan starts the scheduler and the news listener, and stops them on
shutdown. Everything it starts is optional and degrades explicitly: with no
Alpaca credentials there is no news feed, so pillar 3 is *unknown* rather than
failed; with no VAPID keys there are no pushes, and the settings page says so.
An app that pretends to be fully wired when it is not is how a trader finds out
at 08:05 that alerts were never going to arrive.

``MOCK_DATA=1`` swaps the market and news sources for deterministic synthetic
ones so the whole pipeline — UI, push, lake, missed runners — can be exercised
outside market hours (CLAUDE.md 11).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import alerts as alert_pipeline
from app import jobs
from app.collector import snapshot_rows
from app.config import AppConfig, get_secrets, load_config
from app.core.collection import CollectionFilter
from app.core.timeutils import UTC, et_trading_date, to_utc
from app.core.types import Tier
from app.logging_setup import configure as configure_logging
from app.market_calendar import MarketCalendar
from app.mock import MockDay, prev_closes
from app.runtime import RuntimeState
from app.scheduler import TickAction, TickPlan, plan_tick
from app.sources.retry import RetryPolicy, SourceError
from app.sources.tradingview import SnapshotResult, fetch_snapshot
from app.storage import db
from app.storage.lake import LakeWriter
from app.web import routes

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"


def retry_policy(config: AppConfig) -> RetryPolicy:
    """The shared HTTP policy, from config."""
    return RetryPolicy(
        max_retries=config.http.max_retries,
        base_seconds=config.http.backoff_base_seconds,
        max_seconds=config.http.backoff_max_seconds,
        timeout_seconds=config.http.timeout_seconds,
        connect_timeout_seconds=config.http.connect_timeout_seconds,
    )


def collection_filter(config: AppConfig) -> CollectionFilter:
    """The loose live filter, from config."""
    return CollectionFilter(
        min_abs_change_pct=config.collector.min_abs_change_pct,
        min_volume_ratio=config.collector.min_volume_ratio,
        top_dollar_volume_n=config.collector.top_dollar_volume_n,
        min_price=config.collector.min_price,
    )


def build_state(config: AppConfig) -> RuntimeState:
    """Assemble the runtime state and make sure the database exists."""
    secrets = get_secrets()
    sqlite_path = config.storage.sqlite_path
    with db.session(sqlite_path) as connection:
        logger.info("SQLite ready at schema version %s", db.current_version(connection))
    return RuntimeState(
        config=config,
        secrets=secrets,
        calendar=MarketCalendar(config.calendar.exchange),
        sqlite_path=sqlite_path,
        lake=LakeWriter(
            root=config.storage.lake_path,
            compression=config.storage.parquet_compression,
            source="tradingview",
        ),
    )


def take_snapshot(state: RuntimeState, *, now: datetime) -> SnapshotResult | None:
    """Poll the market, or generate a synthetic snapshot in mock mode.

    A failed poll returns ``None`` and is recorded as a gap rather than written
    as an empty result: an outage and a quiet market must never look the same
    in the lake.
    """
    if state.secrets.mock_data:
        day = MockDay(day=et_trading_date(now))
        # Seed the synthetic catalysts so pillar 3 can pass: without news in
        # the cache, mock mode could never produce a tier A and the push path
        # would go untested.
        state.news.extend(day.news(now=now))
        return day.snapshot(now=now)
    try:
        return fetch_snapshot(
            now=now,
            policy=retry_policy(state.config),
            min_price=state.config.collector.min_price,
        )
    except SourceError as exc:
        state.warn(f"TradingView poll failed: {exc}")
        logger.exception("TradingView poll failed; recording a gap rather than an empty poll")
        return None


def run_tick(state: RuntimeState, *, now: datetime) -> TickPlan:
    """One dispatcher tick: decide, poll, evaluate, store.

    Returns the plan so the caller (and the tests) can see what was decided
    without reaching into the state.
    """
    plan = plan_tick(now, state.config, state.calendar, last_poll_utc=state.last_poll_utc)
    if plan.action is TickAction.IDLE:
        return plan

    snapshot = take_snapshot(state, now=now)
    if snapshot is None:
        return plan

    day = et_trading_date(now)
    if plan.is_alert_poll:
        if state.push_state is None or (
            state.baselines is not None
            and plan.window_start_utc is not None
            and state.baselines.window_start != plan.window_start_utc
        ):
            state.open_window(plan.window_start_utc or now)

        context = alert_pipeline.WindowContext(
            window_start_utc=plan.window_start_utc or now,
            thresholds=state.config.pillars.to_thresholds(),
            weights=state.config.ranking.to_weights(),
            float_turnover_low_confidence=state.config.integrity.float_turnover_low_confidence,
            float_asof_max_age_days=state.config.integrity.float_asof_max_age_days,
            recent_runners=state.recent_runners,
            news_feed_healthy=state.feed_health.disconnected_since is None,
        )
        assert state.push_state is not None  # noqa: S101 - just opened above
        run = alert_pipeline.run_window_poll(
            list(snapshot.rows),
            context=context,
            poll_ts_utc=now,
            news_cache=state.news,
            rvol_for={},
            window_open_prices={},
            prev_closes=prev_closes(MockDay(day=day)) if state.secrets.mock_data else {},
            push_state=state.push_state,
        )
        state.record_poll(run.evaluations, now=now)
        state.lake.extend(
            "evaluations",
            day,
            [alert_pipeline.evaluation_row(e, now=now) for e in run.evaluations],
        )
        _persist_alerts(state, run.evaluations, day=day, now=now)

    if state.baselines is None:
        state.open_window(now)
    assert state.baselines is not None  # noqa: S101 - just opened above
    collected = snapshot_rows(
        snapshot,
        rules=collection_filter(state.config),
        session_fraction=state.config.rvol.fallback_session_fraction,
        baselines=state.baselines,
        reference_tracker=state.reference,
        float_turnover_low_confidence=state.config.integrity.float_turnover_low_confidence,
    )
    state.lake.extend("snapshots", day, collected.snapshots)
    state.lake.extend("reference", day, collected.reference)

    flushed = state.lake.flush_if_due(
        now=now,
        last_flush=state.last_flush_utc or to_utc(now),
        interval_seconds=state.config.storage.flush_interval_seconds,
    )
    if flushed or state.last_flush_utc is None:
        state.last_flush_utc = to_utc(now)
    return plan


def create_app(config_path: Path | None = None) -> FastAPI:
    """Build the application.

    Kept as a factory so tests can create an app against a temporary config and
    lake without touching the real one.
    """
    configure_logging()
    config = load_config(config_path)
    state = build_state(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        scheduler = None
        try:
            from app.scheduler import build_daily_jobs, start

            handlers = _daily_handlers(state)
            scheduler = start(
                config,
                state.calendar,
                tick=lambda now: _tick(state, now),
                daily_jobs=build_daily_jobs(config, handlers),
                now=datetime.now(tz=UTC),
            )
            if state.secrets.mock_data:
                logger.warning("MOCK_DATA=1: serving synthetic market and news data")
            if not state.secrets.has_alpaca():
                state.warn("No Alpaca credentials: news and bars are unavailable")
            if not state.secrets.has_vapid():
                state.warn("No VAPID keys: browser push is disabled")
            yield
        finally:
            if scheduler is not None:
                with suppress(Exception):
                    scheduler.shutdown(wait=False)
            flushed = state.lake.flush(now=datetime.now(tz=UTC))
            logger.info("Shutdown: flushed %s part files", len(flushed))

    app = FastAPI(title="Momentum Gap Scanner", version="0.6.0", lifespan=lifespan)
    app.state.runtime = state
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    routes.configure(Jinja2Templates(directory=WEB_DIR / "templates"))
    app.include_router(routes.router)
    return app


def _persist_alerts(
    state: RuntimeState,
    evaluations: list[alert_pipeline.Evaluation],
    *,
    day: date,
    now: datetime,
) -> None:
    """Write the alerting rows to SQLite for the History page.

    Only tiers A and B are recorded here. Watch-tier rows and everything below
    stay in the lake's ``evaluations`` table: History answers "what did it
    alert on?", and folding every near-miss into it would bury the answer.
    """
    alerting = [e for e in evaluations if e.tier in (Tier.A, Tier.B)]
    if not alerting:
        return
    with db.session(state.sqlite_path) as connection:
        for evaluation in alerting:
            db.record_alert(
                connection,
                db.AlertRecord(
                    alert_id=alert_pipeline.alert_id_for(evaluation),
                    ticker=evaluation.ticker,
                    trade_date=day,
                    window_start_utc=evaluation.window_start_utc,
                    tier=evaluation.tier.value,
                    price=evaluation.price,
                    gap_pct=evaluation.gap_pct,
                    rvol=evaluation.rvol,
                    rvol_source=evaluation.rvol_source.value,
                    float_shares=evaluation.float_shares,
                    headline=evaluation.news.headline if evaluation.news else None,
                    pushed=evaluation.pushed,
                    push_reason=(
                        evaluation.push_decision.reason if evaluation.push_decision else None
                    ),
                ),
                now=now,
            )
    logger.info("Recorded %s alert(s) for %s", len(alerting), day)


def _tick(state: RuntimeState, now: datetime) -> None:
    """Scheduler entry point: run a tick and swallow nothing but log it.

    The scheduler thread must keep ticking: an unhandled exception here would
    silence the scanner for the rest of the session, which is the one failure
    mode nobody would notice until a missed alert.
    """
    try:
        run_tick(state, now=to_utc(now))
    except Exception as exc:
        state.warn(f"Tick failed: {exc}")
        logger.exception("Tick failed at %s", to_utc(now).isoformat())


def _daily_handlers(state: RuntimeState) -> dict[str, Callable[[datetime], None]]:
    """Bind each scheduled daily job to the code that does the work.

    Every handler is wrapped so a failure is logged and surfaced on the
    dashboard instead of killing the scheduler thread: an unattended scanner
    that died at 20:15 loses the whole evening, and the next morning looks
    entirely normal.
    """

    def guarded(name: str, run: Callable[[datetime], object]) -> Callable[[datetime], None]:
        def handler(now: datetime) -> None:
            started = to_utc(now)
            logger.info("Daily job %s starting at %s", name, started.isoformat())
            try:
                run(started)
            except Exception as exc:
                state.warn(f"Daily job {name} failed: {exc}")
                logger.exception("Daily job %s failed", name)
            else:
                logger.info("Daily job %s finished", name)

        return handler

    # The full-day run keeps its outcome summaries so the nightly pruning can
    # classify what it collected; without them pruning refuses to discard
    # anything, which is the safe direction but skips the day's retention.
    carried: dict[str, object] = {}

    def full_day(now: datetime) -> None:
        report = jobs.run_outcomes_and_runners(state, now=now)
        carried["summaries"] = report.summaries

    def nightly(now: datetime) -> None:
        summaries = carried.pop("summaries", None)
        jobs.run_nightly(state, now=now, summaries=summaries)  # type: ignore[arg-type]

    premarket_refs = tuple(
        reference
        for reference in state.config.outcomes.reference_times_et
        if reference < state.config.schedules.jobs.premarket_outcomes
    )

    return {
        "corporate_actions": guarded(
            "corporate_actions", lambda now: jobs.run_corporate_actions(state, now=now)
        ),
        "premarket_outcomes": guarded(
            "premarket_outcomes",
            lambda now: jobs.run_outcomes_and_runners(
                state, now=now, reference_times=premarket_refs
            ),
        ),
        "daily_digest_push": guarded(
            "daily_digest_push", lambda now: jobs.run_digest_push(state, now=now)
        ),
        "full_day_outcomes": guarded("full_day_outcomes", full_day),
        "data_quality_and_compaction": guarded("data_quality_and_compaction", nightly),
    }
