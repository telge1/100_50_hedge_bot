"""Configuration for live raw OB archival (OB200 by default; OB1000 via helper).

Disabled by default. OB1000 uses a parallel root/format so OB200 history stays intact.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

FORMAT_VERSION = "ob200_v3_live_archive/v1"
PARSER_VERSION = "ob200_v3"
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ARCHIVE_ROOT = PROJECT_ROOT / "data" / "orderbook_raw_live" / "ob200_v3"

OB1000_FORMAT_VERSION = "ob1000_v1_live_archive/v1"
OB1000_PARSER_VERSION = "ob1000_v1"
DEFAULT_OB1000_ARCHIVE_ROOT = PROJECT_ROOT / "data" / "orderbook_raw_live" / "ob1000_v1"


@dataclass(frozen=True)
class RawArchiveSettings:
    enabled: bool = False
    archive_root: Path = DEFAULT_ARCHIVE_ROOT
    symbols: frozenset[str] = frozenset()
    queue_size: int = 8192
    rotation: str = "day"  # day | hour
    compression: str = "zstd"  # zstd | none
    compression_level: int = 3
    retention_days: int = 0  # 0 = no auto-delete
    min_free_disk_gb: float = 5.0
    warn_free_disk_gb: float = 20.0
    depth: int = 200
    format_version: str = FORMAT_VERSION
    parser_version: str = PARSER_VERSION
    health_nest_key: str | None = None  # e.g. "ob1000_raw_archive" to nest health

    def should_archive(self, symbol: str) -> bool:
        return self.enabled and symbol.upper() in self.symbols


def load_raw_archive_settings(
    *,
    collector_symbols: tuple[str, ...] = (),
) -> RawArchiveSettings:
    enabled = (os.environ.get("OB_V3_RAW_ARCHIVE_ENABLE") or "false").lower() in {
        "1",
        "true",
        "yes",
    }
    root = Path(
        os.environ.get("OB_V3_RAW_ARCHIVE_ROOT") or str(DEFAULT_ARCHIVE_ROOT)
    )
    symbols_raw = os.environ.get("OB_V3_RAW_ARCHIVE_SYMBOLS") or ""
    symbols = frozenset(s.strip().upper() for s in symbols_raw.split(",") if s.strip())
    if enabled and not symbols:
        symbols = frozenset()
    return RawArchiveSettings(
        enabled=enabled,
        archive_root=root,
        symbols=symbols,
        queue_size=int(os.environ.get("OB_V3_RAW_ARCHIVE_QUEUE_SIZE") or 8192),
        rotation=(os.environ.get("OB_V3_RAW_ARCHIVE_ROTATION") or "day").lower(),
        compression=(os.environ.get("OB_V3_RAW_ARCHIVE_COMPRESSION") or "zstd").lower(),
        compression_level=int(os.environ.get("OB_V3_RAW_ARCHIVE_COMPRESSION_LEVEL") or 3),
        retention_days=int(os.environ.get("OB_V3_RAW_ARCHIVE_RETENTION_DAYS") or 0),
        min_free_disk_gb=float(os.environ.get("OB_V3_RAW_ARCHIVE_MIN_FREE_DISK_GB") or 5.0),
        warn_free_disk_gb=float(os.environ.get("OB_V3_RAW_ARCHIVE_WARN_FREE_DISK_GB") or 20.0),
        depth=200,
        format_version=FORMAT_VERSION,
        parser_version=PARSER_VERSION,
        health_nest_key=None,
    )


def load_ob1000_raw_archive_settings(
    *,
    collector_symbols: tuple[str, ...] = (),
) -> RawArchiveSettings:
    """Parallel OB1000 raw archive (env-gated; default off).

    Shares symbols with OB200 archive when ``OB_V3_OB1000_RAW_ARCHIVE_SYMBOLS`` is empty
    and OB200 symbols are set — typical BTC/DOGE pilot.
    """
    enabled = (os.environ.get("OB_V3_OB1000_RAW_ARCHIVE_ENABLE") or "false").lower() in {
        "1",
        "true",
        "yes",
    }
    root = Path(
        os.environ.get("OB_V3_OB1000_RAW_ARCHIVE_ROOT")
        or str(DEFAULT_OB1000_ARCHIVE_ROOT)
    )
    symbols_raw = os.environ.get("OB_V3_OB1000_RAW_ARCHIVE_SYMBOLS") or ""
    symbols = frozenset(s.strip().upper() for s in symbols_raw.split(",") if s.strip())
    if enabled and not symbols:
        # Fall back to OB200 archive symbols, then collector symbols.
        fallback = os.environ.get("OB_V3_RAW_ARCHIVE_SYMBOLS") or ""
        symbols = frozenset(s.strip().upper() for s in fallback.split(",") if s.strip())
        if not symbols and collector_symbols:
            symbols = frozenset(s.upper() for s in collector_symbols)
    return RawArchiveSettings(
        enabled=enabled,
        archive_root=root,
        symbols=symbols,
        queue_size=int(os.environ.get("OB_V3_OB1000_RAW_ARCHIVE_QUEUE_SIZE") or 8192),
        rotation=(os.environ.get("OB_V3_OB1000_RAW_ARCHIVE_ROTATION") or "hour").lower(),
        compression=(os.environ.get("OB_V3_OB1000_RAW_ARCHIVE_COMPRESSION") or "zstd").lower(),
        compression_level=int(
            os.environ.get("OB_V3_OB1000_RAW_ARCHIVE_COMPRESSION_LEVEL") or 3
        ),
        retention_days=int(os.environ.get("OB_V3_OB1000_RAW_ARCHIVE_RETENTION_DAYS") or 0),
        min_free_disk_gb=float(
            os.environ.get("OB_V3_OB1000_RAW_ARCHIVE_MIN_FREE_DISK_GB") or 5.0
        ),
        warn_free_disk_gb=float(
            os.environ.get("OB_V3_OB1000_RAW_ARCHIVE_WARN_FREE_DISK_GB") or 20.0
        ),
        depth=1000,
        format_version=OB1000_FORMAT_VERSION,
        parser_version=OB1000_PARSER_VERSION,
        health_nest_key="ob1000_raw_archive",
    )
