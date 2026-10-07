from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal

import tomlkit

WorkerPriority = Literal["conservative", "background", "balanced", "throughput"]
VerifyMode = Literal["normal", "deep"]

_WORKER_PRIORITIES = ("conservative", "background", "balanced", "throughput")
_VERIFY_MODES = ("normal", "deep")


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


def _choice(mapping: Mapping[str, Any], key: str, allowed: tuple[str, ...]) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or value not in allowed:
        raise ConfigError(f"{key} must be one of: {', '.join(allowed)}")
    return value


def _section(mapping: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = mapping.get(name)
    if not isinstance(value, Mapping):
        raise ConfigError(f"missing [{name}] section")
    return value


def _merge_missing(defaults: Mapping[str, Any], current: MutableMapping[str, Any]) -> bool:
    changed = False
    for key, default_value in defaults.items():
        if key not in current:
            current[key] = default_value
            changed = True
            continue

        current_value = current[key]
        if isinstance(default_value, Mapping) and isinstance(current_value, MutableMapping):
            changed = _merge_missing(default_value, current_value) or changed
    return changed


def reconcile_config_file(path: Path) -> bool:
    """Add missing packaged defaults without changing user-defined or unknown values."""

    defaults = tomlkit.parse(files("endstone_endkeep").joinpath("config.toml").read_text(encoding="utf-8"))
    with path.open("r", encoding="utf-8") as stream:
        current = tomlkit.load(stream)

    if not _merge_missing(defaults, current):
        return False

    tmp = path.with_name(f".{path.name}.tmp")
    with tmp.open("w", encoding="utf-8") as stream:
        tomlkit.dump(current, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path.parent, flags)
    except OSError:
        if os.name == "posix":
            raise
    else:
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return True


@dataclass(frozen=True)
class CaptureConfig:
    times: tuple[str, ...]


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
class WorkerConfig:
    priority: WorkerPriority


@dataclass(frozen=True)
class VerifyConfig:
    mode: VerifyMode


@dataclass(frozen=True)
class EndKeepConfig:
    enabled: bool
    capture: CaptureConfig
    maintenance: MaintenanceConfig
    raw: RawConfig
    logical: LogicalConfig
    retention: RetentionConfig
    storage: StorageConfig
    worker: WorkerConfig
    verify: VerifyConfig

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
        worker = _section(mapping, "worker")
        verify = _section(mapping, "verify")

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
            capture=CaptureConfig(_string_list(capture, "times")),
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
            worker=WorkerConfig(
                priority=_choice(worker, "priority", _WORKER_PRIORITIES),  # type: ignore[arg-type]
            ),
            verify=VerifyConfig(
                mode=_choice(verify, "mode", _VERIFY_MODES),  # type: ignore[arg-type]
            ),
        )
