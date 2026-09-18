"""The alert pipeline: one poll inside a window, end to end.

Snapshot rows in, ``evaluations`` rows and push decisions out::

    snapshot ─► momentum metrics ─► pillar check ─► tiering ─► dedup ─► push
                                        ▲
                     news cache ────────┘

The rule that shapes this module is CLAUDE.md 5.6: **every evaluation is
stored**, not only the ones that alert. The rows that never fired are what make
the missed-runner analysis and all future research possible — a lake of alerts
alone answers "what did I catch?" and nothing else.

Pure apart from the row building: the caller supplies the snapshot, the news
cache, the RVOL inputs and the clock, so a whole window can be replayed in a
test.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import date, datetime

from app.core import metrics, pillars, tiering
from app.core.news import NewsCache
from app.core.timeutils import et_trading_date, to_utc
from app.core.types import (
    FloatConfidence,
    NewsItem,
    PillarStatus,
    PillarThresholds,
    RankWeights,
    RvolSource,
    Tier,
)
from app.sources.tradingview import TradingViewRow
from app.storage.lake import LakeRow

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RvolInputs:
    """Everything needed to resolve a ticker's relative volume.

    ``baseline_volume`` is the time-of-day figure when it has been computed for
    this ticker today; when it is absent the fallback applies and the row
    records that it did.
    """

    baseline_volume: float | None
    average_volume_10d: float | None
    session_fraction: float


@dataclass(frozen=True, slots=True)
class Evaluation:
    """One ticker's full verdict at one poll.

    Carries the raw inputs as well as the verdict so a past evaluation can be
    re-judged under a changed threshold without refetching anything — which is
    what the what-if counter on the Missed Runners page does.
    """

    ticker: str
    poll_ts_utc: datetime
    window_start_utc: datetime
    price: float | None
    gap_pct: float | None
    window_change_pct: float | None
    rvol: float | None
    rvol_source: RvolSource
    float_shares: int | None
    float_confidence: FloatConfidence
    news: NewsItem | None
    score: pillars.PillarScore
    tier: Tier
    is_recent_runner: bool
    rank_score: float | None = None
    push_decision: tiering.PushDecision | None = None

    @property
    def pushed(self) -> bool:
        """Whether this evaluation resulted in a push being sent."""
        return self.push_decision is not None and self.push_decision.should_push


@dataclass(frozen=True, slots=True)
class WindowContext:
    """Everything a window poll needs that does not come from the snapshot."""

    window_start_utc: datetime
    thresholds: PillarThresholds
    weights: RankWeights
    float_turnover_low_confidence: float
    float_asof_max_age_days: int
    recent_runners: frozenset[str] = frozenset()
    news_feed_healthy: bool = True


@dataclass(slots=True)
class WindowRun:
    """Accumulated results of one poll, ready for storage and notification."""

    poll_ts_utc: datetime
    evaluations: list[Evaluation] = field(default_factory=list)
    pushes: list[Evaluation] = field(default_factory=list)

    @property
    def by_tier(self) -> dict[Tier, int]:
        """How many evaluations landed in each tier."""
        counts: dict[Tier, int] = dict.fromkeys(Tier, 0)
        for evaluation in self.evaluations:
            counts[evaluation.tier] += 1
        return counts


def resolve_news(
    cache: NewsCache,
    ticker: str,
    *,
    as_of: datetime,
    fresh_minutes: int,
    feed_healthy: bool,
) -> tuple[NewsItem | None, bool]:
    """The ticker's fresh catalyst, and whether pillar 3 is even evaluable.

    When the feed is down there is no information either way, so the caller
    must record pillar 3 as *unknown* rather than as a failure. Recording a
    feed outage as "no catalyst" would fill the research data with false
    negatives that look exactly like real ones (CLAUDE.md 6.6).
    """
    if not feed_healthy:
        logger.debug("News feed unhealthy at %s; pillar 3 is unknown for %s", as_of, ticker)
        return None, False
    return cache.fresh_for(ticker, as_of=as_of, fresh_minutes=fresh_minutes), True


def evaluate_row(
    row: TradingViewRow,
    *,
    context: WindowContext,
    poll_ts_utc: datetime,
    news_cache: NewsCache,
    rvol_inputs: RvolInputs,
    window_open_price: float | None,
    prev_close: float | None,
) -> Evaluation:
    """Evaluate one ticker at one poll."""
    price = row.close
    gap = metrics.gap_pct(price, prev_close) if price is not None else None
    if gap is None:
        gap = row.change_pct

    rvol, rvol_source = metrics.resolve_rvol(
        row.volume or 0.0,
        rvol_inputs.baseline_volume,
        rvol_inputs.average_volume_10d,
        rvol_inputs.session_fraction,
    )
    turnover = metrics.float_turnover(row.volume or 0.0, row.float_shares)
    confidence = metrics.float_confidence(
        float_shares=row.float_shares,
        turnover=turnover,
        float_asof=None,
        as_of=poll_ts_utc,
        turnover_low_threshold=context.float_turnover_low_confidence,
        max_age_days=context.float_asof_max_age_days,
    )
    fresh_news, news_evaluable = resolve_news(
        news_cache,
        row.ticker,
        as_of=poll_ts_utc,
        fresh_minutes=context.thresholds.news_fresh_minutes,
        feed_healthy=context.news_feed_healthy,
    )

    score = pillars.evaluate(
        pillars.PillarInputs(
            gap_pct=gap,
            rvol=rvol,
            price=price,
            float_shares=row.float_shares,
            float_confidence=confidence,
            fresh_news=fresh_news,
        ),
        context.thresholds,
        as_of=poll_ts_utc,
    )
    if not news_evaluable:
        score = _mark_news_unknown(score)

    is_recent_runner = row.ticker in context.recent_runners
    return Evaluation(
        ticker=row.ticker,
        poll_ts_utc=to_utc(poll_ts_utc),
        window_start_utc=to_utc(context.window_start_utc),
        price=price,
        gap_pct=gap,
        window_change_pct=metrics.window_change_pct(price, window_open_price)
        if price is not None
        else None,
        rvol=rvol,
        rvol_source=rvol_source,
        float_shares=row.float_shares,
        float_confidence=confidence,
        news=fresh_news,
        score=score,
        tier=tiering.classify(score, is_recent_runner=is_recent_runner),
        is_recent_runner=is_recent_runner,
    )


def _mark_news_unknown(score: pillars.PillarScore) -> pillars.PillarScore:
    """Replace pillar 3's verdict with ``unknown`` when the feed was down."""
    replaced = tuple(
        result
        if result.number != 3
        else type(result)(
            number=3,
            name=result.name,
            status=PillarStatus.UNKNOWN,
            value=None,
            threshold=result.threshold,
            detail="news feed unavailable; catalyst not evaluable",
        )
        for result in score.results
    )
    return pillars.PillarScore(results=replaced)


def run_window_poll(
    rows: list[TradingViewRow],
    *,
    context: WindowContext,
    poll_ts_utc: datetime,
    news_cache: NewsCache,
    rvol_for: dict[str, RvolInputs],
    window_open_prices: dict[str, float],
    prev_closes: dict[str, float],
    push_state: tiering.WindowPushState,
) -> WindowRun:
    """Evaluate every row in one poll, rank them and decide on pushes.

    Ranking happens across the whole poll rather than per ticker, since a rank
    is only meaningful relative to the others in the same snapshot.
    """
    run = WindowRun(poll_ts_utc=to_utc(poll_ts_utc))
    evaluations = [
        evaluate_row(
            row,
            context=context,
            poll_ts_utc=poll_ts_utc,
            news_cache=news_cache,
            rvol_inputs=rvol_for.get(row.ticker, RvolInputs(None, row.average_volume_10d, 0.05)),
            window_open_price=window_open_prices.get(row.ticker),
            prev_close=prev_closes.get(row.ticker),
        )
        for row in rows
    ]

    change_ranks = metrics.percentile_ranks([e.window_change_pct for e in evaluations])
    rvol_ranks = metrics.percentile_ranks([e.rvol for e in evaluations])
    gap_ranks = metrics.percentile_ranks([e.gap_pct for e in evaluations])

    ranked = [
        replace(
            evaluation,
            rank_score=metrics.rank_score(
                change_ranks[index], rvol_ranks[index], gap_ranks[index], context.weights
            ),
        )
        for index, evaluation in enumerate(evaluations)
    ]

    # Highest-ranked first, so the window's limited push budget is spent on the
    # strongest candidates rather than on whatever the feed happened to list first.
    ranked.sort(key=lambda e: e.rank_score if e.rank_score is not None else -1.0, reverse=True)

    for evaluation in ranked:
        decision = tiering.decide_push(push_state, evaluation.ticker, evaluation.tier)
        decided = replace(evaluation, push_decision=decision)
        run.evaluations.append(decided)
        if decision.should_push:
            tiering.record_push(push_state, evaluation.ticker, evaluation.tier)
            run.pushes.append(decided)
            logger.info(
                "Tier %s alert: %s at %s (%s)",
                evaluation.tier,
                evaluation.ticker,
                evaluation.price,
                decision.reason,
            )
    return run


def alert_id_for(evaluation: Evaluation) -> str:
    """A stable id for one (ticker, window, tier) alert.

    Deterministic rather than a UUID so the journal can reference an alert the
    trader tapped on, and so replaying a window does not create a second row
    for the same alert.
    """
    day = et_trading_date(evaluation.poll_ts_utc)
    window = to_utc(evaluation.window_start_utc).strftime("%H%M")
    return f"{day.isoformat()}-{window}-{evaluation.ticker}-{evaluation.tier.value}"


def evaluation_row(evaluation: Evaluation, *, now: datetime) -> LakeRow:
    """Build the ``evaluations`` lake row for one verdict.

    Every evaluation is written, alerting or not: this table is the denominator
    for every question the Missed Runners page and the research notebook ask.
    """
    day: date = et_trading_date(evaluation.poll_ts_utc)
    news_age = evaluation.score.by_number(3).value
    return {
        "ticker": evaluation.ticker,
        "date": day,
        "poll_ts_utc": evaluation.poll_ts_utc,
        "window_start_utc": evaluation.window_start_utc,
        "price": evaluation.price,
        "gap_pct": evaluation.gap_pct,
        "window_change_pct": evaluation.window_change_pct,
        "rvol": evaluation.rvol,
        "rvol_source": evaluation.rvol_source.value,
        "float_shares": evaluation.float_shares,
        "float_confidence": evaluation.float_confidence.value,
        "news_age_minutes": news_age,
        "news_id": evaluation.news.news_id if evaluation.news else None,
        "pillar_1_status": evaluation.score.by_number(1).status.value,
        "pillar_2_status": evaluation.score.by_number(2).status.value,
        "pillar_3_status": evaluation.score.by_number(3).status.value,
        "pillar_4_status": evaluation.score.by_number(4).status.value,
        "pillar_5_status": evaluation.score.by_number(5).status.value,
        "pillars_passed": evaluation.score.passed_count,
        "pillars_unknown": evaluation.score.unknown_count,
        "tier": evaluation.tier.value,
        "rank_score": evaluation.rank_score,
        "is_recent_runner": evaluation.is_recent_runner,
        "pushed": evaluation.pushed,
        "push_reason": evaluation.push_decision.reason if evaluation.push_decision else None,
        "written_at_utc": to_utc(now),
    }


def push_payload(evaluation: Evaluation) -> str:
    """The notification text (CLAUDE.md 8).

    Every number in it is one the trader can check against the UI: tier,
    move, relative volume, float, price and the headline that justified it.
    """
    gap = f"{evaluation.gap_pct:+.0f}%" if evaluation.gap_pct is not None else "?"
    rvol = f"{evaluation.rvol:.0f}x" if evaluation.rvol is not None else "RVOL ?"
    if evaluation.float_shares is not None:
        float_text = f"Float {evaluation.float_shares / 1_000_000:.1f}M"
    else:
        float_text = "Float ?"
    price = f"${evaluation.price:.2f}" if evaluation.price is not None else "$?"
    headline = evaluation.news.headline if evaluation.news else "no fresh catalyst"
    return (
        f"[{evaluation.tier.value}] {evaluation.ticker} {gap} | RVOL {rvol} | "
        f"{float_text} | {price} — {headline[:60]}"
    )
