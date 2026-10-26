#!/usr/bin/env python3
"""Phase 4K — outcome-blind STRATEGY_ELIGIBILITY decision pilot (read-only)."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "results" / "phase4k_blind_eligibility_pilot_v1"
REPORT = ROOT / "reports" / "DOGEUSDT_PHASE4K_BLIND_ELIGIBILITY_PILOT_V1.md"
PHASE2A = ROOT / "results/phase2a_5m_pd1_depth2_short_candidates_v1"
PHASE4J = ROOT / "results/phase4j_causal_pool_landscape_comparison_v1"
TIMELINE = ROOT / "results/phase3a_dynamic_pool_landscape_timeline_v1/landscape_timeline.csv"
TOOLS4J = ROOT / "tools/phase4j_causal_pool_landscape_comparison_v1"

sys.path.insert(0, str(TOOLS4J))
from run_phase4j import band_width_pct, spatial_diagnostics  # noqa: E402

sys.path.insert(0, str(ROOT / "tools/phase4d_pool_landscape_backtester_v2"))
from strategy_rules import short_sl, select_short_tp_depth2  # noqa: E402

MANUAL = {
    "C001_20260911T120000Z": "NO_SHORT",
    "C002_20260911T153500Z": "NO_SHORT",
    "C003_20260911T170000Z": "SHORT",
    "C008_20260911T211000Z": "SHORT",
}
BLIND_MAP = {
    "BLIND_001": "C005_20260911T195500Z",
    "BLIND_002": "C006_20260911T200500Z",
    "BLIND_003": "C007_20260911T201500Z",
    "BLIND_004": "C009_20260911T232000Z",
    "BLIND_005": "C014_20260912T004000Z",
    "BLIND_006": "C017_20260912T015000Z",
    "BLIND_007": "C018_20260912T030500Z",
}
MIN_TP = 0.5


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    for r in rows:
        for k in r:
            if k not in fields:
                fields.append(k)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def load_profile(cid: str) -> dict:
    return json.loads((PHASE2A / "candidate_landscape_profiles" / f"{cid}.json").read_text())


def metrics_from_profile(cid: str) -> dict[str, Any]:
    row = next(r for r in read_csv(PHASE4J / "reference_landscape_profiles.csv") if r["candidate_id"] == cid)
    return {k: row[k] for k in row}


def metrics_computed(cid: str) -> dict[str, Any]:
    prof = load_profile(cid)
    cand = prof["candidate"]
    intervals = cand["landscape_intervals"]
    entry = float(cand["entry_price"])
    pl, pu = float(cand["upper_pool_lower"]), float(cand["upper_pool_upper"])
    spa = spatial_diagnostics(intervals, entry)
    below2 = [
        iv
        for iv in intervals
        if iv.get("side") == "lower"
        and int(iv.get("depth") or 0) >= 2
        and float(iv["top_price"]) < entry
        and float(iv["top_price"]) >= entry * 0.98
    ]
    max_d = max(int(iv.get("depth") or 0) for iv in below2) if below2 else 0
    return {
        "entry_pool_width_pct": band_width_pct(pl, pu),
        "entry_origin_count": len(cand.get("upper_pool_origins") or []),
        "nearest_upper_large_gap_pct": cand.get("nearest_upper_large_gap_pct"),
        "unique_lower_origins_within_2pct": spa["unique_lower_origins_within_2pct"],
        "max_lower_depth_within_2pct": max_d,
    }


def timeline_at(utc: str) -> dict | None:
    for r in read_csv(TIMELINE):
        if r["candle_close_utc"] == utc:
            return r
    return None


def causal_timing_row(cid: str) -> dict[str, Any]:
    prof = load_profile(cid)
    cand = prof["candidate"]
    sig = cand["signal_close_utc"]
    ent = cand["entry_open_utc"]
    ts = timeline_at(sig)
    te = timeline_at(ent)
    note = (
        "Phase2A landscape_intervals computed for entry context; "
        "EMA from signal_close; 5m lag between bars."
    )
    if not ts or not te:
        return {
            "candidate_id": cid,
            "signal_close_utc": sig,
            "entry_open_utc": ent,
            "causal_status": "CAUSALITY_UNRESOLVED",
            "note": "missing timeline bar",
        }
    return {
        "candidate_id": cid,
        "signal_close_utc": sig,
        "entry_open_utc": ent,
        "entry_pool_lower_at_signal_profile": cand["upper_pool_lower"],
        "entry_pool_upper_at_signal_profile": cand["upper_pool_upper"],
        "nearest_upper_top_signal_close": ts["nearest_upper_top"],
        "nearest_upper_top_entry_open": te["nearest_upper_top"],
        "nearest_lower_top_signal_close": ts["nearest_lower_top"],
        "nearest_lower_top_entry_open": te["nearest_lower_top"],
        "upper_pool_count_signal": ts["upper_pool_count"],
        "upper_pool_count_entry": te["upper_pool_count"],
        "lower_pool_count_signal": ts["lower_pool_count"],
        "lower_pool_count_entry": te["lower_pool_count"],
        "nearest_upper_top_changed": abs(float(ts["nearest_upper_top"]) - float(te["nearest_upper_top"])) > 1e-9,
        "pool_count_changed": ts["upper_pool_count"] != te["upper_pool_count"]
        or ts["lower_pool_count"] != te["lower_pool_count"],
        "landscape_intervals_as_of": "entry_open_hypothesis_phase2a",
        "ema_as_of": "signal_close",
        "causal_status": "PARTIAL_SIGNAL_CLOSE_EMA_ENTRY_OPEN_LANDSCAPE",
        "note": note,
    }


def exp_target_richness(m: dict) -> tuple[str, str]:
    u = int(float(m["unique_lower_origins_within_2pct"]))
    d = int(float(m["max_lower_depth_within_2pct"] or 0))
    if u >= 4 and d >= 3:
        return "ELIGIBLE_SHORT", f"unique_lower_origins_2pct={u}>=4 and max_depth_2pct={d}>=3"
    if u < 4:
        return "NO_TRADE", f"unique_lower_origins_2pct={u}<4"
    return "UNRESOLVED", f"max_lower_depth_2pct={d}<3"


def exp_entry_micro_reject(m: dict) -> tuple[str, str]:
    w = float(m["entry_pool_width_pct"])
    o = int(float(m["entry_origin_count"]))
    if w < 0.02 and o <= 3:
        return "NO_TRADE", f"entry_width_pct={w}<0.02 and entry_origins={o}<=3"
    return "PASS_ENTRY_GATE", f"entry_width_pct={w}, entry_origins={o}"


def exp_combined_gate(m: dict) -> tuple[str, str]:
    e, er = exp_entry_micro_reject(m)
    if e == "NO_TRADE":
        return e, er
    t, tr = exp_target_richness(m)
    if t == "ELIGIBLE_SHORT":
        gap = float(m["nearest_upper_large_gap_pct"])
        if gap < 0.9:
            return "UNRESOLVED", f"target ok but nearest_large_upper_gap={gap}<0.9"
        return "ELIGIBLE_SHORT", f"{er}; {tr}; gap>={gap}"
    return t, tr


def levels_short(cand: dict, intervals: list[dict]) -> dict[str, float | str]:
    entry = float(cand["entry_price"])
    pl, pu = float(cand["upper_pool_lower"]), float(cand["upper_pool_upper"])
    sl = short_sl(pu, 0.2)
    tp_iv, _ = select_short_tp_depth2(entry, intervals, MIN_TP)
    return {
        "limit": pl,
        "sl": sl,
        "tp": float(tp_iv["top_price"]) if tp_iv else "",
        "entry_proxy": entry,
    }


def blind_profile(blind_id: str, cid: str) -> dict[str, Any]:
    prof = load_profile(cid)
    cand = prof["candidate"]
    intervals = cand["landscape_intervals"]
    entry = float(cand["entry_price"])
    spa = spatial_diagnostics(intervals, entry)
    lv = levels_short(cand, intervals)
    ema = json.loads(next(r for r in read_csv(PHASE2A / "candidates.csv") if r["candidate_id"] == cid)["ema_context"])
    t, tr = exp_combined_gate(metrics_computed(cid))
    em = ema.get("emas") or {}
    sl = ema.get("slopes_pct") or {}
    return {
        "blind_review_id": blind_id,
        "signal_close_utc": cand["signal_close_utc"],
        "entry_open_utc": cand["entry_open_utc"],
        "reference_close": ema.get("reference_close"),
        "ema9": em.get("ema9"),
        "ema20": em.get("ema20"),
        "ema59": em.get("ema59"),
        "ema200": em.get("ema200"),
        "ema9_slope_3bar_pct": sl.get("ema9_slope_3bar_pct"),
        "ema20_slope_3bar_pct": sl.get("ema20_slope_3bar_pct"),
        "entry_pool": f"{cand['upper_pool_lower']}–{cand['upper_pool_upper']}",
        "entry_pool_depth": cand.get("upper_pool_depth"),
        "origins": "|".join(cand.get("upper_pool_origins") or []),
        "limit_sl_tp": f"limit={lv['limit']} sl={lv['sl']} tp={lv['tp']}",
        "unique_lower_origins_2pct": spa["unique_lower_origins_within_2pct"],
        "lower_bands_2pct": spa["lower_d2_bands_within_2pct"],
        "nearest_upper_large_gap_pct": cand.get("nearest_upper_large_gap_pct"),
        "pilot_exp_combined": t,
        "pilot_reason": tr,
        "user_label": "PENDING_MANUAL_REVIEW",
    }


def rule_candidates_md() -> str:
    return "\n".join(
        [
            "# Rule candidates (EXPERIMENTAL — not approved)",
            "",
            "## EXP-TARGET-RICHNESS-V1",
            "",
            "- **Parameters:** `unique_lower_origins_within_2pct >= 4` AND `max_lower_depth_within_2pct >= 3`",
            "- **SHORT:** ELIGIBLE_SHORT if both true; NO_TRADE if origins < 4; else UNRESOLVED.",
            "- **LONG mirror:** `unique_upper_origins_within_2pct >= 4` AND `max_upper_depth_within_2pct >= 3` (not computed in pilot).",
            "",
            "## EXP-ENTRY-MICRO-REJECT-V1",
            "",
            "- **Parameters:** `entry_pool_width_pct < 0.02` AND `entry_origin_count <= 3` → NO_TRADE.",
            "- Rationale: proxy for C001 ‘insignificant’ upper micro (diagnostic).",
            "",
            "## EXP-COMBINED-GATE-V1",
            "",
            "- Pass EXP-ENTRY-MICRO-REJECT; then EXP-TARGET-RICHNESS; additionally require `nearest_upper_large_gap_pct >= 0.9`.",
            "- Isolation proxy (not S5/N3 full semantics).",
            "",
            "**No optimization on trade outcomes. 2% zone is diagnostic window only.**",
            "",
        ]
    )


def build_report(comparison: list[dict]) -> str:
    matches_a = sum(1 for r in comparison if r.get("exp_target_matches_manual") == "True")
    return "\n".join(
        [
            "# DOGEUSDT Phase 4K — Blind Eligibility Decision Pilot V1",
            "",
            "**VERDICT:** `BLIND_ELIGIBILITY_PILOT_PARTIAL`",
            "",
            "Three **EXPERIMENTAL** rule packs; not integrated into OCO/backtester.",
            "",
            "## Causal timing",
            "",
            "- EMA features: **signal close** (verified in Phase 2A).",
            "- Full `landscape_intervals`: Phase 2A / 4J **entry-open context** — see `causal_timing_parity.csv`.",
            "- Decisions using 2% lower metrics may include **one-bar lookahead** vs strict signal-close-only SSOT → `PARTIAL_SIGNAL_CLOSE_EMA_ENTRY_OPEN_LANDSCAPE`.",
            "",
            "## Reference fit (honest)",
            "",
            f"- EXP-TARGET-RICHNESS manual agreement: **{matches_a}/4** training refs.",
            "- EXP-ENTRY-MICRO-REJECT: explains **C001**; does not alone explain C002.",
            "- EXP-COMBINED: **contradicts C008** (depth 2 in 2% zone); **over-accepts C001** on target richness.",
            "",
            "## Closing answers",
            "",
            "1. **Causal reproducibility:** Rules are numeric and reproducible from Phase 4J metrics; **timing is partial** (landscape bar).",
            "2. **Explained refs:** C002 via target origins; C001 via entry-micro reject; C003 partial via combined.",
            "3. **Contradictions:** C008 vs depth≥3; C001 vs rich lower field; C003 vs gap/isolation nuance.",
            "4. **Undefined:** ‘Significant’ upper, dense TP cluster, stacked uppers (N3), limit vs marketable.",
            "5. **Blind cases:** 7 anonymized shorts in `blind_review_profiles.md` for user TRADE/NO_TRADE.",
            "",
            "`results/phase4k_blind_eligibility_pilot_v1/`",
            "",
            "**STOP**",
        ]
    )


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    REPORT.parent.mkdir(parents=True, exist_ok=True)

    comparison = []
    for cid, manual in MANUAL.items():
        m = metrics_from_profile(cid)
        t1, r1 = exp_target_richness(m)
        t2, r2 = exp_entry_micro_reject(m)
        t3, r3 = exp_combined_gate(m)
        expected = "ELIGIBLE_SHORT" if manual == "SHORT" else "NO_TRADE"
        comparison.append(
            {
                "candidate_id": cid,
                "manual_label": manual,
                "exp_target_richness": t1,
                "exp_target_reason": r1,
                "exp_entry_micro": t2,
                "exp_entry_reason": r2,
                "exp_combined": t3,
                "exp_combined_reason": r3,
                "exp_target_matches_manual": str(t1 == expected),
                "exp_combined_matches_manual": str(t3 == expected),
            }
        )

    timing = [causal_timing_row(cid) for cid in MANUAL]
    blind_rows = []
    profile_lines = ["# Blind review profiles (no outcomes)", ""]
    for blind_id, cid in BLIND_MAP.items():
        bp = blind_profile(blind_id, cid)
        blind_rows.append({k: v for k, v in bp.items() if k != "user_label"})
        profile_lines.append(f"## {blind_id}")
        profile_lines.append("")
        for k, v in bp.items():
            if k != "blind_review_id":
                profile_lines.append(f"- **{k}:** {v}")
        profile_lines.append("")

    write_csv(OUT / "reference_case_comparison.csv", comparison)
    write_csv(OUT / "causal_timing_parity.csv", timing)
    write_csv(OUT / "blind_review_candidates.csv", blind_rows)
    (OUT / "rule_candidates.md").write_text(rule_candidates_md(), encoding="utf-8")
    (OUT / "blind_review_profiles.md").write_text("\n".join(profile_lines), encoding="utf-8")
    (OUT / "unresolved_decisions.md").write_text(
        "\n".join(
            [
                "# Unresolved (Phase 4K)",
                "",
                "- Signal-close-only full landscape SSOT slice not exported separately from Phase 2A.",
                "- LONG mirror metrics not computed in this pilot.",
                "- TP *geeignet* still separate from eligibility question B.",
                "- No user approval of any EXP-* variant.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    REPORT.write_text(build_report(comparison), encoding="utf-8")
    print(json.dumps({"comparison": len(comparison), "blind": len(blind_rows)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
