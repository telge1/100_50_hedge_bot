"""Configuration contract for the continuous Full-OB raw archive."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

FORMAT_VERSION = "full_ob_continuous_raw_archive_v1"
SCHEMA_VERSION = "1"
SEGMENT_MINUTES = 60
CHECKPOINT_MINUTES = 5
ARCHIVE_FATAL_EXIT_CODE = 75
PROJECT_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_ARCHIVE_ROOT = PROJECT_ROOT / "data" / "orderbook_raw_shadow" / "full_ob_v1"
DEFAULT_SYMBOLS = frozenset({"BTCUSDT", "DOGEUSDT"})


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class FullObContinuousRawArchiveSettings:
    enabled: bool = False
    archive_root: Path = DEFAULT_ARCHIVE_ROOT
    symbols: frozenset[str] = DEFAULT_SYMBOLS
    queue_size: int = 8192
    compression_level: int = 3
    flush_interval_sec: float = 1.0
    warn_free_disk_gb: float = 20.0
    red_free_disk_gb: float = 5.0
    warn_free_disk_percent: float = 10.0
    red_free_disk_percent: float = 2.0
    segment_minutes: int = SEGMENT_MINUTES
    checkpoint_minutes: int = CHECKPOINT_MINUTES

    def should_archive(self, symbol: str) -> bool:
        return self.enabled and symbol.upper() in self.symbols


def load_full_ob_continuous_raw_archive_settings() -> FullObContinuousRawArchiveSettings:
    prefix = "OB_V3_FULL_OB_RAW_ARCHIVE_"
    enabled_raw = os.environ.get(prefix + "ENABLED")
    if enabled_raw is None:
        enabled_raw = os.environ.get(prefix + "ENABLE")
    if enabled_raw is None:
        enabled_raw = os.environ.get("FULL_OB_CONTINUOUS_ARCHIVE_ENABLED")
    symbols_raw = os.environ.get(prefix + "SYMBOLS")
    symbols = DEFAULT_SYMBOLS
    if symbols_raw is not None:
        symbols = frozenset(x.strip().upper() for x in symbols_raw.split(",") if x.strip())
    unsupported = symbols - DEFAULT_SYMBOLS
    if unsupported:
        raise ValueError(f"unsupported Full-OB archive symbols: {sorted(unsupported)}")
    return FullObContinuousRawArchiveSettings(
        enabled=_truthy(enabled_raw),
        archive_root=Path(os.environ.get(prefix + "ROOT") or DEFAULT_ARCHIVE_ROOT),
        symbols=symbols,
        queue_size=int(os.environ.get(prefix + "QUEUE_SIZE") or 8192),
        compression_level=int(os.environ.get(prefix + "COMPRESSION_LEVEL") or 3),
        flush_interval_sec=float(os.environ.get(prefix + "FLUSH_INTERVAL_SEC") or 1.0),
        warn_free_disk_gb=float(os.environ.get(prefix + "WARN_FREE_DISK_GB") or 20.0),
        red_free_disk_gb=float(os.environ.get(prefix + "RED_FREE_DISK_GB") or 5.0),
        warn_free_disk_percent=float(os.environ.get(prefix + "WARN_FREE_DISK_PERCENT") or 10.0),
        red_free_disk_percent=float(os.environ.get(prefix + "RED_FREE_DISK_PERCENT") or 2.0),
    )
