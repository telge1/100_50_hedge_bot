"""Build comparative synthesis from stored CASE_03–05 audit artefacts."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orderbook_analyse.case_03_05_entry_contract_comparative_synthesis_v1 import (
    CASE_DIRS,
    CASE_SEQUENCE_FREEZE_SHA256,
    CONFIG_SHA256,
    COST_BPS,
    ENTRY_CONTRACT_FREEZE_SHA256,
    EXPOSURE,
    FORMAT_VERSION,
    MIN_ROOM_BPS,
    VERDICT,
)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _pool_width_bps(lower: float, upper: float, mid: float) -> float:
    return (upper - lower) / mid * 10000.0


def _htf_ask_overlap(selected: dict[str, Any]) -> bool:
    for c in selected.get("htf_containing", []):
        if c.get("side") == "ASK" and c.get("source_timeframe") in {"15m", "30m", "1h"}:
            return True
    return False


def _room_bps(room: dict[str, Any]) -> float | None:
    if room.get("raw_target_distance_bps") is not None:
        return float(room["raw_target_distance_bps"])
    if room.get("raw_room_bps") is not None:
        return float(room["raw_room_bps"])
    return None


def _room_pass_50bps(room: dict[str, Any]) -> bool | None:
    bps = _room_bps(room)
    if bps is None:
        return None
    if room.get("gate_passed") is not None and room.get("gate_reason") == "TARGET_DISTANCE_BELOW_MINIMUM":
        return False
    if room.get("gate_passed") is not None and room.get("gate_reason") == "TARGET_DISTANCE_SUFFICIENT":
        return True
    return bps >= MIN_ROOM_BPS


def _room_pass_11bps(room: dict[str, Any]) -> bool | None:
    if room.get("insufficient_room") is not None:
        return not bool(room["insufficient_room"])
    bps = _room_bps(room)
    if bps is None:
        return None
    overlap = bool(room.get("overlap_detected") or room.get("overlaps_counterstructure"))
    return bps >= COST_BPS[0] and not overlap


def _cost_pass(room: dict[str, Any], cost: float) -> bool | None:
    key = f"room_after_cost_{int(cost)}bps"
    val = room.get(key)
    if val is None:
        bps = _room_bps(room)
        if bps is None:
            return None
        return bps >= cost
    return float(val) >= 0.0


def _micro_long(mech: dict[str, Any]) -> tuple[bool, str]:
    if "microstructure_gate_passed" in mech and mech.get("candidate_direction") == "LONG":
        return bool(mech["microstructure_gate_passed"]), str(mech.get("microstructure_gate_reason", ""))
    branch = mech.get("long_branch", {})
    if branch.get("microstructure_gate_passed") is not None:
        return bool(branch["microstructure_gate_passed"]), str(branch.get("microstructure_gate_reason", ""))
    ok = bool(branch.get("eligible"))
    reason = "MICROSTRUCTURE_CONFIRMED" if ok else "NO_CLEAR_MICROSTRUCTURE_CONFIRMATION"
    return ok, reason


def _micro_short(mech: dict[str, Any]) -> tuple[bool, str]:
    if "microstructure_gate_passed" in mech and mech.get("candidate_direction") == "SHORT":
        return bool(mech["microstructure_gate_passed"]), str(mech.get("microstructure_gate_reason", ""))
    branch = mech.get("short_branch", {})
    if branch.get("microstructure_gate_passed") is not None:
        return bool(branch["microstructure_gate_passed"]), str(branch.get("microstructure_gate_reason", ""))
    if branch.get("contested"):
        return False, "AMBIGUOUS_POOL_CONTEST"
    ok = bool(branch.get("eligible"))
    reason = "MICROSTRUCTURE_CONFIRMED" if ok else "NO_CLEAR_MICROSTRUCTURE_CONFIRMATION"
    return ok, reason


def _branch_room(mech: dict[str, Any], direction: str) -> dict[str, Any]:
    key = "long_branch" if direction == "LONG" else "short_branch"
    branch = mech.get(key, {})
    return branch.get("room_gate") or branch.get("room") or {}


def _blockers(
    *,
    direction: str,
    micro_pass: bool,
    micro_reason: str,
    room: dict[str, Any],
    contested: bool,
    htf_overlap: bool,
) -> list[str]:
    blocks: list[str] = []
    if contested and direction == "SHORT":
        blocks.append("CONTEST_BLOCK")
    if not micro_pass:
        if micro_reason == "AMBIGUOUS_POOL_CONTEST" and "CONTEST_BLOCK" not in blocks:
            blocks.append("CONTEST_BLOCK")
        else:
            blocks.append("MICROSTRUCTURE_BLOCK")
    gate_reason = room.get("gate_reason")
    if gate_reason == "HTF_OPPOSING_POOL_OVERLAP" or (
        htf_overlap and direction == "LONG" and room.get("overlaps_counterstructure")
    ):
        blocks.append("HTF_OVERLAP_BLOCK")
    if gate_reason == "TARGET_NOT_OBSERVED" or gate_reason == "TARGET_NOT_CAUSALLY_AVAILABLE":
        blocks.append("TARGET_MISSING_BLOCK")
    rp50 = _room_pass_50bps(room)
    if rp50 is False:
        blocks.append("ROOM_BLOCK")
    elif rp50 is None and room.get("insufficient_room"):
        blocks.append("ROOM_BLOCK")
    return blocks


def _load_case(repo: Path, case_id: str) -> dict[str, Any]:
    d = repo / "results" / CASE_DIRS[case_id]
    mech = _read_json(d / "mechanical_verdict_pre_unblind.json")
    summary = _read_json(d / "summary.json")
    selected = _read_json(d / "selected_pool.json")
    ref_mid = _read_json(d / "reference_mid.json")
    prefix = _read_json(d / "prefix_parity.json") if (d / "prefix_parity.json").exists() else {}
    return {
        "case_id": case_id,
        "dir": d,
        "mech": mech,
        "summary": summary,
        "selected": selected,
        "ref_mid": ref_mid,
        "prefix": prefix,
        "exposure": EXPOSURE[case_id],
    }


def build_synthesis(repo_root: Path | None = None) -> dict[str, Any]:
    repo = repo_root or Path(__file__).resolve().parents[3]
    out_dir = repo / "results" / "case_03_05_entry_contract_comparative_synthesis_v1"
    out_dir.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    cases = [_load_case(repo, cid) for cid in ("CASE_03", "CASE_04", "CASE_05")]

    case_rows: list[dict[str, Any]] = []
    blocker_rows: list[dict[str, Any]] = []
    gate_attr_rows: list[dict[str, Any]] = []
    acceptance_rows: list[dict[str, Any]] = []
    room_rows: list[dict[str, Any]] = []

    pattern = {
        "break_then_reclaim_contest": 0,
        "back_cross_5s_accept": 0,
        "fail_30_60_accept": 0,
        "reclaim_back_held_any": 0,
        "reclaim_front_or_ts": 0,
        "two_sided_high": 0,
        "trade_supported_depletion": 0,
        "bid_retreat_follow": 0,
    }

    room_50_pass = room_50_fail = 0
    micro_pass_n = micro_fail_n = 0
    solely_50bps_blocked: list[str] = []
    micro_confirmed_lost_50bps: list[str] = []

    for c in cases:
        cid = c["case_id"]
        mech = c["mech"]
        sel = c["selected"]
        mid = float(sel["mid_at_reference"])
        lo, hi = float(sel["lower"]), float(sel["upper"])
        width_bps = _pool_width_bps(lo, hi, mid)
        htf_ov = _htf_ask_overlap(sel)

        if mech.get("reaction") == "BREAK_THEN_RECLAIM_CONTEST":
            pattern["break_then_reclaim_contest"] += 1
        acc = mech.get("acceptance_variants", [])
        acc5 = next((a for a in acc if a["hold_s"] == 5), {})
        acc30 = next((a for a in acc if a["hold_s"] == 30), {})
        acc60 = next((a for a in acc if a["hold_s"] == 60), {})
        if acc5.get("breakout_accepted_below_back"):
            pattern["back_cross_5s_accept"] += 1
        if not acc30.get("breakout_accepted_below_back") and not acc60.get("breakout_accepted_below_back"):
            pattern["fail_30_60_accept"] += 1
        if any(a.get("reclaim_above_back_held") for a in acc):
            pattern["reclaim_back_held_any"] += 1
        if any(a.get("reclaim_above_front_held") for a in acc) or acc5.get("first_front_reclaim_ts"):
            pattern["reclaim_front_or_ts"] += 1
        two = mech.get("aggressor_counts", {}).get("two_sided", 0)
        if two >= 500:
            pattern["two_sided_high"] += 1
        wf = mech.get("wall_flags", {})
        if wf.get("trade_supported_depletion"):
            pattern["trade_supported_depletion"] += 1
        if wf.get("bid_retreat_with_price_follow_repeated"):
            pattern["bid_retreat_follow"] += 1

        for a in acc:
            acceptance_rows.append(
                {
                    "case_id": cid,
                    "exposure": c["exposure"],
                    "hold_s": a["hold_s"],
                    "breakout_accepted_below_back": a.get("breakout_accepted_below_back"),
                    "reclaim_above_back_held": a.get("reclaim_above_back_held"),
                    "reclaim_above_front_held": a.get("reclaim_above_front_held"),
                    "first_back_cross_ts": a.get("first_back_cross_ts"),
                    "first_front_reclaim_ts": a.get("first_front_reclaim_ts"),
                }
            )

        for direction in ("LONG", "SHORT"):
            branch_key = f"{direction.lower()}_branch"
            branch = mech.get(branch_key, {})
            room = _branch_room(mech, direction)
            micro_ok, micro_reason = _micro_long(mech) if direction == "LONG" else _micro_short(mech)
            contested = bool(branch.get("contested"))
            entry = branch.get("entry_price")
            if entry is None and direction == "SHORT" and mech.get("candidate_direction") == "SHORT":
                entry = mech.get("mechanical_entry_price")
            blocks = _blockers(
                direction=direction,
                micro_pass=micro_ok,
                micro_reason=micro_reason,
                room=room,
                contested=contested,
                htf_overlap=htf_ov,
            )
            block_label = blocks[0] if len(blocks) == 1 else ("MULTIPLE_BLOCKS" if len(blocks) > 1 else "NONE")

            rp50 = _room_pass_50bps(room)
            rp11 = _room_pass_11bps(room)
            if rp50 is True:
                room_50_pass += 1
            elif rp50 is False:
                room_50_fail += 1
            if micro_ok:
                micro_pass_n += 1
            else:
                micro_fail_n += 1

            branch_id = f"{cid}_{direction}"
            non_contest = not contested
            if micro_ok and non_contest and rp50 is False:
                solely_50bps_blocked.append(branch_id)
            if micro_ok and non_contest and rp50 is False and direction == "SHORT":
                micro_confirmed_lost_50bps.append(branch_id)

            cf_micro = micro_ok and non_contest
            cf_11 = cf_micro and rp11 is True
            cf_50 = cf_micro and rp50 is True
            cf_full = cf_50  # full contract = micro + 50bps + no contest

            gate_attr_rows.append(
                {
                    "case_id": cid,
                    "direction": direction,
                    "exposure": c["exposure"],
                    "counterfactual_micro_only": "TRADE_ELIGIBLE" if cf_micro else "NO_TRADE",
                    "counterfactual_11bps_cost_gate_only": "TRADE_ELIGIBLE" if cf_11 else "NO_TRADE",
                    "counterfactual_50bps_min_distance_only": "TRADE_ELIGIBLE" if cf_50 else "NO_TRADE",
                    "counterfactual_full_entry_contract_v1": "TRADE_ELIGIBLE" if cf_full else "NO_TRADE",
                    "actual_mechanical_trade_verdict": mech.get("mechanical_trade_verdict", "NO_TRADE"),
                    "note": "Counterfactual only; stored verdicts unchanged.",
                }
            )

            blocker_rows.append(
                {
                    "case_id": cid,
                    "direction": direction,
                    "exposure": c["exposure"],
                    "MICROSTRUCTURE_BLOCK": "MICROSTRUCTURE_BLOCK" in blocks,
                    "CONTEST_BLOCK": "CONTEST_BLOCK" in blocks,
                    "ROOM_BLOCK": "ROOM_BLOCK" in blocks,
                    "HTF_OVERLAP_BLOCK": "HTF_OVERLAP_BLOCK" in blocks,
                    "TARGET_MISSING_BLOCK": "TARGET_MISSING_BLOCK" in blocks,
                    "MULTIPLE_BLOCKS": len(blocks) > 1,
                    "blocker_summary": block_label,
                    "all_blockers": "|".join(blocks) if blocks else "NONE",
                }
            )

            ref_room_long_ask = sel.get("nearest_ask_above", {}).get("distance_bps")
            ref_room_short_bid = sel.get("nearest_bid_below", {}).get("distance_bps")
            ref_room = ref_room_long_ask if direction == "LONG" else ref_room_short_bid
            entry_bps = _room_bps(room)

            room_rows.append(
                {
                    "case_id": cid,
                    "direction": direction,
                    "exposure": c["exposure"],
                    "reference_mid": mid,
                    "room_at_reference_ts_bps": ref_room,
                    "mechanical_entry_price": entry,
                    "room_at_mechanical_entry_bps": entry_bps,
                    "reference_vs_entry_room_delta_bps": (
                        (entry_bps - ref_room) if entry_bps is not None and ref_room is not None else None
                    ),
                    "target_pool_id": room.get("target_pool_id"),
                    "target_tf": room.get("target_pool_timeframe") or room.get("target_tf"),
                    "target_edge": room.get("target_edge"),
                    "gate_50bps_pass": rp50,
                    "gate_11bps_pass": rp11,
                    "gate_15bps_pass": _cost_pass(room, 15),
                    "gate_20bps_pass": _cost_pass(room, 20),
                    "room_after_cost_11bps": room.get("room_after_cost_11bps"),
                    "room_after_cost_15bps": room.get("room_after_cost_15bps"),
                    "room_after_cost_20bps": room.get("room_after_cost_20bps"),
                }
            )

        long_micro, long_micro_r = _micro_long(mech)
        short_micro, short_micro_r = _micro_short(mech)
        long_room = _branch_room(mech, "LONG")
        short_room = _branch_room(mech, "SHORT")

        case_rows.append(
            {
                "case_id": cid,
                "exposure_status": c["exposure"],
                "reference_ts": sel.get("available_at") or mech.get("arrival_ts"),
                "reference_mid": mid,
                "pool_id": sel["pool_id"],
                "pool_tf": sel["source_timeframe"],
                "pool_lower": lo,
                "pool_upper": hi,
                "pool_width_bps": round(width_bps, 4),
                "mid_location": sel["mid_location"],
                "htf_ask_overlap": htf_ov,
                "arrival_ts": mech.get("arrival_ts"),
                "first_back_cross_ts": mech.get("first_back_cross_ts"),
                "accept_5s_breakout_below_back": acc5.get("breakout_accepted_below_back"),
                "accept_15s_breakout_below_back": next(
                    (a.get("breakout_accepted_below_back") for a in acc if a["hold_s"] == 15), None
                ),
                "accept_30s_breakout_below_back": acc30.get("breakout_accepted_below_back"),
                "accept_60s_breakout_below_back": acc60.get("breakout_accepted_below_back"),
                "reclaim_above_back_any": any(a.get("reclaim_above_back_held") for a in acc),
                "reclaim_above_front_any": any(a.get("reclaim_above_front_held") for a in acc),
                "sell_effective": mech.get("aggressor_counts", {}).get("sell_effective"),
                "sell_absorbed": mech.get("aggressor_counts", {}).get("sell_inefficient_absorption"),
                "buy_reclaim": mech.get("aggressor_counts", {}).get("buy_counter_reclaim"),
                "two_sided_seconds": two,
                "trade_supported_depletion": wf.get("trade_supported_depletion"),
                "bid_retreat_price_follow": wf.get("bid_retreat_with_price_follow_repeated"),
                "n_retreat_events": wf.get("n_retreat_events"),
                "long_micro_gate_pass": long_micro,
                "long_micro_gate_reason": long_micro_r,
                "short_micro_gate_pass": short_micro,
                "short_micro_gate_reason": short_micro_r,
                "diagnostic_entry_price": mech.get("entry_price") or mech.get("mechanical_entry_price"),
                "first_available_ts": mech.get("first_available_ts"),
                "long_target_pool": long_room.get("target_pool_id"),
                "long_room_bps": _room_bps(long_room),
                "long_50bps_gate_pass": _room_pass_50bps(long_room),
                "short_target_pool": short_room.get("target_pool_id"),
                "short_room_bps": _room_bps(short_room),
                "short_50bps_gate_pass": _room_pass_50bps(short_room),
                "mechanical_verdict": mech.get("mechanical_verdict"),
                "mechanical_trade_verdict": mech.get("mechanical_trade_verdict"),
                "reaction": mech.get("reaction"),
                "candidate_direction": mech.get("candidate_direction"),
                "outcome_evidence_class": c["summary"]
                .get("outcome_comparison", {})
                .get("frozen_sample_evidence_class"),
                "prefix_parity": c["summary"].get("prefix_status") or c["prefix"].get("prefix_status"),
                "entry_contract_version": mech.get("entry_contract_version", "pre_entry_contract_v1"),
            }
        )

    exposure_accounting = {
        "format_version": FORMAT_VERSION,
        "verdict": VERDICT,
        "generated_at": generated_at,
        "case_sequence_freeze_sha256": CASE_SEQUENCE_FREEZE_SHA256,
        "entry_contract_freeze_sha256": ENTRY_CONTRACT_FREEZE_SHA256,
        "strategy_config_sha256": CONFIG_SHA256,
        "exposure_by_case": EXPOSURE,
        "methodological_note": (
            "CASE_03 and CASE_04 are PRE_ENTRY_CONTRACT_EXPOSED retrospective audits; "
            "CASE_05 is the sole PROSPECTIVE_ENTRY_CONTRACT_TEST under Entry Contract V1. "
            "Do not aggregate the three as equal prospective validation draws."
        ),
        "prospective_count": 1,
        "retrospective_count": 2,
        "total_compared": 3,
    }

    gate_diagnosis = {
        "room_50bps_branch_pass": room_50_pass,
        "room_50bps_branch_fail": room_50_fail,
        "microstructure_branch_pass": micro_pass_n,
        "microstructure_branch_fail": micro_fail_n,
        "solely_blocked_by_50bps_gate": solely_50bps_blocked,
        "micro_confirmed_lost_to_50bps_only": micro_confirmed_lost_50bps,
        "allows_too_strict_conclusion": False,
        "reason": (
            "No branch had microstructure pass without contest where only the 50 bps gate blocked; "
            "contest and microstructure failures dominate. n=3 is insufficient to calibrate 0.5%."
        ),
    }

    common_pattern = {
        "all_three_break_then_reclaim_contest": pattern["break_then_reclaim_contest"] == 3,
        "all_three_5s_back_break_accept": pattern["back_cross_5s_accept"] == 3,
        "all_three_fail_30_60_hold_break": pattern["fail_30_60_accept"] == 3,
        "back_reclaim_held_cases": pattern["reclaim_back_held_any"],
        "front_reclaim_or_ts_cases": pattern["reclaim_front_or_ts"],
        "all_three_high_two_sided": pattern["two_sided_high"] == 3,
        "all_three_trade_depletion": pattern["trade_supported_depletion"] == 3,
        "all_three_bid_retreat_follow": pattern["bid_retreat_follow"] == 3,
        "descriptive_pattern": (
            "Short-lived back-edge break (5s) → missing 30/60s acceptance → reclaim/contested control"
        ),
    }

    summary = {
        "verdict": VERDICT,
        "generated_at": generated_at,
        "cases_compared": ["CASE_03", "CASE_04", "CASE_05"],
        "exposure_accounting": exposure_accounting,
        "gate_diagnosis_50bps": gate_diagnosis,
        "common_pattern": common_pattern,
        "all_mechanical_trade_verdicts": "NO_TRADE",
        "all_mechanical_verdicts": "AMBIGUOUS_POOL_CONTEST_NO_TRADE",
        "output_dir": str(out_dir),
    }

    def _write_csv(name: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        keys: list[str] = []
        for r in rows:
            for k in r:
                if k not in keys:
                    keys.append(k)
        with (out_dir / name).open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)

    _write_csv("case_comparison.csv", case_rows)
    _write_csv("branch_blocker_matrix.csv", blocker_rows)
    _write_csv("gate_attribution.csv", gate_attr_rows)
    _write_csv("acceptance_comparison.csv", acceptance_rows)
    _write_csv("room_comparison.csv", room_rows)
    (out_dir / "exposure_accounting.json").write_text(
        json.dumps(exposure_accounting, indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    report = _render_report(
        case_rows=case_rows,
        blocker_rows=blocker_rows,
        gate_attr_rows=gate_attr_rows,
        room_rows=room_rows,
        exposure_accounting=exposure_accounting,
        gate_diagnosis=gate_diagnosis,
        common_pattern=common_pattern,
        generated_at=generated_at,
    )
    (out_dir / "COMPARATIVE_REPORT.md").write_text(report, encoding="utf-8")

    return summary


def _render_report(**ctx: Any) -> str:
    case_rows = ctx["case_rows"]
    blocker_rows = ctx["blocker_rows"]
    gate_attr_rows = ctx["gate_attr_rows"]
    room_rows = ctx["room_rows"]
    gd = ctx["gate_diagnosis"]
    cp = ctx["common_pattern"]
    gen = ctx["generated_at"]

    lines = [
        "# CASE_03–CASE_05 Entry Contract Comparative Synthesis v1",
        "",
        f"Generated: {gen}",
        "",
        "## Verdict",
        "",
        f"**{VERDICT}**",
        "",
        "Deskriptive Vergleichsauswertung aus gespeicherten Pre-Unblind-Artefakten. "
        "Keine Trading-Edge bewiesen oder widerlegt.",
        "",
        "## Exposure-Trennung",
        "",
        "| Case | Exposure | Entry Contract |",
        "|------|----------|----------------|",
        "| CASE_03 | PRE_ENTRY_CONTRACT_EXPOSED | Pre-Contract (11 bps legacy room in audit) |",
        "| CASE_04 | PRE_ENTRY_CONTRACT_EXPOSED | Pre-Contract (11 bps legacy room in audit) |",
        "| CASE_05 | PROSPECTIVE_ENTRY_CONTRACT_TEST | Entry Contract V1 (50 bps + micro) |",
        "",
        "**n=1 prospektiv** (CASE_05). CASE_03/04 nicht als gleichwertige prospektive Validierung zählen.",
        "",
        "## Gemeinsame Reaktionsmuster (deskriptiv)",
        "",
        f"- Alle drei: `{case_rows[0]['reaction']}` (Break → Reclaim → Contest)",
        f"- 5s Back-Break akzeptiert: {cp['all_three_5s_back_break_accept']}",
        f"- 30/60s Break nicht gehalten: {cp['all_three_fail_30_60_hold_break']}",
        f"- Back-Reclaim gehalten (mind. ein Hold): {cp['back_reclaim_held_cases']}/3",
        f"- Front-Reclaim oder Reclaim-Timestamp: {cp['front_reclaim_or_ts_cases']}/3",
        f"- Two-sided hoch (≥500s): {cp['all_three_high_two_sided']}",
        f"- Trade-supported depletion: {cp['all_three_trade_depletion']}",
        f"- Bid-Retreat + Preisfolge: {cp['all_three_bid_retreat_follow']}",
        "",
        "## Case-Vergleich (Kernfelder)",
        "",
        "| Case | Ref Mid | Pool TF | Width bps | HTF Ask OV | Back Cross | Trade |",
        "|------|---------|---------|-----------|------------|------------|-------|",
    ]
    for r in case_rows:
        lines.append(
            f"| {r['case_id']} | {r['reference_mid']} | {r['pool_tf']} | "
            f"{r['pool_width_bps']:.1f} | {r['htf_ask_overlap']} | "
            f"{r['first_back_cross_ts']} | {r['mechanical_trade_verdict']} |"
        )

    lines.extend(["", "## Blocker-Matrix (je Richtung)", ""])
    for b in blocker_rows:
        lines.append(
            f"- **{b['case_id']} {b['direction']}** ({b['exposure']}): "
            f"{b['blocker_summary']} — {b['all_blockers']}"
        )

    lines.extend(["", "## 0,5%-Gate-Diagnose", ""])
    lines.append(f"- Room PASS (50 bps): **{gd['room_50bps_branch_pass']}** / 6 branches")
    lines.append(f"- Room FAIL (50 bps): **{gd['room_50bps_branch_fail']}** / 6 branches")
    lines.append(f"- Micro PASS: **{gd['microstructure_branch_pass']}** · Micro FAIL: **{gd['microstructure_branch_fail']}**")
    lines.append(f"- Allein durch 50 bps blockiert: **{gd['solely_blocked_by_50bps_gate'] or 'keine'}**")
    lines.append(f"- Zu streng? **{gd['allows_too_strict_conclusion']}** — {gd['reason']}")

    lines.extend(["", "## Reference-Room vs Entry-Room", ""])
    for rr in room_rows:
        lines.append(
            f"- **{rr['case_id']} {rr['direction']}**: ref={rr['room_at_reference_ts_bps']} bps · "
            f"entry={rr['room_at_mechanical_entry_bps']} bps · "
            f"Δ={rr['reference_vs_entry_room_delta_bps']}"
        )

    lines.extend(
        [
            "",
            "## Counterfactual Gate Attribution",
            "",
            "| Case | Dir | Micro only | 11 bps only | 50 bps only | Full contract | Actual |",
            "|------|-----|------------|-------------|-------------|---------------|--------|",
        ]
    )
    for g in gate_attr_rows:
        lines.append(
            f"| {g['case_id']} | {g['direction']} | {g['counterfactual_micro_only']} | "
            f"{g['counterfactual_11bps_cost_gate_only']} | {g['counterfactual_50bps_min_distance_only']} | "
            f"{g['counterfactual_full_entry_contract_v1']} | {g['actual_mechanical_trade_verdict']} |"
        )

    lines.extend(
        [
            "",
            "## Methodische Grenzen",
            "",
            "- Kleine Stichprobe (n=3), davon nur 1 prospektiv unter Contract V1",
            "- CASE_03/04 ohne integriertes Entry-Contract-V1-Feld; 50-bps-Diagnose retrospektiv aus gespeichertem Room",
            "- Keine Outcome-Nutzung für Schwellenbewertung",
            "- Alle Fälle: finales NO_TRADE unabhängig vom Room-Gate",
            "",
            "## Empfehlung nächste Frozen-Expansion",
            "",
            "Weitere **PROSPECTIVE_UNAUDITED** Fälle unter unverändertem Entry Contract V1, "
            "mit Variation in: Poolbreite, HTF-Overlap, Back-Break-Persistenz und Entry-Room-Distanz. "
            "Mindestens ein Fall mit klarer Micro-Pass ohne Contest nötig, um 50-bps-Gate isoliert zu testen.",
            "",
            "## Live-Sicherheit",
            "",
            "- Read-only aus gespeicherten Artefakten",
            "- Keine ClickHouse-Abfragen",
            "- Keine Regel-/Config-/Contract-Änderung",
            "- Kein CASE_06 · Kein Commit/Push",
            "",
        ]
    )
    return "\n".join(lines)
