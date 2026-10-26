#!/usr/bin/env python3
"""Phase 4I — pre-order strategy eligibility & TP quality audit (read-only)."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "results" / "phase4i_preorder_strategy_eligibility_v1"
REPORT = ROOT / "reports" / "DOGEUSDT_PHASE4I_PREORDER_STRATEGY_ELIGIBILITY_V1.md"
PHASE2A = ROOT / "results/phase2a_5m_pd1_depth2_short_candidates_v1"
PHASE2D = ROOT / "results/phase2d_5bar_pool_age_comparison_v1/gate_comparison_all_20.csv"
PHASE4H = ROOT / "results/phase4h_oco_pending_entry_v1/oco_order_events.csv"
PHASE4G = ROOT / "results/phase4g_final_decision_gaps_v1/tp_variant_comparison.csv"
TOOLS4D = ROOT / "tools/phase4d_pool_landscape_backtester_v2"
sys.path.insert(0, str(TOOLS4D))

from signal_provider import load_all_signals  # noqa: E402
from strategy_rules import select_short_tp_depth2, select_long_tp_depth2  # noqa: E402

CFG = json.loads((TOOLS4D / "strategy_config_v2.json").read_text())
MIN_TP = float(CFG["min_tp_distance_pct"])

MANUAL_SHORT = {
    "C001_20260911T120000Z": ("NO_TRADE", "MANUALLY_NO_SHORT narrow upper"),
    "C002_20260911T153500Z": ("NO_TRADE", "MANUALLY_NO_SHORT thin lower TP landscape"),
    "C003_20260911T170000Z": ("ELIGIBLE_SHORT", "MANUALLY_CONFIRMED_SHORT"),
    "C008_20260911T211000Z": ("ELIGIBLE_SHORT", "MANUALLY_CONFIRMED_SHORT"),
    "C010_20260911T234000Z": ("ELIGIBLE_SHORT", "SHORT accepted user"),
    "C011_20260911T235500Z": ("DUPLICATE_OPPORTUNITY", "same as C010"),
    "C013_20260912T003500Z": ("DUPLICATE_OPPORTUNITY", "same as C010"),
    "C004_20260911T194500Z": ("UNREVIEWED", "too early / not preferred"),
}

FALLBUCH_SN = {
    "S5": "ELIGIBLE_SHORT",
    "N3": "NO_TRADE",
    "N4": "NO_TRADE",
    "N5": "NO_TRADE",
    "S7": "ELIGIBLE_SHORT",
    "N1": "NO_TRADE",
    "L1": "UNRESOLVED_LONG",
}


def read_csv(path: Path) -> list[dict]:
    if not path.is_file():
        return []
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
    p = PHASE2A / "candidate_landscape_profiles" / f"{cid}.json"
    if p.is_file():
        return json.loads(p.read_text(encoding="utf-8"))
    return {}


def oco_pending_ids() -> set[str]:
    ids: set[str] = set()
    for row in read_csv(PHASE4H):
        if row.get("event_type") == "ORDER_PENDING":
            ids.add(row["candidate_id"])
    return ids


def pool_width_pct(lower: float, upper: float) -> float:
    mid = (lower + upper) / 2.0
    return (upper - lower) / mid * 100.0 if mid else 0.0


def tp_landscape_short(cand: dict, entry: float) -> dict[str, Any]:
    intervals = cand.get("landscape_intervals") or []
    d2, _ = select_short_tp_depth2(entry, intervals, MIN_TP)
    lowers = []
    for iv in intervals:
        if iv.get("side") != "lower":
            continue
        top = float(iv["top_price"])
        if top >= entry:
            continue
        dist = (entry - top) / entry * 100.0
        if dist < MIN_TP:
            continue
        lowers.append(iv)
    lowers.sort(key=lambda x: -float(x["top_price"]))
    max_depth = max((int(x.get("depth") or 0) for x in lowers), default=0)
    depths_ge3 = [x for x in lowers if int(x.get("depth") or 0) >= 3]
    return {
        "tp_depth2_nearest_bottom": d2["bottom_price"] if d2 else "",
        "tp_depth2_nearest_top": d2["top_price"] if d2 else "",
        "tp_depth2_nearest_depth": d2.get("depth") if d2 else "",
        "tp_depth2_origin_count": len(d2.get("origins") or []) if d2 else 0,
        "eligible_lower_band_count_ge_0p5pct": len(lowers),
        "max_depth_among_eligible_lowers": max_depth,
        "deeper_band_count_depth_ge_3": len(depths_ge3),
        "nearest_deeper_top": depths_ge3[0]["top_price"] if depths_ge3 else "",
    }


def ema_snapshot(row: dict) -> dict[str, Any]:
    try:
        ctx = json.loads(row.get("ema_context") or "{}")
    except json.JSONDecodeError:
        return {"ema_status": "MISSING"}
    if ctx.get("status") != "VERIFIED":
        return {"ema_status": ctx.get("status", "MISSING")}
    emas = ctx.get("emas") or {}
    slopes = ctx.get("slopes_pct") or {}
    return {
        "ema_status": "VERIFIED",
        "ema9": emas.get("ema9"),
        "ema20": emas.get("ema20"),
        "ema59": emas.get("ema59"),
        "ema200": emas.get("ema200"),
        "ema9_slope_3bar_pct": slopes.get("ema9_slope_3bar_pct"),
        "ema200_slope_3bar_pct": slopes.get("ema200_slope_3bar_pct"),
        "ema_feature_only": True,
    }


def short_entry_rows() -> tuple[list[dict], list[dict], list[dict]]:
    ages = {r["candidate_id"]: r for r in read_csv(PHASE2D)}
    pending = oco_pending_ids()
    cand_rows = read_csv(PHASE2A / "candidates.csv")
    entry_rows = []
    target_rows = []
    manual_rows = []

    for row in cand_rows:
        cid = row["candidate_id"]
        prof = load_profile(cid)
        cand = prof.get("candidate") or {}
        pl = float(row["upper_pool_lower"])
        pu = float(row["upper_pool_upper"])
        entry = float(row["entry_price"])
        label, note = MANUAL_SHORT.get(cid, ("UNREVIEWED", ""))
        age = ages.get(cid, {})
        tp = tp_landscape_short(cand, entry)
        ema = ema_snapshot(row)
        candle = cand.get("candle") or {}
        bearish_close = candle.get("c") is not None and candle.get("o") is not None and float(candle["c"]) < float(candle["o"])

        proposed = "UNRESOLVED"
        if label == "NO_TRADE":
            proposed = "NO_TRADE"
        elif label == "ELIGIBLE_SHORT":
            proposed = "ELIGIBLE_SHORT"
        elif label == "DUPLICATE_OPPORTUNITY":
            proposed = "NO_TRADE_OR_DEDUP"
        else:
            proposed = "UNRESOLVED"

        false_intent = label == "NO_TRADE" and cid in pending

        entry_rows.append(
            {
                "candidate_id": cid,
                "direction": "SHORT",
                "signal_close_utc": row["signal_close_utc"],
                "manual_user_label": label,
                "manual_note": note,
                "proposed_preorder_eligibility": proposed,
                "oco_order_pending_placed_4h": cid in pending,
                "false_order_intent_vs_manual": false_intent,
                "entry_pool_lower": pl,
                "entry_pool_upper": pu,
                "entry_pool_width_pct": pool_width_pct(pl, pu),
                "entry_pool_depth": row.get("upper_pool_depth"),
                "entry_origin_count": len(cand.get("upper_pool_origins") or []),
                "nearest_upper_large_gap_pct": cand.get("nearest_upper_large_gap_pct"),
                "upper_large_gap_count": cand.get("upper_large_gap_count"),
                "lower_large_gap_count": cand.get("lower_large_gap_count"),
                "continuous_depth2_zone_age_bars": age.get("continuous_depth2_zone_age_bars"),
                "origin_age_bars_at_signal": age.get("origin_age_bars_at_signal"),
                "bearish_signal_candle": bearish_close,
                "trigger_reason": row.get("trigger_reason"),
                **ema,
            }
        )
        target_rows.append(
            {
                "candidate_id": cid,
                "direction": "SHORT",
                "fill_proxy_price": entry,
                "tp_rule_implemented": "NEAREST_DEPTH2_TOP_GE_0P5PCT",
                "tp_user_rule_open": "DENSE_RELEVANT_LOWER_CLUSTER",
                **tp,
            }
        )
        if cid in MANUAL_SHORT:
            manual_rows.append(
                {
                    "case_id": cid,
                    "case_type": "PILOT_SCANNER",
                    "manual_label": label,
                    "proposed_eligibility": proposed,
                    "false_oco_pending": false_intent,
                }
            )

    return entry_rows, target_rows, manual_rows


def long_entry_append(entry_rows: list[dict], target_rows: list[dict]) -> None:
    signals = {s.candidate_id: s for s in load_all_signals(CFG) if s.direction == "LONG"}
    for cid, sig in signals.items():
        entry = sig.pool_upper
        uppers = sig.upper_tp_rows
        d2, _ = select_long_tp_depth2(entry, uppers, MIN_TP)
        entry_rows.append(
            {
                "candidate_id": cid,
                "direction": "LONG",
                "signal_close_utc": sig.signal_close_utc,
                "manual_user_label": "EXPERIMENTAL_3B",
                "manual_note": "No manual LONG freeze label in pilot",
                "proposed_preorder_eligibility": "UNRESOLVED",
                "oco_order_pending_placed_4h": cid in oco_pending_ids(),
                "false_order_intent_vs_manual": False,
                "entry_pool_lower": sig.pool_lower,
                "entry_pool_upper": sig.pool_upper,
                "entry_pool_width_pct": pool_width_pct(sig.pool_lower, sig.pool_upper),
                "entry_pool_depth": 2,
                "entry_origin_count": len(sig.pool_identity.split("|")) if sig.pool_identity else 0,
                "nearest_upper_large_gap_pct": "",
                "upper_large_gap_count": "",
                "lower_large_gap_count": "",
                "continuous_depth2_zone_age_bars": "",
                "origin_age_bars_at_signal": "",
                "bearish_signal_candle": "",
                "trigger_reason": sig.signal_source,
                "ema_status": "NOT_EXTRACTED_FOR_3B",
                "ema_feature_only": True,
            }
        )
        target_rows.append(
            {
                "candidate_id": cid,
                "direction": "LONG",
                "fill_proxy_price": entry,
                "tp_rule_implemented": "NEAREST_DEPTH2_BOTTOM_GE_0P5PCT_3B_SNAPSHOT",
                "tp_user_rule_open": "DENSE_RELEVANT_UPPER_CLUSTER",
                "tp_depth2_nearest_bottom": d2["bottom"] if d2 else "",
                "tp_depth2_nearest_top": d2["top"] if d2 else "",
                "tp_depth2_nearest_depth": 2 if d2 else "",
                "tp_depth2_origin_count": len(d2.get("origins") or []) if d2 else 0,
                "eligible_lower_band_count_ge_0p5pct": len(uppers),
                "max_depth_among_eligible_lowers": max((int(u.get("depth") or 0) for u in uppers), default=0),
                "deeper_band_count_depth_ge_3": sum(1 for u in uppers if int(u.get("depth") or 0) >= 3),
                "nearest_deeper_top": "",
            }
        )


def asymmetry_rows(entry_rows: list[dict]) -> list[dict]:
    rows = []
    for e in entry_rows:
        if e["direction"] != "SHORT":
            continue
        ug = e.get("nearest_upper_large_gap_pct")
        lg = e.get("lower_large_gap_count")
        rows.append(
            {
                "candidate_id": e["candidate_id"],
                "manual_label": e["manual_user_label"],
                "nearest_upper_large_gap_pct": ug,
                "upper_large_gap_count": e.get("upper_large_gap_count"),
                "lower_large_gap_count": lg,
                "asymmetry_note": "User S5/N3: upper gap isolation vs stacked uppers — qualitative",
                "measurable_gap_proxy_only": True,
            }
        )
    return rows


def comparison_md(entry_rows: list[dict]) -> str:
    by_id = {r["candidate_id"]: r for r in entry_rows}

    def block(title: str, a: str, b: str, keys: list[str]) -> list[str]:
        lines = [f"### {title}", ""]
        if a not in by_id or b not in by_id:
            lines.append(f"- **UNRESOLVED:** missing profile for `{a}` or `{b}`")
            lines.append("")
            return lines
        ra, rb = by_id[a], by_id[b]
        lines.append(f"| Metric | {a} | {b} |")
        lines.append("|--------|-----|-----|")
        for k in keys:
            lines.append(f"| {k} | {ra.get(k)} | {rb.get(k)} |")
        lines.append("")
        return lines

    lines = ["# Eligibility comparisons (read-only)", ""]
    lines += block(
        "C001 (NO_TRADE) vs C003 (CONFIRMED SHORT)",
        "C001_20260911T120000Z",
        "C003_20260911T170000Z",
        [
            "entry_pool_width_pct",
            "nearest_upper_large_gap_pct",
            "upper_large_gap_count",
            "continuous_depth2_zone_age_bars",
            "false_order_intent_vs_manual",
        ],
    )
    lines += block(
        "C002 (NO_TRADE) vs C003 (CONFIRMED SHORT)",
        "C002_20260911T153500Z",
        "C003_20260911T170000Z",
        [
            "entry_pool_width_pct",
            "lower_large_gap_count",
            "nearest_upper_large_gap_pct",
            "false_order_intent_vs_manual",
        ],
    )
    lines += block(
        "C008 (CONFIRMED) vs C001 (weak upper)",
        "C008_20260911T211000Z",
        "C001_20260911T120000Z",
        [
            "entry_pool_width_pct",
            "entry_pool_depth",
            "continuous_depth2_zone_age_bars",
            "origin_age_bars_at_signal",
        ],
    )
    lines.extend(
        [
            "### Fallbuch pairs (causal SSOT not in Sep pilot)",
            "",
            "| Pair | User labels | Phase 4I status |",
            "|------|-------------|-----------------|",
            "| S5 vs N3 | accepted vs NO short stacked uppers | **UNRESOLVED_TIMELINE** — no Sep-11 SSOT slice |",
            "| S5 vs N4 | accepted vs weak lower TP | **UNRESOLVED_TIMELINE** |",
            "| N5 vs S7 | rejected vs accepted upper reactions | **UNRESOLVED_TIMELINE** — times ~15:30 vs 16:45 not in C-series |",
            "| N1 vs L1 | no long vs range long | **UNRESOLVED_TIMELINE** — 16 Sep cases outside pilot |",
            "",
        ]
    )
    return "\n".join(lines)


def unresolved_md(false_no_trade: list[str]) -> str:
    return "\n".join(
        [
            "# Unresolved decisions (Phase 4I)",
            "",
            "## False OCO pending vs manual NO_TRADE",
            "",
            f"- Candidates with **ORDER_PENDING** despite manual NO_TRADE: **{', '.join(false_no_trade) or 'none'}**",
            "",
            "## TP quality (still open)",
            "",
            "- Codified selector: nearest depth-2 band ≥0.5%.",
            "- User rule: *relevant / dense* counter-cluster — no approved ranker.",
            "- Phase 4G cohesive proxy changes depth/union only; not approved.",
            "",
            "## Dual-side eligibility",
            "",
            "- No rule yet for when SHORT and LONG may both be `ELIGIBLE` simultaneously.",
            "- 3B LONG remains experimental; not equated to user MS-02.",
            "",
            "## Limit placement semantics",
            "",
            "- Passive limit at pool edge even when price already through level → not specified.",
            "- Marketable limit / skip-if-through not defined.",
            "",
        ]
    )


def build_report(false_nt: list[str], entry_rows: list[dict]) -> str:
    unresolved_count = sum(1 for r in entry_rows if r["proposed_preorder_eligibility"] == "UNREVIEWED" or r["proposed_preorder_eligibility"] == "UNRESOLVED")
    return "\n".join(
        [
            "# DOGEUSDT Phase 4I — Pre-Order Strategy Eligibility & TP Quality V1",
            "",
            "**VERDICT:** `PREORDER_ELIGIBILITY_PARTIAL`",
            "",
            "Read-only audit. No OCO / backtester / scanner changes.",
            "",
            "## 1. False order intents (manual NO_TRADE → 4H ORDER_PENDING)",
            "",
            f"- **{len(false_nt)}** pilot NO_TRADE labels still received `ORDER_PENDING`: "
            + (", ".join(f"`{x}`" for x in false_nt) if false_nt else "none"),
            "- Primary: **C001**, **C002** (C001 cancelled quickly on zone loss; C002 pending to pilot end).",
            "",
            "## 2. Measurable contrasts (good vs NO_TRADE in pilot)",
            "",
            "- **C003 vs C001/C002:** C003 similar narrow upper width but user-confirmed; C002 flagged for **thin lower TP landscape** (not captured by depth-2 nearest TP alone).",
            "- **C008:** higher `continuous_depth2_zone_age_bars` (14) vs C003 (1) — age not a confirmed hard gate.",
            "- **EMA:** documented per candidate (`ema_feature_only`); no new mandatory filter.",
            "",
            "## 3. TP quality gaps",
            "",
            "- Implemented TP ≠ user *geeignet* cluster.",
            "- `target_pool_quality.csv`: depth-2 nearest vs counts of deeper eligible lowers.",
            "- See Phase 4F/4G; no new formula.",
            "",
            "## 4. OCO integration prep (proposal only)",
            "",
            "```",
            "RAW_SIGNAL",
            "  → STRATEGY_ELIGIBILITY (new layer — not built)",
            "  → ELIGIBLE_SHORT | ELIGIBLE_LONG | NO_TRADE | UNRESOLVED",
            "  → PENDING LIMIT (OCO engine — existing)",
            "  → FIRST FILL → OCO_CANCEL → POSITION",
            "```",
            "",
            "- **NO_TRADE guard:** eligibility layer must emit `NO_TRADE` before `ORDER_PENDING` (manual labels + future rules).",
            "- **Dual eligible:** needs explicit rule when both sides allowed (user confirmed for OCO).",
            "- **Same-direction duplicates:** OCO already cancels on first fill; eligibility should dedupe opportunity (C010/C011/C013).",
            "- **Limit vs market:** UNRESOLVED — pass-through limits vs skip-if-marketable.",
            "",
            f"- Unreviewed / unresolved proposed rows: **{unresolved_count}**",
            "",
            "## Artifacts",
            "",
            "`results/phase4i_preorder_strategy_eligibility_v1/`",
            "",
            "**STOP**",
        ]
    )


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    entry_rows, target_rows, manual_rows = short_entry_rows()
    long_entry_append(entry_rows, target_rows)

    for fb_id, label in FALLBUCH_SN.items():
        manual_rows.append(
            {
                "case_id": fb_id,
                "case_type": "FALLBUCH_SN",
                "manual_label": label,
                "proposed_eligibility": "UNRESOLVED_TIMELINE",
                "false_oco_pending": False,
            }
        )

    write_csv(OUT / "manual_case_eligibility.csv", manual_rows)
    write_csv(OUT / "entry_pool_quality.csv", entry_rows)
    write_csv(OUT / "target_pool_quality.csv", target_rows)
    write_csv(OUT / "pool_landscape_asymmetry.csv", asymmetry_rows(entry_rows))

    false_nt = [
        r["candidate_id"]
        for r in entry_rows
        if r.get("false_order_intent_vs_manual")
    ]
    (OUT / "eligibility_comparisons.md").write_text(comparison_md(entry_rows), encoding="utf-8")
    (OUT / "unresolved_decisions.md").write_text(unresolved_md(false_nt), encoding="utf-8")
    REPORT.write_text(build_report(false_nt, entry_rows), encoding="utf-8")
    print(json.dumps({"false_no_trade_pending": false_nt, "entry_rows": len(entry_rows)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
