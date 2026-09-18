"""Config validation: the shipped file parses, and typos are refused."""

from __future__ import annotations

from datetime import time
from pathlib import Path

import pytest
import yaml
from app.config import AppConfig, Secrets, load_config
from app.core.types import MarketSession
from pydantic import ValidationError

REPO_CONFIG = Path("config.yaml")


@pytest.fixture
def raw() -> dict:
    return yaml.safe_load(REPO_CONFIG.read_text(encoding="utf-8"))


def test_shipped_config_validates():
    config = load_config(REPO_CONFIG)
    assert config.schema_version >= 1


def test_pillar_defaults_match_the_spec():
    pillars = load_config(REPO_CONFIG).pillars
    assert (pillars.gap_pct_min, pillars.rvol_min) == (10.0, 5.0)
    assert (pillars.price_min, pillars.price_max) == (2.00, 20.00)
    assert pillars.float_shares_max == 20_000_000


def test_windows_parse_to_et_times():
    windows = load_config(REPO_CONFIG).schedules.alert_windows
    assert windows.interval_seconds == 30
    assert windows.windows[0].as_tuple() == (time(8, 0), time(8, 5))
    assert len(windows.windows) == 5


def test_session_bounds_shape():
    bounds = load_config(REPO_CONFIG).sessions.bounds()
    assert bounds[MarketSession.PRE] == (time(4, 0), time(9, 30))
    assert set(bounds) == set(MarketSession)


def test_collector_filter_stays_looser_than_the_alert_thresholds():
    """CLAUDE.md 6.0: tightening this to the alert rules destroys the denominator."""
    config = load_config(REPO_CONFIG)
    assert config.collector.min_abs_change_pct < config.pillars.gap_pct_min


def test_unknown_key_is_rejected(raw):
    """A typo must fail loudly, not silently fall back to a default."""
    raw["pillars"]["gap_pct_minimum"] = 12.0
    with pytest.raises(ValidationError):
        AppConfig.model_validate(raw)


def test_inverted_price_range_is_rejected(raw):
    raw["pillars"]["price_max"] = 1.0
    with pytest.raises(ValidationError, match="price_max"):
        AppConfig.model_validate(raw)


def test_zero_session_fraction_is_rejected(raw):
    raw["rvol"]["fallback_session_fraction"] = 0
    with pytest.raises(ValidationError):
        AppConfig.model_validate(raw)


def test_config_is_frozen():
    config = load_config(REPO_CONFIG)
    with pytest.raises(ValidationError):
        config.pillars.gap_pct_min = 1.0


def test_missing_file_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.yaml")


def test_non_mapping_config_raises(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(TypeError, match="YAML mapping"):
        load_config(path)


# --- secrets -----------------------------------------------------------------


def test_secrets_repr_never_leaks_a_key():
    secrets = Secrets(
        alpaca_key_id="AKREALKEY",
        alpaca_secret_key="supersecret",
        vapid_private_key="privkey",
    )
    rendered = f"{secrets!r} {secrets}"
    assert "AKREALKEY" not in rendered
    assert "supersecret" not in rendered
    assert "privkey" not in rendered
    assert "alpaca=True" in rendered


def test_secrets_presence_helpers():
    empty = Secrets(_env_file=None)
    assert not empty.has_alpaca()
    assert not empty.has_vapid()
    full = Secrets(
        vapid_public_key="pub",
        vapid_private_key="priv",
        vapid_subject="mailto:a@b.c",
    )
    assert full.has_vapid()
