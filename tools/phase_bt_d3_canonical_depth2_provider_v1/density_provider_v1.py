"""
Canonical Exact Unique-Origin density bands for backtester (BT-D3).

Uses ``build_landscape_window`` + Node ``analyzer_depth2_landscape.js`` (chart path:
mergeExactOriginSegments → filterSegmentsByMinOrigins).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from band_format_v1 import band_sort_key, interval_to_band
from density_contract_v1 import DensityContractError, validate_request
from node_bridge_v1 import DensityNodeBridgeError, run_chart_path_density
from ssot_bootstrap_v1 import ensure_ssot_import_path
from window_policy_v1 import causal_window_bounds, parity_fixture_bounds

PROVIDER_VERSION = "bt_density_provider_v1"


class DensityProviderError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass
class DensityBandsResult:
    symbol: str
    decision_close_utc: str
    timeframe: str
    pool_detail: int
    min_unique_origins: int
    density_method: str
    bands: list[dict[str, Any]]
    manifest: dict[str, Any]

    @property
    def upper(self) -> list[dict[str, Any]]:
        return [b for b in self.bands if b["side"] == "upper"]

    @property
    def lower(self) -> list[dict[str, Any]]:
        return [b for b in self.bands if b["side"] == "lower"]


def _provenance_from_window(window: dict[str, Any]) -> tuple[str | None, str | None]:
    prov = window.get("provenance") or {}
    return prov.get("pipeline_line_id"), prov.get("config_hash")


def _load_ssot_window(
    vis_from: str,
    vis_to: str,
    *,
    timeframe: str,
    pool_detail: int,
) -> dict[str, Any]:
    ensure_ssot_import_path()
    from pd1_landscape_viewer_v1.historical_window_v1 import (
        LandscapeWindowError,
        build_landscape_window,
    )

    try:
        return build_landscape_window(
            vis_from,
            vis_to,
            timeframe=timeframe,
            pool_detail=pool_detail,
        )
    except LandscapeWindowError as exc:
        raise DensityProviderError(exc.code, exc.message, exc.details) from exc


def get_density_bands(
    symbol: str,
    decision_close_utc: str,
    *,
    timeframe: str = "5m",
    pool_detail: int = 1,
    min_unique_origins: int = 2,
    density_method: str = "exact_unique_origin",
    use_parity_fixture_window: bool = False,
) -> DensityBandsResult:
    """
    Return exact-density bands at one causal 5m decision close.

    Fail-closed on contract mismatch or unpublished SSOT scope.
    """
    try:
        contract = validate_request(
            symbol=symbol,
            timeframe=timeframe,
            pool_detail=pool_detail,
            min_unique_origins=min_unique_origins,
            density_method=density_method,
        )
    except DensityContractError as exc:
        raise DensityProviderError(exc.code, exc.message, exc.details) from exc

    fixture = parity_fixture_bounds(decision_close_utc) if use_parity_fixture_window else None
    if fixture:
        vis_from, vis_to = fixture
    else:
        vis_from, vis_to = causal_window_bounds(decision_close_utc)

    window = _load_ssot_window(vis_from, vis_to, timeframe=timeframe, pool_detail=pool_detail)
    pipeline_id, config_hash = _provenance_from_window(window)

    try:
        node_out = run_chart_path_density(
            window,
            as_of_utc=decision_close_utc,
            slot_minutes=5,
        )
    except DensityNodeBridgeError as exc:
        raise DensityProviderError("DENSITY_NODE_BRIDGE_FAILED", str(exc)) from exc

    landscape = node_out.get("landscape") or {}
    parity = landscape.get("parity") or {}
    if parity.get("ok") is False:
        raise DensityProviderError(
            "CHART_FORENSIC_PARITY_FAILED",
            "internal chart-path parity check failed in analyzer",
            {"parity": parity},
        )

    intervals = landscape.get("display_intervals") or []
    bands: list[dict[str, Any]] = []
    for iv in intervals:
        depth = int(iv.get("depth") or 0)
        if depth < min_unique_origins:
            continue
        bands.append(
            interval_to_band(
                iv,
                as_of_utc=decision_close_utc,
                pipeline_line_id=pipeline_id,
                config_hash=config_hash,
            )
        )
    bands.sort(key=band_sort_key)

    win = window.get("window") or {}
    manifest = {
        **contract.to_manifest_dict(),
        "provider_version": PROVIDER_VERSION,
        "symbol": symbol,
        "decision_close_utc": decision_close_utc,
        "visible_from_utc": win.get("from_utc") or vis_from,
        "visible_to_utc": win.get("to_utc") or vis_to,
        "context_from_utc": win.get("context_from_utc"),
        "pipeline_line_id": pipeline_id,
        "config_hash": config_hash,
        "adapter_path": node_out.get("adapter_path"),
        "node_landscape_parity": parity,
        "upper_band_count": sum(1 for b in bands if b["side"] == "upper"),
        "lower_band_count": sum(1 for b in bands if b["side"] == "lower"),
        "data_provenance": "build_landscape_window + analyzer_depth2_landscape.js chart.depth2",
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    manifest["manifest_hash"] = hashlib.sha256(
        json.dumps(manifest, sort_keys=True).encode()
    ).hexdigest()[:16]

    return DensityBandsResult(
        symbol=symbol,
        decision_close_utc=decision_close_utc,
        timeframe=timeframe,
        pool_detail=pool_detail,
        min_unique_origins=min_unique_origins,
        density_method=density_method,
        bands=bands,
        manifest=manifest,
    )


def export_manifest_json(result: DensityBandsResult, path: Path) -> None:
    payload = {
        "manifest": result.manifest,
        "upper": result.upper,
        "lower": result.lower,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
