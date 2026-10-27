"""DensitySession adapter for published ClickHouse fast path (BT-D76)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

TOOLS_D3 = Path(__file__).resolve().parents[1] / "phase_bt_d3_canonical_depth2_provider_v1"
TOOLS_D4 = Path(__file__).resolve().parents[1] / "phase_bt_d4_enforced_depth2_backtester_v1"
TOOLS_D6 = Path(__file__).resolve().parents[1] / "phase_bt_d6_config_driven_strategy_pnl_foundation_v1"
TOOLS_D76 = Path(__file__).resolve().parent

for p in (str(TOOLS_D3), str(TOOLS_D4), str(TOOLS_D6), str(TOOLS_D76)):
    if p not in sys.path:
        sys.path.insert(0, p)

from config_validate_v1 import ConfigValidationError, validate_config_dict  # noqa: E402
from research_density_gateway_v1 import snapshot_payload  # noqa: E402

from published_ch_density_provider_v1 import get_density_bands_published_clickhouse  # noqa: E402


class PublishedClickhouseDensitySessionV1:
    """Published ACTIVE CH + batched membership cache; fail-closed on missing publish."""

    def __init__(self, config: dict[str, Any]) -> None:
        validate_config_dict(config)
        self._strategy_config = config
        si = config.get("strategy_identity") or {}
        if si.get("research_mode") != "depth2_density_research_v1":
            raise ConfigValidationError(
                "PUBLISHED_CH_RESEARCH_ONLY",
                "published_clickhouse density_source requires depth2_density_research_v1",
            )
        d = config["density"]
        if d.get("density_source") != "published_clickhouse":
            raise ConfigValidationError("PUBLISHED_CH_SOURCE_MISMATCH", "expected published_clickhouse")
        if d["min_unique_origins"] != 2:
            raise ConfigValidationError("DENSITY_MIN_ORIGINS_LOCKED", "depth 2 required")
        self._member_cache: dict[str, list[dict]] = {}

    def collect_at_close(self, decision_close_utc: str) -> dict[str, Any]:
        validate_config_dict(self._strategy_config)
        bt = self._strategy_config["backtest"]
        dens = self._strategy_config["density"]
        result = get_density_bands_published_clickhouse(
            bt["symbol"],
            decision_close_utc,
            timeframe=dens["timeframe"],
            pool_detail=int(dens["pool_detail"]),
            min_unique_origins=int(dens["min_unique_origins"]),
            density_method=str(dens["method"]),
            member_cache=self._member_cache,
        )
        snap = snapshot_payload(result)
        snap["density_transport"] = "published_clickhouse"
        return snap
