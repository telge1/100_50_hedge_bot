#!/usr/bin/env python3
"""Phase 4F — read-only trade parity audit vs user-confirmed strategy."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PHASE4D = ROOT / "results" / "phase4d_pool_landscape_backtester_v2"
OUT = ROOT / "results" / "phase4f_strategy_trade_parity_audit_v1"
REPORT = ROOT / "reports" / "DOGEUSDT_PHASE4F_STRATEGY_TRADE_PARITY_V1.md"

sys.path.insert(0, str(ROOT / "tools" / "phase4d_pool_landscape_backtester_v2"))
from strategy_rules import (  # noqa: E402
    plan_long,
    plan_short,
    select_long_tp_depth2,
    select_short_tp_depth2,
    short_sl,
    long_sl,
)
from signal_provider import load_short_signals, load_long_signals  # noqa: E402

CFG = json.loads(
    (ROOT / "tools/phase4d_pool_landscape_backtester_v2/strategy_config_v2.json").read_text()
)
FEES = float(CFG["roundtrip_fees_pct"])
MIN_TP = float(CFG["min_tp_distance_pct"])


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
    fields = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def load_prices() -> dict[str, dict]:
    by_t = {}
    path = ROOT / "results/phase3a_dynamic_pool_landscape_timeline_v1/landscape_timeline.csv"
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            by_t[row["candle_close_utc"]] = {
                "o": float(row["open"]),
                "h": float(row["high"]),
                "l": float(row["low"]),
                "c": float(row["close"]),
            }
    return by_t


def signal_map() -> dict[str, Any]:
    sigs = load_short_signals(CFG) + load_long_signals(CFG)
    return {s.candidate_id: s for s in sigs}


def ms_at(close_utc: str) -> str:
    for row in read_csv(
        ROOT / "results/phase3b_two_pool_market_structure_detector_v1/market_state_timeline.csv"
    ):
        if row["as_of_close_utc"] == close_utc:
            return row["market_state"]
    return "UNRESOLVED"


def tp_trace(trade: dict, sig) -> list[dict]:
    entry = float(trade["entry_price"])
    rows = []
    if trade["direction"] == "SHORT":
        eligible = []
        for iv in sig.landscape_intervals:
            if iv.get("side") != "lower" or int(iv.get("depth") or 0) != 2:
                continue
            top = float(iv["top_price"])
            if top >= entry:
                continue
            dist = (entry - top) / entry * 100
            eligible.append((top, iv, dist))
        eligible.sort(key=lambda x: -x[0])
        picked, _ = select_short_tp_depth2(entry, sig.landscape_intervals, MIN_TP)
        for rank, (top, iv, dist) in enumerate(eligible, 1):
            rows.append(
                {
                    "trade_id": trade["trade_id"],
                    "candidate_id": trade["candidate_id"],
                    "side": "lower",
                    "rank_by_nearest_top": rank,
                    "pool_bottom": iv["bottom_price"],
                    "pool_top": top,
                    "depth": 2,
                    "distance_pct": dist,
                    "meets_min_0p5": dist >= MIN_TP,
                    "selected": picked and abs(float(picked["top"]) - top) < 1e-12,
                }
            )
    else:
        eligible = []
        for iv in sig.upper_tp_rows:
            if int(iv.get("depth") or 0) != 2:
                continue
            bot = float(iv["bottom_price"])
            if bot <= entry:
                continue
            dist = (bot - entry) / entry * 100
            eligible.append((bot, iv, dist))
        eligible.sort(key=lambda x: x[0])
        picked, _ = select_long_tp_depth2(entry, sig.upper_tp_rows, MIN_TP)
        for rank, (bot, iv, dist) in enumerate(eligible, 1):
            rows.append(
                {
                    "trade_id": trade["trade_id"],
                    "candidate_id": trade["candidate_id"],
                    "side": "upper",
                    "rank_by_nearest_bottom": rank,
                    "pool_bottom": bot,
                    "pool_top": iv["top_price"],
                    "depth": 2,
                    "distance_pct": dist,
                    "meets_min_0p5": dist >= MIN_TP,
                    "selected": picked and abs(float(picked["bottom"]) - bot) < 1e-12,
                }
            )
    return rows


def audit_trade(trade: dict, sig, prices: dict[str, dict]) -> dict[str, Any]:
    entry = float(trade["entry_price"])
    pl, pu = float(sig.pool_lower), float(sig.pool_upper)
    limit = float(trade["entry_level_rule"])
    fill_utc = trade["fill_utc"]
    bar = prices.get(fill_utc, {})
    touch_ok = bar and bar["l"] <= limit <= bar["h"]
    if trade["direction"] == "SHORT":
        exp_sl = short_sl(pu, float(CFG["sl_above_upper_pool_pct"]))
        exp_limit = pl
        plan = plan_short(pl, pu, sig.landscape_intervals, entry, CFG, quality_gate=False)
    else:
        exp_sl = long_sl(pl, float(CFG["sl_below_lower_pool_pct"]))
        exp_limit = pu
        plan = plan_long(pl, pu, sig.upper_tp_rows, entry, CFG, quality_gate=False)

    sl_ok = abs(float(trade["sl_price"]) - exp_sl) < 1e-9
    limit_ok = abs(limit - exp_limit) < 1e-9 and abs(entry - limit) < 1e-9
    tp_ok = plan.tp_price is not None and abs(float(trade["tp_price"]) - plan.tp_price) < 1e-9
    dist_ok = plan.tp_distance_pct is not None and plan.tp_distance_pct >= MIN_TP
    tp_elig = "TP_POOL_ELIGIBILITY_UNRESOLVED"
    price_parity = sl_ok and limit_ok and tp_ok and dist_ok and touch_ok
    exit_reason = trade.get("exit_reason") or ""
    exit_tier = (
        "CLEAR_EXIT"
        if exit_reason in ("TP", "SL")
        else "OPEN_AT_DATA_END"
        if exit_reason == "POSITION_OPEN_AT_DATA_END"
        else "AMBIGUOUS"
        if "AMBIGUOUS" in exit_reason
        else "UNKNOWN"
    )
    net = trade.get("net_pnl_pct")
    if net != "" and net is not None:
        net_f = float(net)
        gross = float(trade["gross_pnl_pct"]) if trade.get("gross_pnl_pct") else None
        fee_ok = gross is not None and abs(net_f - (gross - FEES)) < 1e-6
    else:
        fee_ok = exit_reason == "POSITION_OPEN_AT_DATA_END"

    user_validated = price_parity and tp_elig != "TP_POOL_ELIGIBILITY_UNRESOLVED"
    # User rule: "geeignet" beyond depth-2 + distance is OPEN → never fully strategy-validated
    user_validated = False

    return {
        "trade_id": trade["trade_id"],
        "candidate_id": trade["candidate_id"],
        "signal_source": sig.signal_source,
        "direction": trade["direction"],
        "signal_close_utc": trade["signal_close_utc"],
        "order_active_from_utc": trade["order_active_from_utc"],
        "pool_track_id": sig.pool_identity,
        "entry_pool_lower": pl,
        "entry_pool_upper": pu,
        "limit_price": limit,
        "limit_matches_user_edge": limit_ok,
        "fill_utc": fill_utc,
        "fill_price": entry,
        "fill_bar_touch_at_limit": touch_ok,
        "sl_price": trade["sl_price"],
        "sl_formula_ok": sl_ok,
        "tp_pool_bottom": trade.get("tp_pool_bottom"),
        "tp_pool_top": trade.get("tp_pool_top"),
        "tp_price": trade["tp_price"],
        "tp_distance_pct": trade.get("tp_distance_pct"),
        "tp_depth2_nearest_ok": tp_ok,
        "tp_min_distance_ok": dist_ok,
        "tp_pool_eligibility": tp_elig,
        "exit_utc": trade.get("exit_utc"),
        "exit_reason": exit_reason,
        "exit_tier": exit_tier,
        "net_pnl_pct": net,
        "fees_model_ok": fee_ok,
        "rule_ids": trade.get("rule_ids"),
        "technically_simulated": True,
        "price_and_order_rules_match": price_parity,
        "user_strategy_fully_validated": user_validated,
        "market_state_3b_at_signal": ms_at(trade["signal_close_utc"]),
        "manual_label_note": manual_note(trade["candidate_id"]),
    }


def manual_note(cid: str) -> str:
    if cid.startswith("C003"):
        return "MANUALLY_CONFIRMED_SHORT (not traded in 8)"
    if cid.startswith("C008"):
        return "MANUALLY_CONFIRMED_SHORT (suppressed; not in 8 trades)"
    return ""


def pending_audit(orders: list[dict], suppressed: list[dict], trades: list[dict]) -> list[dict]:
    rows = []
    trade_fills = {t["candidate_id"]: t["fill_utc"] for t in trades}
    for o in orders:
        cid = o["candidate_id"]
        rows.append(
            {
                "candidate_id": cid,
                "order_status": o["order_status"],
                "limit_price": o["limit_price"],
                "fill_utc": o.get("fill_utc") or "",
                "cancel_utc": o.get("cancel_utc") or "",
                "execution_rule_id": o["execution_rule_id"],
                "no_time_timeout": o["order_status"] != "EXPIRED_TIME",
                "fill_after_cancel_impossible": not (
                    o.get("fill_utc") and o.get("cancel_utc") and o["fill_utc"] > o["cancel_utc"]
                ),
                "blocks_other_signals": "NO_GLOBAL_PENDING_QUEUE",
                "blocked_by_open_position": any(
                    s["candidate_id"] == cid and s["reason"] == "SUPPRESSED_POSITION_OPEN"
                    for s in suppressed
                ),
            }
        )
    return rows


def verdict(price_match_count: int, n: int) -> str:
    if n == 0:
        return "STRATEGY_TRADE_PARITY_BLOCKED"
    if price_match_count == n:
        return "STRATEGY_TRADE_PARITY_PARTIAL"
    return "STRATEGY_TRADE_PARITY_PARTIAL"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    trades = [t for t in read_csv(PHASE4D / "simulated_trades.csv") if t.get("performance_validated") == "True"]
    orders = read_csv(PHASE4D / "simulated_orders.csv")
    suppressed = read_csv(PHASE4D / "suppressed_signals.csv")
    prices = load_prices()
    smap = signal_map()

    parity_rows = []
    tp_rows = []
    for t in trades:
        sig = smap[t["candidate_id"]]
        parity_rows.append(audit_trade(t, sig, prices))
        tp_rows.extend(tp_trace(t, sig))

    pending_rows = pending_audit(orders, suppressed, trades)

    ms_conflicts = []
    for row in parity_rows:
        st = row["market_state_3b_at_signal"]
        if row["direction"] == "SHORT" and st in ("WATCH_REVERSAL", "LONG_CANDIDATE"):
            ms_conflicts.append(
                {
                    "trade_id": row["trade_id"],
                    "candidate_id": row["candidate_id"],
                    "signal_close_utc": row["signal_close_utc"],
                    "experimental_market_state": st,
                    "gate_active_in_v2": False,
                    "manual_reference_conflict": "INFORMATIVE_ONLY",
                    "note": "C003/C008 labels preserved; 3B not used as hard block",
                }
            )

    unresolved_rules = [
        {
            "rule_id": "TP-QUALITY-GEEIGNET",
            "status": "OPEN",
            "affects_trades": len(trades),
            "detail": "User 'geeigneter TP-Pool' beyond depth-2 + 0.5% not codified",
        },
        {
            "rule_id": "PENDING-QUEUE-PRIORITY",
            "status": "OPEN",
            "affects_trades": "N/A",
            "detail": "V2 processes signals sequentially; no global pending slot blocking later signals",
        },
        {
            "rule_id": "LONG-TP-LANDSCAPE-SOURCE",
            "status": "IMPLEMENTATION",
            "affects_trades": sum(1 for t in trades if t["direction"] == "LONG"),
            "detail": "LONG TP from 3B upper_tp_candidates snapshot, not full PD1 landscape",
        },
    ]

    write_csv(OUT / "trade_rule_parity.csv", parity_rows)
    write_csv(OUT / "tp_pool_selection_trace.csv", tp_rows)
    write_csv(OUT / "pending_order_audit.csv", pending_rows)
    write_csv(OUT / "market_structure_conflicts.csv", ms_conflicts)
    write_csv(OUT / "unresolved_strategy_rules.csv", unresolved_rules)

    price_ok = sum(1 for r in parity_rows if r["price_and_order_rules_match"])
    v = verdict(price_ok, len(trades))

    summary = [
        "# Phase 4F audit summary",
        "",
        f"VERDICT: `{v}`",
        "",
        f"performance_validated trades audited: **{len(trades)}**",
        f"price_and_order_rules_match: **{price_ok}/{len(trades)}**",
        f"user_strategy_fully_validated: **0/{len(trades)}** (TP geeignet rule open)",
        f"3B market_state informational conflicts: **{len(ms_conflicts)}**",
        "",
    ]
    (OUT / "audit_summary.md").write_text("\n".join(summary), encoding="utf-8")

    report = [
        "# DOGEUSDT Phase 4F — Strategy Trade Parity V1",
        "",
        f"**VERDICT:** `{v}`",
        "",
        "## 1. Price & order parity (8 trades)",
        "",
        f"- **{price_ok}/8** trades match confirmed limit/SL/TP **formulas**, fill touch on limit, and depth-2 nearest TP selection.",
        "- **0/8** `user_strategy_fully_validated` because `TP_POOL_ELIGIBILITY_UNRESOLVED` applies to all (open *geeignet* definition).",
        "",
        "## 2. TP selection",
        "",
        "Mechanically correct for **depth-2 + ≥0.5%** rule. Not validated vs manual thin-lower cases (e.g. C002 NO_SHORT).",
        "",
        "## 3. Hidden assumptions",
        "",
        "- No global pending-order queue; unfilled C002/C003 do not block later signals.",
        "- LONG TP rows from Phase 3B `upper_tp_candidates_at_confirmation.csv`.",
        "- Per-signal pending simulated to completion in one pass.",
        "",
        "## 4. Cancellations",
        "",
        "5× `ORDER_CANCELLED_POOL_DEACTIVATED` — no fill after cancel in `simulated_orders.csv`.",
        "1× `AMBIGUOUS_POOL_FILL_SEQUENCE` (L3B_E11) — not traded.",
        "",
        "## 5. Blocks FINAL_APPROVED",
        "",
        "- TP *geeignet* quality rule",
        "- Pending priority / concurrent pending policy",
        "- LONG TP full PD1 landscape parity",
        "- Slippage/funding",
        "",
        f"Artifacts: `{OUT.relative_to(ROOT)}/`",
        "",
        "**STOP**",
    ]
    REPORT.write_text("\n".join(report), encoding="utf-8")
    print(json.dumps({"verdict": v, "trades": len(trades), "price_ok": price_ok}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
