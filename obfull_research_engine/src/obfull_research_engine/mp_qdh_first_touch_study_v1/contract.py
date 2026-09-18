"""Freeze contract: parameter body + SHA256 of all active transitive sources.

Byte-hash of listed source files: any byte change (including comments) changes
CONTRACT_HASH. Old parameter-only hash (2bbd0ec0…) is rejected as stale.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from . import (
    ABSORPTION_RATIO_STATUS,
    CONTRACT_VERSION,
    COST_ROUNDTRIP_PCT,
    HORIZONS_MIN,
    PACKAGE_NAME,
    REACH_THRESHOLDS_PCT,
    SCHEMA_VERSION,
    SL_STOPS_PCT,
    TARGET_REACH_PCT,
    VACUUM_SCORE_STATUS,
)
# Inline schema ids to avoid import-time dependency on drilldown/orderbook_analyse.
TYPED_TIMELINE_SCHEMA_VERSION = "typed_wall_flow_timeline_v1"
FOOTPRINT_SCHEMA_VERSION = "footprint_cluster_event_v1"

# Package root: .../obfull_research_engine/src/obfull_research_engine
_PKG_ROOT = Path(__file__).resolve().parents[1]
_SRC_ROOT = _PKG_ROOT  # obfull_research_engine package root

# Explicit transitive active path (sorted). Keep in sync with ACTIVE_PIPELINE docs.
CONTRACT_SOURCE_RELPATHS: tuple[str, ...] = tuple(
    sorted(
        {
            # First-touch package
            "mp_qdh_first_touch_study_v1/__init__.py",
            "mp_qdh_first_touch_study_v1/analyze_event.py",
            "mp_qdh_first_touch_study_v1/confidence.py",
            "mp_qdh_first_touch_study_v1/contract.py",
            "mp_qdh_first_touch_study_v1/footprint_cluster.py",
            "mp_qdh_first_touch_study_v1/outcomes.py",
            "mp_qdh_first_touch_study_v1/run_cli.py",
            "mp_qdh_first_touch_study_v1/run_smoke.py",
            "mp_qdh_first_touch_study_v1/run_study.py",
            "mp_qdh_first_touch_study_v1/source_run.py",
            "mp_qdh_first_touch_study_v1/typed_timeline.py",
            "mp_qdh_first_touch_study_v1/universe.py",
            # V2 flow
            "mp_qdh_30event_case_control_v2/__init__.py",
            "mp_qdh_30event_case_control_v2/analyze_one.py",
            "mp_qdh_30event_case_control_v2/coverage.py",
            "mp_qdh_30event_case_control_v2/features_v2.py",
            "mp_qdh_30event_case_control_v2/flow_v2.py",
            # V1 helpers used transitively
            "mp_qdh_30event_case_control_v1/paired.py",
            "mp_qdh_30event_case_control_v1/wall_movement.py",
            # Wall linkage
            "mp_qdh_wall_linkage_audit_v1/__init__.py",
            "mp_qdh_wall_linkage_audit_v1/audit_one.py",
            "mp_qdh_wall_linkage_audit_v1/flow_series.py",
            "mp_qdh_wall_linkage_audit_v1/trade_funnel.py",
            # Canonical
            "mp_qdh_canonical_integration_v1/event_load.py",
            "mp_qdh_canonical_integration_v1/near_zero.py",
            # V2 coverage gate (active)
            "mp_qdh_30event_case_control_v2/coverage.py",
            # Engines
            "level_first_episode1_wall_flow_qdh_base_v1/__init__.py",
            "level_first_episode1_wall_flow_qdh_base_v1/aggressor_flow.py",
            "level_first_episode1_wall_flow_qdh_base_v1/canonical_trades.py",
            "level_first_episode1_wall_flow_qdh_base_v1/mass_balance.py",
            "level_first_episode1_wall_flow_qdh_base_v1/price_response.py",
            "level_first_episode1_wall_flow_qdh_base_v1/queue_depletion_hazard.py",
            "level_first_episode1_wall_flow_qdh_base_v1/timeline_100ms.py",
            "level_first_episode1_wall_flow_qdh_base_v1/wall_flow_attribution.py",
            # Price path / candles
            "mp_price_path_4h_v1/__init__.py",
            "mp_price_path_4h_v1/candles.py",
            "mp_price_path_4h_v1/geometry.py",
            "mp_price_path_4h_v1/path_engine.py",
            # Stats helper
            "mp_big_move_case_control_v1/stats.py",
            # Silver adapters used by analyze_one
            "mp_wall_flow_qdh_silver_v1/silver_adapters.py",
            # Time helpers
            "timeparse.py",
            "drilldown/aggregation_100ms.py",
        }
    )
)

LEGACY_PARAM_ONLY_CONTRACT_HASH = (
    "2bbd0ec0712d298ed866ee79e55c4858d835de66f51795426a70d33d0d9faa10"
)
# Freeze tag contract (before external source-run resolver)
LEGACY_FREEZE_V1_CONTRACT_HASH = (
    "6287f655ba5a4a643f6d2bde31fbe34cb062b5628edba5aa0ccba59753618248"
)

CONTRACT_BODY: dict[str, Any] = {
    "package": PACKAGE_NAME,
    "contract_version": "1.2.0-external-source-run",
    "schema_version": SCHEMA_VERSION,
    "typed_timeline_schema": TYPED_TIMELINE_SCHEMA_VERSION,
    "footprint_schema": FOOTPRINT_SCHEMA_VERSION,
    "hash_policy": "sha256_bytes_of_listed_sources_plus_parameter_body",
    "source_run_resolution": {
        "priority": ["cli_source_run_dir", "OBFULL_RESEARCH_SOURCE_RUN_DIR", "default_repo_relative_if_exists"],
        "required_files": ["events_all.csv", "episodes.csv", "batch_windows.csv"],
        "path_not_in_contract_hash": True,
        "content_sha256_as_run_metadata": True,
    },
    "confidence_split": {
        "flow_attribution_confidence": ["HIGH", "MEDIUM", "LOW", "BLOCKED"],
        "availability_confidence": [
            "RECEIVE_TIME_OBSERVED",
            "EVENT_AVAILABLE_AT_OBSERVED",
            "EXCHANGE_TIME_WITH_CAUSAL_PROXY",
            "RECEIVE_TIME_NOT_AVAILABLE",
            "AVAILABILITY_BLOCKED",
        ],
        "receive_time_does_not_force_flow_low": True,
        "coverage_independent": True,
    },
    "mass_balance": {
        "fill_capped": "min(raw_fill, book_decrease)",
        "pull": "max(book_decrease - fill_capped, 0) unless LOW+no-fill → UNKNOWN",
        "refill": "inferred max((q1-q0)+fill_capped, 0)",
        "unknown_excluded_from_qdh": True,
        "unmatched_fill_excluded_from_qdh": True,
    },
    "qdh": {
        "formula": "max(EWMA(KnownNetDepletion/dt),0)/(queue_after+eps)",
        "queue": "defended_band_queue_after",
        "auc": "trapz(valid_qdh)/valid_seconds",
        "exhausted": "NULL + QUEUE_EXHAUSTED",
        "persistence_adjusted_name": "qdh_persistence_adjusted_research",
        "M_OI": 1.0,
        "M_Liq": 1.0,
    },
    "impact_efficiency": {
        "formula": "progress_bps / (hit_notional_usdt/1e6)",
        "no_epsilon_padding": True,
        "zero_notional": "NOT_AVAILABLE",
    },
    "depth": {
        "baseline": "120s_pre_touch_same_side_2bps_median",
        "norm": "raw/baseline - 1",
    },
    "first_touch_universe": {
        "is_first_touch_of_zone_version": True,
        "exclude_UNRESOLVED": True,
        "require_trade_side": True,
        "no_outcome_filter": True,
        "no_feature_filter": True,
    },
    "outcomes": {
        "units": "percent",
        "target_reach_pct": TARGET_REACH_PCT,
        "reach_thresholds_pct": list(REACH_THRESHOLDS_PCT),
        "horizons_min": list(HORIZONS_MIN),
        "sl_stops_pct": list(SL_STOPS_PCT),
        "costs_roundtrip_pct": list(COST_ROUNDTRIP_PCT),
        "candle_source": "signal_generator.candles_1m",
        "intrabar": "AMBIGUOUS_when_tp_and_sl_same_1m_candle",
        "true_break_uses_break_side": True,
    },
    "placeholders": {
        "absorption_ratio": ABSORPTION_RATIO_STATUS,
        "vacuum_score": VACUUM_SCORE_STATUS,
        "normalized_impact_efficiency": "NOT_CALIBRATED",
    },
    "footprint": {
        "same_trade_ids_as_qdh_hits": True,
        "adds_to_qdh_hits": False,
        "normalized_ie": "NOT_CALIBRATED",
    },
    "typed_timeline": {
        "view_only": True,
        "second_attribution_engine": False,
        "default_excludes_post_decision": True,
    },
}


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def collect_contract_inputs() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for rel in CONTRACT_SOURCE_RELPATHS:
        path = _SRC_ROOT / rel
        if not path.exists():
            raise FileNotFoundError(f"contract source missing: {rel}")
        rows.append(
            {
                "relpath": rel,
                "sha256": _file_sha256(path),
                "bytes": path.stat().st_size,
            }
        )
    rows.append(
        {
            "relpath": "__CONTRACT_BODY_JSON__",
            "sha256": hashlib.sha256(
                json.dumps(CONTRACT_BODY, sort_keys=True, separators=(",", ":"), default=str).encode()
            ).hexdigest(),
            "bytes": None,
        }
    )
    return rows


def compute_contract_hash(*, include_sources: bool = True) -> str:
    h = hashlib.sha256()
    body = json.dumps(CONTRACT_BODY, sort_keys=True, separators=(",", ":"), default=str)
    h.update(b"CONTRACT_BODY\n")
    h.update(body.encode("utf-8"))
    if include_sources:
        for row in collect_contract_inputs():
            if row["relpath"] == "__CONTRACT_BODY_JSON__":
                continue
            h.update(b"\nFILE:")
            h.update(row["relpath"].encode("utf-8"))
            h.update(b"\n")
            h.update(row["sha256"].encode("utf-8"))
    return h.hexdigest()


CONTRACT_HASH = compute_contract_hash()
CONTRACT_INPUTS = collect_contract_inputs()


def contract_manifest() -> dict[str, Any]:
    return {
        "package": PACKAGE_NAME,
        "contract_version": CONTRACT_BODY["contract_version"],
        "contract_hash": CONTRACT_HASH,
        "legacy_param_only_hash": LEGACY_PARAM_ONLY_CONTRACT_HASH,
        "n_source_files": sum(1 for r in CONTRACT_INPUTS if r["relpath"] != "__CONTRACT_BODY_JSON__"),
        "inputs": CONTRACT_INPUTS,
        "body": CONTRACT_BODY,
    }


def validate_checkpoint_contract(
    checkpoint: dict[str, Any],
    *,
    expected_hash: str = CONTRACT_HASH,
    expected_universe_hash: str | None = None,
) -> dict[str, Any]:
    got = checkpoint.get("contract_hash")
    if not got:
        return {"ok": False, "reason": "STALE_CHECKPOINT_REJECTED", "detail": "missing_contract_hash"}
    legacy = {LEGACY_PARAM_ONLY_CONTRACT_HASH, LEGACY_FREEZE_V1_CONTRACT_HASH}
    if str(got) in legacy and str(expected_hash) not in legacy:
        return {
            "ok": False,
            "reason": "STALE_CHECKPOINT_REJECTED",
            "detail": f"legacy_contract_hash={got}",
        }
    if str(got) != str(expected_hash):
        return {
            "ok": False,
            "reason": "STALE_CHECKPOINT_REJECTED",
            "detail": f"hash_mismatch got={got} expected={expected_hash}",
        }
    if expected_universe_hash is not None:
        uh = checkpoint.get("universe_hash")
        if str(uh) != str(expected_universe_hash):
            return {
                "ok": False,
                "reason": "STALE_CHECKPOINT_REJECTED",
                "detail": f"universe_mismatch got={uh} expected={expected_universe_hash}",
            }
    return {"ok": True, "reason": None, "detail": None}
