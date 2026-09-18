"""Configuration boundary: ``.env`` for secrets, ``config.yaml`` for behaviour.

This is one of the few places pydantic belongs (CLAUDE.md 1.1). Everything is
validated exactly once, here, and the rest of the app receives well-typed
objects it can trust. Unknown keys are rejected rather than ignored: a typo in
``config.yaml`` silently falling back to a default threshold is precisely the
kind of failure that is invisible until a month of alerts turns out to have
used the wrong rule.
"""

from __future__ import annotations

import logging
from datetime import time
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.types import MarketSession, PillarThresholds, RankWeights

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path("config.yaml")

Pct = Annotated[float, Field(ge=0)]
PositiveInt = Annotated[int, Field(gt=0)]


def _parse_et_time(value: str | time) -> time:
    """Parse an ``HH:MM`` ET wall-clock string from config."""
    if isinstance(value, time):
        return value
    hours, _, minutes = value.partition(":")
    return time(int(hours), int(minutes))


class _Model(BaseModel):
    """Base config model: immutable and strict about unknown keys."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class TimeWindow(_Model):
    """An ET wall-clock window, half-open ``[start, end)``."""

    start: time
    end: time

    @classmethod
    def from_pair(cls, pair: list[str] | tuple[str, str]) -> TimeWindow:
        """Build from the ``["08:00", "08:05"]`` shape used in config.yaml."""
        start, end = pair
        return cls(start=_parse_et_time(start), end=_parse_et_time(end))

    def as_tuple(self) -> tuple[time, time]:
        """The pair form the pure core functions take."""
        return (self.start, self.end)


class TimezoneConfig(_Model):
    market: str = "America/New_York"
    display: str = "Europe/Paris"


class CalendarConfig(_Model):
    exchange: str = "XNYS"


class SessionsConfig(_Model):
    pre: TimeWindow
    regular: TimeWindow
    post: TimeWindow

    @field_validator("pre", "regular", "post", mode="before")
    @classmethod
    def _coerce(cls, value: Any) -> Any:
        return TimeWindow.from_pair(value) if isinstance(value, list) else value

    def bounds(self) -> dict[MarketSession, tuple[time, time]]:
        """Session bounds in the shape ``timeutils.session_of`` expects."""
        return {
            MarketSession.PRE: self.pre.as_tuple(),
            MarketSession.REGULAR: self.regular.as_tuple(),
            MarketSession.POST: self.post.as_tuple(),
        }


class CadenceConfig(_Model):
    """A polling cadence and the ET windows it applies to."""

    interval_seconds: PositiveInt
    windows: tuple[TimeWindow, ...]

    @field_validator("windows", mode="before")
    @classmethod
    def _coerce(cls, value: Any) -> Any:
        if isinstance(value, list):
            return [TimeWindow.from_pair(v) if isinstance(v, list) else v for v in value]
        return value


class CollectorSchedule(_Model):
    hot: CadenceConfig
    cold: CadenceConfig


class JobTimes(_Model):
    premarket_outcomes: time
    daily_digest_push: time
    corporate_actions: time
    full_day_outcomes: time
    data_quality_and_compaction: time

    @field_validator("*", mode="before")
    @classmethod
    def _coerce(cls, value: Any) -> Any:
        return _parse_et_time(value) if isinstance(value, str) else value


class SchedulesConfig(_Model):
    news_listener: TimeWindow
    collector: CollectorSchedule
    alert_windows: CadenceConfig
    jobs: JobTimes

    @field_validator("news_listener", mode="before")
    @classmethod
    def _coerce(cls, value: Any) -> Any:
        return TimeWindow.from_pair(value) if isinstance(value, list) else value


class PillarsConfig(_Model):
    gap_pct_min: float
    rvol_min: float
    news_fresh_minutes: PositiveInt
    price_min: float
    price_max: float
    float_shares_max: PositiveInt

    @field_validator("price_max")
    @classmethod
    def _range_is_ordered(cls, value: float, info: Any) -> float:
        low = info.data.get("price_min")
        if low is not None and value <= low:
            msg = f"price_max ({value}) must exceed price_min ({low})"
            raise ValueError(msg)
        return value

    def to_thresholds(self) -> PillarThresholds:
        """Hand the pure core a plain frozen dataclass, not a pydantic model."""
        return PillarThresholds(
            gap_pct_min=self.gap_pct_min,
            rvol_min=self.rvol_min,
            news_fresh_minutes=self.news_fresh_minutes,
            price_min=self.price_min,
            price_max=self.price_max,
            float_shares_max=self.float_shares_max,
        )


class RvolConfig(_Model):
    baseline_days: PositiveInt
    fallback_session_fraction: Annotated[float, Field(gt=0, le=1)]
    cache_table: str


class RankingConfig(_Model):
    window_change_weight: Pct
    rvol_weight: Pct
    gap_weight: Pct

    def to_weights(self) -> RankWeights:
        """The weights as the pure core takes them."""
        return RankWeights(
            window_change=self.window_change_weight,
            rvol=self.rvol_weight,
            gap=self.gap_weight,
        )


class AlertsConfig(_Model):
    tier_b_push_enabled: bool
    max_pushes_per_window: PositiveInt
    daily_digest_enabled: bool


class NewsConfig(_Model):
    backfill_minutes: PositiveInt
    today_start_et: time
    premarket_today_start_prev_day_et: time
    finnhub_max_calls_per_minute: PositiveInt
    feed_latency_p95_warn_seconds: PositiveInt

    @field_validator("today_start_et", "premarket_today_start_prev_day_et", mode="before")
    @classmethod
    def _coerce(cls, value: Any) -> Any:
        return _parse_et_time(value) if isinstance(value, str) else value


class CollectorConfig(_Model):
    min_abs_change_pct: Pct
    min_volume_ratio: Pct
    top_dollar_volume_n: PositiveInt
    min_price: Pct


class RetentionConfig(_Model):
    move_threshold_pct: Pct
    control_sample_pct: Annotated[int, Field(ge=0, le=100)]
    max_lake_gb: PositiveInt
    lake_warn_fraction: Annotated[float, Field(gt=0, le=1)]
    bars_1m_raw_days: PositiveInt
    bars_1m_thinned_minutes: PositiveInt
    snapshots_months: PositiveInt


class IntegrityConfig(_Model):
    suspect_price_change: Pct
    halt_min_silent_minutes: PositiveInt
    float_turnover_low_confidence: Pct
    float_asof_max_age_days: PositiveInt


class TradabilityConfig(_Model):
    min_tradeable_dollar_volume: Pct
    max_tradeable_spread_pct: Pct
    window_minutes: PositiveInt


class OutcomesConfig(_Model):
    reference_times_et: tuple[time, ...]
    forward_minutes: tuple[PositiveInt, ...]
    max_bar_tickers: PositiveInt
    session_change_min_pct: Pct

    @field_validator("reference_times_et", mode="before")
    @classmethod
    def _coerce(cls, value: Any) -> Any:
        if isinstance(value, list):
            return [_parse_et_time(v) for v in value]
        return value


class RunnersConfig(_Model):
    high_of_day_pct_min: Pct
    intraday_move_pct_min: Pct
    intraday_lookback_minutes: PositiveInt
    intraday_window_et: TimeWindow
    postmarket_move_pct_min: Pct
    min_price: Pct
    min_dollar_volume: Pct
    move_start_trigger_pct: Pct
    near_miss_fraction: Annotated[float, Field(gt=0, le=1)]
    recent_runner_days: PositiveInt
    top_gainers_n: PositiveInt

    @field_validator("intraday_window_et", mode="before")
    @classmethod
    def _coerce(cls, value: Any) -> Any:
        return TimeWindow.from_pair(value) if isinstance(value, list) else value


class ResearchConfig(_Model):
    holdout_fraction: Annotated[float, Field(gt=0, lt=1)]
    sql_timeout_seconds: PositiveInt
    sql_row_cap: PositiveInt


class HttpConfig(_Model):
    """Every external call gets these. No unbounded request exists in this app."""

    timeout_seconds: PositiveInt
    connect_timeout_seconds: PositiveInt
    max_retries: Annotated[int, Field(ge=0)]
    backoff_base_seconds: Annotated[float, Field(gt=0)]
    backoff_max_seconds: Annotated[float, Field(gt=0)]


class StorageConfig(_Model):
    lake_path: Path
    sqlite_path: Path
    flush_interval_seconds: PositiveInt
    parquet_compression: str


class AppConfig(_Model):
    """The whole of ``config.yaml``, validated."""

    schema_version: PositiveInt
    timezones: TimezoneConfig
    calendar: CalendarConfig
    sessions: SessionsConfig
    schedules: SchedulesConfig
    pillars: PillarsConfig
    rvol: RvolConfig
    ranking: RankingConfig
    alerts: AlertsConfig
    news: NewsConfig
    collector: CollectorConfig
    retention: RetentionConfig
    integrity: IntegrityConfig
    tradability: TradabilityConfig
    outcomes: OutcomesConfig
    runners: RunnersConfig
    research: ResearchConfig
    http: HttpConfig
    storage: StorageConfig


class Secrets(BaseSettings):
    """API credentials, read from the environment or ``.env``.

    Every field defaults to empty so the app can start in mock mode with no
    credentials at all. Callers that need a key check it explicitly and fail
    with a clear message; nothing here is ever logged (CLAUDE.md 1.1).
    """

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", frozen=True
    )

    alpaca_key_id: str = ""
    alpaca_secret_key: str = ""
    finnhub_api_key: str = ""
    vapid_public_key: str = ""
    vapid_private_key: str = ""
    vapid_subject: str = ""
    mock_data: bool = False

    def has_alpaca(self) -> bool:
        """Whether Alpaca credentials are present (never logs their value)."""
        return bool(self.alpaca_key_id and self.alpaca_secret_key)

    def has_finnhub(self) -> bool:
        """Whether a Finnhub key is present."""
        return bool(self.finnhub_api_key)

    def has_vapid(self) -> bool:
        """Whether a usable VAPID key pair is present."""
        return bool(self.vapid_public_key and self.vapid_private_key and self.vapid_subject)

    def __repr__(self) -> str:
        """Redacted repr, so a stray log line cannot leak a key."""
        return (
            f"Secrets(alpaca={self.has_alpaca()}, finnhub={self.has_finnhub()}, "
            f"vapid={self.has_vapid()}, mock_data={self.mock_data})"
        )

    __str__ = __repr__


def load_config(path: Path | None = None) -> AppConfig:
    """Read and validate ``config.yaml``.

    Raises rather than falling back to defaults: running a scanner for a month
    on a threshold nobody intended is worse than refusing to start.
    """
    config_path = path or DEFAULT_CONFIG_PATH
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        msg = f"{config_path} must contain a YAML mapping, got {type(raw).__name__}"
        raise TypeError(msg)
    config = AppConfig.model_validate(raw)
    logger.info(
        "Loaded config schema_version=%s from %s (market tz %s)",
        config.schema_version,
        config_path,
        config.timezones.market,
    )
    return config


@lru_cache(maxsize=1)
def get_secrets() -> Secrets:
    """Process-wide secrets, read once."""
    return Secrets()
