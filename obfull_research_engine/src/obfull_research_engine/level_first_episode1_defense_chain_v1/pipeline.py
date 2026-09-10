"""Pipeline: relevance → defense chain → attacker evolution → oracle."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from ..bounded_level_first_analyzer_pilot_v1.persist import atomic_write_json, atomic_write_text
from ..drilldown.aggregation_100ms import _as_dt
from ..level_first_episode1_detection_to_wall_flow_integration_v1.handoff import validate_handoff
from ..market_profile_lld_shared_event_materialization_v1.hashing import sha256_hex
from ..paths import ENGINE_ROOT
from ..timeparse import format_utc_z
from . import (
    ALLOW_CLICKHOUSE_WRITES,
    ASK_WALL_BREACH,
    CHAIN_CALIBRATION_STATUS,
    CHAIN_STATUS_EP1,
    EPOCH4,
    EPOCH4_COVERAGE_END,
    EPOCH4_COVERAGE_START,
    EPOCH5,
    EPOCH5_CHECKPOINT,
    FROZEN_HANDOFF_PATH,
    FROZEN_WALL_FLOW_TIMELINE,
    FROZEN_WALL_MIGRATION_RUN,
    RELEVANCE_CONTRACT,
    TIME_BASIS,
    VERDICT_MECHANICS_NO_COMPLETE,
    VERDICT_PROVEN,
    WALL_RELEVANCE_STATUS,
    WALL_STATE,
)
from .attacker_evolution import build_attacker_evolution
from .chain_builder import build_ask_defense_chain
from .oracle import oracle_audit
from .reclaim_drivers_raw import build_reclaim_drivers_raw
from .relevance import score_generations_past_only, summarize_generation_vs_relevant


def default_paths() -> dict[str, Path]:
    return {
        "wall_migration_run": ENGINE_ROOT / FROZEN_WALL_MIGRATION_RUN,
        "handoff": ENGINE_ROOT / FROZEN_HANDOFF_PATH,
        "wall_flow_timeline": ENGINE_ROOT / FROZEN_WALL_FLOW_TIMELINE,
    }


def _load_wf_csv(path: Path) -> list[dict[str, Any]]:
    with Path(path).open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _breach_horizon_table(chain: dict[str, Any], liquidity_rows: list[dict[str, Any]], wf_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    breach = _as_dt(ASK_WALL_BREACH)
    cov_end = _as_dt(EPOCH4_COVERAGE_END)
    out = []
    from datetime import timedelta

    for off in (1, 3, 5, 10, 15, 30, 60, 120):
        t = breach + timedelta(seconds=off)
        if t > cov_end:
            out.append(
                {
                    "offset_s": off,
                    "status": "CENSORED_BY_EPOCH_BOUNDARY",
                    "decision_time": format_utc_z(t),
                }
            )
            continue
        liq = None
        for r in liquidity_rows:
            if _as_dt(r["timestamp"]) <= t:
                liq = r
            else:
                break
        wf = None
        for r in wf_rows:
            avail = r.get("feature_available_at") or r.get("timestamp")
            if avail and _as_dt(avail) <= t:
                wf = r
            else:
                if avail and _as_dt(avail) > t:
                    break
        mid = None if not liq else liq.get("midprice")
        out.append(
            {
                "offset_s": off,
                "status": "OK",
                "decision_time": format_utc_z(t),
                "midprice": mid,
                "nearest_relevant_ask": None if not liq else liq.get("nearest_relevant_ask_price"),
                "ask_centroid": None if not liq else liq.get("ask_liquidity_centroid"),
                "qdh_base": None if not wf else wf.get("qdh_base"),
                "persistence_ratio": None if not wf else wf.get("persistence_ratio"),
                "impact_efficiency": None if not wf else wf.get("impact_efficiency"),
                "reclaim": bool(mid is not None and float(mid) < 79780.0),
                "chain_front_price": chain.get("current_front_wall_price"),
            }
        )
    return out


def run_defense_chain(
    *,
    run_key: str,
    out_dir: Path,
    wall_migration_dir: Path | None = None,
) -> dict[str, Any]:
    if ALLOW_CLICKHOUSE_WRITES:
        raise RuntimeError("CH writes forbidden")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = default_paths()
    wm = Path(wall_migration_dir or paths["wall_migration_run"])
    handoff = validate_handoff(json.loads(paths["handoff"].read_text(encoding="utf-8")))

    gens4 = json.loads((wm / "wall_generations_epoch4.json").read_text(encoding="utf-8"))["generations"]
    gens5 = json.loads((wm / "wall_generations_epoch5.json").read_text(encoding="utf-8"))["generations"]
    liq4 = json.loads((wm / "ask_liquidity_timeline_epoch4.json").read_text(encoding="utf-8"))["rows"]
    liq5 = json.loads((wm / "ask_liquidity_timeline_epoch5.json").read_text(encoding="utf-8")).get("rows") or []
    breach_struct = json.loads((wm / "breach_structure_epoch4.json").read_text(encoding="utf-8"))
    wf_rows = _load_wf_csv(paths["wall_flow_timeline"])

    scored4 = score_generations_past_only(gens4, liquidity_by_time=liq4)
    summary4 = summarize_generation_vs_relevant(scored4)
    scored5 = score_generations_past_only(gens5, liquidity_by_time=liq5)
    summary5 = summarize_generation_vs_relevant(scored5)

    chain4 = build_ask_defense_chain(
        scored_gens=scored4,
        replay_epoch=EPOCH4,
        coverage_start=EPOCH4_COVERAGE_START,
        coverage_end=EPOCH4_COVERAGE_END,
        wall_flow_rows=wf_rows,
        liquidity_rows=liq4,
    )
    chain5 = build_ask_defense_chain(
        scored_gens=scored5,
        replay_epoch=EPOCH5,
        coverage_start=EPOCH5_CHECKPOINT,
        coverage_end="2026-09-06T20:21:00Z",
        wall_flow_rows=wf_rows,
        liquidity_rows=liq5,
        breach_iso=None,
    )
    # Force ep5 censor independence
    c4 = chain4.to_dict()
    c5 = chain5.to_dict()
    c5["censor_reason"] = "INDEPENDENT_EPOCH5_SEGMENT"
    c5["chain_status"] = CHAIN_STATUS_EP1

    # Hard fail if any shared node ids
    ids4 = {n["wall_generation_id"] for n in c4.get("nodes") or []}
    ids5 = {n["wall_generation_id"] for n in c5.get("nodes") or []}
    if ids4 & ids5:
        raise RuntimeError(f"epoch bridge detected in chain nodes: {ids4 & ids5}")

    attacker = build_attacker_evolution(c4)
    reclaim_raw = build_reclaim_drivers_raw(
        wall_flow_rows=wf_rows,
        coverage_start=EPOCH4_COVERAGE_START,
        coverage_end=EPOCH4_COVERAGE_END,
    )
    horizons = _breach_horizon_table(c4, liq4, wf_rows)

    oracle = oracle_audit(
        scored=scored4,
        summary=summary4,
        chain=c4,
        chain_ep5=c5,
        breach_iso=ASK_WALL_BREACH,
    )

    # Verdict: complete multi-node chain with >=1 transition to another layer
    real_transitions = [
        t
        for t in (c4.get("transitions") or [])
        if t.get("to_generation_id") and t.get("transition_type") not in (None, "NO_NEXT_RELEVANT_LAYER", "CENSORED_BEFORE_RESOLUTION")
    ]
    if oracle.get("ok") and c4.get("node_count", 0) >= 2 and len(real_transitions) >= 1:
        verdict = VERDICT_PROVEN
    elif oracle.get("ok"):
        verdict = VERDICT_MECHANICS_NO_COMPLETE
    else:
        verdict = VERDICT_MECHANICS_NO_COMPLETE

    report = {
        "relevant_walls_in_epoch4_chain": c4.get("node_count"),
        "attacked_nodes": c4.get("attacked_node_count"),
        "pre_existing_nodes": c4.get("pre_existing_node_count"),
        "new_post_breach_nodes": c4.get("new_post_breach_node_count"),
        "original_wall_fill_vs_pull": breach_struct.get("original_wall_fill_vs_pull"),
        "chain_advance_ticks": c4.get("chain_advance_ticks"),
        "price_progress_ticks": c4.get("price_progress_ticks"),
        "centroid_shift_ticks": c4.get("centroid_shift_ticks"),
        "attacker_evolution_hops": len(attacker),
        "qdh_path": [
            {"node": n["chain_node_index"], "price": n["wall_price"], "qdh_start": n.get("qdh_base_start"), "qdh_end": n.get("qdh_base_end")}
            for n in (c4.get("nodes") or [])
        ],
        "impact_efficiency_path": [
            {
                "node": n["chain_node_index"],
                "price": n["wall_price"],
                "ie_start": n.get("impact_efficiency_start"),
                "ie_end": n.get("impact_efficiency_end"),
            }
            for n in (c4.get("nodes") or [])
        ],
        "breach_horizons": horizons,
        "censored_statements": [
            "Any reclaim after Epoch-4 coverage end",
            "Any chain continuity into Epoch 5",
            "WALL_FIRST_TOUCH/DETECTION horizons beyond continuous coverage",
            "Order-identity / same-seller migration claims",
        ],
        "reclaim_drivers_note": reclaim_raw.get("episode1_statement"),
        "generation_vs_relevant": summary4,
    }

    sem = {
        "schema": "defense_chain_v1",
        "relevance_contract": RELEVANCE_CONTRACT,
        "summary_ep4": summary4,
        "summary_ep5": {
            "generation_count": summary5["generation_count"],
            "relevant_wall_count": summary5["relevant_wall_count"],
        },
        "chain_ep4": {
            "chain_id": c4.get("chain_id"),
            "node_count": c4.get("node_count"),
            "attacked_node_count": c4.get("attacked_node_count"),
            "pre_existing_node_count": c4.get("pre_existing_node_count"),
            "new_post_breach_node_count": c4.get("new_post_breach_node_count"),
            "chain_advance_ticks": c4.get("chain_advance_ticks"),
            "transitions": [
                {"type": t.get("transition_type"), "from": t.get("from_price"), "to": t.get("to_price")}
                for t in (c4.get("transitions") or [])
            ],
            "chain_status": c4.get("chain_status"),
            "calibration_status": c4.get("calibration_status"),
        },
        "chain_ep5_node_count": c5.get("node_count"),
        "n_attacker_hops": len(attacker),
        "oracle_ok": oracle.get("ok"),
        "oracle_fp": oracle.get("fp"),
        "oracle_fn": oracle.get("fn"),
        "oracle_mass_error": oracle.get("mass_error"),
        "oracle_look_ahead": oracle.get("look_ahead"),
        "wall_relevance_status": WALL_RELEVANCE_STATUS,
        "WALL_STATE": WALL_STATE,
        "time_basis": TIME_BASIS,
        "verdict": verdict,
        "handoff_wall_generation_id": handoff.wall_generation_id,
    }
    sem_hash = sha256_hex(sem)

    atomic_write_json(out_dir / "relevance_contract.json", RELEVANCE_CONTRACT)
    atomic_write_json(out_dir / "generation_vs_relevant_epoch4.json", summary4)
    atomic_write_json(out_dir / "generation_vs_relevant_epoch5.json", summary5)
    atomic_write_json(out_dir / "scored_walls_epoch4.json", {"walls": scored4})
    atomic_write_json(out_dir / "defense_chain_epoch4.json", c4)
    atomic_write_json(out_dir / "defense_chain_epoch5.json", c5)
    atomic_write_json(out_dir / "attacker_evolution.json", {"hops": attacker})
    atomic_write_json(out_dir / "reclaim_drivers_raw.json", reclaim_raw)
    atomic_write_json(out_dir / "breach_horizons.json", {"horizons": horizons})
    atomic_write_json(out_dir / "episode1_pflichtbericht.json", report)
    atomic_write_json(out_dir / "oracle_audit.json", oracle)
    atomic_write_json(out_dir / "episode1_handoff.json", handoff.to_dict())
    atomic_write_json(out_dir / "semantic_fingerprint.json", {**sem, "semantic_hash": sem_hash})

    ok = bool(oracle.get("ok")) and verdict in (VERDICT_PROVEN, VERDICT_MECHANICS_NO_COMPLETE)
    result = {
        "ok": ok,
        "verdict": verdict,
        "run_key": run_key,
        "semantic_hash": sem_hash,
        "oracle": {k: oracle.get(k) for k in ("ok", "fp", "fn", "mass_error", "look_ahead")},
        "generation_count": summary4["generation_count"],
        "relevant_wall_count": summary4["relevant_wall_count"],
        "attacked_relevant_wall_count": summary4["attacked_relevant_wall_count"],
        "chain_node_count": c4.get("node_count"),
        "chain_attacked_nodes": c4.get("attacked_node_count"),
        "n_real_transitions": len(real_transitions),
        "chain_status": CHAIN_STATUS_EP1,
        "calibration_status": CHAIN_CALIBRATION_STATUS,
        "wall_relevance_status": WALL_RELEVANCE_STATUS,
        "time_basis": TIME_BASIS,
    }
    atomic_write_json(out_dir / "run_manifest.json", result)
    atomic_write_text(out_dir / "STATUS", verdict + "\n")
    return result
