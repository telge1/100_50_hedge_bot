"""Contract hash for ema_trend_live_analyzer_v1 + collector fanout/archive sources."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from . import (
    ALLOW_CLICKHOUSE_WRITES,
    ALLOW_MYSQL_WRITES,
    ARCHIVE_ENABLED,
    CONTRACT_VERSION,
    ENGINE_VERSION,
    FEATURE_HORIZONS_S,
    FIRST_CANDIDATE_ELIGIBLE_SECONDS,
    FIRST_EARLY_EVIDENCE_SECONDS,
    FULL_OB_OBSERVATION_SECONDS,
    LIVE_TRADING,
    MAX_ACCEPTED_CASES,
    MP_REQUIRED,
    OUTCOME_HORIZON_SECONDS,
    PACKAGE_NAME,
    PUBLIC_TRADES_MODE,
    PUBLIC_TRADES_SOURCE,
    SCHEMA_VERSION,
    SECOND_BYBIT_OB_WS,
    SECOND_PUBLIC_TRADE_WS,
    WARMUP_REQUIRED,
)

_PKG = Path(__file__).resolve().parent
_ENGINE_ROOT = _PKG.parent

ANALYZER_SOURCES: tuple[str, ...] = tuple(
    sorted(p.name for p in _PKG.glob("*.py") if p.name != "__pycache__")
)

ENGINE_SOURCES: tuple[str, ...] = (
    "level_first_episode1_wall_flow_qdh_base_v1/wall_flow_attribution.py",
    "level_first_episode1_wall_flow_qdh_base_v1/canonical_trades.py",
    "level_first_episode1_wall_flow_qdh_base_v1/mass_balance.py",
    "level_first_episode1_wall_flow_qdh_base_v1/aggressor_flow.py",
    "level_first_episode1_wall_flow_qdh_base_v1/queue_depletion_hazard.py",
    "level_first_episode1_wall_flow_qdh_base_v1/price_response.py",
    "mp_qdh_30event_case_control_v2/flow_v2.py",
    "mp_qdh_30event_case_control_v1/wall_movement.py",
    "mp_qdh_first_touch_study_v1/footprint_cluster.py",
    "mp_qdh_first_touch_study_v1/typed_timeline.py",
)

COLLECTOR_FANOUT_SOURCES: tuple[str, ...] = (
    "orderbook_v2_live/full_ob_event_fanout.py",
    "orderbook_v2_live/full_ob_case_archive.py",
    "orderbook_v2_live/on_demand_full.py",
    "orderbook_v2_live/full_ob_continuous_raw_archive/__init__.py",
    "orderbook_v2_live/full_ob_continuous_raw_archive/checkpoint.py",
    "orderbook_v2_live/full_ob_continuous_raw_archive/config.py",
    "orderbook_v2_live/full_ob_continuous_raw_archive/disk_safety.py",
    "orderbook_v2_live/full_ob_continuous_raw_archive/envelope.py",
    "orderbook_v2_live/full_ob_continuous_raw_archive/manager.py",
    "orderbook_v2_live/full_ob_continuous_raw_archive/metrics.py",
    "orderbook_v2_live/full_ob_continuous_raw_archive/queue.py",
    "orderbook_v2_live/full_ob_continuous_raw_archive/replay.py",
    "orderbook_v2_live/full_ob_continuous_raw_archive/segment.py",
    "orderbook_v2_live/full_ob_continuous_raw_archive/tmp_recovery.py",
)

RAW_ARCHIVE_FORMAT_VERSION = "full_ob_continuous_raw_archive_v1"
RAW_ARCHIVE_SOURCE_PROVENANCE = "UNCOMMITTED_WORKING_TREE"
RAW_ARCHIVE_SOURCE_REPO = "/home/telgenbuescher/projects/orderbook_analyse"
RAW_ARCHIVE_SOURCE_HEAD = "4b364d1993145c08b195fd7cf9902a84bd09f0fd"
RAW_ARCHIVE_NOTE = (
    "Package not present in base commit 4b364d1; byte-identical copy from "
    "orderbook_analyse untracked working tree. Imported via dedicated collector commit."
)


def _sha_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def build_contract_manifest(
    *,
    collector_src_root: Path | None = None,
    collector_commit_sha: str | None = None,
) -> dict[str, Any]:
    analyzer_hashes = {}
    for name in ANALYZER_SOURCES:
        p = _PKG / name
        if p.is_file():
            analyzer_hashes[name] = _sha_file(p)
    engine_hashes = {}
    for rel in ENGINE_SOURCES:
        p = _ENGINE_ROOT / rel
        if p.is_file():
            engine_hashes[rel] = _sha_file(p)
    collector_hashes = {}
    if collector_src_root is not None:
        root = Path(collector_src_root)
        for rel in COLLECTOR_FANOUT_SOURCES:
            p = root / rel
            if p.is_file():
                collector_hashes[rel] = _sha_file(p)

    body = {
        "package": PACKAGE_NAME,
        "schema_version": SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "engine_version": ENGINE_VERSION,
        "mp_required": MP_REQUIRED,
        "warmup_required": WARMUP_REQUIRED,
        "live_trading": LIVE_TRADING,
        "max_signals": MAX_ACCEPTED_CASES,
        "full_ob_observation_seconds": FULL_OB_OBSERVATION_SECONDS,
        "outcome_horizon_seconds": OUTCOME_HORIZON_SECONDS,
        "first_early_evidence_seconds": FIRST_EARLY_EVIDENCE_SECONDS,
        "first_candidate_eligible_seconds": FIRST_CANDIDATE_ELIGIBLE_SECONDS,
        "feature_horizons_s": list(FEATURE_HORIZONS_S),
        "archive_enabled": ARCHIVE_ENABLED,
        "public_trades_source": PUBLIC_TRADES_SOURCE,
        "public_trades_mode": PUBLIC_TRADES_MODE,
        "second_bybit_ob_ws": SECOND_BYBIT_OB_WS,
        "second_public_trade_ws": SECOND_PUBLIC_TRADE_WS,
        "allow_clickhouse_writes": ALLOW_CLICKHOUSE_WRITES,
        "allow_mysql_writes": ALLOW_MYSQL_WRITES,
        "raw_archive_format_version": RAW_ARCHIVE_FORMAT_VERSION,
        "raw_archive_source_provenance": RAW_ARCHIVE_SOURCE_PROVENANCE,
        "raw_archive_source_repo": RAW_ARCHIVE_SOURCE_REPO,
        "raw_archive_source_head_at_copy": RAW_ARCHIVE_SOURCE_HEAD,
        "raw_archive_provenance_note": RAW_ARCHIVE_NOTE,
        "queue_default_size": 8192,
        "collector_commit_sha": collector_commit_sha,
        "analyzer_sources": analyzer_hashes,
        "engine_sources": engine_hashes,
        "collector_sources": collector_hashes,
        "event_schema": [
            "symbol",
            "side",
            "price",
            "old_qty",
            "new_qty",
            "delta_qty",
            "exchange_event_time",
            "receive_time_ns",
            "sequence_id",
            "update_id",
            "snapshot_generation",
            "record_ordinal",
            "source",
        ],
        "candidate_states": [
            "UNRESOLVED",
            "EARLY_EVIDENCE",
            "CONTINUATION_EVIDENCE",
            "MEAN_REVERSION_EVIDENCE",
            "CONTINUATION_CANDIDATE",
            "MEAN_REVERSION_CANDIDATE",
            "CONFLICTING",
            "BLOCKED_COVERAGE",
            "BLOCKED_RAW_ARCHIVE",
            "EXPIRED",
        ],
        "forensic_package_import_forbidden": "ema_trend_analyzer_v1",
    }
    blob = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    contract_hash = hashlib.sha256(blob).hexdigest()
    body["contract_hash"] = contract_hash
    return body


def write_contract_manifest(
    path: Path,
    *,
    collector_src_root: Path | None = None,
    collector_commit_sha: str | None = None,
) -> dict[str, Any]:
    man = build_contract_manifest(
        collector_src_root=collector_src_root,
        collector_commit_sha=collector_commit_sha,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(man, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return man
