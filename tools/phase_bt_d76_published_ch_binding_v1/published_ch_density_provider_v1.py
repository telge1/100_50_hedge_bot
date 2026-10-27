"""Published ClickHouse density bands (same Node depth2 contract, faster window build)."""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TOOLS_D3 = Path(__file__).resolve().parents[1] / "phase_bt_d3_canonical_depth2_provider_v1"
for p in (str(TOOLS_D3),):
    if p not in sys.path:
        sys.path.insert(0, p)

from band_format_v1 import band_sort_key, interval_to_band  # noqa: E402
from density_contract_v1 import DensityContractError, validate_request  # noqa: E402
from density_provider_v1 import DensityBandsResult, DensityProviderError  # noqa: E402
from node_bridge_v1 import DensityNodeBridgeError, run_chart_path_density  # noqa: E402
from window_policy_v1 import causal_window_bounds, parity_fixture_bounds  # noqa: E402

from import_bootstrap_v1 import load_pools_landscape_window_module  # noqa: E402

PROVIDER_VERSION = "bt_published_ch_density_provider_v1"


def get_density_bands_published_clickhouse(
    symbol: str,
    decision_close_utc: str,
    *,
    timeframe: str = "5m",
    pool_detail: int = 1,
    min_unique_origins: int = 2,
    density_method: str = "exact_unique_origin",
    use_parity_fixture_window: bool = False,
    member_cache: dict[str, list[dict]] | None = None,
) -> DensityBandsResult:
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

    lw = load_pools_landscape_window_module()
    cache = member_cache if member_cache is not None else {}
    try:
        window = lw.build_landscape_window(
            vis_from,
            vis_to,
            timeframe=timeframe,
            pool_detail=pool_detail,
            member_cache=cache,
            density_only=True,
        )
    except lw.LandscapeWindowError as exc:
        raise DensityProviderError(exc.code, exc.message, exc.details) from exc

    pipeline_id = (window.get("provenance") or {}).get("pipeline_line_id")
    config_hash = (window.get("provenance") or {}).get("config_hash")
    if not pipeline_id or not config_hash:
        raise DensityProviderError("PROVENANCE_MISSING", "published window missing pipeline/config")

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
        "data_provenance": (
            "published_clickhouse_member_cache + build_landscape_window(density_only) "
            "+ analyzer_depth2_landscape.js chart.depth2"
        ),
        "window_timing_ms": window.get("timing_ms"),
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
