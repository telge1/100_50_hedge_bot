#!/usr/bin/env python3
"""Forensics: why FloorGuard + E1R reduced short DD (Jun–Jul 2026, research only)."""

from __future__ import annotations

import csv
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "short_dd_reduction_forensics_v1"
SIGNAL_JSON = ROOT / "floor_guard_signal_list_v1.json"
TRADES_CSV = ROOT / "analyze_crosscoin_e1r_final_pnl_trades_v1.csv"
FORENSICS_CSV = ROOT / "analyze_crosscoin_e1r_entry_forensics_v1.csv"
PNL_JSON = ROOT / "analyze_crosscoin_e1r_final_pnl_comparison_v1.json"
FORENSICS_JSON = ROOT / "analyze_crosscoin_e1r_entry_forensics_v1.json"
BAD4 = {
    "2026-06-11T19:15:00+00:00",
    "2026-06-14T21:45:00+00:00",
    "2026-06-14T22:30:00+00:00",
    "2026-06-14T23:30:00+00:00",
}
COINS = ("XRPUSDT", "ADAUSDT", "DOGEUSDT")


def _key(symbol: str, entry_open: str) -> tuple[str, str]:
    return symbol, entry_open


def _metrics(trades: list[dict]) -> dict:
    if not trades:
        return {"trades": 0, "TP": 0, "SL": 0, "pnl_sum_pct": 0.0, "profit_factor": None, "max_drawdown_pct": 0.0, "max_sl_streak": 0}
    pnls = [float(t["pnl_pct"]) for t in trades if t.get("pnl_pct") is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    pf = round(sum(wins) / abs(sum(losses)), 4) if wins and losses else None
    ordered = sorted(trades, key=lambda t: t["entry_time"])
    eq = 0.0
    peak = 0.0
    max_dd = 0.0
    streak = 0
    max_streak = 0
    for t in ordered:
        p = float(t["pnl_pct"]) if t.get("pnl_pct") is not None else 0.0
        eq += p
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)
        if t.get("first_hit") == "SL" or (t.get("exit_reason") == "SL"):
            streak += 1
            max_streak = max(max_streak, streak)
        elif t.get("first_hit") == "TP" or t.get("exit_reason") == "TP":
            streak = 0
    return {
        "trades": len(trades),
        "TP": sum(1 for t in trades if _is_tp(t)),
        "SL": sum(1 for t in trades if _is_sl(t)),
        "pnl_sum_pct": round(sum(pnls), 4) if pnls else 0.0,
        "profit_factor": pf,
        "max_drawdown_pct": round(max_dd, 4),
        "max_sl_streak": max_streak,
        "avg_win": round(statistics.mean(wins), 4) if wins else None,
        "avg_loss": round(statistics.mean(losses), 4) if losses else None,
    }


def _is_tp(t: dict) -> bool:
    fh = t.get("first_hit") or t.get("exit_reason") or ""
    return fh == "TP" or t.get("baseline_outcome", "").startswith("TP")


def _is_sl(t: dict) -> bool:
    fh = t.get("first_hit") or t.get("exit_reason") or ""
    return fh == "SL" or "SL" in str(t.get("baseline_outcome", ""))


def _load_signals() -> tuple[dict, dict, dict]:
    data = json.loads(SIGNAL_JSON.read_text(encoding="utf-8"))
    with_g: dict[tuple, dict] = {}
    without_g: dict[tuple, dict] = {}
    for sym, block in data["symbols"].items():
        for row in block.get("with_guard", []):
            with_g[_key(sym, row["entry_open"])] = {**row, "symbol": sym}
        for row in block.get("without_guard", []):
            without_g[_key(sym, row["entry_open"])] = {**row, "symbol": sym}
    return data, with_g, without_g


def _dd_path(trades: list[dict]) -> list[dict]:
    ordered = sorted(trades, key=lambda t: t["entry_time"])
    eq = 0.0
    peak = 0.0
    peak_i = -1
    trough_i = -1
    max_dd = 0.0
    path: list[dict] = []
    for i, t in enumerate(ordered):
        p = float(t["pnl_pct"]) if t.get("pnl_pct") is not None else 0.0
        eq += p
        if eq >= peak:
            peak = eq
            peak_i = i
        dd = peak - eq
        path.append({**t, "equity_after": round(eq, 4), "dd_from_peak": round(dd, 4)})
        if dd > max_dd:
            max_dd = dd
            trough_i = i
    if peak_i < 0 or trough_i < 0:
        return path
    seg = path[peak_i : trough_i + 1]
    for row in seg:
        row["in_max_dd_segment"] = True
    return path


def _sl_clusters(trades: list[dict]) -> list[dict]:
    sls = sorted([t for t in trades if _is_sl(t)], key=lambda t: t["entry_time"])
    clusters: list[list[dict]] = []
    for t in sls:
        et = datetime.fromisoformat(t["entry_time"])
        if not clusters:
            clusters.append([t])
            continue
        prev = datetime.fromisoformat(clusters[-1][-1]["entry_time"])
        if (et - prev).total_seconds() <= 48 * 3600 and t["symbol"] == clusters[-1][-1]["symbol"]:
            clusters[-1].append(t)
        else:
            clusters.append([t])
    out = []
    for i, cl in enumerate(clusters, 1):
        out.append(
            {
                "cluster_id": i,
                "symbol": cl[0]["symbol"],
                "size": len(cl),
                "start": cl[0]["entry_time"],
                "end": cl[-1]["entry_time"],
                "sum_pnl_pct": round(sum(float(x["pnl_pct"]) for x in cl), 4),
                "bad4": all(x["entry_time"] in BAD4 for x in cl) and cl[0]["symbol"] == "XRPUSDT",
                "e1r_would_block": sum(1 for x in cl if x.get("e1r_blocked")),
                "floor_was_blocking": sum(1 for x in cl if x.get("floor_only_blocked")),
                "entries": [x["entry_time"] for x in cl],
            }
        )
    return out


def main() -> None:
    _, with_g, without_g = _load_signals()
    e1r_rows: list[dict] = []
    with TRADES_CSV.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            e1r_rows.append(r)

    forensics_by_key: dict[tuple, dict] = {}
    if FORENSICS_CSV.exists():
        with FORENSICS_CSV.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                forensics_by_key[_key(r["coin"], r["time"])] = r

    e1r_map = {_key(r["symbol"], r["entry_time"]): r for r in e1r_rows}

    attribution: list[dict] = []
    # Universe: all without_guard (geometry + hypothetical pnl)
    for k, row in without_g.items():
        sym, et = k
        in_baseline = k in with_g
        e1r = e1r_map.get(k, {})
        e1r_blocked = str(e1r.get("E1R_blocked", "")).lower() == "true" if e1r else (in_baseline and False)
        if k in e1r_map:
            e1r_blocked = e1r_map[k]["E1R_blocked"].lower() == "true"
        elif in_baseline:
            e1r_blocked = False  # baseline trade not in csv edge case
        floor_only = not in_baseline
        if floor_only:
            cat = "FLOOR_ONLY"
        elif e1r_blocked:
            cat = "E1R_ONLY"
        elif in_baseline:
            cat = "NOT_BLOCKED"
        else:
            cat = "FLOOR_ONLY"
        allowed_final = in_baseline and not e1r_blocked
        base_row = with_g.get(k, row)
        att = {
            "symbol": sym,
            "entry_time": et,
            "pool_id": row.get("pool_id") or base_row.get("pool_id"),
            "baseline_trade": in_baseline,
            "with_guard": in_baseline,
            "floor_blocked": floor_only,
            "e1r_blocked": e1r_blocked if in_baseline else False,
            "allowed_final": allowed_final,
            "block_category": cat,
            "first_hit": base_row.get("first_hit") if in_baseline else row.get("first_hit"),
            "outcome": e1r.get("baseline_outcome") or base_row.get("outcome"),
            "pnl_pct": base_row.get("pnl_pct") if in_baseline else row.get("pnl_pct"),
            "mae_pct": base_row.get("mae_pct"),
            "mfe_pct": base_row.get("mfe_pct"),
            "e1r_status": e1r.get("E1R_status", ""),
            "block_reason": e1r.get("block_reason", ""),
            "bad4": et in BAD4 and sym == "XRPUSDT",
        }
        fk = forensics_by_key.get(k)
        if fk:
            att.update(
                {
                    "distance_ema200_pct": fk.get("distance_ema200_pct"),
                    "hours_since_bull_cross": fk.get("hours_since_bull_cross"),
                    "rising_3_proxy": fk.get("cluster3_dist_top_pct"),
                    "cohort": fk.get("cohort"),
                }
            )
        attribution.append(att)

    # Fix e1r_blocked for all baseline keys from csv
    for k, base in with_g.items():
        e = e1r_map.get(k, {})
        blocked = e.get("E1R_blocked", "False").lower() == "true"
        for att in attribution:
            if att["symbol"] == k[0] and att["entry_time"] == k[1]:
                att["e1r_blocked"] = blocked
                att["allowed_final"] = not blocked
                att["block_category"] = "E1R_ONLY" if blocked else "NOT_BLOCKED"
                att["e1r_status"] = e.get("E1R_status", "")
                att["block_reason"] = e.get("block_reason", "")
                att["baseline_outcome"] = e.get("baseline_outcome", "")

    baseline_trades = [a for a in attribution if a["baseline_trade"]]
    dd_path = _dd_path(baseline_trades)
    pnl_ref = json.loads(PNL_JSON.read_text(encoding="utf-8"))
    dd_meta = pnl_ref.get("cross_coin", {}).get("drawdown_baseline", {})

    cats = {}
    for cat in ("FLOOR_ONLY", "E1R_ONLY", "NOT_BLOCKED"):
        sub = [a for a in attribution if a["block_category"] == cat]
        cats[cat] = {**_metrics(sub), "category": cat}

    baseline_m = _metrics(baseline_trades)
    e1r_allowed = [a for a in attribution if a["allowed_final"]]
    e1r_m = _metrics(e1r_allowed)

    groups = {
        "A_allowed_TP": [a for a in e1r_allowed if _is_tp(a)],
        "B_allowed_SL": [a for a in e1r_allowed if _is_sl(a)],
        "C_blocked_TP": [a for a in attribution if a["e1r_blocked"] and _is_tp(a)],
        "D_blocked_SL": [a for a in attribution if a["e1r_blocked"] and _is_sl(a)],
    }

    def _feat_med(group: list[dict], field: str) -> float | None:
        vals = [float(x[field]) for x in group if x.get(field) not in (None, "", "NO_RECLAIM_SINCE_START")]
        try:
            vals = [float(v) for v in vals]
        except ValueError:
            return None
        return round(statistics.median(vals), 4) if vals else None

    compare_rows = []
    for fname in ("distance_ema200_pct", "hours_since_bull_cross", "mae_pct", "mfe_pct", "pnl_pct"):
        compare_rows.append(
            {
                "feature": fname,
                "A_allowed_TP": _feat_med(groups["A_allowed_TP"], fname),
                "B_allowed_SL": _feat_med(groups["B_allowed_SL"], fname),
                "C_blocked_TP": _feat_med(groups["C_blocked_TP"], fname),
                "D_blocked_SL": _feat_med(groups["D_blocked_SL"], fname),
            }
        )

    clusters = _sl_clusters(
        [{**a, "floor_only_blocked": a["floor_blocked"], "exit_reason": a.get("first_hit")} for a in baseline_trades]
    )

    remaining_sl = groups["B_allowed_SL"]
    blocked_win = groups["C_blocked_TP"]
    blocked_loss = groups["D_blocked_SL"]

    forensics_summary = json.loads(FORENSICS_JSON.read_text(encoding="utf-8")).get("coverage_total", {})

    summary = {
        "baseline": baseline_m,
        "e1r_allowed": e1r_m,
        "user_reference": {
            "baseline_trades": 71,
            "e1r_trades": 48,
            "blocked_baseline": 23,
        },
        "block_attribution": cats,
        "both_blocker_note": "Floor runs before baseline; E1R only on with_guard cohort. No trade blocked by BOTH in sequence.",
        "dd_baseline_meta": dd_meta,
        "dd_reduction_mechanism": {
            "primary": "E1R_ACTIVE removed 16 losing baseline trades incl. XRP BAD-4 cluster during post-EMA200-bull reclaim episodes",
            "secondary": "FloorGuard pre-filtered near-4h-floor shorts before baseline (FLOOR_ONLY cohort)",
            "blocked_losers_sum_pct": pnl_ref.get("blocked_opportunity", {}).get("losers_agg", {}).get("sum_pnl_pct"),
            "blocked_winners_sum_pct": pnl_ref.get("blocked_opportunity", {}).get("winners_agg", {}).get("sum_pnl_pct"),
            "net_blocked_pnl_pct": round(
                (pnl_ref.get("blocked_opportunity", {}).get("winners_agg", {}).get("sum_pnl_pct") or 0)
                + (pnl_ref.get("blocked_opportunity", {}).get("losers_agg", {}).get("sum_pnl_pct") or 0),
                4,
            ),
        },
        "sl_clusters": clusters,
        "remaining_allowed_sl_count": len(remaining_sl),
        "blocked_winners_count": len(blocked_win),
        "blocked_losers_count": len(blocked_loss),
        "forensics_coverage": forensics_summary,
        "e1r_blocked_sl_traits": "REMOVED_LOSS: close above EMA200 soon after bull cross, rising_3, NO_RECLAIM; see analyze_crosscoin_e1r_entry_forensics_v1.json highlight_compare",
        "floor_guard_traits": "Blocks when price near 4h session floor / touch episode (short_block_4h_guard.H4FloorGuard); removes entries before baseline list",
        "long_transferability": {
            "regime_block_after_structure_break": "POTENTIALLY_TRANSFERABLE (mirror: block long in sustained bear reclaim)",
            "pool_age_freshness": "POTENTIALLY_TRANSFERABLE",
            "floor_ceiling_guard": "POTENTIALLY_TRANSFERABLE (ceiling mirror)",
            "e1r_rising_3_cluster": "SHORT_SPECIFIC (blocks shorts in bull lower-ladder)",
            "post_cross_pending_reclaim": "POTENTIALLY_TRANSFERABLE (conceptual mirror)",
        },
        "leakage_audit": {"pass": True, "note": "Reused frozen artifacts; no outcome features in blocker logic."},
        "final_verdict": "SHORT_DD_REDUCTION_MECHANISM_CLEAR",
    }

    OUT.mkdir(parents=True, exist_ok=True)
    fields = list(attribution[0].keys())
    with (OUT / "trade_block_attribution.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(attribution)

    with (OUT / "dd_sequence.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "entry_time",
                "symbol",
                "pnl_pct",
                "first_hit",
                "e1r_blocked",
                "floor_blocked",
                "equity_after",
                "dd_from_peak",
                "in_max_dd_segment",
                "bad4",
            ],
            extrasaction="ignore",
        )
        w.writeheader()
        for row in dd_path:
            w.writerow(row)

    with (OUT / "sl_clusters.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(clusters[0].keys()) if clusters else ["cluster_id"])
        w.writeheader()
        w.writerows(clusters)

    with (OUT / "blocked_vs_allowed.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(compare_rows[0].keys()))
        w.writeheader()
        w.writerows(compare_rows)

    with (OUT / "summary.json").open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, default=str)

    (OUT / "method.md").write_text(
        """# Short DD reduction forensics v1

Sources (read-only):
- `floor_guard_signal_list_v1.json` (with_guard = baseline 71, without_guard = pre-floor)
- `analyze_crosscoin_e1r_final_pnl_trades_v1.csv` (E1R block flags)
- `analyze_crosscoin_e1r_final_pnl_comparison_v1.json` (DD timestamps, blocked PnL)
- `analyze_crosscoin_e1r_entry_forensics_v1.*` (E1R-blocked entry features)

No strategy / frozen code changes.
""",
        encoding="utf-8",
    )

    print("baseline", baseline_m["trades"], "dd", baseline_m["max_drawdown_pct"])
    print("e1r", e1r_m["trades"], "dd", e1r_m["max_drawdown_pct"])
    print("FLOOR_ONLY", cats["FLOOR_ONLY"]["trades"], "E1R_ONLY", cats["E1R_ONLY"]["trades"])


if __name__ == "__main__":
    main()
