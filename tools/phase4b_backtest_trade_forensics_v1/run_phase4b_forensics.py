#!/usr/bin/env python3
"""Phase 4B — read-only forensics on Phase 4A simulated trades."""

from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
TOOLS = Path(__file__).resolve().parent
PHASE4A = ROOT / "results" / "phase4a_single_position_backtest_pilot_v1"
PHASE2A = ROOT / "results" / "phase2a_5m_pd1_depth2_short_candidates_v1"
RESULTS = ROOT / "results" / "phase4b_backtest_trade_forensics_v1"
REPORT = ROOT / "reports" / "DOGEUSDT_PHASE4B_BACKTEST_TRADE_FORENSICS_V1.md"

sys.path.insert(0, str(ROOT / "tools" / "phase4a_single_position_backtest_pilot_v1"))
from backtest_pilot import (  # noqa: E402
    lower_targets,
    select_long_tp,
    select_short_tp,
    upper_targets,
)

MIN_TP = 0.5
SL_UP = 0.002
SL_LO = 0.002


def read_csv(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def load_profile(candidate_id: str) -> dict[str, Any]:
    p = PHASE2A / "candidate_landscape_profiles" / f"{candidate_id}.json"
    if p.is_file():
        return json.loads(p.read_text(encoding="utf-8"))
    return {}


def intervals_for_trade(trade: dict) -> list[dict]:
    cid = trade["candidate_id"]
    if cid.startswith("L3B_"):
        conf = read_csv(
            ROOT
            / "results/phase3b_two_pool_market_structure_detector_v1/second_lower_confirmation_events.csv"
        )
        m = re.match(r"L3B_E(\d+)_", cid)
        ep_id = str(int(m.group(1))) if m else ""
        row = next((r for r in conf if r.get("episode_id") == ep_id), None)
        if not row:
            for r in conf:
                if r["confirmation_close_utc"].replace(":", "").replace("-", "") in cid:
                    row = r
                    break
        if row:
            tp_rows = read_csv(
                ROOT
                / "results/phase3b_two_pool_market_structure_detector_v1/upper_tp_candidates_at_confirmation.csv"
            )
            tp1 = next(
                (
                    t
                    for t in tp_rows
                    if t.get("episode_id") == row["episode_id"]
                    and t.get("tp_rank") == "1"
                    and t["confirmation_close_utc"] == row["confirmation_close_utc"]
                ),
                None,
            )
            lowers = [
                {
                    "side": "lower",
                    "bottom_price": float(row["second_lower_bottom"]),
                    "top_price": float(row["second_lower_top"]),
                    "depth": 2,
                    "origins": json.loads(row.get("second_origins") or "[]"),
                }
            ]
            uppers = []
            if tp1:
                uppers = [
                    {
                        "side": "upper",
                        "bottom_price": float(tp1["bottom_price"]),
                        "top_price": float(tp1["top_price"]),
                        "depth": int(tp1.get("depth") or 2),
                        "origins": json.loads(tp1.get("origins") or "[]"),
                    }
                ]
            return lowers + uppers
        return []
    prof = load_profile(cid)
    return (prof.get("candidate") or {}).get("landscape_intervals") or []


def origins_for_interval(iv: dict) -> list[str]:
    return iv.get("origins") or []


def audit_short_tp(entry: float, intervals: list[dict], selected_top: float) -> dict[str, Any]:
    all_lowers = lower_targets(entry, intervals, MIN_TP)
    eligible = [x for x in all_lowers if x["meets_min"]]
    picked, code = select_short_tp(entry, intervals, MIN_TP)
    max_depth = max((e.get("depth") or 0) for e in eligible) if eligible else 0
    picked_depth = picked.get("depth") if picked else None
    denser = [e for e in eligible if (e.get("depth") or 0) > (picked_depth or 0)]
    higher_top_alternate = [
        e for e in eligible if e["top"] > selected_top + 1e-9 and e["meets_min"]
    ]
    rule_4a = "nearest_lower_cluster_top_by_price_sort_desc"
    user_dense_rule = "large_dense_lower_cluster_not_nearest_pool_top"
    validation = "VERIFIED_4A_CODE_ONLY"
    if denser or higher_top_alternate:
        validation = "TP_SELECTION_NOT_VALIDATED"
    elif not picked:
        validation = "TP_SELECTION_NOT_VALIDATED"
    return {
        "phase4a_rule": rule_4a,
        "user_rule_gap": user_dense_rule,
        "eligible_cluster_count": len(eligible),
        "selected_cluster_bottom": picked["bottom"] if picked else None,
        "selected_cluster_top": picked["top"] if picked else selected_top,
        "selected_depth": picked_depth,
        "max_depth_among_eligible": max_depth,
        "higher_depth_eligible_count": len(denser),
        "closer_higher_top_alternates": len(higher_top_alternate),
        "min_distance_entry_to_tp_top_pct": picked["distance_entry_to_cluster_top_pct"]
        if picked
        else None,
        "meets_0p5pct_min": bool(picked and picked["meets_min"]),
        "tp_validation": validation,
        "eligible_clusters_json": json.dumps(eligible[:8]),
    }


def audit_long_tp(entry: float, intervals: list[dict], selected_bot: float, tp_method: str) -> dict[str, Any]:
    all_uppers = upper_targets(entry, intervals, MIN_TP)
    eligible = [x for x in all_uppers if x["meets_min"]]
    picked, code = select_long_tp(entry, intervals, MIN_TP)
    max_depth = max((e.get("depth") or 0) for e in eligible) if eligible else 0
    picked_depth = picked.get("depth") if picked else None
    denser = [e for e in eligible if (e.get("depth") or 0) > (picked_depth or 0)]
    validation = "VERIFIED_4A_CODE_ONLY"
    if tp_method == "PHASE3B_NEAREST_UPPER_RANK1":
        validation = "TP_SELECTION_NOT_VALIDATED"
    elif denser:
        validation = "TP_SELECTION_NOT_VALIDATED"
    elif not picked:
        validation = "TP_SELECTION_NOT_VALIDATED"
    return {
        "phase4a_rule": "nearest_upper_cluster_bottom_by_price_sort_asc"
        if tp_method != "PHASE3B_NEAREST_UPPER_RANK1"
        else "phase3b_rank1_upper_from_confirmation_csv",
        "eligible_upper_count": len(eligible),
        "selected_cluster_bottom": selected_bot,
        "selected_cluster_top": picked["top"] if picked else None,
        "selected_depth": picked_depth,
        "max_depth_among_eligible": max_depth,
        "deeper_clusters_above_selected": len(denser),
        "min_distance_entry_to_tp_bottom_pct": picked["distance_entry_to_cluster_bottom_pct"]
        if picked
        else None,
        "tp_validation": validation,
        "eligible_uppers_json": json.dumps(eligible[:8]),
    }


def build_all_trade_forensics(trades: list[dict]) -> list[dict]:
    ages = {r["candidate_id"]: r for r in read_csv(ROOT / "results/phase2d_5bar_pool_age_comparison_v1/gate_comparison_all_20.csv")}
    rows = []
    for t in trades:
        entry = float(t["entry_price"])
        intervals = intervals_for_trade(t)
        direction = t["direction"]
        if direction == "SHORT":
            sl_derived = float(t["entry_pool_upper"]) * (1 + SL_UP)
            sl_formula = "entry_pool_upper * 1.002"
            tp_audit = audit_short_tp(entry, intervals, float(t["tp_price"]))
        else:
            sl_derived = float(t["entry_pool_lower"]) * (1 - SL_LO)
            sl_formula = "entry_pool_lower * 0.998"
            tp_audit = audit_long_tp(
                entry,
                intervals,
                float(t["tp_price"]),
                t.get("tp_selection") or "",
            )
        prof = load_profile(t["candidate_id"]) if not t["candidate_id"].startswith("L3B") else {}
        cand = prof.get("candidate") or {}
        origins = cand.get("upper_pool_origins") if direction == "SHORT" else []
        if direction == "LONG" and intervals:
            for iv in intervals:
                if iv.get("side") == "lower":
                    origins = iv.get("origins") or []
        age_row = ages.get(t["candidate_id"], {})
        net = t.get("net_pnl_pct")
        gaps = cand.get("gaps_lower") or []
        large_gaps = [g for g in gaps if g.get("large_gap")]
        rows.append(
            {
                "trade_id": t["trade_id"],
                "scenario": t["scenario"],
                "candidate_id": t["candidate_id"],
                "direction": direction,
                "signal_close_utc": t["signal_close_utc"],
                "entry_open_utc": t["entry_open_utc"],
                "entry_price": entry,
                "entry_pool_lower": t["entry_pool_lower"],
                "entry_pool_upper": t["entry_pool_upper"],
                "origin_ids": json.dumps(origins),
                "pool_age_bars": t.get("pool_age_bars") or age_row.get("continuous_depth2_zone_age_bars"),
                "sl_price": t["sl_price"],
                "sl_derivation": sl_formula,
                "sl_derived_check": sl_derived,
                "sl_formula_ok": abs(float(t["sl_price"]) - sl_derived) < 1e-8,
                "lower_large_gap_count": cand.get("lower_large_gap_count"),
                "nearest_lower_large_gap_json": json.dumps(large_gaps[:2]) if large_gaps else "",
                "tp_cluster_bottom": t["tp_cluster_bottom"],
                "tp_cluster_top": t["tp_cluster_top"],
                "tp_price_used": t["tp_price"],
                "tp_validation": tp_audit["tp_validation"],
                "exit_utc": t.get("exit_utc"),
                "exit_reason": t.get("exit_reason"),
                "net_pnl_pct": net if net not in ("", None) else "",
                "pnl_defined": net not in ("", None),
                "market_state_at_entry": t.get("market_state_at_entry"),
                "tp_selection_method": t.get("tp_selection"),
                **{f"audit_{k}": v for k, v in tp_audit.items() if k != "tp_validation"},
            }
        )
    return rows


def same_pool_audit(trades: list[dict], suppressed: list[dict]) -> list[dict]:
    opp = {r["candidate_id"]: r for r in read_csv(ROOT / "results/phase2g_upper_opportunity_dedup_v1/raw_candidates_with_opportunity_id.csv")}
    id_map = {
        "C009": "C009_20260911T232000Z",
        "C010": "C010_20260911T234000Z",
        "C011": "C011_20260911T235500Z",
        "C013": "C013_20260912T003500Z",
    }
    c009_trade = next(
        (t for t in trades if t["candidate_id"] == id_map["C009"] and "A_short" in t["scenario"]),
        None,
    )
    rows = []
    for key, full_id in id_map.items():
        o = opp.get(full_id, {})
        sup_a = [s for s in suppressed if s["candidate_id"] == full_id and "A_short" in s["scenario"]]
        traded = any(t["candidate_id"] == full_id and "A_short" in t["scenario"] for t in trades)
        classification = "IDENTITY_NOT_VERIFIED"
        if o.get("dedup_reason") == "SAME_OPPORTUNITY_DUPLICATE":
            classification = "SAME_POOL_OPPORTUNITY"
        elif o.get("dedup_reason") == "PRIMARY":
            classification = "NEW_OPPORTUNITY"
        if sup_a and not traded:
            classification = f"SUPPRESSED_POSITION_OPEN;{classification}"
        rows.append(
            {
                "candidate_id": key,
                "opportunity_id": o.get("opportunity_id"),
                "dedup_reason_phase2g": o.get("dedup_reason"),
                "duplicate_of": o.get("duplicate_of_candidate_id"),
                "classification": classification,
                "traded_scenario_a_age_off": any(
                    t["candidate_id"] == full_id and t["scenario"] == "A_short_single_no_3b_age_off"
                    for t in trades
                ),
                "suppressed_scenario_a_age_off": len(sup_a) > 0,
                "c009_trade_exit": c009_trade.get("exit_utc") if c009_trade and key != "C009" else "",
                "c009_trade_entry": c009_trade.get("entry_open_utc") if c009_trade and key != "C009" else "",
                "notes": "C013 traded after C009 position closed (00:25 exit vs 00:40 entry)" if key == "C013" else "",
            }
        )
    return rows


def market_block_audit() -> list[dict]:
    raw_a = [r for r in read_csv(PHASE4A / "raw_signals.csv") if r["scenario"] == "A_short_single_no_3b_age_off" and r["direction"] == "SHORT"]
    raw_b = [r for r in read_csv(PHASE4A / "raw_signals.csv") if r["scenario"] == "B_short_single_3b_block_age_off" and r["direction"] == "SHORT"]
    trades_b = {t["candidate_id"] for t in read_csv(PHASE4A / "simulated_trades.csv") if t["scenario"] == "B_short_single_3b_block_age_off"}
    blocked = {r["candidate_id"]: r for r in read_csv(ROOT / "results/phase3b_two_pool_market_structure_detector_v1/blocked_short_windows.csv")}
    reach = read_csv(ROOT / "results/phase3b_two_pool_market_structure_detector_v1/detected_lower_reach_episodes.csv")
    first_watch = reach[0]["reach_utc"] if reach else ""
    rows = []
    by_id = {r["candidate_id"]: r for r in raw_a}
    for cid, ra in by_id.items():
        rb = next((x for x in raw_b if x["candidate_id"] == cid), {})
        bl = blocked.get(cid, {})
        rows.append(
            {
                "candidate_id": cid,
                "signal_close_utc": ra["signal_close_utc"],
                "eligible_scenario_a": ra.get("eligible"),
                "eligible_scenario_b": rb.get("eligible"),
                "ineligible_reason_b": rb.get("ineligible_reason"),
                "market_state_experimental": ra.get("market_state_experimental"),
                "watch_from_utc_phase3b_list": bl.get("watch_from_utc"),
                "blocked_in_3b_csv": cid in blocked,
                "traded_scenario_b": cid in trades_b,
                "first_global_watch_reversal_approx": first_watch,
                "block_validated": "NOT_VALIDATED_EXPERIMENTAL_3B",
                "manual_short_reference": "C003=MANUALLY_CONFIRMED_SHORT per Signal_definierung (not auto-overridden)",
            }
        )
    return rows


def scenario_reconciliation() -> list[dict]:
    rows = []
    for path_name in [
        "raw_signals.csv",
        "executable_candidates.csv",
        "simulated_trades.csv",
        "suppressed_signals.csv",
        "unresolved_entries.csv",
    ]:
        data = read_csv(PHASE4A / path_name)
        for scenario in sorted({d["scenario"] for d in data}):
            sub = [d for d in data if d["scenario"] == scenario]
            rows.append(
                {
                    "artifact": path_name,
                    "scenario": scenario,
                    "row_count": len(sub),
                    "notes": "unresolved rows are rejected entry attempts, not trades"
                    if path_name == "unresolved_entries.csv"
                    else "",
                }
            )
    c = [r for r in read_csv(PHASE4A / "unresolved_entries.csv") if r["scenario"] == "C_resolved_short_and_exp_long"]
    rows.append(
        {
            "artifact": "scenario_C_reconciliation",
            "scenario": "C_resolved_short_and_exp_long",
            "row_count": len(c),
            "notes": f"14 TP_SELECTION_UNRESOLVED are LONG entry attempts without trade; 11 simulated_trades includes shorts+longs with resolved TP",
        }
    )
    return rows


def verdict(short_audits: list[dict], long_audits: list[dict]) -> str:
    if not short_audits and not long_audits:
        return "BACKTEST_TRADE_FORENSICS_BLOCKED"
    not_val = sum(1 for r in short_audits if r.get("tp_validation") == "TP_SELECTION_NOT_VALIDATED")
    not_val += sum(1 for r in long_audits if r.get("tp_validation") == "TP_SELECTION_NOT_VALIDATED")
    if not_val > 0:
        return "BACKTEST_TRADE_FORENSICS_PARTIAL"
    return "BACKTEST_TRADE_FORENSICS_READY"


def write_reference_review(trades: list[dict], suppressed: list[dict]) -> str:
    c009 = next((t for t in trades if "C009" in t["candidate_id"] and "A_short" in t["scenario"]), None)
    lines = [
        "# Reference trade review — C009 / C010 / C011 / C013",
        "",
    ]
    if c009:
        lines.extend(
            [
                "## C009 (scenario A trade)",
                f"- Entry: `{c009['entry_open_utc']}` @ {c009['entry_price']}",
                f"- SL: {c009['sl_price']} · TP: {c009['tp_price']} (cluster top {c009['tp_cluster_top']})",
                f"- Exit: `{c009['exit_utc']}` **{c009['exit_reason']}** net={c009.get('net_pnl_pct')}%",
                "",
            ]
        )
    for key in ["C010", "C011", "C013"]:
        sup = [s for s in suppressed if key in s["candidate_id"] and "A_short" in s["scenario"]]
        lines.append(f"## {key}")
        if sup:
            lines.append(f"- SUPPRESSED_POSITION_OPEN while `{sup[0]['open_trade_id']}` open")
        t = next((x for x in trades if key in x["candidate_id"] and "A_short" in x["scenario"]), None)
        if t:
            lines.append(f"- Later traded: `{t['trade_id']}` entry `{t['entry_open_utc']}`")
        lines.append("")
    lines.append("Phase 2G: C010 PRIMARY; C011/C013 SAME_OPPORTUNITY_DUPLICATE of C010 upper pool.")
    lines.append("Phase 4A single-position: only one trade at a time — not the same as opportunity merge.")
    text = "\n".join(lines)
    (RESULTS / "reference_trade_review.md").write_text(text, encoding="utf-8")
    return text


def main() -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    REPORT.parent.mkdir(parents=True, exist_ok=True)

    trades = read_csv(PHASE4A / "simulated_trades.csv")
    suppressed = read_csv(PHASE4A / "suppressed_signals.csv")

    forensics = build_all_trade_forensics(trades)
    short_aud = [r for r in forensics if r["direction"] == "SHORT"]
    long_aud = [r for r in forensics if r["direction"] == "LONG"]

    write_csv(RESULTS / "all_trade_forensics.csv", forensics)
    write_csv(RESULTS / "short_tp_cluster_audit.csv", short_aud)
    write_csv(RESULTS / "long_tp_cluster_audit.csv", long_aud)
    write_csv(RESULTS / "same_pool_reentry_audit.csv", same_pool_audit(trades, suppressed))
    write_csv(RESULTS / "market_state_block_audit.csv", market_block_audit())
    write_csv(RESULTS / "scenario_count_reconciliation.csv", scenario_reconciliation())

    ref = write_reference_review(trades, suppressed)
    final = verdict(short_aud, long_aud)

    summary = [
        "# Phase 4B forensic summary",
        "",
        f"**VERDICT:** `{final}`",
        "",
        f"- Trades forensically documented: **{len(forensics)}**",
        f"- Short TP NOT_VALIDATED vs dense-cluster user rule: **{sum(1 for r in short_aud if r.get('tp_validation')=='TP_SELECTION_NOT_VALIDATED')}** / {len(short_aud)}",
        f"- Long TP NOT_VALIDATED: **{sum(1 for r in long_aud if r.get('tp_validation')=='TP_SELECTION_NOT_VALIDATED')}** / {len(long_aud)}",
        "- Scenario C: 14 unresolved = failed LONG **entry** attempts; 11 rows in simulated_trades (not 11 minus 14).",
        "",
    ]
    (RESULTS / "forensic_summary.md").write_text("\n".join(summary), encoding="utf-8")

    if not REPORT.is_file():
        REPORT.write_text(
            f"# DOGEUSDT Phase 4B\n\n**VERDICT:** `{final}`\n\nSee `results/phase4b_backtest_trade_forensics_v1/forensic_summary.md`.\n",
            encoding="utf-8",
        )

    print(json.dumps({"verdict": final, "trades": len(forensics)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
