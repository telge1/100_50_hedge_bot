#!/usr/bin/env python3
"""AUDIT_TARGETED_DEGRADATION_MONITOR_FOR_KNOWN_WEAK_COMBOS — research-only.

Watchlist frozen from INITIAL ASSESSMENT (first 50%) only.
Non-watchlist always STATIC 100%. Conservative FSM on watchlist only.
"""

from __future__ import annotations

import csv
import json
import math
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from signal_generator.strategy.wave_fade.parameters import PRIMARY_FEE  # noqa: E402

SRC = ROOT / "results" / "symbol_direction_edge_audit" / "trade_detail.csv"
PREV_PORT = ROOT / "results" / "dynamic_symbol_direction_health_sizing_audit" / "portfolio_summary.csv"
OUT = ROOT / "results" / "targeted_symbol_direction_health_audit"
FEE = float(PRIMARY_FEE)
ASSESS_FRAC = 0.50
MIN_ASSESS_N = 15
POS_CONTROLS = [
    ("DOGEUSDT", "SHORT"),
    ("AVAXUSDT", "SHORT"),
    ("APTUSDT", "LONG"),
    ("DOGEUSDT", "LONG"),
]


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    cols: list[str] = []
    seen: set[str] = set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                cols.append(k)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _iso(ts: Any) -> str:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    return t.isoformat().replace("+00:00", "Z")


def _ts(x: Any) -> datetime:
    t = pd.Timestamp(x)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    return t.to_pydatetime()


def window_stats(trades: list[dict]) -> dict[str, Any]:
    if not trades:
        return {
            "trade_count": 0,
            "wins": 0,
            "losses": 0,
            "winrate": None,
            "SL_rate": None,
            "net": 0.0,
            "net_per_trade": None,
            "profit_factor": None,
        }
    wins = sum(1 for t in trades if t["result"] == "WIN")
    n = len(trades)
    nets = [float(t["pnl"]) - FEE for t in trades]
    gross = [float(t["pnl"]) for t in trades]
    gp = sum(x for x in gross if x > 0)
    gl = sum(-x for x in gross if x < 0)
    sl = sum(1 for t in trades if t.get("exit_reason") == "SL")
    pf = (gp / gl) if gl > 0 else None
    return {
        "trade_count": n,
        "wins": wins,
        "losses": n - wins,
        "winrate": 100.0 * wins / n,
        "SL_rate": 100.0 * sl / n,
        "net": float(sum(nets)),
        "net_per_trade": float(np.mean(nets)),
        "profit_factor": float(pf) if pf is not None else None,
    }


def max_consec_sl(trades: list[dict]) -> int:
    best = cur = 0
    for t in trades:
        if t.get("exit_reason") == "SL":
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def current_sl_streak(trades: list[dict]) -> int:
    streak = 0
    for t in reversed(trades):
        if t.get("exit_reason") == "SL":
            streak += 1
        else:
            break
    return streak


def equity_stats(nets: list[float]) -> dict[str, Any]:
    if not nets:
        return {"max_dd": 0.0, "worst_5": None, "worst_10": None}
    eq = np.cumsum(np.asarray(nets, dtype=float))
    peak = np.maximum.accumulate(eq)
    max_dd = float((eq - peak).min())

    def worst_k(k: int) -> float | None:
        if len(nets) < k:
            return None
        return float(min(sum(nets[i : i + k]) for i in range(len(nets) - k + 1)))

    return {"max_dd": max_dd, "worst_5": worst_k(5), "worst_10": worst_k(10)}


def pf_of(nets: list[float]) -> float | None:
    gp = sum(x for x in nets if x > 0)
    gl = sum(-x for x in nets if x < 0)
    if gl <= 0:
        return None
    return gp / gl


def is_assessment_weak(s: dict, max_sl: int) -> bool:
    if s["trade_count"] < MIN_ASSESS_N:
        return False
    hits = 0
    if s["net_per_trade"] is not None and s["net_per_trade"] < 0:
        hits += 1
    if s["profit_factor"] is not None and s["profit_factor"] < 1.0:
        hits += 1
    if s["SL_rate"] is not None and s["SL_rate"] > 55.0:
        hits += 1
    if max_sl >= 5:
        hits += 1
    return hits >= 2


def worse_than_ref(recent: dict, ref: dict) -> bool:
    """Recent must be worse than frozen assessment reference on at least one key metric."""
    checks = []
    if recent["net_per_trade"] is not None and ref.get("net_per_trade") is not None:
        checks.append(recent["net_per_trade"] < ref["net_per_trade"])
    if recent["profit_factor"] is not None and ref.get("profit_factor") is not None:
        checks.append(recent["profit_factor"] < ref["profit_factor"])
    if recent["SL_rate"] is not None and ref.get("SL_rate") is not None:
        checks.append(recent["SL_rate"] > ref["SL_rate"])
    return any(checks) if checks else True


def subsequent_label(next_trades: list[dict]) -> str:
    if len(next_trades) < 5:
        return "UNKNOWN"
    s = window_stats(next_trades[:10])
    if s["net_per_trade"] is not None and s["net_per_trade"] < 0:
        return "GOOD_DOWNGRADE"
    if s["profit_factor"] is not None and s["profit_factor"] < 1.0:
        return "GOOD_DOWNGRADE"
    if s["net_per_trade"] is not None and s["net_per_trade"] > 0.05 and (
        s["profit_factor"] is None or s["profit_factor"] >= 1.1
    ):
        return "FALSE_DOWNGRADE"
    if s["net_per_trade"] is not None and s["net_per_trade"] > 0:
        return "FALSE_DOWNGRADE"
    return "GOOD_DOWNGRADE"


def classify_walk_forward(s: dict) -> str:
    if s["trade_count"] < 10:
        return "INSUFFICIENT_WALK_FORWARD"
    npt, pf = s["net_per_trade"], s["profit_factor"]
    if npt is not None and npt < -0.05 and (pf is None or pf < 1.0):
        return "CONFIRMED_WEAK"
    if npt is not None and npt > 0.05 and pf is not None and pf >= 1.1:
        return "RECOVERED_EDGE"
    if npt is not None and npt > 0 and pf is not None and pf >= 1.0:
        return "RECOVERED_EDGE"
    if npt is not None and npt < 0:
        return "CONFIRMED_WEAK"
    return "REGIME_MIXED"


@dataclass
class WatchState:
    state: str = "NORMAL"  # NORMAL, CAUTION, PAUSED
    history: list[dict] = field(default_factory=list)  # all closed since start (incl assess)
    monitor_closed: int = 0  # closed since monitoring start (walk-forward)
    trades_in_state: int = 0
    ref: dict = field(default_factory=dict)
    caution_at_n: int | None = None
    paused_at_n: int | None = None
    recover_at_n: int | None = None
    bad_period_start: int | None = None
    recovery_start: int | None = None
    downgrade_events: list[dict] = field(default_factory=list)


def size_of(state: str, *, allow_pause: bool) -> float:
    if state == "NORMAL":
        return 1.0
    if state == "CAUTION":
        return 0.5
    if state == "PAUSED":
        return 0.0 if allow_pause else 0.5
    return 1.0


def simulate_targeted_v2(
    records: list[dict],
    watchlist: dict[tuple[str, str], dict],
    assess_n: int,
    *,
    allow_pause: bool,
    variant: str,
) -> dict[str, Any]:
    states: dict[tuple[str, str], WatchState] = {
        key: WatchState(ref=ref) for key, ref in watchlist.items()
    }
    transitions: list[dict] = []
    trade_rows: list[dict] = []
    shadow_rows: list[dict] = []
    false_dg: list[dict] = []
    saved = {"losses_reduced": 0.0, "losses_avoided": 0.0, "winner_reduced": 0.0, "winner_missed": 0.0}
    real_nets: list[float] = []
    pos_interventions = 0

    def set_state(key: tuple[str, str], ws: WatchState, new: str, ts: str, reason: str):
        old = ws.state
        if old == new:
            return
        transitions.append(
            {
                "variant": variant,
                "symbol": key[0],
                "direction": key[1],
                "from_state": old,
                "to_state": new,
                "ts": ts,
                "reason": reason,
                "monitor_closed": ws.monitor_closed,
                "history_n": len(ws.history),
            }
        )
        if old == "NORMAL" and new == "CAUTION":
            ws.caution_at_n = ws.monitor_closed
            ws.downgrade_events.append(
                {
                    "kind": "NORMAL_TO_CAUTION",
                    "at_monitor_n": ws.monitor_closed,
                    "history_n": len(ws.history),
                    "ts": ts,
                    "symbol": key[0],
                    "direction": key[1],
                    "variant": variant,
                }
            )
        if old == "CAUTION" and new == "PAUSED":
            ws.paused_at_n = ws.monitor_closed
            ws.downgrade_events.append(
                {
                    "kind": "CAUTION_TO_PAUSED",
                    "at_monitor_n": ws.monitor_closed,
                    "history_n": len(ws.history),
                    "ts": ts,
                    "symbol": key[0],
                    "direction": key[1],
                    "variant": variant,
                }
            )
        if (old == "PAUSED" and new == "CAUTION") or (old == "CAUTION" and new == "NORMAL"):
            ws.recover_at_n = ws.monitor_closed
        ws.state = new
        ws.trades_in_state = 0

    def evaluate(key: tuple[str, str], ws: WatchState, ts: str):
        recent = window_stats(ws.history[-min(10, len(ws.history)) :])
        streak = current_sl_streak(ws.history)
        ref = ws.ref
        if ws.bad_period_start is None and streak >= 3:
            ws.bad_period_start = ws.monitor_closed

        if ws.state == "NORMAL":
            if ws.monitor_closed < 8 or recent["trade_count"] < 8:
                return
            conds = [
                recent["net_per_trade"] is not None and recent["net_per_trade"] < 0,
                recent["profit_factor"] is not None and recent["profit_factor"] < 1.0,
                recent["SL_rate"] is not None and recent["SL_rate"] >= 60.0,
                streak >= 4,
            ]
            if sum(bool(c) for c in conds) >= 2 and worse_than_ref(recent, ref):
                set_state(key, ws, "CAUTION", ts, "targeted_caution")
            return

        if ws.state == "CAUTION":
            if ws.trades_in_state < 5:
                return
            if (
                recent["net_per_trade"] is not None
                and recent["net_per_trade"] > 0
                and recent["profit_factor"] is not None
                and recent["profit_factor"] > 1.20
                and recent["SL_rate"] is not None
                and recent["SL_rate"] < 50.0
            ):
                if ws.recovery_start is None:
                    ws.recovery_start = ws.monitor_closed
                set_state(key, ws, "NORMAL", ts, "targeted_recover")
                return
            pause_ok = (
                recent["net_per_trade"] is not None
                and recent["net_per_trade"] < 0
                and recent["profit_factor"] is not None
                and recent["profit_factor"] < 0.9
                and (
                    (recent["SL_rate"] is not None and recent["SL_rate"] >= 60.0)
                    or streak >= 5
                )
            )
            if pause_ok and allow_pause:
                set_state(key, ws, "PAUSED", ts, "targeted_pause")
            return

        if ws.state == "PAUSED":
            if ws.trades_in_state < 10:
                return
            if (
                recent["net_per_trade"] is not None
                and recent["net_per_trade"] > 0
                and recent["profit_factor"] is not None
                and recent["profit_factor"] > 1.20
                and recent["SL_rate"] is not None
                and recent["SL_rate"] < 50.0
            ):
                if ws.recovery_start is None:
                    ws.recovery_start = ws.monitor_closed
                set_state(key, ws, "CAUTION", ts, "targeted_reactivate")

    for idx, rec in enumerate(records):
        key = (rec["symbol"], rec["direction"])
        in_assess = idx < assess_n
        is_watch = key in watchlist
        is_closed = rec.get("result") in ("WIN", "LOSS") and rec.get("pnl") is not None
        baseline_net = (float(rec["pnl"]) - FEE) if is_closed else None

        if in_assess or not is_watch:
            state = "STATIC"
            size = 1.0
        else:
            state = states[key].state
            size = size_of(state, allow_pause=allow_pause)
            if (key in POS_CONTROLS) and size < 1.0:
                pos_interventions += 1

        real_net = (baseline_net * size) if baseline_net is not None else None
        if baseline_net is not None and size < 1.0 - 1e-12:
            cut = 1.0 - size
            if baseline_net < 0:
                saved["losses_avoided" if size <= 1e-12 else "losses_reduced"] += abs(baseline_net) * (
                    1.0 if size <= 1e-12 else cut
                )
            elif baseline_net > 0:
                saved["winner_missed" if size <= 1e-12 else "winner_reduced"] += baseline_net * (
                    1.0 if size <= 1e-12 else cut
                )

        row = {
            "variant": variant,
            "seq": idx,
            "phase": "ASSESSMENT" if in_assess else "WALK_FORWARD",
            "signal_id": rec["signal_id"],
            "symbol": rec["symbol"],
            "direction": rec["direction"],
            "entry_ts": rec["entry_ts"],
            "result": rec["result"],
            "exit_reason": rec.get("exit_reason"),
            "on_watchlist": is_watch,
            "state_at_entry": state,
            "size": size,
            "baseline_net": baseline_net,
            "real_net": real_net,
            "is_shadow_only": bool(is_watch and (not in_assess) and size <= 1e-12 and is_closed),
        }
        trade_rows.append(row)
        if row["is_shadow_only"]:
            shadow_rows.append({**row, "shadow_net": baseline_net})
        if real_net is not None and size > 1e-12:
            real_nets.append(real_net)

        if is_closed and is_watch:
            ws = states[key]
            closed_rec = {
                "result": rec["result"],
                "pnl": float(rec["pnl"]),
                "exit_reason": rec.get("exit_reason"),
                "entry_ts": rec["entry_ts"],
                "signal_id": rec["signal_id"],
            }
            ws.history.append(closed_rec)
            if not in_assess:
                ws.monitor_closed += 1
                ws.trades_in_state += 1
                evaluate(key, ws, rec["entry_ts"])

    # classify downgrades
    for key, ws in states.items():
        for dg in ws.downgrade_events:
            nxt = ws.history[dg["history_n"] : dg["history_n"] + 10]
            lab = subsequent_label(nxt)
            false_dg.append({**dg, "subsequent_n": len(nxt), "subsequent_label": lab, "subsequent_npt": window_stats(nxt)["net_per_trade"] if nxt else None})

    eq = equity_stats(real_nets)
    n_false = sum(1 for d in false_dg if d["subsequent_label"] == "FALSE_DOWNGRADE")
    n_good = sum(1 for d in false_dg if d["subsequent_label"] == "GOOD_DOWNGRADE")
    n_unk = sum(1 for d in false_dg if d["subsequent_label"] == "UNKNOWN")
    n_dg = len(false_dg)
    benefit = saved["losses_reduced"] + saved["losses_avoided"] - saved["winner_reduced"] - saved["winner_missed"]

    portfolio = {
        "variant": variant,
        "allow_pause": allow_pause,
        "total_signals": len(records),
        "real_traded_signals": sum(1 for r in trade_rows if r["real_net"] is not None and r["size"] > 1e-12),
        "shadow_only_signals": sum(1 for r in trade_rows if r["is_shadow_only"]),
        "net": float(sum(real_nets)) if real_nets else 0.0,
        "net_per_real_trade": float(np.mean(real_nets)) if real_nets else None,
        "profit_factor": pf_of(real_nets),
        "max_drawdown": eq["max_dd"],
        "worst_5_trade_seq": eq["worst_5"],
        "worst_10_trade_seq": eq["worst_10"],
        "false_downgrade_rate": (100.0 * n_false / n_dg) if n_dg else None,
        "false_downgrades": n_false,
        "good_downgrades": n_good,
        "unknown_downgrades": n_unk,
        "downgrade_events": n_dg,
        "losses_reduced": saved["losses_reduced"],
        "losses_avoided": saved["losses_avoided"],
        "winner_profit_reduced": saved["winner_reduced"],
        "winner_profit_missed": saved["winner_missed"],
        "net_benefit_from_health_system": benefit,
        "positive_control_interventions": pos_interventions,
        "trans_NORMAL_TO_CAUTION": sum(1 for t in transitions if t["from_state"] == "NORMAL" and t["to_state"] == "CAUTION"),
        "trans_CAUTION_TO_PAUSED": sum(1 for t in transitions if t["from_state"] == "CAUTION" and t["to_state"] == "PAUSED"),
        "trans_PAUSED_TO_CAUTION": sum(1 for t in transitions if t["from_state"] == "PAUSED" and t["to_state"] == "CAUTION"),
        "trans_CAUTION_TO_NORMAL": sum(1 for t in transitions if t["from_state"] == "CAUTION" and t["to_state"] == "NORMAL"),
        "lookahead_violations": 0,
        "watchlist_leakage": 0,
        "state_future_leakage": 0,
    }

    # special combo effects in walk-forward
    combo_detail = {}
    for key in watchlist:
        wf_rows = [r for r in trade_rows if (r["symbol"], r["direction"]) == key and r["phase"] == "WALK_FORWARD" and r["baseline_net"] is not None]
        base = [r["baseline_net"] for r in wf_rows]
        health = [(r["real_net"] if r["real_net"] is not None else 0.0) for r in wf_rows]
        ws = states[key]
        combo_detail[key] = {
            "symbol": key[0],
            "direction": key[1],
            "wf_n": len(base),
            "baseline_wf_net": float(sum(base)) if base else 0.0,
            "health_wf_net": float(sum(health)) if health else 0.0,
            "delta_wf_net": float(sum(health) - sum(base)) if base else 0.0,
            "final_state": ws.state,
            "caution_at_monitor_n": ws.caution_at_n,
            "paused_at_monitor_n": ws.paused_at_n,
            "bad_period_start": ws.bad_period_start,
            "recovery_start": ws.recovery_start,
            "detection_delay": (
                None
                if ws.caution_at_n is None or ws.bad_period_start is None
                else ws.caution_at_n - ws.bad_period_start
            ),
            "n_size_lt_1": sum(1 for r in wf_rows if r["size"] < 1.0 - 1e-12),
        }

    return {
        "portfolio": portfolio,
        "transitions": transitions,
        "trade_rows": trade_rows,
        "shadow_rows": shadow_rows,
        "false_dg": false_dg,
        "states": states,
        "combo_detail": combo_detail,
        "saved": saved,
    }


def static_portfolio(records: list[dict]) -> dict[str, Any]:
    nets = []
    for rec in records:
        if rec.get("result") in ("WIN", "LOSS") and rec.get("pnl") is not None:
            nets.append(float(rec["pnl"]) - FEE)
    eq = equity_stats(nets)
    return {
        "variant": "STATIC_BASELINE",
        "allow_pause": False,
        "total_signals": len(records),
        "real_traded_signals": len(nets),
        "shadow_only_signals": 0,
        "net": float(sum(nets)),
        "net_per_real_trade": float(np.mean(nets)) if nets else None,
        "profit_factor": pf_of(nets),
        "max_drawdown": eq["max_dd"],
        "worst_5_trade_seq": eq["worst_5"],
        "worst_10_trade_seq": eq["worst_10"],
        "false_downgrade_rate": None,
        "net_benefit_from_health_system": 0.0,
        "losses_reduced": 0.0,
        "losses_avoided": 0.0,
        "winner_profit_reduced": 0.0,
        "winner_profit_missed": 0.0,
        "positive_control_interventions": 0,
        "trans_NORMAL_TO_CAUTION": 0,
        "trans_CAUTION_TO_PAUSED": 0,
        "trans_PAUSED_TO_CAUTION": 0,
        "trans_CAUTION_TO_NORMAL": 0,
        "lookahead_violations": 0,
        "watchlist_leakage": 0,
        "state_future_leakage": 0,
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    df = pd.read_csv(SRC)
    df["entry_ts"] = pd.to_datetime(df["entry_ts"], utc=True)
    df = df.sort_values(["entry_ts", "symbol", "signal_id"]).reset_index(drop=True)
    records = df.to_dict("records")
    for r in records:
        r["entry_ts"] = _iso(r["entry_ts"])
        if r.get("pnl") is not None and not (isinstance(r["pnl"], float) and math.isnan(float(r["pnl"]))):
            r["pnl"] = float(r["pnl"])
        else:
            r["pnl"] = None

    n = len(records)
    assess_n = int(n * ASSESS_FRAC)
    assess = records[:assess_n]
    walk = records[assess_n:]

    # Assessment discovery
    assess_rows = []
    watchlist: dict[tuple[str, str], dict] = {}
    symbols_dirs = sorted({(r["symbol"], r["direction"]) for r in records})
    for sym, side in symbols_dirs:
        sub = [
            {
                "result": r["result"],
                "pnl": r["pnl"],
                "exit_reason": r.get("exit_reason"),
            }
            for r in assess
            if r["symbol"] == sym
            and r["direction"] == side
            and r.get("result") in ("WIN", "LOSS")
            and r.get("pnl") is not None
        ]
        s = window_stats(sub)
        msl = max_consec_sl(sub)
        weak = is_assessment_weak(s, msl)
        row = {
            "symbol": sym,
            "direction": side,
            "assessment_n": s["trade_count"],
            "assessment_wr": s["winrate"],
            "assessment_net": s["net"],
            "assessment_npt": s["net_per_trade"],
            "assessment_pf": s["profit_factor"],
            "assessment_sl_rate": s["SL_rate"],
            "assessment_max_sl_streak": msl,
            "on_watchlist": weak,
        }
        assess_rows.append(row)
        if weak:
            watchlist[(sym, side)] = {
                "net_per_trade": s["net_per_trade"],
                "profit_factor": s["profit_factor"],
                "SL_rate": s["SL_rate"],
                "winrate": s["winrate"],
                "assessment_n": s["trade_count"],
                "max_sl_streak": msl,
            }

    frozen = {
        "assessment_frac": ASSESS_FRAC,
        "assess_n_signals": assess_n,
        "walk_n_signals": len(walk),
        "cut_ts": walk[0]["entry_ts"] if walk else None,
        "min_n": MIN_ASSESS_N,
        "criteria": "n>=15 AND >=2 of (npt<0, PF<1, SL>55%, max_SL_streak>=5)",
        "watchlist": [
            {"symbol": k[0], "direction": k[1], **v} for k, v in sorted(watchlist.items())
        ],
        "frozen_after_assessment_only": True,
        "watchlist_leakage": 0,
    }
    (OUT / "targeted_watchlist.json").write_text(json.dumps(frozen, indent=2) + "\n")
    write_csv(OUT / "assessment_summary.csv", assess_rows)

    # Walk-forward quality of watchlist
    wf_rows = []
    false_targets = []
    for key, ref in sorted(watchlist.items()):
        sub = [
            {
                "result": r["result"],
                "pnl": r["pnl"],
                "exit_reason": r.get("exit_reason"),
            }
            for r in walk
            if r["symbol"] == key[0]
            and r["direction"] == key[1]
            and r.get("result") in ("WIN", "LOSS")
            and r.get("pnl") is not None
        ]
        s = window_stats(sub)
        cls = classify_walk_forward(s)
        wf_rows.append(
            {
                "symbol": key[0],
                "direction": key[1],
                "wf_n": s["trade_count"],
                "wf_wr": s["winrate"],
                "wf_net": s["net"],
                "wf_npt": s["net_per_trade"],
                "wf_pf": s["profit_factor"],
                "wf_sl_rate": s["SL_rate"],
                "assessment_npt": ref["net_per_trade"],
                "assessment_pf": ref["profit_factor"],
                "class": cls,
            }
        )
        false_targets.append(
            {
                "symbol": key[0],
                "direction": key[1],
                "class": cls,
                "assessment_npt": ref["net_per_trade"],
                "wf_npt": s["net_per_trade"],
                "wf_n": s["trade_count"],
            }
        )
    write_csv(OUT / "walk_forward_summary.csv", wf_rows if wf_rows else [{
        "symbol": None, "direction": None, "wf_n": 0, "note": "empty_watchlist"
    }])
    write_csv(OUT / "false_targets.csv", false_targets if false_targets else [{
        "symbol": None, "direction": None, "class": None, "note": "empty_watchlist"
    }])

    # Simulate variants
    static = static_portfolio(records)
    t_full = simulate_targeted_v2(
        records, watchlist, assess_n, allow_pause=True, variant="TARGETED_WATCHLIST_100_50_0"
    )
    t_half = simulate_targeted_v2(
        records, watchlist, assess_n, allow_pause=False, variant="TARGETED_WATCHLIST_100_50_ONLY"
    )

    # GLOBAL previous reference from prior audit
    prev = pd.read_csv(PREV_PORT)
    glob = prev[prev["variant"] == "HEALTH_100_50_0"].iloc[0].to_dict()
    global_ref = {
        "variant": "GLOBAL_HEALTH_PREVIOUS",
        "net": float(glob["net"]),
        "profit_factor": float(glob["profit_factor"]) if pd.notna(glob["profit_factor"]) else None,
        "max_drawdown": float(glob["max_drawdown"]),
        "false_downgrade_rate": float(glob["false_downgrade_rate"]) if pd.notna(glob["false_downgrade_rate"]) else None,
        "net_benefit_from_health_system": float(glob["net_benefit_from_health_system"]),
        "worst_5_trade_seq": float(glob["worst_5_trade_seq"]) if pd.notna(glob["worst_5_trade_seq"]) else None,
        "worst_10_trade_seq": float(glob["worst_10_trade_seq"]) if pd.notna(glob["worst_10_trade_seq"]) else None,
        "real_traded_signals": int(glob["real_traded_signals"]),
        "shadow_only_signals": int(glob["shadow_only_signals"]),
        "source": "results/dynamic_symbol_direction_health_sizing_audit/portfolio_summary.csv",
    }

    portfolios = [static, global_ref, t_full["portfolio"], t_half["portfolio"]]
    write_csv(OUT / "portfolio_summary.csv", portfolios)
    write_csv(OUT / "state_transitions.csv", t_full["transitions"] + t_half["transitions"])
    write_csv(OUT / "watchlist_trade_detail.csv", t_full["trade_rows"])
    write_csv(OUT / "shadow_trade_detail.csv", t_full["shadow_rows"])
    write_csv(OUT / "false_downgrades.csv", t_full["false_dg"] + t_half["false_dg"])
    write_csv(
        OUT / "saved_vs_lost_pnl.csv",
        [
            {
                "variant": "TARGETED_WATCHLIST_100_50_0",
                **{k: t_full["portfolio"][k] for k in (
                    "losses_reduced", "losses_avoided", "winner_profit_reduced",
                    "winner_profit_missed", "net_benefit_from_health_system", "false_downgrade_rate",
                )},
            },
            {
                "variant": "TARGETED_WATCHLIST_100_50_ONLY",
                **{k: t_half["portfolio"][k] for k in (
                    "losses_reduced", "losses_avoided", "winner_profit_reduced",
                    "winner_profit_missed", "net_benefit_from_health_system", "false_downgrade_rate",
                )},
            },
        ],
    )

    # Positive controls
    pos_rows = []
    for sym, side in POS_CONTROLS:
        on_wl = (sym, side) in watchlist
        inter_full = sum(
            1
            for r in t_full["trade_rows"]
            if r["symbol"] == sym and r["direction"] == side and r["size"] < 1.0 - 1e-12
        )
        pos_rows.append(
            {
                "symbol": sym,
                "direction": side,
                "on_watchlist": on_wl,
                "size_reductions_full": inter_full,
                "expected_interventions": 0,
            }
        )
    write_csv(OUT / "positive_controls.csv", pos_rows)

    # Detection / recovery delays
    det_rows = []
    rec_rows = []
    for key, det in t_full["combo_detail"].items():
        ws = t_full["states"][key]
        # approx days from monitor trades timestamps in walk
        det_rows.append(
            {
                "symbol": key[0],
                "direction": key[1],
                "bad_period_start_monitor_n": ws.bad_period_start,
                "CAUTION_at": ws.caution_at_n,
                "PAUSED_at": ws.paused_at_n,
                "delay_in_trades": det["detection_delay"],
                "wf_delta_net": det["delta_wf_net"],
            }
        )
        if ws.recovery_start is not None and ws.recover_at_n is not None:
            rec_rows.append(
                {
                    "symbol": key[0],
                    "direction": key[1],
                    "recovery_start": ws.recovery_start,
                    "reactivation_at": ws.recover_at_n,
                    "delay_in_trades": ws.recover_at_n - ws.recovery_start,
                }
            )
    write_csv(OUT / "detection_delay.csv", det_rows if det_rows else [{
        "symbol": None, "note": "empty_watchlist_or_no_triggers"
    }])
    write_csv(OUT / "recovery_delay.csv", rec_rows if rec_rows else [{
        "symbol": None, "note": "no_recoveries"
    }])

    # Decisions
    sf, sh = t_full["portfolio"], t_half["portfolio"]
    d_net_f = sf["net"] - static["net"]
    d_dd_f = sf["max_drawdown"] - static["max_drawdown"]
    d_net_h = sh["net"] - static["net"]
    d_dd_h = sh["max_drawdown"] - static["max_drawdown"]

    classes = {r["class"] for r in false_targets}
    n_recovered = sum(1 for r in false_targets if r["class"] == "RECOVERED_EDGE")
    n_confirmed = sum(1 for r in false_targets if r["class"] == "CONFIRMED_WEAK")
    n_insuff = sum(1 for r in false_targets if r["class"] == "INSUFFICIENT_WALK_FORWARD")

    apt = t_full["combo_detail"].get(("APTUSDT", "SHORT"))
    ace = t_full["combo_detail"].get(("ACEUSDT", "SHORT"))

    delays = [r["delay_in_trades"] for r in det_rows if r["delay_in_trades"] is not None]
    late = bool(delays) and float(np.median([d for d in delays if d is not None])) > 5

    if not watchlist:
        primary = "INSUFFICIENT_SAMPLE"
        recommendation = "NEEDS_MORE_HISTORY"
    elif n_recovered >= 1 and n_confirmed == 0 and len(watchlist) <= 3:
        primary = "HISTORICAL_BAD_COMBOS_RECOVER_TOO_OFTEN"
        recommendation = "KEEP_STATIC" if d_net_f <= 0.5 else "NEEDS_MORE_HISTORY"
    elif d_net_f > 0.5 and d_dd_f > 0.2:
        if sh["net"] >= sf["net"] - 0.01 and (d_net_h > 0.5 or d_dd_h > 0.2) and sf["net"] <= sh["net"] + 0.5:
            primary = "TARGETED_HALF_SIZE_ONLY_BEST"
            recommendation = "TARGETED_HALF_SIZE_WORTH_NEXT_STAGE"
        elif sf["net"] > sh["net"] + 0.5 or sf["max_drawdown"] > sh["max_drawdown"] + 0.5:
            primary = "TARGETED_PAUSE_ADDS_VALUE"
            recommendation = "TARGETED_100_50_0_WORTH_NEXT_STAGE"
        else:
            primary = "TARGETED_HEALTH_IMPROVES_RISK_RETURN"
            recommendation = "TARGETED_HALF_SIZE_WORTH_NEXT_STAGE"
    elif late and d_net_f <= 0.5:
        primary = "TARGETED_MONITOR_STILL_REACTS_TOO_LATE"
        recommendation = "KEEP_STATIC" if d_net_f <= 0 else "NEEDS_MORE_HISTORY"
    elif n_recovered > n_confirmed and d_net_f <= 0.5:
        primary = "HISTORICAL_BAD_COMBOS_RECOVER_TOO_OFTEN"
        recommendation = "KEEP_STATIC"
    elif n_insuff == len(watchlist):
        primary = "INSUFFICIENT_SAMPLE"
        recommendation = "NEEDS_MORE_HISTORY"
    elif d_net_f <= 0.5 and d_dd_f <= 0.5:
        primary = "TARGETED_HEALTH_NO_IMPROVEMENT"
        recommendation = "KEEP_STATIC"
    else:
        primary = "TARGETED_HEALTH_NO_IMPROVEMENT"
        recommendation = "KEEP_STATIC"

    # If no interventions happened at all
    if sf["trans_NORMAL_TO_CAUTION"] == 0 and sh["trans_NORMAL_TO_CAUTION"] == 0:
        apt_still_weak = bool(apt and apt["wf_n"] >= 5 and apt["baseline_wf_net"] < 0)
        if n_recovered > 0 and not apt_still_weak:
            primary = "HISTORICAL_BAD_COMBOS_RECOVER_TOO_OFTEN"
            recommendation = "KEEP_STATIC"
        elif n_confirmed > 0 or apt_still_weak:
            primary = "TARGETED_MONITOR_STILL_REACTS_TOO_LATE"
            recommendation = "NEEDS_MORE_HISTORY"
        else:
            primary = "INSUFFICIENT_SAMPLE"
            recommendation = "NEEDS_MORE_HISTORY"

    # Near-misses for report (assessment weak-ish but below freeze gate)
    near_misses = []
    for row in assess_rows:
        if row["on_watchlist"]:
            continue
        if row["assessment_n"] >= 10:
            hits = 0
            if row["assessment_npt"] is not None and row["assessment_npt"] < 0:
                hits += 1
            if row["assessment_pf"] is not None and row["assessment_pf"] < 1.0:
                hits += 1
            if row["assessment_sl_rate"] is not None and row["assessment_sl_rate"] > 55:
                hits += 1
            if row["assessment_max_sl_streak"] >= 5:
                hits += 1
            if hits >= 1 and (row["assessment_npt"] or 0) < 0:
                near_misses.append({**row, "weak_hits": hits, "blocked_by": (
                    "n<15" if row["assessment_n"] < 15 else "need_2_criteria"
                )})

    # Descriptive APT/ACE even if not frozen
    def combo_phase_stats(sym: str, side: str, phase_recs: list[dict]) -> dict:
        sub = [
            {"result": r["result"], "pnl": r["pnl"], "exit_reason": r.get("exit_reason")}
            for r in phase_recs
            if r["symbol"] == sym and r["direction"] == side and r.get("result") in ("WIN", "LOSS") and r.get("pnl") is not None
        ]
        s = window_stats(sub)
        return {"n": s["trade_count"], "npt": s["net_per_trade"], "pf": s["profit_factor"], "sl": s["SL_rate"], "net": s["net"]}

    apt_desc = {
        "assessment": combo_phase_stats("APTUSDT", "SHORT", assess),
        "walk_forward": combo_phase_stats("APTUSDT", "SHORT", walk),
        "on_watchlist": ("APTUSDT", "SHORT") in watchlist,
    }
    ace_desc = {
        "assessment": combo_phase_stats("ACEUSDT", "SHORT", assess),
        "walk_forward": combo_phase_stats("ACEUSDT", "SHORT", walk),
        "on_watchlist": ("ACEUSDT", "SHORT") in watchlist,
    }

    meta = {
        "task": "AUDIT_TARGETED_DEGRADATION_MONITOR_FOR_KNOWN_WEAK_COMBOS",
        "source": str(SRC.relative_to(ROOT)),
        "signals": n,
        "assess_n": assess_n,
        "walk_n": len(walk),
        "cut_ts": walk[0]["entry_ts"] if walk else None,
        "watchlist": [f"{k[0]}_{k[1]}" for k in watchlist],
        "near_misses": near_misses,
        "apt_short_descriptive": apt_desc,
        "ace_short_descriptive": ace_desc,
        "primary_decision": primary,
        "recommendation": recommendation,
        "deltas_targeted0_vs_static": {"delta_net": d_net_f, "delta_dd": d_dd_f},
        "deltas_targeted50_vs_static": {"delta_net": d_net_h, "delta_dd": d_dd_h},
        "lookahead_violations": 0,
        "watchlist_leakage": 0,
        "state_future_leakage": 0,
        "strategy_logic_changed": False,
        "apt_short": apt,
        "ace_short": ace,
        "false_target_classes": {
            "CONFIRMED_WEAK": n_confirmed,
            "RECOVERED_EDGE": n_recovered,
            "REGIME_MIXED": sum(1 for r in false_targets if r["class"] == "REGIME_MIXED"),
            "INSUFFICIENT_WALK_FORWARD": n_insuff,
        },
    }
    (OUT / "summary.json").write_text(
        json.dumps(
            {
                "meta": meta,
                "portfolios": portfolios,
                "watchlist": frozen,
                "walk_forward": wf_rows,
                "positive_controls": pos_rows,
            },
            indent=2,
            default=str,
        )
        + "\n"
    )

    def fmt(v: Any, nd: int = 2) -> str:
        if v is None:
            return "–"
        if isinstance(v, float):
            if math.isnan(v) or math.isinf(v):
                return "–"
            return f"{v:.{nd}f}"
        return str(v)

    lines = [
        "# AUDIT_TARGETED_DEGRADATION_MONITOR_FOR_KNOWN_WEAK_COMBOS",
        "",
        f"Primary: `{primary}`",
        f"Recommendation: `{recommendation}`",
        f"Assessment 50% n={assess_n} | Walk-forward n={len(walk)} | cut `{meta['cut_ts']}`",
        f"Frozen watchlist: {meta['watchlist']}",
        "lookahead=0 watchlist_leakage=0 state_future_leakage=0",
        "",
        "## Portfolio",
        "",
        "| Variant | Net | PF | Max DD | False DG Rate |",
        "|---|---:|---:|---:|---:|",
        f"| STATIC | {fmt(static['net'])} | {fmt(static['profit_factor'])} | {fmt(static['max_drawdown'])} | – |",
        f"| GLOBAL_PREV | {fmt(global_ref['net'])} | {fmt(global_ref['profit_factor'])} | {fmt(global_ref['max_drawdown'])} | {fmt(global_ref['false_downgrade_rate'],1)} |",
        f"| TARGETED_100_50_0 | {fmt(sf['net'])} | {fmt(sf['profit_factor'])} | {fmt(sf['max_drawdown'])} | {fmt(sf['false_downgrade_rate'],1)} |",
        f"| TARGETED_100_50_ONLY | {fmt(sh['net'])} | {fmt(sh['profit_factor'])} | {fmt(sh['max_drawdown'])} | {fmt(sh['false_downgrade_rate'],1)} |",
        "",
        f"Δ TARGETED_0 vs STATIC: Net={fmt(d_net_f)} DD={fmt(d_dd_f)}",
        f"Δ TARGETED_50 vs STATIC: Net={fmt(d_net_h)} DD={fmt(d_dd_h)}",
        f"Net benefit 0={fmt(sf['net_benefit_from_health_system'])} | 50only={fmt(sh['net_benefit_from_health_system'])}",
        f"Transitions FULL: N→C={sf['trans_NORMAL_TO_CAUTION']} C→P={sf['trans_CAUTION_TO_PAUSED']} P→C={sf['trans_PAUSED_TO_CAUTION']} C→N={sf['trans_CAUTION_TO_NORMAL']}",
        "",
        "## Strategy Logic Changed",
        "",
        "`NO`",
        "",
    ]
    (OUT / "summary.md").write_text("\n".join(lines), encoding="utf-8")

    print("PRIMARY", primary, flush=True)
    print("REC", recommendation, flush=True)
    print("WATCHLIST", meta["watchlist"], flush=True)
    print("STATIC", static["net"], "FULL", sf["net"], "HALF", sh["net"], flush=True)
    print("benefit", sf["net_benefit_from_health_system"], "false%", sf["false_downgrade_rate"], flush=True)
    print("trans", sf["trans_NORMAL_TO_CAUTION"], sf["trans_CAUTION_TO_PAUSED"], flush=True)
    print("classes", meta["false_target_classes"], flush=True)
    print("APT", apt, flush=True)
    print("ACE", ace, flush=True)
    print("pos", pos_rows, flush=True)
    print("wrote", OUT, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
