"""BT-D6 config + BT-D4 ResearchDensityRunV1 (only allowed density path)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Protocol

BT_D4 = Path(__file__).resolve().parents[1] / "phase_bt_d4_enforced_depth2_backtester_v1"
BT_D6 = Path(__file__).resolve().parents[1] / "phase_bt_d6_config_driven_strategy_pnl_foundation_v1"
BT_D8 = Path(__file__).resolve().parents[1] / "phase_bt_d8_canonical_density_cache_v1"
BT_D76 = Path(__file__).resolve().parents[1] / "phase_bt_d76_published_ch_binding_v1"
for p in (str(BT_D4), str(BT_D6), str(BT_D8), str(BT_D76)):
    if p not in sys.path:
        sys.path.insert(0, p)

from config_validate_v1 import ConfigValidationError, validate_config_dict  # noqa: E402
from research_backtester_v1 import ResearchDensityRunV1  # noqa: E402
from cache_session_v1 import ValidatedDensityCacheSessionV1  # noqa: E402
from published_ch_density_session_v1 import PublishedClickhouseDensitySessionV1  # noqa: E402


class DensitySession(Protocol):
    def collect_at_close(self, decision_close_utc: str) -> dict[str, Any]: ...


class ResearchDensitySessionV1:
    """Wraps ResearchDensityRunV1.collect_at_close — sole SSOT density path."""

    def __init__(self, config: dict[str, Any]) -> None:
        validate_config_dict(config)
        self._strategy_config = config
        self._run = ResearchDensityRunV1.from_config_path()

    def collect_at_close(self, decision_close_utc: str) -> dict[str, Any]:
        validate_config_dict(self._strategy_config)
        d = self._strategy_config["density"]
        if d["min_unique_origins"] != 2:
            raise ConfigValidationError("DENSITY_MIN_ORIGINS_LOCKED", "depth 2 required")
        return self._run.collect_at_close(decision_close_utc)


class FixtureDensitySessionV1:
    """Synthetic snapshots for integration tests (no CH)."""

    def __init__(self, snapshots_by_close: dict[str, dict[str, Any]]) -> None:
        self._snapshots = snapshots_by_close

    def collect_at_close(self, decision_close_utc: str) -> dict[str, Any]:
        if decision_close_utc not in self._snapshots:
            raise KeyError(f"fixture missing snapshot for {decision_close_utc}")
        return self._snapshots[decision_close_utc]


def create_density_session(config: dict[str, Any]) -> DensitySession:
    validate_config_dict(config)
    dens = config["density"]
    source = dens.get("density_source", "canonical_provider")
    if source == "canonical_provider":
        return ResearchDensitySessionV1(config)
    if source == "validated_cache":
        vc = dens["validated_cache"]
        bundle = Path(str(vc["bundle_path"]))
        if not bundle.is_dir():
            raise ConfigValidationError(
                "VALIDATED_CACHE_BUNDLE_MISSING",
                f"cache bundle not found: {bundle}",
            )
        return ValidatedDensityCacheSessionV1(bundle, strategy_config=config)
    if source == "published_clickhouse":
        return PublishedClickhouseDensitySessionV1(config)
    raise ConfigValidationError("INVALID_DENSITY_SOURCE", str(source))
