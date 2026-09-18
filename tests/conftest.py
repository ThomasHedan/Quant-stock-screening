"""Shared fixtures.

Everything here builds synthetic, timezone-aware data. No test touches the
network, and the only tests touching disk are the lake tests, which use
``tmp_path``.
"""

from __future__ import annotations

from datetime import date, time

import pytest
from app.core.types import MarketSession, PillarThresholds, RankWeights


@pytest.fixture
def thresholds() -> PillarThresholds:
    """The config.yaml defaults, as the core receives them."""
    return PillarThresholds(
        gap_pct_min=10.0,
        rvol_min=5.0,
        news_fresh_minutes=15,
        price_min=2.00,
        price_max=20.00,
        float_shares_max=20_000_000,
    )


@pytest.fixture
def weights() -> RankWeights:
    """Default rank-score weights."""
    return RankWeights(window_change=0.5, rvol=0.3, gap=0.2)


@pytest.fixture
def session_bounds() -> dict[MarketSession, tuple[time, time]]:
    """ET session bounds, half-open."""
    return {
        MarketSession.PRE: (time(4, 0), time(9, 30)),
        MarketSession.REGULAR: (time(9, 30), time(16, 0)),
        MarketSession.POST: (time(16, 0), time(20, 0)),
    }


@pytest.fixture
def trading_day() -> date:
    """A plain Tuesday with no holiday or early close nearby."""
    return date(2026, 3, 10)
