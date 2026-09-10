"""Pipeline for within-epoch wall migration / structure analysis."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..bounded_level_first_analyzer_pilot_v1.persist import atomic_write_json, atomic_write_text
from ..drilldown.aggregation_100ms import _as_dt
from ..level_first_episode1_detection_to_wall_flow_integration_v1.handoff import validate_handoff
from ..level_first_episode1_wall_flow_qdh_base_v1.pipeline import _load_trades
from ..market_profile_lld_shared_event_materialization_v1.hashing import sha256_hex
from ..paths import ENGINE_ROOT
from ..timeparse import format_utc_z
from . import (
    ALLOW_CLICKHOUSE_WRITES,
    ASK_WALL_BREACH,
    BAND_CONTRACT,
    FROZEN_BOOK_DIR,
    FROZEN_HANDOFF_PATH,
    FROZEN_TRADES,
    FROZEN_WALL_FLOW_TIMELINE,
    LAYER_POST_EPOCH,
    ORIGINAL_WALL_GENERATION_ID,
    TIME_BASIS,
    VERDICT_INDETERMINATE,
    VERDICT_NOT_OBSERVED,
    VERDICT_STRUCTURE_PROVEN,
    WALL_MIGRATION_STATUS,
    WALL_STATE,
)
from .band_liquidity import build_liquidity_timeline
from .breach_structure import analyze_epoch4_breach_structure, analyze_epoch5_independent_landscape
from .generations import build_ask_generations_for_segment
from .migration_proxy import build_migration_proxies
from .oracle import oracle_audit
from .outcomes import build_breach_outcomes
from .segments import epoch4_segment, epoch5_segment, load_payload, segment_to_dict


def default_paths() -> dict[str, Path]:
    return {
        "book": ENGINE_ROOT / FROZEN_BOOK_DIR,
        "handoff": ENGINE_ROOT / FROZEN_HANDOFF_PATH,
        "trades": ENGINE_ROOT / FROZEN_TRADES,
        "wall_flow_timeline": ENGINE_ROOT / FROZEN_WALL_FLOW_TIMELINE,
    }


def _decide_verdict(ep4_struct: dict[str, Any], liq: list[dict[str, Any]], proxies: list[dict[str, Any]]) -> str:
    """Structure proven if within-epoch aggregated ask structure movement is evidenced."""
    if not liq:
        return VERDICT_INDETERMINATE
    shifts = [r.get("centroid_shift_ticks") for r in liq if r.get("centroid_shift_ticks") is not None]
    front = [r.get("front_wall_shift_ticks") for r in liq if r.get("front_wall_shift_ticks") is not None]
    moved = bool(ep4_struct.get("ask_centroid_moved_higher")) or (
        any(abs(float(s)) >= 1.0 for s in shifts) if shifts else False
    ) or (any(abs(float(s)) >= 1.0 for s in front) if front else False)
    stacked = bool(ep4_struct.get("stacked_defense_visible_within_epoch4"))
    pre_n = int(ep4_struct.get("n_pre_existing_above") or 0)
    post_n = int(ep4_struct.get("n_post_breach_new") or 0)
    if moved or stacked or pre_n >= 1:
        # Movement of aggregated structure OR stacked pre-existing layers above wall
        if moved or stacked:
            return VERDICT_STRUCTURE_PROVEN
        # Only pre-existing thin layers without movement → still structure observation of defense stack
        if pre_n >= 2:
            return VERDICT_STRUCTURE_PROVEN
        return VERDICT_STRUCTURE_PROVEN if pre_n >= 1 and (post_n >= 0) else VERDICT_NOT_OBSERVED
    if proxies:
        return VERDICT_NOT_OBSERVED
    return VERDICT_NOT_OBSERVED


def run_wall_migration_structure(
    *,
    run_key: str,
    out_dir: Path,
    persist_dir: Path | None = None,
    trades_path: Path | None = None,
    wall_flow_csv: Path | None = None,
    handoff_path: Path | None = None,
) -> dict[str, Any]:
    if ALLOW_CLICKHOUSE_WRITES:
        raise RuntimeError("CH writes forbidden")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = default_paths()
    persist_dir = Path(persist_dir or paths["book"])
    trades_path = Path(trades_path or paths["trades"])
    wall_flow_csv = Path(wall_flow_csv or paths["wall_flow_timeline"])
    handoff = validate_handoff(json.loads(Path(handoff_path or paths["handoff"]).read_text(encoding="utf-8")))

    payload = load_payload(persist_dir)
    trades, _ = _load_trades(trades_path)

    seg4 = epoch4_segment()
    seg5 = epoch5_segment()

    gens4 = build_ask_generations_for_segment(payload=payload, segment=seg4, trades=trades, breach_iso=ASK_WALL_BREACH)
    gens5 = build_ask_generations_for_segment(payload=payload, segment=seg5, trades=trades, breach_iso=None)

    gens4_dicts = [g.to_dict() for g in gens4]
    gens5_fixed = []
    for g in gens5:
        d = g.to_dict()
        d["layer_class"] = LAYER_POST_EPOCH
        d["replay_epoch"] = 5
        gens5_fixed.append(d)
    ids4 = {g["wall_generation_id"] for g in gens4_dicts}
    if ids4 & {g["wall_generation_id"] for g in gens5_fixed}:
        raise RuntimeError("epoch boundary violated: shared wall_generation_id across epochs")

    liq4 = build_liquidity_timeline(payload=payload, segment=seg4, generations=gens4)
    liq5 = build_liquidity_timeline(payload=payload, segment=seg5, generations=gens5)

    proxies4 = build_migration_proxies(gens4, breach_iso=ASK_WALL_BREACH, replay_epoch=4)
    # Enrich price_follow_delay using best ask timeline
    ba_tl = [(_as_dt(r["timestamp"]), float(r["best_ask"])) for r in liq4 if r.get("best_ask") is not None]
    for p in proxies4:
        next_px = float(p["next_price"])
        # find next gen start
        ng = next(g for g in gens4 if g.wall_generation_id == p["next_wall_generation_id"])
        start = _as_dt(ng.start_time)
        delay = None
        for t, ba in ba_tl:
            if t >= start and ba + 1e-12 >= next_px:
                delay = int(round((t - start).total_seconds() * 1000))
                break
        p["price_follow_delay_ms"] = delay

    # No ep4→ep5 proxies
    proxies5 = build_migration_proxies(gens5, breach_iso=None, replay_epoch=5)

    ep4_struct = analyze_epoch4_breach_structure(gens=gens4, liquidity_rows=liq4)
    ep5_land = analyze_epoch5_independent_landscape(payload=payload, segment=seg5, gens=gens5)

    outcomes = build_breach_outcomes(
        payload=payload, segment=seg4, wall_flow_csv=wall_flow_csv, liquidity_rows=liq4
    )

    oracle = oracle_audit(
        gens_ep4=gens4_dicts,
        gens_ep5=gens5_fixed,
        liquidity_rows=liq4,
        proxies=proxies4,
        breach_iso=ASK_WALL_BREACH,
    )

    verdict = _decide_verdict(ep4_struct, liq4, proxies4)
    if not oracle.get("ok"):
        verdict = VERDICT_INDETERMINATE

    sem = {
        "schema": "wall_migration_structure_v1",
        "band_contract": BAND_CONTRACT,
        "handoff_wall_generation_id": handoff.wall_generation_id,
        "expected_original_gen": ORIGINAL_WALL_GENERATION_ID,
        "epoch4_segment": segment_to_dict(seg4),
        "epoch5_segment": segment_to_dict(seg5),
        "n_gens_ep4": len(gens4_dicts),
        "n_gens_ep5": len(gens5_fixed),
        "n_pre_existing_above": ep4_struct.get("n_pre_existing_above"),
        "n_post_breach_new": ep4_struct.get("n_post_breach_new"),
        "centroid_moved_higher": ep4_struct.get("ask_centroid_moved_higher"),
        "stacked_defense": ep4_struct.get("stacked_defense_visible_within_epoch4"),
        "original_fill_vs_pull": ep4_struct.get("original_wall_fill_vs_pull"),
        "n_migration_proxies_ep4": len(proxies4),
        "n_liq_rows_ep4": len(liq4),
        "breach_outcomes_ok": sum(1 for o in outcomes if o.get("status") == "OK"),
        "breach_outcomes_censored": sum(1 for o in outcomes if o.get("status") == "CENSORED_BY_EPOCH_BOUNDARY" or o.get("status") == "CENSORED"),
        "oracle_ok": oracle.get("ok"),
        "oracle_fp": oracle.get("fp"),
        "oracle_fn": oracle.get("fn"),
        "oracle_mass_error": oracle.get("mass_error"),
        "oracle_look_ahead": oracle.get("look_ahead"),
        "wall_migration_status": WALL_MIGRATION_STATUS,
        "WALL_STATE": WALL_STATE,
        "time_basis": TIME_BASIS,
        "verdict": verdict,
    }
    sem_hash = sha256_hex(sem)

    atomic_write_json(out_dir / "band_contract.json", BAND_CONTRACT)
    atomic_write_json(out_dir / "episode1_handoff.json", handoff.to_dict())
    atomic_write_json(out_dir / "wall_generations_epoch4.json", {"generations": gens4_dicts})
    atomic_write_json(out_dir / "wall_generations_epoch5.json", {"generations": gens5_fixed})
    atomic_write_json(out_dir / "migration_proxies_epoch4.json", {"status": WALL_MIGRATION_STATUS, "proxies": proxies4})
    atomic_write_json(out_dir / "migration_proxies_epoch5.json", {"status": WALL_MIGRATION_STATUS, "proxies": proxies5})
    atomic_write_json(out_dir / "ask_liquidity_timeline_epoch4.json", {"rows": liq4})
    atomic_write_json(out_dir / "ask_liquidity_timeline_epoch5.json", {"rows": liq5})
    atomic_write_json(out_dir / "breach_structure_epoch4.json", ep4_struct)
    atomic_write_json(out_dir / "epoch5_independent_landscape.json", ep5_land)
    atomic_write_json(out_dir / "breach_outcomes.json", {"outcomes": outcomes})
    atomic_write_json(out_dir / "oracle_audit.json", oracle)
    atomic_write_json(out_dir / "semantic_fingerprint.json", {**sem, "semantic_hash": sem_hash})

    ok = bool(oracle.get("ok")) and verdict in (VERDICT_STRUCTURE_PROVEN, VERDICT_NOT_OBSERVED)
    result = {
        "ok": ok,
        "verdict": verdict,
        "run_key": run_key,
        "semantic_hash": sem_hash,
        "oracle": {k: oracle.get(k) for k in ("ok", "fp", "fn", "mass_error", "look_ahead")},
        "n_gens_ep4": len(gens4_dicts),
        "n_gens_ep5": len(gens5_fixed),
        "n_pre_existing_above": ep4_struct.get("n_pre_existing_above"),
        "n_post_breach_new": ep4_struct.get("n_post_breach_new"),
        "ask_centroid_moved_higher": ep4_struct.get("ask_centroid_moved_higher"),
        "stacked_defense_visible_within_epoch4": ep4_struct.get("stacked_defense_visible_within_epoch4"),
        "original_wall_fill_vs_pull": ep4_struct.get("original_wall_fill_vs_pull"),
        "wall_migration_status": WALL_MIGRATION_STATUS,
        "time_basis": TIME_BASIS,
        "band_contract": BAND_CONTRACT,
    }
    atomic_write_json(out_dir / "run_manifest.json", result)
    atomic_write_text(out_dir / "STATUS", verdict + "\n")
    return result
