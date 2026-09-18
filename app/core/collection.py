"""The live collection filter — deliberately loose (CLAUDE.md 6.0, 6.2).

This is the most counter-intuitive rule in the project, so it is worth stating
plainly next to the code that implements it: **the collector must keep stocks
the alert pipeline would never look at twice.**

The question the lake has to answer one day is

    of all stocks that had RVOL > 5 and float < 20M at 08:05,
    what fraction actually ran?

That needs a denominator: the stocks that met the setup and went *nowhere*.
Tightening this filter towards the alert thresholds would produce a lake of
winners only, and every rule derived from it would look brilliant and fail
live. Whatever is collected loosely can still be pruned strictly at 20:45, when
the outcome is known — the reverse is impossible.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date

from app.core.metrics import dollar_volume


@dataclass(frozen=True, slots=True)
class CollectionFilter:
    """Thresholds of the loose live filter, from ``config.yaml``."""

    min_abs_change_pct: float
    min_volume_ratio: float
    top_dollar_volume_n: int
    min_price: float


@dataclass(frozen=True, slots=True)
class CollectionCandidate:
    """The few fields the filter needs, extracted from a snapshot row."""

    ticker: str
    price: float | None
    change_pct: float | None
    volume: float | None
    average_volume_10d: float | None


@dataclass(frozen=True, slots=True)
class CollectionDecision:
    """Whether a candidate is collected, and which rule let it in.

    The rule is kept because the mix of reasons is itself a diagnostic: a day
    where everything arrives through ``top_dollar_volume`` means the change and
    volume rules stopped matching, which usually means a feed problem rather
    than a calm market.
    """

    ticker: str
    collected: bool
    rule: str


def expected_volume_so_far(
    average_volume_10d: float | None, session_fraction: float
) -> float | None:
    """Crude expectation of volume by this point in the session.

    Same approximation as the RVOL fallback, and deliberately so: the collector
    filter has to be computable for every stock on every poll, including the
    thousands with no time-of-day baseline.
    """
    if average_volume_10d is None or average_volume_10d <= 0:
        return None
    return average_volume_10d * session_fraction


def decide(
    candidate: CollectionCandidate,
    rules: CollectionFilter,
    *,
    session_fraction: float,
    in_top_dollar_volume: bool,
) -> CollectionDecision:
    """Apply the loose filter to one candidate.

    Matching **any** rule is enough. The only hard gate is the price floor,
    which exists to keep sub-penny names — where a one-tick move is +50% — out
    of the lake entirely.
    """
    if candidate.price is None or candidate.price < rules.min_price:
        return CollectionDecision(candidate.ticker, collected=False, rule="below_price_floor")

    if candidate.change_pct is not None and abs(candidate.change_pct) >= rules.min_abs_change_pct:
        # Losers count: a low-float stock with fresh news that dumps is the
        # failure mode of the very setup being traded (CLAUDE.md 6.3).
        return CollectionDecision(candidate.ticker, collected=True, rule="abs_change")

    expected = expected_volume_so_far(candidate.average_volume_10d, session_fraction)
    if (
        candidate.volume is not None
        and expected is not None
        and expected > 0
        and candidate.volume >= rules.min_volume_ratio * expected
    ):
        return CollectionDecision(candidate.ticker, collected=True, rule="volume_ratio")

    if in_top_dollar_volume:
        return CollectionDecision(candidate.ticker, collected=True, rule="top_dollar_volume")

    return CollectionDecision(candidate.ticker, collected=False, rule="no_rule_matched")


def top_dollar_volume_tickers(candidates: list[CollectionCandidate], count: int) -> frozenset[str]:
    """The ``count`` tickers with the largest session dollar volume.

    Ties are broken by ticker so the set is deterministic; otherwise the same
    input could yield different lakes on two machines.
    """
    if count <= 0:
        return frozenset()
    ranked = sorted(
        (
            (dollar_volume(c.price, c.volume), c.ticker)
            for c in candidates
            if c.price is not None and c.volume is not None
        ),
        key=lambda pair: (-pair[0], pair[1]),
    )
    return frozenset(ticker for _value, ticker in ranked[:count])


def control_sample_hash(ticker: str, day: date) -> int:
    """Stable 0–99 bucket for the control sample (CLAUDE.md 6.1).

    Deterministic by construction: the same ticker-day always lands in the same
    bucket, on any machine, forever. A sample drawn with ``random`` would drift
    towards whatever is interesting on the day it was drawn and quietly break
    the re-weighting that research depends on.
    """
    digest = hashlib.sha256(f"{ticker}{day.isoformat()}".encode()).hexdigest()
    return int(digest[:8], 16) % 100


def in_control_sample(ticker: str, day: date, control_sample_pct: int) -> bool:
    """Whether a ticker-day belongs to the retained control group."""
    if not 0 <= control_sample_pct <= 100:
        msg = f"control_sample_pct must be in [0, 100], got {control_sample_pct}"
        raise ValueError(msg)
    return control_sample_hash(ticker, day) < control_sample_pct


def control_weight(control_sample_pct: int) -> float:
    """Multiplier that turns retained control rows back into a population.

    Every research query estimating a rate must apply this to its control rows
    (CLAUDE.md 6.1). It is the single easiest way to get a wrong answer out of
    this lake, which is why it lives in code with a test rather than only in a
    notebook comment.
    """
    if control_sample_pct <= 0:
        msg = "control_sample_pct must be positive to re-weight a sample"
        raise ValueError(msg)
    return 100.0 / control_sample_pct
