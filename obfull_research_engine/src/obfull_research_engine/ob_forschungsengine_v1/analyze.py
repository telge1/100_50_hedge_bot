"""Unified per-event analysis: QDH_base (+ optional MP enrichment) + gated stubs."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from obfull_research_engine.bounded_level_first_analyzer_pilot_v1.persist import (
    atomic_write_json,
)
from obfull_research_engine.clickhouse_research_store_v1.helpers import get_clickhouse_client
from obfull_research_engine.timeparse import format_utc_z

from .contract import (
    ANALYSIS_CONTRACT_VERSION,
    AUDIT_ID,
    FeatureGates,
    MODULE_REGISTRY,
    PACKAGE_NAME,
    SCHEMA_VERSION,
    SILVER_DATABASE,
    VERDICT_BLOCKED,
    VERDICT_EVENT_OK,
    default_gates,
)
from .event_spec import WallEventSpec, episode1_spec, spec_from_mp_event
from .optional_modules import (
    flow_type_enrichment,
    footprint_cluster_shadow,
    liquidation_shadow,
    oi_shadow,
    signal_v2,
    wall_state_classify,
)
from .qdh_engine import run_qdh_base


def _optional_bundle(gates: FeatureGates) -> dict[str, Any]:
    return {
        "footprint_cluster": footprint_cluster_shadow(enabled=gates.footprint_cluster),
        "oi_context": oi_shadow(enabled=gates.oi_shadow),
        "liquidation_flow": liquidation_shadow(enabled=gates.liquidation_shadow),
        "wall_state": wall_state_classify(enabled=gates.wall_state_classifier),
        "signal_v2": signal_v2(enabled=gates.signal_v2),
        "flow_type": flow_type_enrichment(enabled=gates.flow_type_lep),
    }


def analyze_event(
    spec: WallEventSpec,
    *,
    out_dir: Path,
    gates: FeatureGates | None = None,
    database: str = SILVER_DATABASE,
    client: Any | None = None,
    mp_event: dict[str, Any] | None = None,
    mp_window: dict[str, Any] | None = None,
    persist_timeline: bool = True,
) -> dict[str, Any]:
    """One-event research analysis under the unified contract."""
    gates = gates or default_gates()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()

    own = client is None
    client = client or get_clickhouse_client(role="ob_forschungsengine_analyze")
    enrichment_row: dict[str, Any] | None = None
    try:
        qdh = run_qdh_base(
            spec,
            out_dir=out_dir / "qdh_base" if persist_timeline else None,
            database=database,
            client=client,
            persist=persist_timeline,
        )

        if gates.mp_hit_pull_enrichment and mp_event is not None and mp_window is not None:
            from obfull_research_engine.mp_ob_feature_enrichment_v1.params import EnrichmentParams
            from obfull_research_engine.mp_ob_feature_enrichment_v1.run_cli import enrich_one

            enrichment_row = enrich_one(
                client,
                event={str(k): str(v) if v is not None else "" for k, v in mp_event.items()},
                window=mp_window,
                params=EnrichmentParams(
                    batch_run_dir=Path("."),
                    out_dir=out_dir,
                    silver_database=database,
                    symbol=spec.symbol,
                ),
                chunk_cache={},
            )
            # Drop internal meta rows from top-level persist
            enrichment_row = {
                k: v for k, v in enrichment_row.items() if not str(k).startswith("_")
            }
    finally:
        if own:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    optional = _optional_bundle(gates)
    ok = bool(qdh.get("ok"))
    verdict = VERDICT_EVENT_OK if ok else str(qdh.get("verdict") or VERDICT_BLOCKED)

    # Base result must ignore optional shadows (M_OI/M_LIQ already fixed at 1 in QDH).
    result = {
        "ok": ok,
        "verdict": verdict,
        "package": PACKAGE_NAME,
        "audit_id": AUDIT_ID,
        "schema_version": SCHEMA_VERSION,
        "contract_version": ANALYSIS_CONTRACT_VERSION,
        "event_id": spec.event_id,
        "gates": gates.to_dict(),
        "gates_active": gates.active_modules(),
        "gates_deferred": gates.deferred_modules(),
        "module_registry": MODULE_REGISTRY,
        "event_spec": spec.to_dict(),
        "qdh_base": {
            "ok": qdh.get("ok"),
            "verdict": qdh.get("verdict"),
            "manifest": qdh.get("manifest"),
            "anchor_qdh": (qdh.get("manifest") or {}).get("anchor_qdh"),
            "counts": (qdh.get("manifest") or {}).get("counts")
            or (qdh.get("content") or {}).get("counts"),
            "out_dir": qdh.get("out_dir"),
        },
        "mp_enrichment": enrichment_row,
        "optional": optional,
        "elapsed_s": round(time.monotonic() - t0, 3),
        "created_at": format_utc_z(datetime.now(timezone.utc)),
    }
    atomic_write_json(out_dir / "analysis_manifest.json", result)
    return result


def analyze_episode1(
    *,
    out_dir: Path,
    gates: FeatureGates | None = None,
    database: str = SILVER_DATABASE,
    client: Any | None = None,
) -> dict[str, Any]:
    """Convenience: frozen Episode-1 through the unified analyzer."""
    return analyze_event(
        episode1_spec(),
        out_dir=out_dir,
        gates=gates,
        database=database,
        client=client,
        persist_timeline=True,
    )


def analyze_mp_event(
    event: dict[str, Any],
    *,
    out_dir: Path,
    window: dict[str, Any] | None = None,
    gates: FeatureGates | None = None,
    database: str = SILVER_DATABASE,
    client: Any | None = None,
    tick_size: float = 0.1,
    band_ticks: int = 5,
    chain_version: str | None = None,
) -> dict[str, Any]:
    """MP batch event → WallEventSpec → unified analysis."""
    gates = gates or default_gates()
    spec = spec_from_mp_event(
        event,
        symbol=str(event.get("symbol") or "BTCUSDT"),
        tick_size=tick_size,
        band_ticks=band_ticks,
        chain_version=chain_version,
    )
    # Enrichment needs a window; synthesize from touch/trigger if absent.
    if window is None:
        touch_ns = int(event["first_touch_ts_ns"])
        trigger_raw = event.get("trigger_ts_ns")
        end_ns = int(trigger_raw) if trigger_raw not in (None, "", "None") else touch_ns + 120_000_000_000
        window = {
            "start_ns": touch_ns - 180_000_000_000,
            "end_ns": end_ns + 1,
            "replay_epoch": event.get("replay_epoch") or event.get("epoch_id") or "",
        }
    return analyze_event(
        spec,
        out_dir=out_dir,
        gates=gates,
        database=database,
        client=client,
        mp_event=event if gates.mp_hit_pull_enrichment else None,
        mp_window=window if gates.mp_hit_pull_enrichment else None,
        persist_timeline=True,
    )
