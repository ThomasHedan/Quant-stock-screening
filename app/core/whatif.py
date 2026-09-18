"""Threshold what-if counts for the Missed Runners insights panel.

Answers "float < 30M instead of 20M → +N runners caught, +M extra alerts that
did not run" from stored evaluations, without refetching anything. That is only
possible because every evaluation is stored with its raw values, not just its
verdict (CLAUDE.md 5.6, 7.3).

Two rules this module exists to enforce:

* **Both numbers, always.** A loosened threshold that catches three more
  runners while producing forty more alerts that went nowhere is not an
  improvement, and showing only the first number would make it look like one.
* **It never changes anything.** Thresholds are the trader's decision, made in
  settings. This module counts; it does not tune (CLAUDE.md 7.3).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EvaluationRecord:
    """One stored evaluation, reduced to what a what-if needs.

    ``was_runner`` comes from the end-of-day runner table, so it is the
    outcome, not a prediction: this is a retrospective count over labelled
    days, not a backtest.
    """

    ticker: str
    day: str
    gap_pct: float | None
    rvol: float | None
    price: float | None
    float_shares: int | None
    had_fresh_news: bool
    was_runner: bool
    already_alerted: bool


@dataclass(frozen=True, slots=True)
class Thresholds:
    """A candidate threshold set."""

    gap_pct_min: float
    rvol_min: float
    price_min: float
    price_max: float
    float_shares_max: int
    require_news: bool = True


@dataclass(frozen=True, slots=True)
class WhatIfResult:
    """What a threshold change would have done over the sampled days.

    ``extra_alerts_that_ran`` and ``extra_alerts_that_did_not`` are reported
    separately rather than as a hit rate, because with a handful of runners a
    ratio looks precise while resting on three observations.
    """

    label: str
    runners_caught: int
    runners_missed: int
    extra_runners_caught: int
    extra_alerts_that_ran: int
    extra_alerts_that_did_not: int
    baseline_alerts: int
    candidate_alerts: int

    @property
    def summary(self) -> str:
        """One line for the insights panel, always showing both numbers."""
        return (
            f"{self.label}: +{self.extra_runners_caught} runners caught, "
            f"+{self.extra_alerts_that_did_not} extra alerts that did not run"
        )


def passes(record: EvaluationRecord, thresholds: Thresholds) -> bool:
    """Whether a stored evaluation would alert under a threshold set.

    Missing data never passes: an unknown float is not evidence of a small
    one, and counting it as a pass would inflate every what-if in the
    direction of loosening (CLAUDE.md 5.4).
    """
    if record.gap_pct is None or record.gap_pct < thresholds.gap_pct_min:
        return False
    if record.rvol is None or record.rvol < thresholds.rvol_min:
        return False
    if record.price is None or not (thresholds.price_min <= record.price <= thresholds.price_max):
        return False
    if record.float_shares is None or record.float_shares >= thresholds.float_shares_max:
        return False
    return not (thresholds.require_news and not record.had_fresh_news)


def evaluate_change(
    records: list[EvaluationRecord],
    *,
    baseline: Thresholds,
    candidate: Thresholds,
    label: str,
) -> WhatIfResult:
    """Count what a threshold change would have caught and cost.

    Counts are per ticker-day, not per poll: a ticker evaluated ten times in a
    window is one opportunity, and counting polls would multiply every number
    by the poll rate.
    """
    by_day: dict[tuple[str, str], list[EvaluationRecord]] = {}
    for record in records:
        by_day.setdefault((record.ticker, record.day), []).append(record)

    baseline_alerts = 0
    candidate_alerts = 0
    runners_caught = 0
    runners_missed = 0
    extra_runners = 0
    extra_ran = 0
    extra_did_not = 0

    for (ticker, day), polls in by_day.items():
        was_runner = any(poll.was_runner for poll in polls)
        base_hit = any(passes(poll, baseline) for poll in polls)
        cand_hit = any(passes(poll, candidate) for poll in polls)

        baseline_alerts += int(base_hit)
        candidate_alerts += int(cand_hit)

        if was_runner and cand_hit:
            runners_caught += 1
        if was_runner and not cand_hit:
            runners_missed += 1
        if cand_hit and not base_hit:
            if was_runner:
                extra_runners += 1
                extra_ran += 1
            else:
                extra_did_not += 1
        logger.debug(
            "what-if %s/%s: base=%s candidate=%s runner=%s",
            ticker,
            day,
            base_hit,
            cand_hit,
            was_runner,
        )

    return WhatIfResult(
        label=label,
        runners_caught=runners_caught,
        runners_missed=runners_missed,
        extra_runners_caught=extra_runners,
        extra_alerts_that_ran=extra_ran,
        extra_alerts_that_did_not=extra_did_not,
        baseline_alerts=baseline_alerts,
        candidate_alerts=candidate_alerts,
    )


def sweep(
    records: list[EvaluationRecord],
    *,
    baseline: Thresholds,
    variations: dict[str, Callable[[Thresholds], Thresholds]],
) -> list[WhatIfResult]:
    """Run several candidate changes against the same records.

    Presented as a list rather than a ranking on purpose. Sorting by runners
    caught would nominate a "best" threshold, and with a few hundred
    observations the best of fifty variations is usually chance
    (CLAUDE.md 6.8, multiple testing).
    """
    return [
        evaluate_change(records, baseline=baseline, candidate=change(baseline), label=label)
        for label, change in variations.items()
    ]
