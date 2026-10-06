from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Raised when config.toml contains an invalid EndKeep setting."""


def parse_hhmm(value: str) -> tuple[int, int]:
    parts = value.split(":")
    if len(parts) != 2 or any(not part.isdigit() for part in parts):
        raise ConfigError(f"invalid time {value!r}; expected HH:MM")
    hour, minute = (int(part) for part in parts)
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ConfigError(f"invalid time {value!r}; expected HH:MM")
    if value != f"{hour:02d}:{minute:02d}":
        raise ConfigError(f"invalid time {value!r}; expected zero-padded HH:MM")
    return hour, minute


def _string_list(mapping: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = mapping.get(key)
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{key} must be a non-empty list of HH:MM strings")
    result = tuple(value)
    for item in result:
        parse_hhmm(item)
    if len(set(result)) != len(result):
        raise ConfigError(f"{key} contains duplicate times")
    return result


def _positive_int(mapping: Mapping[str, Any], key: str, *, allow_zero: bool = False) -> int:
    value = mapping.get(key)
    lower = 0 if allow_zero else 1
    if not isinstance(value, int) or isinstance(value, bool) or value < lower:
        relation = "non-negative" if allow_zero else "positive"
        raise ConfigError(f"{key} must be a {relation} integer")
    return value


def _section(mapping: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = mapping.get(name)
    if not isinstance(value, Mapping):
        raise ConfigError(f"missing [{name}] section")
    return value


@dataclass(frozen=True)
class CaptureConfig:
    times: tuple[str, ...]
    query_retries: int


@dataclass(frozen=True)
class MaintenanceConfig:
    times: tuple[str, ...]

    def mode_for(self, configured_time: str) -> str:
        try:
            index = self.times.index(configured_time)
        except ValueError as exc:
            raise ConfigError(f"unknown maintenance time: {configured_time}") from exc
        return "FULL" if index == 0 else "LOGIC_ONLY"


@dataclass(frozen=True)
class RawConfig:
    max_pending: int
    max_age_days: int


@dataclass(frozen=True)
class LogicalConfig:
    compression_level: int
    compression_threads: int


@dataclass(frozen=True)
class RetentionConfig:
    keep_days: int
    keep_last: int


@dataclass(frozen=True)
class StorageConfig:
    path: Path
    min_free_space_gib: int


@dataclass(frozen=True)
class EndKeepConfig:
    enabled: bool
    capture: CaptureConfig
    maintenance: MaintenanceConfig
    raw: RawConfig
    logical: LogicalConfig
    retention: RetentionConfig
    storage: StorageConfig

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> EndKeepConfig:
        enabled = mapping.get("enabled")
        if not isinstance(enabled, bool):
            raise ConfigError("enabled must be a boolean")

        capture = _section(mapping, "capture")
        maintenance = _section(mapping, "maintenance")
        raw = _section(mapping, "raw")
        logical = _section(mapping, "logical")
        retention = _section(mapping, "retention")
        storage = _section(mapping, "storage")

        query_retries = capture.get("query_retries", 300)
        if not isinstance(query_retries, int) or isinstance(query_retries, bool) or query_retries < 0:
            raise ConfigError("capture.query_retries must be a non-negative integer")

        storage_path = storage.get("path")
        if not isinstance(storage_path, str) or not storage_path.strip():
            raise ConfigError("storage.path must be a non-empty path string")

        compression_level = logical.get("compression_level")
        if (
            not isinstance(compression_level, int)
            or isinstance(compression_level, bool)
            or not -7 <= compression_level <= 22
        ):
            raise ConfigError("logical.compression_level must be an integer between -7 and 22")

        keep_days = _positive_int(retention, "keep_days", allow_zero=True)
        keep_last = _positive_int(retention, "keep_last", allow_zero=True)
        if keep_days == 0 and keep_last == 0:
            raise ConfigError("retention.keep_days and retention.keep_last cannot both be zero")

        return cls(
            enabled=enabled,
            capture=CaptureConfig(
                times=_string_list(capture, "times"),
                query_retries=query_retries,
            ),
            maintenance=MaintenanceConfig(_string_list(maintenance, "times")),
            raw=RawConfig(
                max_pending=_positive_int(raw, "max_pending"),
                max_age_days=_positive_int(raw, "max_age_days"),
            ),
            logical=LogicalConfig(
                compression_level=compression_level,
                compression_threads=_positive_int(logical, "compression_threads"),
            ),
            retention=RetentionConfig(
                keep_days=keep_days,
                keep_last=keep_last,
            ),
            storage=StorageConfig(
                path=Path(storage_path),
                min_free_space_gib=_positive_int(storage, "min_free_space_gib"),
            ),
        )
