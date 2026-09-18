"""Mock mode: a deterministic synthetic market for testing outside market hours.

``MOCK_DATA=1`` makes the whole pipeline runnable at any hour — UI, push, lake,
missed runners — which is the only way to check that a tier A alert really does
produce a browser notification without waiting for 08:05 on a weekday
(CLAUDE.md 11).

Everything here is derived from hashes of ticker and date rather than
``random``, so a mock day is byte-for-byte reproducible. An acceptance test
that cannot be replayed is not much of a test, and a flaky one is worse than
none.

The synthetic market is shaped, not uniform. It deliberately contains:

* a handful of **perfect setups** — small float, $2–20, big gap, fresh news —
  so tier A is reachable;
* a **07:20 runner** that moves outside every alert window, so the
  ``OUTSIDE_WINDOW`` path is exercised;
* a **23M-float runner**, for ``FAILED_PILLAR`` plus ``NEAR_MISS``;
* names with **missing float data**, so the ``unknown`` paths are not dead code.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from app.core.moves import Bar
from app.core.timeutils import to_utc
from app.core.types import NewsItem
from app.sources.alpaca_bars import mock_bars
from app.sources.tradingview import SnapshotResult, TradingViewRow

logger = logging.getLogger(__name__)

#: Tickers with a complete five-pillar setup.
PERFECT_SETUPS: tuple[str, ...] = ("MKAAA", "MKBBB")

#: A runner whose move starts at 07:20 ET, outside every alert window.
EARLY_RUNNER = "MKEARLY"

#: A runner that fails pillar 5 by three million shares.
NEAR_MISS_RUNNER = "MKNEAR"

#: A runner nobody could have traded: the post-signal dollar volume is ~$10k.
ILLIQUID_RUNNER = "MKTHIN"


@dataclass(frozen=True, slots=True)
class MockDay:
    """One synthetic trading day."""

    day: date

    def snapshot(self, *, now: datetime) -> SnapshotResult:
        """Rows as the scanner would see them at ``now``."""
        rows = [
            *(self._perfect(ticker, now=now) for ticker in PERFECT_SETUPS),
            self._early_runner(now=now),
            self._near_miss(now=now),
            self._illiquid(now=now),
            *(self._filler(index, now=now) for index in range(40)),
        ]
        return SnapshotResult(poll_ts_utc=to_utc(now), rows=tuple(rows), total_matched=len(rows))

    def news(self, *, now: datetime) -> list[NewsItem]:
        """Fresh catalysts for the perfect setups, stale ones elsewhere.

        The perfect setups get news two minutes old so pillar 3 passes at any
        poll, with a fresh article every ten minutes; the near-miss runner gets
        one article from six hours ago, so the ``NEWS_STALE`` path is exercised
        too.
        """
        fresh = to_utc(now) - timedelta(minutes=2)
        stale = to_utc(now) - timedelta(hours=6)
        # The id carries a ten-minute bucket so a *new* article arrives
        # periodically. With one id per day the cache would deduplicate every
        # later poll, the single article would age past the freshness window,
        # and mock mode could never produce a tier A again after ten minutes —
        # which looks exactly like a bug in the pillar-3 check.
        bucket = to_utc(now).strftime("%H%M")[:3]
        items = [
            NewsItem(
                news_id=f"mock-{ticker}-{self.day}-{bucket}",
                symbols=(ticker,),
                headline=f"{ticker} announces positive Phase 3 topline results",
                source="benzinga",
                url=f"https://example.invalid/{ticker}",
                created_at=fresh,
                received_at=fresh + timedelta(seconds=2),
            )
            for ticker in PERFECT_SETUPS
        ]
        items.append(
            NewsItem(
                news_id=f"mock-{NEAR_MISS_RUNNER}-{self.day}",  # stale by design, one per day
                symbols=(NEAR_MISS_RUNNER,),
                headline=f"{NEAR_MISS_RUNNER} prices public offering",
                source="benzinga",
                url=None,
                created_at=stale,
                received_at=stale + timedelta(seconds=3),
            )
        )
        return items

    def bars(self) -> dict[str, tuple[Bar, ...]]:
        """Minute bars for the runners, shaped to produce known outcomes."""
        return {
            EARLY_RUNNER: mock_bars(
                EARLY_RUNNER, self.day, start_et=time(7, 20), minutes=180, open_price=4.00
            ),
            NEAR_MISS_RUNNER: mock_bars(
                NEAR_MISS_RUNNER, self.day, start_et=time(8, 1), minutes=180, open_price=6.00
            ),
            ILLIQUID_RUNNER: tuple(
                # ~$10k of dollar volume in the five minutes after 08:05: a real
                # move on paper that nobody could have taken (CLAUDE.md 11.11).
                Bar(
                    minute=bar.minute,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=200.0,
                    vwap=bar.vwap,
                    trade_count=3,
                )
                for bar in mock_bars(
                    ILLIQUID_RUNNER, self.day, start_et=time(8, 1), minutes=120, open_price=10.0
                )
            ),
            PERFECT_SETUPS[0]: mock_bars(
                PERFECT_SETUPS[0], self.day, start_et=time(8, 1), minutes=180, open_price=5.00
            ),
        }

    # -- row builders --------------------------------------------------------

    def _perfect(self, ticker: str, *, now: datetime) -> TradingViewRow:
        """Small float, in range, big gap, and fresh news to match."""
        drift = (to_utc(now).minute % 10) / 100
        return TradingViewRow(
            ticker=ticker,
            exchange="NASDAQ",
            instrument_type="stock",
            typespecs=("common",),
            close=round(5.20 * (1 + drift), 2),
            change_pct=round(34.0 + drift * 100, 2),
            volume=8_200_000.0,
            average_volume_10d=900_000.0,
            average_volume_30d=850_000.0,
            relative_volume_10d=9.1,
            float_shares=4_100_000,
            shares_outstanding=12_000_000,
            market_cap=62_400_000.0,
            sector="Health Technology",
            industry="Biotechnology",
        )

    def _early_runner(self, *, now: datetime) -> TradingViewRow:
        """Already up 80% by the time any window opens."""
        return TradingViewRow(
            ticker=EARLY_RUNNER,
            exchange="NASDAQ",
            instrument_type="stock",
            typespecs=("common",),
            close=7.20,
            change_pct=80.0,
            volume=12_000_000.0,
            average_volume_10d=600_000.0,
            float_shares=6_000_000,
            shares_outstanding=15_000_000,
            market_cap=108_000_000.0,
            sector="Health Technology",
        )

    def _near_miss(self, *, now: datetime) -> TradingViewRow:
        """23M float against a 20M rule: fails pillar 5 by a hair."""
        return TradingViewRow(
            ticker=NEAR_MISS_RUNNER,
            exchange="NASDAQ",
            instrument_type="stock",
            typespecs=("common",),
            close=9.60,
            change_pct=60.0,
            volume=15_000_000.0,
            average_volume_10d=800_000.0,
            float_shares=23_000_000,
            shares_outstanding=40_000_000,
            market_cap=384_000_000.0,
            sector="Technology Services",
        )

    def _illiquid(self, *, now: datetime) -> TradingViewRow:
        """A big percentage move on almost no volume."""
        return TradingViewRow(
            ticker=ILLIQUID_RUNNER,
            exchange="NASDAQ",
            instrument_type="stock",
            typespecs=("common",),
            close=14.00,
            change_pct=40.0,
            volume=9_000.0,
            average_volume_10d=12_000.0,
            float_shares=2_000_000,
            shares_outstanding=5_000_000,
            market_cap=70_000_000.0,
            sector="Finance",
        )

    def _filler(self, index: int, *, now: datetime) -> TradingViewRow:
        """Ordinary names that went nowhere — the lake's denominator.

        Every tenth one has no float figure at all, so the ``unknown`` pillar
        path stays exercised rather than becoming dead code nobody notices has
        broken.
        """
        drift = ((index * 7 + to_utc(now).minute) % 13) - 6
        return TradingViewRow(
            ticker=f"MK{index:03d}",
            exchange="NASDAQ" if index % 2 else "NYSE",
            instrument_type="stock",
            typespecs=("common",),
            close=round(3.0 + index * 0.4, 2),
            change_pct=float(drift),
            volume=120_000.0 + index * 1_000,
            average_volume_10d=400_000.0,
            float_shares=None if index % 10 == 0 else 30_000_000 + index * 1_000_000,
            shares_outstanding=60_000_000,
            market_cap=180_000_000.0,
            sector="Finance",
        )


def prev_closes(day: MockDay) -> dict[str, float]:
    """Previous closes consistent with the synthetic gaps."""
    return {
        **{ticker: 3.88 for ticker in PERFECT_SETUPS},
        EARLY_RUNNER: 4.00,
        NEAR_MISS_RUNNER: 6.00,
        ILLIQUID_RUNNER: 10.00,
    }
