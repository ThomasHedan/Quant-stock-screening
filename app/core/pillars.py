"""The Warrior Trading 5 Pillars, evaluated from already-fetched values.

Three rules govern every pillar here and they are the whole point of the
module:

1. Missing data yields ``UNKNOWN``, never ``FAIL`` and never a default value.
   A null float that silently became ``0`` would pass the supply pillar.
2. Thresholds are compared with explicit ``>=`` / ``<=`` — never ``==`` — and
   are tested exactly at the boundary.
3. Every result carries the raw value and the threshold, so the end-of-day
   missed-runner diagnosis can say *by how much* a pillar missed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.core.timeutils import minutes_between
from app.core.types import (
    FloatConfidence,
    NewsItem,
    PillarResult,
    PillarStatus,
    PillarThresholds,
)

PILLAR_NAMES: dict[int, str] = {
    1: "up_on_day",
    2: "relative_volume",
    3: "news_catalyst",
    4: "price_range",
    5: "float",
}


@dataclass(frozen=True, slots=True)
class PillarInputs:
    """Everything the five pillars need, gathered by the caller.

    A single explicit container rather than a bare dict so that a renamed or
    missing upstream field is a type error here instead of a silent ``None``
    deep inside a comparison (CLAUDE.md 1.1).
    """

    gap_pct: float | None
    rvol: float | None
    price: float | None
    float_shares: int | None
    float_confidence: FloatConfidence
    fresh_news: NewsItem | None


@dataclass(frozen=True, slots=True)
class PillarScore:
    """The five verdicts plus the counts the tiering rules are written in."""

    results: tuple[PillarResult, ...]

    def by_number(self, number: int) -> PillarResult:
        """The verdict for one pillar."""
        for result in self.results:
            if result.number == number:
                return result
        msg = f"no pillar numbered {number}"
        raise KeyError(msg)

    @property
    def passed_count(self) -> int:
        """How many pillars explicitly passed. Unknown never counts."""
        return sum(1 for result in self.results if result.status is PillarStatus.PASS)

    @property
    def unknown_count(self) -> int:
        """How many pillars could not be evaluated at all."""
        return sum(1 for result in self.results if result.status is PillarStatus.UNKNOWN)

    def passed(self, *numbers: int) -> bool:
        """Whether all of the given pillars explicitly passed."""
        return all(self.by_number(number).status is PillarStatus.PASS for number in numbers)


def _verdict(value: float | None, *, ok: bool) -> PillarStatus:
    """Map a present/absent value plus a comparison into a status."""
    if value is None:
        return PillarStatus.UNKNOWN
    return PillarStatus.PASS if ok else PillarStatus.FAIL


def check_up_on_day(gap_pct: float | None, threshold: float) -> PillarResult:
    """Pillar 1 — demand: the stock is up meaningfully on the session."""
    return PillarResult(
        number=1,
        name=PILLAR_NAMES[1],
        status=_verdict(gap_pct, ok=gap_pct is not None and gap_pct >= threshold),
        value=gap_pct,
        threshold=threshold,
        detail="" if gap_pct is not None else "gap_pct unavailable",
    )


def check_relative_volume(rvol: float | None, threshold: float) -> PillarResult:
    """Pillar 2 — demand: unusual participation for this time of day."""
    return PillarResult(
        number=2,
        name=PILLAR_NAMES[2],
        status=_verdict(rvol, ok=rvol is not None and rvol >= threshold),
        value=rvol,
        threshold=threshold,
        detail="" if rvol is not None else "no baseline and no fallback input",
    )


def check_news_catalyst(
    fresh_news: NewsItem | None,
    *,
    as_of: datetime,
    fresh_minutes: int,
) -> PillarResult:
    """Pillar 3 — demand: a genuinely fresh catalyst.

    Freshness is measured on ``created_at`` only. ``updated_at`` moves when an
    article is revised hours later, so judging freshness on it would let
    tomorrow's edit make today's news look fresh — lookahead through the back
    door (CLAUDE.md 6.4.6).

    Absence of news is a definite ``FAIL``, not ``UNKNOWN``: the feed being
    connected and quiet is real information. A caller that knows the feed was
    *down* should not call this at all and should record the pillar unknown.
    """
    if fresh_news is None:
        return PillarResult(
            number=3,
            name=PILLAR_NAMES[3],
            status=PillarStatus.FAIL,
            value=None,
            threshold=float(fresh_minutes),
            detail="no news within the freshness window",
        )
    age_minutes = minutes_between(fresh_news.created_at, as_of)
    is_fresh = 0 <= age_minutes <= fresh_minutes
    detail = f"{fresh_news.headline[:80]}"
    if age_minutes < 0:
        detail = f"news timestamped {abs(age_minutes):.1f} min in the future; not trusted"
    return PillarResult(
        number=3,
        name=PILLAR_NAMES[3],
        status=PillarStatus.PASS if is_fresh else PillarStatus.FAIL,
        value=age_minutes,
        threshold=float(fresh_minutes),
        detail=detail,
    )


def check_price_range(price: float | None, low: float, high: float) -> PillarResult:
    """Pillar 4 — demand: the price band the strategy is defined on."""
    in_range = price is not None and low <= price <= high
    return PillarResult(
        number=4,
        name=PILLAR_NAMES[4],
        status=_verdict(price, ok=in_range),
        value=price,
        threshold=high,
        detail="" if price is not None else "price unavailable",
    )


def check_float(
    float_shares: int | None,
    confidence: FloatConfidence,
    threshold: int,
) -> PillarResult:
    """Pillar 5 — supply: a small enough float for demand to move the price.

    Low confidence forces ``UNKNOWN`` whatever the number says. This is the
    single most important guard in the file: float is the least reliable field
    in free data, it is stale exactly where it matters most (recent IPOs,
    post-offering names), and it is the only supply pillar (CLAUDE.md 6.4.4).
    """
    if confidence is FloatConfidence.LOW:
        return PillarResult(
            number=5,
            name=PILLAR_NAMES[5],
            status=PillarStatus.UNKNOWN,
            value=float(float_shares) if float_shares is not None else None,
            threshold=float(threshold),
            detail="float_confidence=low; not trusted either way",
        )
    value = float(float_shares) if float_shares is not None else None
    return PillarResult(
        number=5,
        name=PILLAR_NAMES[5],
        status=_verdict(value, ok=float_shares is not None and float_shares < threshold),
        value=value,
        threshold=float(threshold),
        detail="" if float_shares is not None else "float unavailable",
    )


def evaluate(
    inputs: PillarInputs,
    thresholds: PillarThresholds,
    *,
    as_of: datetime,
) -> PillarScore:
    """Evaluate all five pillars for one ticker at one instant."""
    return PillarScore(
        results=(
            check_up_on_day(inputs.gap_pct, thresholds.gap_pct_min),
            check_relative_volume(inputs.rvol, thresholds.rvol_min),
            check_news_catalyst(
                inputs.fresh_news,
                as_of=as_of,
                fresh_minutes=thresholds.news_fresh_minutes,
            ),
            check_price_range(inputs.price, thresholds.price_min, thresholds.price_max),
            check_float(inputs.float_shares, inputs.float_confidence, thresholds.float_shares_max),
        )
    )


def near_miss(result: PillarResult, fraction: float) -> bool:
    """Whether a failed pillar missed its threshold by less than ``fraction``.

    Used by the missed-runner page to separate "this setup was nowhere close"
    from "a 23M float against a 20M rule", which is the difference between a
    rule that is wrong and a threshold that is slightly mistuned
    (CLAUDE.md 7.2).
    """
    if result.status is not PillarStatus.FAIL:
        return False
    if result.value is None or result.threshold is None:
        return False
    if result.threshold == 0:
        return False
    relative_gap = abs(result.value - result.threshold) / abs(result.threshold)
    return relative_gap <= fraction
