"""BT-D79 — one lifetime build per chunk, per-close Node as_of (research only)."""

from __future__ import annotations

import copy
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

TOOLS_D3 = Path(__file__).resolve().parents[1] / "phase_bt_d3_canonical_depth2_provider_v1"
TOOLS_D4 = Path(__file__).resolve().parents[1] / "phase_bt_d4_enforced_depth2_backtester_v1"
TOOLS_D76 = Path(__file__).resolve().parents[1] / "phase_bt_d76_published_ch_binding_v1"
for p in (str(TOOLS_D3), str(TOOLS_D4), str(TOOLS_D76)):
    if p not in sys.path:
        sys.path.insert(0, p)

from band_format_v1 import band_sort_key, interval_to_band  # noqa: E402
from density_contract_v1 import DensityContractError, validate_request  # noqa: E402
from density_provider_v1 import DensityBandsResult, DensityProviderError  # noqa: E402
from node_bridge_v1 import DensityNodeBridgeError, run_chart_path_density  # noqa: E402
from research_density_gateway_v1 import snapshot_payload  # noqa: E402
from window_policy_v1 import causal_window_bounds  # noqa: E402

from import_bootstrap_v1 import load_pools_landscape_window_module  # noqa: E402

PROVIDER_VERSION = "bt_chunk_lifetime_density_v1"


@dataclass
class ChunkLifetimeSessionV1:
    """Precomputed chunk lifetimes; per-close clip + Node (fail-closed)."""

    closes: tuple[str, ...]
    chunk_vis_from: str
    chunk_vis_to: str
    member_cache: dict[str, list[dict]] = field(default_factory=dict)
    _base_window: dict[str, Any] = field(default_factory=dict)
    _pools_full: list[dict[str, Any]] = field(default_factory=list)
    _snapshots: dict[str, dict[str, Any]] = field(default_factory=dict)
    timing: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        closes: list[str],
        *,
        timeframe: str = "5m",
        pool_detail: int = 1,
    ) -> ChunkLifetimeSessionV1:
        if not closes:
            raise ValueError("closes required")
        vis_lo = causal_window_bounds(closes[0])[0]
        vis_hi = causal_window_bounds(closes[-1])[1]
        lw = load_pools_landscape_window_module()
        t0 = time.perf_counter()
        window = lw.build_landscape_window(
            vis_lo,
            vis_hi,
            timeframe=timeframe,
            pool_detail=pool_detail,
            member_cache={},
            density_only=True,
            expose_pools_full=True,
        )
        life_s = time.perf_counter() - t0
        pools_full = window.pop("_research_pools_full", None)
        if pools_full is None:
            raise DensityProviderError("CHUNK_BUILD_FAILED", "expose_pools_full missing")
        return cls(
            closes=tuple(closes),
            chunk_vis_from=vis_lo,
            chunk_vis_to=vis_hi,
            _base_window=window,
            _pools_full=pools_full,
            timing={"chunk_lifetime_build_s": round(life_s, 4), "timing_ms": window.get("timing_ms")},
        )

    def collect_at_close(self, decision_close_utc: str) -> dict[str, Any]:
        if decision_close_utc in self._snapshots:
            return self._snapshots[decision_close_utc]
        if decision_close_utc not in self.closes:
            raise DensityProviderError(
                "CHUNK_CLOSE_OUT_OF_SCOPE",
                "close not in pilot chunk",
                {"close": decision_close_utc},
            )
        snap = self._bands_at_close(decision_close_utc)
        self._snapshots[decision_close_utc] = snap
        return snap

    def _bands_at_close(self, decision_close_utc: str) -> dict[str, Any]:
        lw = load_pools_landscape_window_module()
        vis_from, vis_to = causal_window_bounds(decision_close_utc)
        vis_from_dt = lw._parse_utc(vis_from)
        vis_to_dt = lw._parse_utc(vis_to)
        pools = lw._clip_segments_to_window(self._pools_full, vis_from_dt, vis_to_dt)
        payload = copy.deepcopy(self._base_window)
        payload["pools"] = pools
        payload["window"] = {
            "from_utc": vis_from,
            "to_utc": vis_to,
            "context_from_utc": self._base_window.get("window", {}).get("context_from_utc"),
        }
        result = _node_to_bands(
            payload,
            decision_close_utc=decision_close_utc,
            vis_from=vis_from,
            vis_to=vis_to,
        )
        snap = snapshot_payload(result)
        snap["density_transport"] = "chunk_lifetime_amortized"
        return snap


def _node_to_bands(
    window: dict[str, Any],
    *,
    decision_close_utc: str,
    vis_from: str,
    vis_to: str,
) -> DensityBandsResult:
    try:
        contract = validate_request(
            symbol="DOGEUSDT",
            timeframe="5m",
            pool_detail=1,
            min_unique_origins=2,
            density_method="exact_unique_origin",
        )
    except DensityContractError as exc:
        raise DensityProviderError(exc.code, exc.message, exc.details) from exc

    prov = window.get("provenance") or {}
    pipeline_id = prov.get("pipeline_line_id")
    config_hash = prov.get("config_hash")
    if not pipeline_id or not config_hash:
        raise DensityProviderError("PROVENANCE_MISSING", "chunk window missing provenance")

    try:
        node_out = run_chart_path_density(window, as_of_utc=decision_close_utc, slot_minutes=5)
    except DensityNodeBridgeError as exc:
        raise DensityProviderError("DENSITY_NODE_BRIDGE_FAILED", str(exc)) from exc

    landscape = node_out.get("landscape") or {}
    parity = landscape.get("parity") or {}
    if parity.get("ok") is False:
        raise DensityProviderError("CHART_FORENSIC_PARITY_FAILED", "analyzer parity failed", {"parity": parity})

    intervals = landscape.get("display_intervals") or []
    bands: list[dict[str, Any]] = []
    for iv in intervals:
        depth = int(iv.get("depth") or 0)
        if depth < 2:
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

    manifest = {
        **contract.to_manifest_dict(),
        "provider_version": PROVIDER_VERSION,
        "symbol": "DOGEUSDT",
        "decision_close_utc": decision_close_utc,
        "visible_from_utc": vis_from,
        "visible_to_utc": vis_to,
        "context_from_utc": (window.get("window") or {}).get("context_from_utc"),
        "pipeline_line_id": pipeline_id,
        "config_hash": config_hash,
        "adapter_path": node_out.get("adapter_path"),
        "node_landscape_parity": parity,
        "data_provenance": "chunk_lifetime_amortized + analyzer_depth2_landscape.js",
    }
    return DensityBandsResult(
        symbol="DOGEUSDT",
        decision_close_utc=decision_close_utc,
        timeframe="5m",
        pool_detail=1,
        min_unique_origins=2,
        density_method="exact_unique_origin",
        bands=bands,
        manifest=manifest,
    )


def benchmark_chunk_collect(
    session: ChunkLifetimeSessionV1,
    closes: list[str],
) -> dict[str, Any]:
    """Per-close Node+clip after shared lifetime (excludes initial build)."""
    t0 = time.perf_counter()
    node_ms: list[float] = []
    for c in closes:
        t1 = time.perf_counter()
        session._bands_at_close(c)
        node_ms.append((time.perf_counter() - t1) * 1000)
    total = time.perf_counter() - t0
    return {
        "per_close_node_clip_total_s": round(total, 4),
        "per_close_mean_ms": round(sum(node_ms) / len(node_ms), 3) if node_ms else 0,
        "per_close_max_ms": round(max(node_ms), 3) if node_ms else 0,
    }
