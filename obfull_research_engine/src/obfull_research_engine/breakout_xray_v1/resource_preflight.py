"""Local host resource preflight (no DB connection)."""

from __future__ import annotations

import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from obfull_research_engine.clickhouse_research_store_v1.analysis_readiness_v1_3 import (
    _swap_used_bytes,
)
from obfull_research_engine.clickhouse_research_store_v1.silver_full_build_v1_3 import (
    _available_memory_bytes,
)

STOP_RESOURCE_PREFLIGHT_RAM = "STOP_RESOURCE_PREFLIGHT_RAM"
STOP_RESOURCE_PREFLIGHT_DISK = "STOP_RESOURCE_PREFLIGHT_DISK"
STOP_RESOURCE_PREFLIGHT_SWAP = "STOP_RESOURCE_PREFLIGHT_SWAP"


@dataclass(frozen=True)
class ResourcePreflightConfig:
    """Hard floors — never silently weaken to 1 byte."""

    min_available_ram_bytes: int = 6 * 1024**3  # 6 GiB
    min_free_disk_bytes: int = 20 * 1024**3  # 20 GiB at output parent
    # Existing swap is documented as baseline; only additional growth is limited later.
    max_additional_swap_bytes: int = 512 * 1024**2  # 512 MiB growth tolerance

    def __post_init__(self) -> None:
        if self.min_available_ram_bytes < 1 * 1024**3:
            raise ValueError("STOP_RESOURCE_PREFLIGHT_CONFIG:min_ram_too_low")
        if self.min_free_disk_bytes < 1 * 1024**3:
            raise ValueError("STOP_RESOURCE_PREFLIGHT_CONFIG:min_disk_too_low")


@dataclass(frozen=True)
class ResourcePreflightResult:
    available_ram_bytes: int
    swap_used_baseline_bytes: int
    free_disk_bytes: int
    min_available_ram_bytes: int
    min_free_disk_bytes: int
    max_additional_swap_bytes: int
    output_parent: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_host_resource_preflight(
    *,
    output_dir: Path,
    config: ResourcePreflightConfig | None = None,
) -> ResourcePreflightResult:
    """Fail-closed local preflight before any ClickHouse client is created."""
    cfg = config or ResourcePreflightConfig()
    parent = Path(output_dir).expanduser().resolve().parent
    parent.mkdir(parents=True, exist_ok=True)

    available = int(_available_memory_bytes())
    if available < int(cfg.min_available_ram_bytes):
        raise RuntimeError(
            f"{STOP_RESOURCE_PREFLIGHT_RAM}:available={available}"
            f"<min={cfg.min_available_ram_bytes}"
        )

    free_disk = int(shutil.disk_usage(str(parent)).free)
    if free_disk < int(cfg.min_free_disk_bytes):
        raise RuntimeError(
            f"{STOP_RESOURCE_PREFLIGHT_DISK}:free={free_disk}"
            f"<min={cfg.min_free_disk_bytes}"
        )

    swap_used = int(_swap_used_bytes())
    # Existing swap is allowed as baseline; growth is enforced by sentinel later.
    return ResourcePreflightResult(
        available_ram_bytes=available,
        swap_used_baseline_bytes=swap_used,
        free_disk_bytes=free_disk,
        min_available_ram_bytes=int(cfg.min_available_ram_bytes),
        min_free_disk_bytes=int(cfg.min_free_disk_bytes),
        max_additional_swap_bytes=int(cfg.max_additional_swap_bytes),
        output_parent=str(parent),
    )


def assert_swap_growth_within_tolerance(
    *,
    baseline_swap_used_bytes: int,
    max_additional_swap_bytes: int,
) -> int:
    current = int(_swap_used_bytes())
    growth = current - int(baseline_swap_used_bytes)
    if growth > int(max_additional_swap_bytes):
        raise RuntimeError(
            f"{STOP_RESOURCE_PREFLIGHT_SWAP}:growth={growth}"
            f">max={max_additional_swap_bytes}"
        )
    return current
