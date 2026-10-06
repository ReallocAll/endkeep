from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from endstone_endkeep.config import ConfigError, EndKeepConfig, parse_hhmm


def _defaults() -> dict:
    path = Path("src/endstone_endkeep/config.toml")
    with path.open("rb") as stream:
        return tomllib.load(stream)


def test_default_config() -> None:
    config = EndKeepConfig.from_mapping(_defaults())
    assert config.enabled is True
    assert config.capture.times == ("12:00", "16:30", "20:30", "23:45")
    assert config.maintenance.times == ("06:00", "18:30")
    assert config.maintenance.mode_for("06:00") == "FULL"
    assert config.maintenance.mode_for("18:30") == "LOGIC_ONLY"
    assert config.raw.max_pending == 18
    assert config.raw.max_age_days == 3
    assert config.logical.compression_level == 6
    assert config.logical.compression_threads == 4
    assert config.retention.keep_days == 7
    assert config.retention.keep_last == 28
    assert config.storage.path == Path("backups")
    assert config.storage.min_free_space_gib == 5


@pytest.mark.parametrize("value", ["6:00", "24:00", "12:60", "xx:yy", "12"])
def test_invalid_time(value: str) -> None:
    with pytest.raises(ConfigError):
        parse_hhmm(value)
