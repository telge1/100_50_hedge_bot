"""Pipeline: handoff → price-response timeline → snapshots → oracle → outputs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..bounded_level_first_analyzer_pilot_v1.persist import atomic_write_json, atomic_write_text
from ..level_first_episode1_detection_to_wall_flow_integration_v1.adapter import (
    validate_wall_generation_against_book,
)
from ..level_first_episode1_detection_to_wall_flow_integration_v1.handoff import (
    Episode1Handoff,
    HandoffError,
    validate_handoff,
)
from ..market_profile_lld_shared_event_materialization_v1.hashing import sha256_hex
from ..paths import ENGINE_ROOT
from . import (
    ALLOW_CLICKHOUSE_WRITES,
    FROZEN_BOOK_DIR,
    FROZEN_HANDOFF_PATH,
    FROZEN_WALL_FLOW_TIMELINE,
    RECLAIM_STATUS,
    TIME_BASIS,
    VERDICT_BLOCKED,
    VERDICT_OK,
    WALL_STATE,
)
from .book_stream import last_book_coverage_end, load_book_payload
from .impact_link import build_impact_efficiency_table
from .oracle import oracle_audit_timeline
from .snapshots import build_decision_snapshots
from .timeline import build_price_response_timeline, load_handoff, write_timeline_csv


def default_paths() -> dict[str, Path]:
    return {
        "handoff": ENGINE_ROOT / FROZEN_HANDOFF_PATH,
        "book": ENGINE_ROOT / FROZEN_BOOK_DIR,
        "wall_flow_timeline": ENGINE_ROOT / FROZEN_WALL_FLOW_TIMELINE,
    }


def semantic_payload(
    *,
    handoff: Episode1Handoff,
    meta: dict[str, Any],
    reclaim: dict[str, Any],
    snapshots: list[dict[str, Any]],
    oracle: dict[str, Any],
) -> dict[str, Any]:
    valid_snaps = [
        {
            "anchor": s["anchor"],
            "offset_s": s["offset_s"],
            "valid": s["valid"],
            "invalid_reason": s.get("invalid_reason"),
            "feature_mid": (s.get("FEATURE") or {}).get("midprice") if s.get("FEATURE") else None,
            "feature_micro": (s.get("FEATURE") or {}).get("microprice") if s.get("FEATURE") else None,
            "feature_crossed": (s.get("FEATURE") or {}).get("wall_side_crossed") if s.get("FEATURE") else None,
            "recross": (s.get("FEATURE") or {}).get("recross_count") if s.get("FEATURE") else None,
        }
        for s in snapshots
    ]
    return {
        "schema": "price_response_reclaim_v1",
        "episode_id": handoff.episode_id,
        "wall_generation_id": handoff.wall_generation_id,
        "replay_epoch": handoff.replay_epoch,
        "n_timeline_rows": meta.get("n_rows"),
        "n_coverage_ok": meta.get("n_coverage_ok"),
        "n_look_ahead": meta.get("n_look_ahead"),
        "reclaim_status": reclaim.get("reclaim_status"),
        "first_price_cross_at": reclaim.get("first_price_cross_at"),
        "first_microprice_cross_at": reclaim.get("first_microprice_cross_at"),
        "first_joint_cross_at": reclaim.get("first_joint_cross_at"),
        "recross_count": reclaim.get("recross_count"),
        "longest_defender_dwell_ms": reclaim.get("longest_defender_dwell_ms"),
        "snapshots": valid_snaps,
        "oracle_ok": oracle.get("ok"),
        "oracle_fp": oracle.get("fp"),
        "oracle_fn": oracle.get("fn"),
        "oracle_value_mismatch": oracle.get("value_mismatch"),
        "oracle_look_ahead": oracle.get("look_ahead"),
        "WALL_STATE": WALL_STATE,
        "time_basis": TIME_BASIS,
    }


def run_price_response(
    *,
    run_key: str,
    out_dir: Path,
    handoff: Episode1Handoff | dict[str, Any] | None = None,
    handoff_path: Path | None = None,
    persist_dir: Path | None = None,
    wall_flow_timeline: Path | None = None,
    skip_generation_check: bool = False,
) -> dict[str, Any]:
    if ALLOW_CLICKHOUSE_WRITES:
        raise RuntimeError("CH writes forbidden")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = default_paths()
    persist_dir = Path(persist_dir or paths["book"])
    wall_flow_timeline = Path(wall_flow_timeline or paths["wall_flow_timeline"])

    try:
        if handoff is None:
            hp = Path(handoff_path or paths["handoff"])
            if not hp.is_file():
                raise HandoffError(f"handoff file missing: {hp}")
            handoff = load_handoff(hp)
        else:
            handoff = validate_handoff(handoff)

        if not skip_generation_check:
            gen_check = validate_wall_generation_against_book(handoff, persist_dir)
        else:
            gen_check = {"ok": True, "skipped": True}

        bundle = build_price_response_timeline(
            handoff=handoff,
            persist_dir=persist_dir,
            wall_flow_timeline_csv=wall_flow_timeline,
        )
        rows = bundle["rows"]
        reclaim = bundle["reclaim_raw"]
        meta = bundle["meta"]
        payload = load_book_payload(persist_dir)
        coverage_end = last_book_coverage_end(payload)
        snapshots = build_decision_snapshots(
            handoff=handoff, rows=rows, book_coverage_end=coverage_end
        )
        oracle = oracle_audit_timeline(
            rows, wall_price=handoff.wall_price, wall_side=handoff.wall_side
        )
        impact_table = build_impact_efficiency_table(rows)

        write_timeline_csv(out_dir / "price_response_timeline.csv", rows)
        atomic_write_json(out_dir / "episode1_handoff.json", handoff.to_dict())
        atomic_write_json(out_dir / "reclaim_raw.json", reclaim)
        atomic_write_json(out_dir / "decision_snapshots.json", {"snapshots": snapshots})
        atomic_write_json(out_dir / "cross_dwell_table.json", {
            "price": reclaim.get("price_cross_detail"),
            "microprice": reclaim.get("microprice_cross_detail"),
            "reclaim_status": RECLAIM_STATUS,
        })
        atomic_write_json(out_dir / "impact_efficiency_raw_table.json", {"rows": impact_table})
        atomic_write_json(out_dir / "oracle_audit.json", oracle)

        sem = semantic_payload(
            handoff=handoff, meta=meta, reclaim=reclaim, snapshots=snapshots, oracle=oracle
        )
        sem_hash = sha256_hex(sem)
        atomic_write_json(out_dir / "semantic_fingerprint.json", {**sem, "semantic_hash": sem_hash})

        ok = bool(oracle.get("ok")) and int(meta.get("n_look_ahead") or 0) == 0 and int(meta.get("n_coverage_ok") or 0) > 0
        # Require at least some valid pre-epoch-change snapshots
        valid_snaps = [s for s in snapshots if s.get("valid")]
        if not valid_snaps:
            ok = False
        verdict = VERDICT_OK if ok else VERDICT_BLOCKED
        result = {
            "ok": ok,
            "verdict": verdict,
            "run_key": run_key,
            "semantic_hash": sem_hash,
            "oracle": {
                "ok": oracle.get("ok"),
                "fp": oracle.get("fp"),
                "fn": oracle.get("fn"),
                "value_mismatch": oracle.get("value_mismatch"),
                "look_ahead": oracle.get("look_ahead"),
            },
            "n_timeline_rows": meta.get("n_rows"),
            "n_coverage_ok": meta.get("n_coverage_ok"),
            "n_valid_snapshots": len(valid_snaps),
            "n_invalid_snapshots": sum(1 for s in snapshots if not s.get("valid")),
            "reclaim_status": RECLAIM_STATUS,
            "time_basis": TIME_BASIS,
            "WALL_STATE": WALL_STATE,
            "generation_check": gen_check,
            "first_price_cross_at": reclaim.get("first_price_cross_at"),
            "first_microprice_cross_at": reclaim.get("first_microprice_cross_at"),
            "recross_count": reclaim.get("recross_count"),
            "fail_closed": False,
        }
        atomic_write_json(out_dir / "run_manifest.json", result)
        atomic_write_text(out_dir / "STATUS", verdict + "\n")
        return result
    except HandoffError as exc:
        result = {
            "ok": False,
            "verdict": VERDICT_BLOCKED,
            "run_key": run_key,
            "fail_closed": True,
            "error": str(exc),
            "error_type": "HandoffError",
        }
        atomic_write_json(out_dir / "run_manifest.json", result)
        atomic_write_text(out_dir / "STATUS", VERDICT_BLOCKED + "\n")
        return result
