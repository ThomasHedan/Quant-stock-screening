"""The daily job chain, 20:10 through 20:45.

Every module needed for the nightly work existed before this file; nothing ran
them. This is the wiring, in the order the data depends on:

1. **20:10 corporate actions** — first, because a split has to be on record
   before any move metric is computed. A 1:10 reverse split read as a -90%
   move would poison the day's retention decisions and every RVOL baseline.
2. **20:15 bars, Tier 0, outcomes, runners** — fetch the scoped bars once and
   reuse them for all three, since they are the same minute series.
3. **20:45 pruning, compaction, retention, data quality** — last, because
   pruning needs the move metrics and the quality row needs the pruning counts.

Each job takes ``now`` explicitly and returns a report rather than logging and
vanishing, so the scheduler, the tests and a future "run this by hand" CLI all
drive the same code. Every one of them catches its own failures: an unattended
scanner that dies at 20:15 loses the whole evening's work, and the next morning
looks normal.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from app.core import moves, outcomes, runners, universe
from app.core.integrity import (
    CorporateAction,
    action_on,
    baselines_to_recompute,
    days_since_reverse_split,
    halt_summary,
    infer_halts,
)
from app.core.moves import Bar
from app.core.timeutils import et_datetime, et_trading_date, to_utc
from app.core.types import MarketSession, Tier
from app.recent_runners import RecentRunner
from app.recent_runners import add as watchlist_add
from app.recent_runners import prune as watchlist_prune
from app.runtime import RuntimeState
from app.sources import alpaca_actions, alpaca_bars
from app.sources.alpaca import AlpacaCredentials
from app.sources.retry import RetryPolicy, SourceError
from app.storage import db, lake, pruning, quality
from app.storage.pruning import RetentionPolicy, TickerDaySummary

logger = logging.getLogger(__name__)


def _policy(state: RuntimeState) -> RetryPolicy:
    """The shared HTTP policy, from config."""
    http = state.config.http
    return RetryPolicy(
        max_retries=http.max_retries,
        base_seconds=http.backoff_base_seconds,
        max_seconds=http.backoff_max_seconds,
        timeout_seconds=http.timeout_seconds,
        connect_timeout_seconds=http.connect_timeout_seconds,
    )


def _credentials(state: RuntimeState) -> AlpacaCredentials:
    """Alpaca credentials from the environment."""
    return AlpacaCredentials(
        key_id=state.secrets.alpaca_key_id, secret_key=state.secrets.alpaca_secret_key
    )


# --- 20:10 corporate actions -------------------------------------------------


@dataclass(frozen=True, slots=True)
class ActionsReport:
    """What the corporate-actions job did."""

    day: date
    actions: tuple[CorporateAction, ...] = ()
    baselines_invalidated: int = 0
    error: str | None = None


def run_corporate_actions(state: RuntimeState, *, now: datetime) -> ActionsReport:
    """Pull splits, renames and delistings, and invalidate affected baselines.

    Runs before the outcome jobs so that a split is known *before* any move
    metric is computed from that day's prices.
    """
    day = et_trading_date(now)
    try:
        if state.secrets.mock_data:
            result = alpaca_actions.mock_actions(day)
        elif not state.secrets.has_alpaca():
            state.warn("Corporate actions skipped: no Alpaca credentials")
            return ActionsReport(day=day, error="no credentials")
        else:
            start, end = alpaca_actions.default_window(day)
            result = alpaca_actions.fetch_actions(
                _credentials(state), start=start, end=end, policy=_policy(state)
            )
    except SourceError as exc:
        state.warn(f"Corporate actions failed: {exc}")
        logger.exception("Corporate actions job failed for %s", day)
        return ActionsReport(day=day, error=str(exc))

    state.lake.extend(
        "corporate_actions", day, alpaca_actions.action_rows(result.actions, day, now=now)
    )

    affected = baselines_to_recompute(list(result.actions), day)
    invalidated = 0
    if affected:
        with db.session(state.sqlite_path) as connection:
            from app.rvol import invalidate

            invalidated = invalidate(connection, affected, day)
    logger.info(
        "Corporate actions for %s: %s recorded, %s baselines invalidated",
        day,
        len(result.actions),
        invalidated,
    )
    return ActionsReport(day=day, actions=result.actions, baselines_invalidated=invalidated)


# --- 20:15 bars, Tier 0, outcomes, runners -----------------------------------


@dataclass(frozen=True, slots=True)
class OutcomesReport:
    """What the outcome and runner job produced."""

    day: date
    tickers_fetched: tuple[str, ...] = ()
    tickers_skipped: tuple[str, ...] = ()
    bar_rows: int = 0
    universe_rows: int = 0
    outcome_rows: int = 0
    runner_rows: int = 0
    tradeable_rows: int = 0
    halt_count: int = 0
    inferred_halt_count: int = 0
    summaries: dict[str, TickerDaySummary] = field(default_factory=dict)
    error: str | None = None


def scope_from_evaluations(state: RuntimeState, day: date) -> dict[str, float]:
    """Tickers worth fetching bars for, with the session change that got them in.

    Scoped rather than universe-wide (CLAUDE.md 6.5): a name that reached 10%
    at any poll, or was evaluated at Watch tier or above. Bars are refetchable,
    so a tight scope costs nothing permanent.
    """
    table = lake.read_day(state.config.storage.lake_path, "evaluations", day)
    if table.num_rows == 0:
        return {}
    threshold = state.config.outcomes.session_change_min_pct
    candidates: dict[str, float] = {}
    for row in table.to_pylist():
        ticker = str(row["ticker"])
        gap = row.get("gap_pct")
        tier = str(row.get("tier") or Tier.NONE.value)
        qualifies = (gap is not None and gap >= threshold) or tier in {
            Tier.WATCH.value,
            Tier.B.value,
            Tier.A.value,
        }
        if qualifies:
            candidates[ticker] = max(candidates.get(ticker, 0.0), gap or 0.0)
    return candidates


def _fetch_bars(
    state: RuntimeState, tickers: tuple[str, ...], day: date, *, now: datetime
) -> dict[str, tuple[Bar, ...]]:
    """Bars for the scoped tickers, real or synthetic."""
    if state.secrets.mock_data:
        from app.mock import MockDay

        available = MockDay(day=day).bars()
        return {ticker: available[ticker] for ticker in tickers if ticker in available}
    if not state.secrets.has_alpaca():
        state.warn("Bar fetch skipped: no Alpaca credentials")
        return {}
    start = et_datetime(day, state.config.sessions.pre.start)
    end = et_datetime(day, state.config.sessions.post.end)
    result = alpaca_bars.fetch_bars(
        _credentials(state), tickers, start=start, end=end, now=now, policy=_policy(state)
    )
    return result.bars


def run_outcomes_and_runners(
    state: RuntimeState,
    *,
    now: datetime,
    reference_times: tuple[time, ...] | None = None,
) -> OutcomesReport:
    """Fetch the day's bars once and derive everything that needs them.

    Tier 0 rollups, outcome labels and runner diagnosis all read the same
    minute series, so they share one fetch. ``reference_times`` lets the 11:05
    run cover only the pre-market references.
    """
    day = et_trading_date(now)
    candidates = scope_from_evaluations(state, day)
    fetched, skipped = outcomes.scope_bar_fetch(
        candidates, max_tickers=state.config.outcomes.max_bar_tickers
    )
    if skipped:
        state.warn(f"Bar fetch capped: {len(skipped)} ticker(s) skipped on {day}")

    try:
        bars_by_ticker = _fetch_bars(state, fetched, day, now=now)
    except SourceError as exc:
        state.warn(f"Bar fetch failed: {exc}")
        logger.exception("Bar fetch failed for %s", day)
        return OutcomesReport(day=day, tickers_skipped=skipped, error=str(exc))

    refs = reference_times or state.config.outcomes.reference_times_et
    forward = tuple(state.config.outcomes.forward_minutes)
    actions = _actions_for(state, day)
    bounds = state.config.sessions.bounds()

    closes = previous_closes(state, tuple(sorted(bars_by_ticker)), day, now=now)

    bar_rows = universe_rows = outcome_rows = runner_rows = tradeable = 0
    halts_total = inferred_total = 0
    summaries: dict[str, TickerDaySummary] = {}
    runner_records: list[RecentRunner] = []

    for ticker, bars in sorted(bars_by_ticker.items()):
        series = list(bars)
        if not series:
            continue
        bar_rows += len(series)
        state.lake.extend("bars_1m", day, alpaca_bars.bar_rows(ticker, day, bars, now=now))

        action = action_on(actions, ticker, day)
        prev_close = _adjust_prev_close(closes.get(ticker), action)
        halts = infer_halts(
            series, min_silent_minutes=state.config.integrity.halt_min_silent_minutes
        )
        halt_n, halt_minutes = halt_summary(halts)
        halts_total += halt_n
        inferred_total += sum(1 for halt in halts if halt.inferred)

        metrics = moves.compute(series, prev_close=prev_close)
        summaries[ticker] = TickerDaySummary(
            ticker=ticker,
            metrics=metrics,
            best_tier=_best_tier(state, ticker, day),
            poll_count=len(series),
            first_poll_ts_utc=series[0].minute,
            last_poll_ts_utc=series[-1].minute,
            session_volume=sum(bar.volume for bar in series),
            last_price=series[-1].close,
            max_gap_pct=metrics.up_move_pct,
        )

        state.lake.extend(
            "daily_universe",
            day,
            universe.daily_rows(
                ticker,
                day,
                series,
                bounds,
                now=now,
                prev_close=prev_close,
                halt_count=halt_n,
                halt_minutes=halt_minutes,
                split_flag=action is not None and action.is_split,
                split_ratio=action.ratio if action is not None else None,
                days_since_reverse_split=days_since_reverse_split(actions, ticker, day),
            ),
        )
        universe_rows += len(bounds)

        for reference in refs:
            reference_ts = et_datetime(day, reference)
            if to_utc(reference_ts) > to_utc(now):
                continue
            metrics_at = outcomes.compute(
                series,
                reference_ts=reference_ts,
                forward_minutes=forward,
                prev_close=prev_close,
                halts=halts,
                min_tradeable_dollar_volume=state.config.tradability.min_tradeable_dollar_volume,
                max_tradeable_spread_pct=state.config.tradability.max_tradeable_spread_pct,
                tradability_window_minutes=state.config.tradability.window_minutes,
            )
            state.lake.append("outcomes", day, outcomes.outcome_row(ticker, metrics_at, now=now))
            outcome_rows += 1
            tradeable += int(metrics_at.tradeable)

        detection = runners.detect(series, prev_close=prev_close, rules=_runner_rules(state))
        if not detection.is_runner:
            continue
        diagnosis = runners.diagnose(
            runners.DiagnosisInputs(
                detection=detection,
                summary=_evaluation_summary(state, ticker, day),
                alert_windows=tuple(
                    w.as_tuple() for w in state.config.schedules.alert_windows.windows
                ),
                first_news_utc=_first_news(state, ticker, now),
                near_miss_fraction=state.config.runners.near_miss_fraction,
                news_fresh_minutes=state.config.pillars.news_fresh_minutes,
            )
        )
        state.lake.append(
            "runners",
            day,
            runners.runner_row(detection, diagnosis, day, float_shares=None, now=now),
        )
        runner_rows += 1
        if not diagnosis.caught:
            runner_records.append(
                RecentRunner(
                    ticker=ticker,
                    run_date=day,
                    high_pct=detection.high_of_day_pct or 0.0,
                    float_shares=None,
                    headline=None,
                    expires_on=day + timedelta(days=state.config.runners.recent_runner_days),
                )
            )

    _update_watchlist(state, runner_records, day=day)
    logger.info(
        "Outcomes for %s: %s tickers, %s bar rows, %s outcome rows, %s runners",
        day,
        len(bars_by_ticker),
        bar_rows,
        outcome_rows,
        runner_rows,
    )
    return OutcomesReport(
        day=day,
        tickers_fetched=fetched,
        tickers_skipped=skipped,
        bar_rows=bar_rows,
        universe_rows=universe_rows,
        outcome_rows=outcome_rows,
        runner_rows=runner_rows,
        tradeable_rows=tradeable,
        halt_count=halts_total,
        inferred_halt_count=inferred_total,
        summaries=summaries,
    )


def _actions_for(state: RuntimeState, day: date) -> list[CorporateAction]:
    """Corporate actions on record for a day."""
    table = lake.read_day(state.config.storage.lake_path, "corporate_actions", day)
    return [
        CorporateAction(
            ticker=str(row["ticker"]),
            effective_date=row["effective_date"],
            action_type=str(row["action_type"]),
            ratio=row.get("ratio"),
            old_symbol=row.get("old_symbol"),
            new_symbol=row.get("new_symbol"),
        )
        for row in table.to_pylist()
    ]


def previous_closes(
    state: RuntimeState,
    tickers: tuple[str, ...],
    day: date,
    *,
    now: datetime,
) -> dict[str, float]:
    """Previous official closes for the scoped tickers.

    Two sources, in order, and never an intraday snapshot: snapshots are
    unadjusted on split days, so reading one back turns a 1:10 reverse split
    into a -90% move (CLAUDE.md 6.4.1).

    1. Yesterday's Tier 0 regular-session close, which this app wrote from
       split-adjusted bars.
    2. For anything missing — every ticker on the first day of operation, and
       any name that was not in yesterday's scope — one batched fetch of the
       previous trading day's bars from the same adjusted source.

    A ticker with no previous close at all is *logged*, not silently skipped:
    without it ``gap_pct`` and the high-of-day runner rule cannot be computed,
    and a runner missed for that reason would otherwise look like a runner that
    never happened.
    """
    previous = state.calendar.previous_trading_days(day, 1)
    if not previous:
        return {}
    prior = previous[0]

    closes: dict[str, float] = {}
    table = lake.read_day(state.config.storage.lake_path, "daily_universe", prior)
    for row in table.to_pylist():
        if row["session"] != MarketSession.REGULAR.value or row["close"] is None:
            continue
        closes[str(row["ticker"])] = float(row["close"])

    missing = tuple(ticker for ticker in tickers if ticker not in closes)
    if missing:
        for ticker, close in _closes_from_bars(state, missing, prior, now=now).items():
            closes[ticker] = close

    still_missing = [ticker for ticker in tickers if ticker not in closes]
    if still_missing:
        logger.warning(
            "No previous close for %s ticker(s) on %s (%s…): gap and high-of-day "
            "rules cannot be evaluated for them",
            len(still_missing),
            day,
            ", ".join(sorted(still_missing)[:5]),
        )
    return closes


def _closes_from_bars(
    state: RuntimeState, tickers: tuple[str, ...], prior: date, *, now: datetime
) -> dict[str, float]:
    """Last regular-session close per ticker on ``prior``, from the bar source."""
    if state.secrets.mock_data:
        from app.mock import MockDay, prev_closes

        available = prev_closes(MockDay(day=prior))
        return {ticker: available[ticker] for ticker in tickers if ticker in available}
    if not state.secrets.has_alpaca():
        return {}

    regular_end = et_datetime(prior, state.config.sessions.regular.end)
    try:
        result = alpaca_bars.fetch_bars(
            _credentials(state),
            tickers,
            start=et_datetime(prior, state.config.sessions.regular.start),
            end=regular_end,
            now=now,
            policy=_policy(state),
        )
    except SourceError as exc:
        state.warn(f"Previous-close fetch failed for {prior}: {exc}")
        logger.exception("Previous-close fetch failed for %s", prior)
        return {}
    return {
        ticker: bars[-1].close
        for ticker, bars in result.bars.items()
        if bars and to_utc(bars[-1].minute) <= to_utc(regular_end)
    }


def _adjust_prev_close(close: float | None, action: CorporateAction | None) -> float | None:
    """Put a previous close on the same basis as today's prices."""
    if close is None or action is None or not action.is_split or not action.ratio:
        return close
    from app.core.integrity import adjust_for_split

    return adjust_for_split(close, action.ratio)


def _best_tier(state: RuntimeState, ticker: str, day: date) -> Tier:
    """The highest tier this ticker reached on the day."""
    order = {Tier.NONE: 0, Tier.WATCH: 1, Tier.B: 2, Tier.A: 3}
    table = lake.read_day(state.config.storage.lake_path, "evaluations", day)
    best = Tier.NONE
    for row in table.to_pylist():
        if str(row["ticker"]) != ticker:
            continue
        tier = Tier(str(row["tier"]))
        if order[tier] > order[best]:
            best = tier
    return best


def _evaluation_summary(state: RuntimeState, ticker: str, day: date) -> runners.EvaluationSummary:
    """What the scanner saw for one ticker, rebuilt from stored evaluations."""
    from app.core.pillars import PILLAR_NAMES
    from app.core.types import PillarResult, PillarStatus

    table = lake.read_day(state.config.storage.lake_path, "evaluations", day)
    rows = [row for row in table.to_pylist() if str(row["ticker"]) == ticker]
    if not rows:
        return runners.EvaluationSummary(ticker=ticker, evaluated=False)

    latest = max(rows, key=lambda row: to_utc(row["poll_ts_utc"]))
    thresholds = state.config.pillars
    limits = {
        1: thresholds.gap_pct_min,
        2: thresholds.rvol_min,
        3: float(thresholds.news_fresh_minutes),
        4: thresholds.price_max,
        5: float(thresholds.float_shares_max),
    }
    values = {
        1: latest.get("gap_pct"),
        2: latest.get("rvol"),
        3: latest.get("news_age_minutes"),
        4: latest.get("price"),
        5: float(latest["float_shares"]) if latest.get("float_shares") is not None else None,
    }
    results = tuple(
        PillarResult(
            number=number,
            name=PILLAR_NAMES[number],
            status=PillarStatus(str(latest[f"pillar_{number}_status"])),
            value=values[number],
            threshold=limits[number],
        )
        for number in range(1, 6)
    )
    return runners.EvaluationSummary(
        ticker=ticker,
        best_tier=_best_tier(state, ticker, day),
        pillar_results=results,
        evaluated=True,
    )


def _first_news(state: RuntimeState, ticker: str, now: datetime) -> datetime | None:
    """The day's first headline for a ticker, from the live cache."""
    item = state.news.first_for(ticker, on=now)
    return item.created_at if item else None


def _runner_rules(state: RuntimeState) -> runners.RunnerRules:
    """Runner definition from config."""
    config = state.config.runners
    return runners.RunnerRules(
        high_of_day_pct_min=config.high_of_day_pct_min,
        intraday_move_pct_min=config.intraday_move_pct_min,
        intraday_lookback_minutes=config.intraday_lookback_minutes,
        intraday_window=config.intraday_window_et.as_tuple(),
        postmarket_move_pct_min=config.postmarket_move_pct_min,
        min_price=config.min_price,
        min_dollar_volume=config.min_dollar_volume,
        move_start_trigger_pct=config.move_start_trigger_pct,
    )


def _update_watchlist(state: RuntimeState, records: list[RecentRunner], *, day: date) -> None:
    """Add the day's missed runners to the watchlist and expire the old ones."""
    with db.session(state.sqlite_path) as connection:
        for record in records:
            watchlist_add(connection, record)
        watchlist_prune(connection, today=day)
        from app.recent_runners import tickers as watchlist_tickers

        state.set_recent_runners(watchlist_tickers(connection, today=day))


# --- 11:10 digest ------------------------------------------------------------


def run_digest_push(state: RuntimeState, *, now: datetime) -> str | None:
    """Send the optional daily digest push. Returns the message, or ``None``."""
    if not state.config.alerts.daily_digest_enabled:
        return None
    day = et_trading_date(now)
    table = lake.read_day(state.config.storage.lake_path, "runners", day)
    rows = table.to_pylist()
    missed = [row for row in rows if "CAUGHT" not in (row.get("miss_reasons") or [])]
    if not missed:
        logger.info("Digest skipped: nothing missed on %s", day)
        return None

    from app.notify import push

    entries = [
        RecentRunner(
            ticker=str(row["ticker"]),
            run_date=day,
            high_pct=float(row.get("high_of_day_pct") or 0.0),
            float_shares=row.get("float_shares"),
            headline=None,
            expires_on=day,
        )
        for row in missed
    ]
    from app.recent_runners import digest_line

    message = digest_line(entries, missed=len(missed))
    if not state.secrets.has_vapid():
        logger.info("Digest not pushed (no VAPID keys): %s", message)
        return message

    keys = push.VapidKeys(
        public_key=state.secrets.vapid_public_key,
        private_key=state.secrets.vapid_private_key,
        subject=state.secrets.vapid_subject,
    )
    body = push.notification_body(
        "Missed runners", message, url="/runners", tag=f"digest-{day.isoformat()}"
    )
    with db.session(state.sqlite_path) as connection:
        push.broadcast(connection, body, keys, sender=push.pywebpush_sender(), now=now)
    return message


# --- 20:45 pruning, compaction, retention, quality ---------------------------


@dataclass(frozen=True, slots=True)
class NightlyReport:
    """What the closing job did."""

    day: date
    prune: pruning.PruneReport | None = None
    retention_actions: int = 0
    compacted_tables: int = 0
    quality_rows: int = 0
    size_warning: str | None = None


def run_nightly(
    state: RuntimeState,
    *,
    now: datetime,
    summaries: dict[str, TickerDaySummary] | None = None,
) -> NightlyReport:
    """Prune, compact, apply retention and write the data-quality rows.

    Runs last because pruning needs the day's move metrics and the quality row
    needs the pruning counts. Flushes the buffer first: pruning rewrites the
    ``snapshots`` partition, and rows still in memory would be lost.
    """
    day = et_trading_date(now)
    state.lake.flush(now=now)

    prune_report: pruning.PruneReport | None = None
    if summaries:
        prune_report, pruned_rows = pruning.prune_day(
            state.config.storage.lake_path,
            day,
            summaries,
            now=now,
            move_threshold_pct=state.config.retention.move_threshold_pct,
            control_sample_pct=state.config.retention.control_sample_pct,
            compression=state.config.storage.parquet_compression,
        )
        state.lake.extend("pruned_summary", day, pruned_rows)
        state.lake.flush(now=now)
    else:
        logger.warning(
            "Nightly pruning skipped for %s: no move metrics, so nothing can be "
            "classified and discarding snapshots would be unrecoverable",
            day,
        )

    compacted = 0
    for table in _lake_tables():
        if lake.partition_path(state.config.storage.lake_path, table, day).exists():
            lake.compact_day(
                state.config.storage.lake_path,
                table,
                day,
                now=now,
                compression=state.config.storage.parquet_compression,
            )
            compacted += 1

    policy = RetentionPolicy(
        bars_1m_raw_days=state.config.retention.bars_1m_raw_days,
        bars_1m_thinned_minutes=state.config.retention.bars_1m_thinned_minutes,
        snapshots_months=state.config.retention.snapshots_months,
        max_lake_gb=state.config.retention.max_lake_gb,
        warn_fraction=state.config.retention.lake_warn_fraction,
    )
    actions = pruning.apply_retention(
        state.config.storage.lake_path, today=day, policy=policy, now=now
    )

    sizes = lake.lake_size_bytes(state.config.storage.lake_path)
    warning = pruning.size_warning(sizes, policy)
    if warning:
        state.warn(warning)

    rows = _quality_rows(state, day, prune_report, now=now)
    state.lake.extend("data_quality", day, rows)
    state.lake.flush(now=now)

    logger.info(
        "Nightly for %s: %s compacted, %s retention actions, %s quality rows",
        day,
        compacted,
        len(actions),
        len(rows),
    )
    return NightlyReport(
        day=day,
        prune=prune_report,
        retention_actions=len(actions),
        compacted_tables=compacted,
        quality_rows=len(rows),
        size_warning=warning,
    )


def _lake_tables() -> tuple[str, ...]:
    """Tables worth compacting at the end of a day."""
    return ("snapshots", "evaluations", "reference", "bars_1m", "outcomes", "news")


def _quality_rows(
    state: RuntimeState,
    day: date,
    prune_report: pruning.PruneReport | None,
    *,
    now: datetime,
) -> list[lake.LakeRow]:
    """One data-quality row per table, with the integrity metrics of 6.4."""
    root = state.config.storage.lake_path
    rows: list[lake.LakeRow] = []

    evaluations = lake.read_day(root, "evaluations", day).to_pylist()
    low_confidence = sum(1 for row in evaluations if row.get("float_confidence") == "low")
    outcome_rows = lake.read_day(root, "outcomes", day).to_pylist()
    news_rows = lake.read_day(root, "news", day).to_pylist()
    latencies = [
        float(row["feed_latency_s"]) for row in news_rows if row.get("feed_latency_s") is not None
    ]
    snapshots = lake.read_day(root, "snapshots", day).to_pylist()

    counters = quality.QualityCounters(
        table_name="evaluations",
        rows_collected=len(evaluations),
        pillar5_evaluations=len(evaluations),
        pillar5_low_confidence=low_confidence,
    )
    rows.append(quality.build_row(counters, day, now=now))

    outcome_counters = quality.QualityCounters(
        table_name="outcomes",
        rows_collected=len(outcome_rows),
        outcome_rows=len(outcome_rows),
        tradeable_rows=sum(1 for row in outcome_rows if row.get("tradeable")),
    )
    rows.append(quality.build_row(outcome_counters, day, now=now))

    news_counters = quality.QualityCounters(
        table_name="news",
        rows_collected=len(news_rows),
        news_latencies_s=latencies,
        ws_disconnect_minutes=state.feed_health.outage_minutes(now=now),
    )
    rows.append(quality.build_row(news_counters, day, now=now))

    snapshot_counters = quality.QualityCounters(
        table_name="snapshots",
        rows_collected=len(snapshots),
        suspect_price_rows=sum(1 for row in snapshots if row.get("suspect_price")),
        mover_count=prune_report.mover_count if prune_report else 0,
        control_count=prune_report.control_count if prune_report else 0,
        dropped_count=prune_report.dropped_count if prune_report else 0,
        move_threshold_pct=prune_report.move_threshold_pct if prune_report else None,
    )
    rows.append(quality.build_row(snapshot_counters, day, now=now))

    for row in rows:
        for warning in quality.warnings_for(
            row, latency_p95_warn_s=state.config.news.feed_latency_p95_warn_seconds
        ):
            state.warn(warning.message)
    return rows
