"""
Single gateway from research backtester → BT-D3 provider (no duplicate density math).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

TOOLS_D3 = Path(__file__).resolve().parents[1] / "phase_bt_d3_canonical_depth2_provider_v1"
TOOLS_D4 = Path(__file__).resolve().parent

for p in (str(TOOLS_D3), str(TOOLS_D4)):
    if p not in sys.path:
        sys.path.insert(0, p)

from config_enforcement_v1 import ResearchBacktesterConfigV1, ResearchConfigError  # noqa: E402
from density_provider_v1 import DensityBandsResult, DensityProviderError, get_density_bands  # noqa: E402


class ResearchDensityGatewayError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _validate_provider_result(cfg: ResearchBacktesterConfigV1, result: DensityBandsResult) -> None:
    if result.density_method != cfg.density_method:
        raise ResearchDensityGatewayError(
            "DENSITY_METHOD_DRIFT",
            "provider returned unexpected density_method",
            {"expected": cfg.density_method, "got": result.density_method},
        )
    if result.min_unique_origins != cfg.min_unique_origins:
        raise ResearchDensityGatewayError(
            "MIN_UNIQUE_ORIGINS_DRIFT",
            "provider min_unique_origins mismatch",
        )
    pipeline = result.manifest.get("pipeline_line_id")
    if not pipeline:
        raise ResearchDensityGatewayError(
            "PROVENANCE_MISSING",
            "pipeline_line_id missing from provider manifest",
        )
    if pipeline != cfg.expected_pipeline_line_id:
        raise ResearchDensityGatewayError(
            "PIPELINE_MISMATCH",
            "published pipeline does not match enforced config",
            {"expected": cfg.expected_pipeline_line_id, "got": pipeline},
        )
    config_hash = result.manifest.get("config_hash")
    if not config_hash:
        raise ResearchDensityGatewayError("PROVENANCE_MISSING", "config_hash missing")
    for band in result.bands:
        if int(band.get("unique_origin_count") or 0) < cfg.min_unique_origins:
            raise ResearchDensityGatewayError(
                "BAND_BELOW_MIN_ORIGINS",
                "band below enforced min_unique_origins",
                {"band": band},
            )


def fetch_density_bands_enforced(
    cfg: ResearchBacktesterConfigV1,
    decision_close_utc: str,
    *,
    use_parity_fixture_window: bool = False,
) -> DensityBandsResult:
    """Fail-closed fetch; only path research mode may use for density."""
    cfg.enforce_contract()
    if cfg.min_unique_origins != 2:
        raise ResearchDensityGatewayError("DEPTH_ENFORCEMENT_FAILED", "only depth 2 allowed")

    try:
        result = get_density_bands(
            cfg.symbol,
            decision_close_utc,
            timeframe=cfg.timeframe,
            pool_detail=cfg.pool_detail,
            min_unique_origins=cfg.min_unique_origins,
            density_method=cfg.density_method,
            use_parity_fixture_window=use_parity_fixture_window,
        )
    except DensityProviderError as exc:
        raise ResearchDensityGatewayError(exc.code, exc.message, exc.details) from exc

    _validate_provider_result(cfg, result)
    return result


def snapshot_payload(result: DensityBandsResult) -> dict[str, Any]:
    """Per-close data contract for research backtester (no trade fields)."""
    return {
        "decision_close_utc": result.decision_close_utc,
        "as_of_utc": result.decision_close_utc,
        "upper_bands": result.upper,
        "lower_bands": result.lower,
        "upper_band_count": len(result.upper),
        "lower_band_count": len(result.lower),
        "total_band_count": len(result.bands),
        "provider_manifest": result.manifest,
        "data_provenance": result.manifest.get("data_provenance"),
        "pipeline_line_id": result.manifest.get("pipeline_line_id"),
        "config_hash": result.manifest.get("config_hash"),
        "adapter_path": result.manifest.get("adapter_path"),
    }
