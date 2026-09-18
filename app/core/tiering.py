"""Alert tiering, push deduplication and the per-window push budget.

All of it is pure: the caller supplies the window's push history, this module
decides. Keeping the decision here rather than inside the notifier is what
makes "one push per (ticker, tier) per window, but a B to A upgrade pushes
again" testable without a browser or a network (CLAUDE.md 5.6).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.pillars import PillarScore
from app.core.types import Tier

#: A tier is an upgrade of another when it appears later in this ordering.
_TIER_ORDER: dict[Tier, int] = {Tier.NONE: 0, Tier.WATCH: 1, Tier.B: 2, Tier.A: 3}


def is_upgrade(previous: Tier, current: Tier) -> bool:
    """Whether ``current`` outranks ``previous``."""
    return _TIER_ORDER[current] > _TIER_ORDER[previous]


def classify(score: PillarScore, *, is_recent_runner: bool = False) -> Tier:
    """Assign a tier from the five pillar verdicts.

    ``is_recent_runner`` feeds yesterday's movers back into today's scan: a
    stock that ran 84% two days ago and is up again today is worth eyes even
    when it passes only pillar 1 (CLAUDE.md 7.4).
    """
    if score.passed(1, 2, 3, 4, 5):
        return Tier.A
    if score.passed(1, 2, 4, 5):
        return Tier.B
    if is_recent_runner and score.passed(1):
        return Tier.WATCH
    if score.passed(1) and score.passed_count >= 3:
        return Tier.WATCH
    return Tier.NONE


@dataclass(slots=True)
class WindowPushState:
    """Push bookkeeping for one alert window.

    Lives for the duration of a single window. The caller persists nothing from
    it beyond the alert rows themselves; a restart mid-window therefore starts
    the budget over, which is the safer direction (a duplicate push beats a
    missed one).
    """

    max_pushes: int
    tier_b_enabled: bool
    pushed: dict[str, Tier] = field(default_factory=dict)
    push_count: int = 0

    @property
    def budget_left(self) -> int:
        """Pushes still available in this window."""
        return max(0, self.max_pushes - self.push_count)


@dataclass(frozen=True, slots=True)
class PushDecision:
    """Whether to push, and the reason — logged either way.

    The reason is kept because "why did I not get an alert for that?" is a
    question the trader will ask about a specific ticker weeks later, and the
    UI can only answer it if the refusal was recorded.
    """

    should_push: bool
    reason: str


def decide_push(state: WindowPushState, ticker: str, tier: Tier) -> PushDecision:
    """Decide whether this (ticker, tier) warrants a browser push right now.

    The rules, in order: only A and B push at all; B pushes only if enabled in
    settings; the same tier never pushes twice for the same ticker in a window;
    an upgrade from B to A does push again; and the window's budget caps the
    total, with everything above it still reaching the UI.
    """
    if tier not in (Tier.A, Tier.B):
        return PushDecision(should_push=False, reason=f"tier {tier} is UI-only")
    if tier is Tier.B and not state.tier_b_enabled:
        return PushDecision(should_push=False, reason="tier B pushes disabled in settings")

    previous = state.pushed.get(ticker)
    if previous is not None and not is_upgrade(previous, tier):
        return PushDecision(
            should_push=False,
            reason=f"already pushed {ticker} at tier {previous} in this window",
        )
    if state.budget_left <= 0:
        return PushDecision(
            should_push=False,
            reason=f"window push budget of {state.max_pushes} exhausted; UI only",
        )
    if previous is not None:
        return PushDecision(should_push=True, reason=f"upgrade {previous} -> {tier}")
    return PushDecision(should_push=True, reason=f"first {tier} alert for {ticker}")


def record_push(state: WindowPushState, ticker: str, tier: Tier) -> None:
    """Register a push that was actually sent, consuming budget.

    Separate from :func:`decide_push` so a push that fails to send does not
    consume the window's budget and does not suppress the next attempt.
    """
    state.pushed[ticker] = tier
    state.push_count += 1
