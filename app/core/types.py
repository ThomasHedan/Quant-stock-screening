"""Value types shared across the pure core.

These are deliberately plain frozen dataclasses and enums rather than pydantic
models: pydantic belongs at the boundaries where untrusted data enters, and the
core only ever sees already-validated values (CLAUDE.md 1.1).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class PillarStatus(StrEnum):
    """Outcome of a single pillar check.

    ``UNKNOWN`` exists because missing data is not a failure: a null float means
    the supply pillar could not be evaluated, and silently treating it as zero
    would make every data gap look like a perfect setup.
    """

    PASS = "pass"  # noqa: S105 — a pillar verdict, not a credential
    FAIL = "fail"
    UNKNOWN = "unknown"


class Tier(StrEnum):
    """Alert tier. ``NONE`` means the row is recorded but not surfaced."""

    A = "A"
    B = "B"
    WATCH = "watch"
    NONE = "none"


class RvolSource(StrEnum):
    """Where a relative-volume figure came from.

    ``FALLBACK`` numbers are far cruder than ``BASELINE`` ones and are labelled
    so research can exclude them rather than silently mixing the two.
    """

    BASELINE = "baseline"
    FALLBACK = "fallback"
    UNKNOWN = "unknown"


class FloatConfidence(StrEnum):
    """Trust level of a reported float figure (CLAUDE.md 6.4.4)."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class RetentionClass(StrEnum):
    """Why a Tier 1 row survived the nightly pruning (CLAUDE.md 6.1)."""

    MOVER = "mover"
    SIGNAL = "signal"
    CONTROL = "control"


class MarketSession(StrEnum):
    """The three ET sessions a trading day is split into."""

    PRE = "pre"
    REGULAR = "regular"
    POST = "post"


@dataclass(frozen=True, slots=True)
class PillarThresholds:
    """The five pillar rules, as configured.

    Held as a frozen dataclass so the core never reaches for a config global:
    the caller loads ``config.yaml`` and hands the values down explicitly.
    """

    gap_pct_min: float
    rvol_min: float
    news_fresh_minutes: int
    price_min: float
    price_max: float
    float_shares_max: int


@dataclass(frozen=True, slots=True)
class RankWeights:
    """Weights of the composite rank score. They are expected to sum to 1."""

    window_change: float
    rvol: float
    gap: float


@dataclass(frozen=True, slots=True)
class PillarResult:
    """One pillar's verdict, with everything needed to explain it later.

    ``value`` and ``threshold`` are kept alongside the status because the
    missed-runner diagnosis (CLAUDE.md 7.2) has to report *how far* a pillar
    missed, not merely that it did.
    """

    number: int
    name: str
    status: PillarStatus
    value: float | None
    threshold: float | None
    detail: str = ""

    @property
    def passed(self) -> bool:
        """True only on an explicit pass; unknown is never a pass."""
        return self.status is PillarStatus.PASS


@dataclass(frozen=True, slots=True)
class NewsItem:
    """A news article as the core sees it: identity, symbols and two clocks.

    ``updated_at`` is carried but deliberately never used for freshness —
    articles get revised hours later, and judging freshness on the revision
    time injects lookahead (CLAUDE.md 6.4.6).
    """

    news_id: str
    symbols: tuple[str, ...]
    headline: str
    source: str
    url: str | None
    created_at: datetime
    received_at: datetime
    updated_at: datetime | None = None
