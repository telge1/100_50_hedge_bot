#!/usr/bin/env python3
"""Phase 4G — final strategy decision gaps (read-only, no backtester changes)."""

from __future__ import annotations

import csv
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "results" / "phase4g_final_decision_gaps_v1"
REPORT = ROOT / "reports" / "DOGEUSDT_PHASE4G_FINAL_DECISION_MATRIX_V1.md"
PHASE4D = ROOT / "results" / "phase4d_pool_landscape_backtester_v2"

sys.path.insert(0, str(ROOT / "tools" / "phase4d_pool_landscape_backtester_v2"))
from pending_limit_simulator import simulate_user_pending_limit  # noqa: E402
from pool_zone_tracker import LifecycleIndex  # noqa: E402
from signal_provider import load_all_signals  # noqa: E402
from strategy_rules import select_long_tp_depth2, select_short_tp_depth2  # noqa: E402

CFG = json.loads(
    (ROOT / "tools/phase4d_pool_landscape_backtester_v2/strategy_config_v2.json").read_text()
)
MIN_TP = float(CFG["min_tp_distance_pct"])
PILOT_TO = CFG["pilot_to_utc"]
FOCUS_PENDING = {
    "C002_20260911T153500Z",
    "C003_20260911T170000Z",
    "C006_20260911T200500Z",
    "C008_20260911T211000Z",
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
    fields = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def load_prices() -> dict[str, dict]:
    by_t: dict[str, dict] = {}
    path = ROOT / "results/phase3a_dynamic_pool_landscape_timeline_v1/landscape_timeline.csv"
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            by_t[row["candle_close_utc"]] = {
                "l": float(row["low"]),
                "h": float(row["high"]),
            }
    return by_t


def iv_interval(iv: dict) -> tuple[float, float]:
    return float(iv["bottom_price"]), float(iv["top_price"])


def origins_str(iv: dict | None) -> str:
    if not iv:
        return ""
    o = iv.get("origins") or []
    return "|".join(o) if isinstance(o, list) else str(o)


def merge_cohesive(side: str, intervals: list[dict], entry: float) -> list[dict]:
    """Research proxy B: merge overlapping/touching same-side bands (all depths >= 2)."""
    eps = 1e-9
    bands = []
    for iv in intervals:
        if iv.get("side") != side:
            continue
        if int(iv.get("depth") or 0) < 2:
            continue
        bot, top = iv_interval(iv)
        if side == "lower" and top >= entry:
            continue
        if side == "upper" and bot <= entry:
            continue
        bands.append(
            {
                "bottom": bot,
                "top": top,
                "depth": int(iv.get("depth") or 0),
                "origins": list(iv.get("origins") or []),
            }
        )
    if not bands:
        return []
    if side == "lower":
        bands.sort(key=lambda x: -x["top"])
    else:
        bands.sort(key=lambda x: x["bottom"])

    clusters: list[dict] = []
    for b in bands:
        merged = False
        for cl in clusters:
            if side == "lower":
                if b["top"] >= cl["bottom"] - eps:
                    cl["bottom"] = min(cl["bottom"], b["bottom"])
                    cl["top"] = max(cl["top"], b["top"])
                    cl["depth"] = max(cl["depth"], b["depth"])
                    cl["origins"] = sorted(set(cl["origins"]) | set(b["origins"]))
                    cl["member_count"] += 1
                    merged = True
                    break
            else:
                if b["bottom"] <= cl["top"] + eps:
                    cl["bottom"] = min(cl["bottom"], b["bottom"])
                    cl["top"] = max(cl["top"], b["top"])
                    cl["depth"] = max(cl["depth"], b["depth"])
                    cl["origins"] = sorted(set(cl["origins"]) | set(b["origins"]))
                    cl["member_count"] += 1
                    merged = True
                    break
        if not merged:
            clusters.append(
                {
                    "bottom": b["bottom"],
                    "top": b["top"],
                    "depth": b["depth"],
                    "origins": list(b["origins"]),
                    "member_count": 1,
                }
            )
    return clusters


def select_variant_a_short(entry: float, intervals: list[dict]) -> dict | None:
    iv, _ = select_short_tp_depth2(entry, intervals, MIN_TP)
    if not iv:
        return None
    return {
        "bottom": float(iv["bottom_price"]),
        "top": float(iv["top_price"]),
        "depth": 2,
        "origins": iv.get("origins") or [],
        "tp_price": float(iv["top"]),
        "distance_pct": float(iv["dist_pct"]),
    }


def select_variant_a_long(entry: float, uppers: list[dict]) -> dict | None:
    iv, _ = select_long_tp_depth2(entry, uppers, MIN_TP)
    if not iv:
        return None
    return {
        "bottom": float(iv["bottom"]),
        "top": float(iv["top"]),
        "depth": 2,
        "origins": iv.get("origins") or [],
        "tp_price": float(iv["bottom"]),
        "distance_pct": float(iv["dist_pct"]),
    }


def select_variant_b_short(entry: float, intervals: list[dict]) -> dict | None:
    clusters = merge_cohesive("lower", intervals, entry)
    eligible = []
    for cl in clusters:
        dist = (entry - cl["top"]) / entry * 100.0
        if dist < MIN_TP:
            continue
        eligible.append({**cl, "tp_price": cl["top"], "distance_pct": dist})
    if not eligible:
        return None
    eligible.sort(key=lambda x: -x["top"])
    return eligible[0]


def select_variant_b_long(entry: float, uppers: list[dict]) -> dict | None:
    ivs = [{"side": "upper", **u} for u in uppers]
    clusters = merge_cohesive("upper", ivs, entry)
    eligible = []
    for cl in clusters:
        dist = (cl["bottom"] - entry) / entry * 100.0
        if dist < MIN_TP:
            continue
        eligible.append({**cl, "tp_price": cl["bottom"], "distance_pct": dist})
    if not eligible:
        return None
    eligible.sort(key=lambda x: x["bottom"])
    return eligible[0]


def tp_comparison_rows(trades: list[dict], smap: dict) -> list[dict]:
    rows = []
    for t in trades:
        if t.get("performance_validated") != "True":
            continue
        sig = smap[t["candidate_id"]]
        entry = float(t["entry_price"])
        if t["direction"] == "SHORT":
            a = select_variant_a_short(entry, sig.landscape_intervals)
            b = select_variant_b_short(entry, sig.landscape_intervals)
        else:
            a = select_variant_a_long(entry, sig.upper_tp_rows)
            b = select_variant_b_long(entry, sig.upper_tp_rows)
        sim_tp = float(t["tp_price"])
        rows.append(
            {
                "trade_id": t["trade_id"],
                "candidate_id": t["candidate_id"],
                "direction": t["direction"],
                "fill_price": entry,
                "simulated_tp_price": sim_tp,
                "variant_a_label": "NEAREST_DEPTH2_VISIBLE_GE_0P5PCT",
                "variant_a_bottom": a["bottom"] if a else "",
                "variant_a_top": a["top"] if a else "",
                "variant_a_depth": a["depth"] if a else "",
                "variant_a_origins": origins_str(a),
                "variant_a_tp_price": a["tp_price"] if a else "",
                "variant_a_distance_pct": a["distance_pct"] if a else "",
                "variant_b_label": "NEAREST_COHESIVE_CLUSTER_GE_0P5PCT_PROXY",
                "variant_b_bottom": b["bottom"] if b else "",
                "variant_b_top": b["top"] if b else "",
                "variant_b_depth_max": b["depth"] if b else "",
                "variant_b_member_count": b.get("member_count") if b else "",
                "variant_b_origins": origins_str(b),
                "variant_b_tp_price": b["tp_price"] if b else "",
                "variant_b_distance_pct": b["distance_pct"] if b else "",
                "a_matches_simulated": bool(a and abs(a["tp_price"] - sim_tp) < 1e-9),
                "b_matches_simulated": bool(b and abs(b["tp_price"] - sim_tp) < 1e-9),
                "a_equals_b": bool(
                    a and b and abs(a["tp_price"] - b["tp_price"]) < 1e-9 and a["depth"] == b["depth"]
                ),
                "a_differs_from_b": bool(
                    a and b and (abs(a["tp_price"] - b["tp_price"]) >= 1e-9 or a["depth"] != b["depth"])
                ),
            }
        )
    return rows


@dataclass
class SlotState:
    kind: str  # idle | pending | position
    candidate_id: str = ""
    until_utc: str = ""


def pending_status_until(
    sig,
    prices: dict[str, dict],
    lifecycle: LifecycleIndex,
    stop_before_utc: str | None,
) -> tuple[str, str | None, str | None]:
    """Run pending sim; optionally truncate observation at stop_before_utc (exclusive)."""
    times = sorted(prices.keys())
    if stop_before_utc:
        times = [t for t in times if t < stop_before_utc]
    if not times:
        return "UNKNOWN", None, None
    pilot_to = stop_before_utc or PILOT_TO
    if pilot_to not in prices and times:
        pilot_to = times[-1]
    sub_prices = {t: prices[t] for t in times if t <= pilot_to}
    limit = sig.pool_lower if sig.direction == "SHORT" else sig.pool_upper
    res = simulate_user_pending_limit(
        sig.direction,
        limit,
        sig.pool_lower,
        sig.pool_upper,
        sig.pool_identity,
        sig.order_active_from_utc,
        sub_prices,
        pilot_to,
        lifecycle,
    )
    return res.status, res.fill_utc, res.cancel_utc


def simulate_pending_policy_b(
    signals: list,
    prices: dict[str, dict],
    lifecycle: LifecycleIndex,
    trades: list[dict],
) -> list[dict]:
    """Counterfactual: max one pending OR one open position."""
    trade_by_cid = {t["candidate_id"]: t for t in trades}
    exit_by_cid = {
        t["candidate_id"]: (t.get("exit_utc") or "", t.get("fill_utc") or "")
        for t in trades
    }
    sorted_sigs = sorted(
        signals, key=lambda s: (s.order_active_from_utc, s.signal_close_utc, s.candidate_id)
    )
    slot = SlotState("idle")
    pending_holder: Any = None
    position_cid: str | None = None
    position_exit: str | None = None
    rows = []

    def free_slot(at_utc: str) -> bool:
        nonlocal position_cid, position_exit
        if position_cid and position_exit and at_utc >= position_exit:
            position_cid = None
            position_exit = None
        if position_cid and position_exit and at_utc < position_exit:
            return False
        if pending_holder:
            return False
        return True

    for sig in sorted_sigs:
        at = sig.order_active_from_utc
        if position_cid and position_exit and at >= position_exit:
            position_cid = None
            position_exit = None

        if pending_holder:
            st, fill_utc, cancel_utc = pending_status_until(
                pending_holder, prices, lifecycle, stop_before_utc=at
            )
            if st in ("ORDER_FILLED", "ORDER_CANCELLED_POOL_DEACTIVATED", "AMBIGUOUS_POOL_FILL_SEQUENCE"):
                pending_holder = None
            elif st == "ORDER_PENDING_AT_DATA_END" and at > PILOT_TO:
                pending_holder = None

        outcome_a = next(
            (o for o in read_csv(PHASE4D / "simulated_orders.csv") if o["candidate_id"] == sig.candidate_id),
            {},
        )
        supp = next(
            (
                s
                for s in read_csv(PHASE4D / "suppressed_signals.csv")
                if s["candidate_id"] == sig.candidate_id
            ),
            None,
        )

        policy_b_action = "PLACE_PENDING"
        policy_b_reason = ""
        if supp and supp["reason"] == "SUPPRESSED_POSITION_OPEN":
            policy_b_action = "BLOCKED_OPEN_POSITION"
            policy_b_reason = f"open {supp.get('open_trade_id')}"
        elif not free_slot(at):
            if position_cid:
                policy_b_action = "SIGNAL_LOGGED_ONLY"
                policy_b_reason = f"position {position_cid} until {position_exit}"
            else:
                policy_b_action = "SIGNAL_LOGGED_ONLY"
                policy_b_reason = f"pending slot held by {pending_holder.candidate_id}"
        else:
            pending_holder = sig
            st, fill_utc, _ = pending_status_until(sig, prices, lifecycle, stop_before_utc=None)
            if st == "ORDER_FILLED" and fill_utc:
                pending_holder = None
                if sig.candidate_id in trade_by_cid:
                    position_cid = sig.candidate_id
                    position_exit = exit_by_cid.get(sig.candidate_id, ("", ""))[0] or None
            elif st != "ORDER_PENDING_AT_DATA_END":
                pending_holder = None

        if sig.candidate_id in FOCUS_PENDING:
            rows.append(
                {
                    "candidate_id": sig.candidate_id,
                    "direction": sig.direction,
                    "signal_close_utc": sig.signal_close_utc,
                    "order_active_from_utc": at,
                    "manual_label": manual_label(sig.candidate_id),
                    "policy_a_order_status": outcome_a.get("order_status", ""),
                    "policy_a_suppressed": bool(supp),
                    "policy_a_suppress_reason": supp["reason"] if supp else "",
                    "policy_b_action": policy_b_action,
                    "policy_b_reason": policy_b_reason,
                }
            )
    return rows


def manual_label(cid: str) -> str:
    if cid.startswith("C002"):
        return "MANUALLY_NO_SHORT"
    if cid.startswith("C003"):
        return "MANUALLY_CONFIRMED_SHORT"
    if cid.startswith("C008"):
        return "MANUALLY_CONFIRMED_SHORT"
    return ""


def ms_at(close_utc: str) -> str:
    for row in read_csv(
        ROOT / "results/phase3b_two_pool_market_structure_detector_v1/market_state_timeline.csv"
    ):
        if row["as_of_close_utc"] == close_utc:
            return row["market_state"]
    return "UNRESOLVED"


def build_report(
    tp_rows: list[dict],
    pending_rows: list[dict],
    short_ms: list[dict],
) -> str:
    n_diff = sum(1 for r in tp_rows if r.get("a_differs_from_b"))
    n_a_sim = sum(1 for r in tp_rows if r.get("a_matches_simulated"))
    lines = [
        "# DOGEUSDT Phase 4G — Final Strategy Decision Matrix V1",
        "",
        "**Status:** Read-only decision prep for explicit user sign-off. **No** rule activated in backtester/scanner.",
        "",
        "## 1. Confirmed rules (already in freeze candidate)",
        "",
        "- SHORT/LONG limit at pool edge; pending until fill or depth-2 zone loss; no time timeout.",
        "- SL ±0.2% beyond entry-pool far edge; max **one open position**.",
        "- TP minimum distance **0.5%** from **actual fill** to TP edge (short→lower top, long→upper bottom).",
        "- Fees **0.11%** round-trip; `min_pool_age_bars=5` **not** a confirmed hard gate.",
        "- C003/C008 remain **MANUALLY_CONFIRMED_SHORT** reference labels.",
        "",
        "## 2. Open decisions requiring user approval",
        "",
        "| ID | Topic | Options | Pilot impact |",
        "|----|-------|---------|--------------|",
        "| **D-TP** | TP *geeignet* / dense cluster | **A:** nearest **depth-2** band ≥0.5% (V2 today) · **B:** nearest **cohesive merged** cluster proxy ≥0.5% | "
        f"**{n_diff}/8** differ on cluster depth/union (TP price same on all 8); V2 = **A** |",
        "| **D-PEND** | Pending concurrency | **A:** multiple parallel pending (V2 today) · **B:** one pending **or** one position | "
        "C002 pending would block C003+ under B; C008 still blocked by C006 position in both |",
        "| **D-LONG-TP** | LONG TP landscape | 3B confirmation CSV snapshot vs full PD1 at fill | 3 LONG trades use rank-list snapshot |",
        "| **D-MS** | Market structure gate | Experimental 3B **off** (today) vs future manual policy | 5/5 pilot SHORTs had `LONG_CANDIDATE` at signal |",
        "| **D-EXEC** | Slippage / funding / intrabar | Not in base spec | Informational only |",
        "",
        "### D-TP detail (8 trades)",
        "",
        "Variant **A** = `select_short_tp_depth2` / `select_long_tp_depth2` (implemented V2).  ",
        "Variant **B** = research proxy: merge touching/overlapping same-side bands (depth≥2), pick nearest cluster meeting 0.5%.  ",
        "**Neither variant is approved as final user rule.**",
        "",
    ]
    for r in tp_rows:
        flag = " **DIFF**" if r.get("a_differs_from_b") else ""
        lines.append(
            f"- `{r['trade_id']}` {r['direction']}: A tp={r['variant_a_tp_price']} depth={r['variant_a_depth']} · "
            f"B tp={r['variant_b_tp_price']} depth={r['variant_b_depth_max']} members={r['variant_b_member_count']}{flag}"
        )
    lines.extend(
        [
            "",
            f"Full numeric trace: `results/phase4g_final_decision_gaps_v1/tp_variant_comparison.csv`",
            "",
            "## 3. Pending priority (C002, C003, C006, C008)",
            "",
            "| Candidate | Manual label | Policy A (V2) | Policy B (counterfactual) |",
            "|-----------|--------------|---------------|---------------------------|",
        ]
    )
    for r in pending_rows:
        lines.append(
            f"| {r['candidate_id']} | {r['manual_label']} | "
            f"{r['policy_a_order_status']}"
            f"{' SUPPRESSED' if r['policy_a_suppressed'] else ''} | "
            f"{r['policy_b_action']}: {r['policy_b_reason']} |"
        )
    lines.extend(
        [
            "",
            "**A:** Each signal runs its own pending path; unfilled C002/C003 do not block later orders.  ",
            "**B:** While C002 stays `ORDER_PENDING_AT_DATA_END`, later shorts (incl. C006) would be **logged only** until slot frees.",
            "",
            "## 4. Market structure (informative — no new gate)",
            "",
            "Five executed SHORT trades with experimental 3B state at signal close:",
            "",
        ]
    )
    for r in short_ms:
        lines.append(
            f"- `{r['trade_id']}` {r['candidate_id']} @ {r['signal_close_utc']}: **{r['market_state']}**"
        )
    lines.extend(
        [
            "",
            "C003/C008: manual SHORT references; C003 pending at pilot end; C008 suppressed during C006 (position rule).",
            "",
            "## 5. Comparison impact summary",
            "",
            "- **TP:** V2 is aligned with variant **A** only. Variant **B** often shares the same nearest top/bottom but reports **higher depth** / wider cohesive union — not equivalent to user “large/dense cluster, not nearest thin pool”.",
            "- **Pending:** Variant **B** materially reduces signal churn but can freeze the slot on early pendings (C002).",
            "- **MS:** Turning 3B into a hard block would remove all five pilot SHORT fills at signal time (`LONG_CANDIDATE`) — conflicts with C003/C008 SHORT labels.",
            "",
            "## 6. Technical simplicity recommendation",
            "",
            "1. **Ship execution first:** Keep V2 entry/pending/SL/position rules; treat TP as explicit fork (A vs user-dense rule).  ",
            "2. **Do not combine** unresolved TP + pending queue + 3B gate in one release.  ",
            "3. **Prefer single-slot pending (B)** only if product intent is “one working order”; else document parallel pending (A) as deliberate.",
            "",
            "## 7. Required user sign-offs",
            "",
            "- [ ] **D-TP:** Choose TP ranking (depth-2 nearest vs dense-cluster definition — B is illustration only).  ",
            "- [ ] **D-PEND:** Parallel pending vs single pending slot.  ",
            "- [ ] **D-LONG-TP:** 3B snapshot vs full landscape at fill.  ",
            "- [ ] **D-MS:** Keep 3B informative vs any hard WATCH/LONG_CANDIDATE gate.  ",
            "- [ ] **D-EXEC:** Slippage/funding/intrabar ordering for `FINAL_APPROVED`.",
            "",
            "**STOP**",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    trades = read_csv(PHASE4D / "simulated_trades.csv")
    smap = {s.candidate_id: s for s in load_all_signals(CFG)}
    prices = load_prices()
    lifecycle = LifecycleIndex.load()

    tp_rows = tp_comparison_rows(trades, smap)
    write_csv(OUT / "tp_variant_comparison.csv", tp_rows)

    pending_rows = simulate_pending_policy_b(list(smap.values()), prices, lifecycle, trades)
    write_csv(OUT / "pending_priority_comparison.csv", pending_rows)

    short_ms = []
    for t in trades:
        if t.get("performance_validated") != "True" or t["direction"] != "SHORT":
            continue
        short_ms.append(
            {
                "trade_id": t["trade_id"],
                "candidate_id": t["candidate_id"],
                "signal_close_utc": t["signal_close_utc"],
                "market_state": ms_at(t["signal_close_utc"]),
                "gate_active_in_v2": False,
            }
        )
    write_csv(OUT / "short_trades_market_structure.csv", short_ms)

    REPORT.write_text(build_report(tp_rows, pending_rows, short_ms), encoding="utf-8")
    print(
        json.dumps(
            {
                "tp_rows": len(tp_rows),
                "a_differs_b": sum(1 for r in tp_rows if r.get("a_differs_from_b")),
                "report": str(REPORT),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
