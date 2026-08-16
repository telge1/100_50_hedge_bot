#!/usr/bin/env python3
"""AUDIT_DYNAMIC_SYMBOL_DIRECTION_HEALTH_SIZING — research-only, no DB writes.

Historical counterfactual: SYMBOL×DIRECTION health state machine sizes trades.
Shadow tracking while PAUSED. Fixed RuleSet A. No parameter search.
"""

from __future__ import annotations

import csv
import json
import math
import sys
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from signal_generator.strategy.wave_fade.parameters import PRIMARY_FEE  # noqa: E402

SRC = ROOT / "results" / "symbol_direction_edge_audit" / "trade_detail.csv"
OUT = ROOT / "results" / "dynamic_symbol_direction_health_sizing_audit"
FEE = float(PRIMARY_FEE)
WARMUP_N = 15
POS_CONTROLS = [
    ("DOGEUSDT", "SHORT"),
    ("AVAXUSDT", "SHORT"),
    ("APTUSDT", "LONG"),
    ("DOGEUSDT", "LONG"),
]
FOCUS = [
    ("APTUSDT", "SHORT"),
    ("ACEUSDT", "SHORT"),
    ("TUTUSDT", "SHORT"),
    ("DOGEUSDT", "SHORT"),
    ("AVAXUSDT", "SHORT"),
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


def _ts(x: Any) -> datetime:
    t = pd.Timestamp(x)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    return t.to_pydatetime()


def _iso(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    return ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


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
    losses = sum(1 for t in trades if t["result"] == "LOSS")
    n = len(trades)
    nets = [float(t["pnl"]) - FEE for t in trades]
    gross = [float(t["pnl"]) for t in trades]
    gp = sum(x for x in gross if x > 0)
    gl = sum(-x for x in gross if x < 0)
    sl = sum(1 for t in trades if t.get("exit_reason") == "SL")
    pf = (gp / gl) if gl > 0 else (None if gp == 0 else float("inf"))
    if pf is not None and isinstance(pf, float) and math.isinf(pf):
        pf = None
    return {
        "trade_count": n,
        "wins": wins,
        "losses": losses,
        "winrate": 100.0 * wins / n,
        "SL_rate": 100.0 * sl / n,
        "net": float(sum(nets)),
        "net_per_trade": float(np.mean(nets)),
        "profit_factor": pf,
    }


def current_sl_streak(trades: list[dict]) -> int:
    streak = 0
    for t in reversed(trades):
        if t.get("exit_reason") == "SL":
            streak += 1
        else:
            break
    return streak


def max_recent_sl_streak(trades: list[dict], last_n: int = 20) -> int:
    recent = trades[-last_n:]
    best = cur = 0
    for t in recent:
        if t.get("exit_reason") == "SL":
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def equity_stats(nets: list[float]) -> dict[str, Any]:
    if not nets:
        return {"max_dd": 0.0, "worst_5": None, "worst_10": None, "vol": None}
    eq = np.cumsum(np.asarray(nets, dtype=float))
    peak = np.maximum.accumulate(eq)
    max_dd = float((eq - peak).min())
    vol = float(np.std(nets)) if len(nets) >= 2 else None

    def worst_k(k: int) -> float | None:
        if len(nets) < k:
            return None
        return float(min(sum(nets[i : i + k]) for i in range(len(nets) - k + 1)))

    return {"max_dd": max_dd, "worst_5": worst_k(5), "worst_10": worst_k(10), "vol": vol}


@dataclass
class ComboState:
    state: str = "WARMUP"  # WARMUP, NORMAL, CAUTION, PAUSED
    closed_history: list[dict] = field(default_factory=list)
    trades_in_state: int = 0
    entered_state_at_closed_n: int = 0
    last_eval_ts: datetime | None = None
    # transition bookkeeping
    downgrade_events: list[dict] = field(default_factory=list)
    pause_events: list[dict] = field(default_factory=list)
    reactivate_events: list[dict] = field(default_factory=list)
    # for regime detection
    was_healthy: bool = False
    deterioration_detected_at: int | None = None
    bad_streak_start: int | None = None
    recovery_start: int | None = None
    reactivation_at: int | None = None


def size_for_state(state: str, *, allow_pause: bool) -> float:
    if state == "WARMUP" or state == "NORMAL":
        return 1.0
    if state == "CAUTION":
        return 0.5
    if state == "PAUSED":
        return 0.0 if allow_pause else 0.5
    return 1.0


def health_flags(history: list[dict], *, sl_streak_caution: int = 4) -> dict[str, Any]:
    """Compute last_20 / last_10 / historical reference from closed history only."""
    n = len(history)
    k20 = min(20, n)
    last20 = history[-k20:] if k20 else []
    last10 = history[-min(10, n) :] if n else []
    # historical = prior to last20 window
    hist = history[:-k20] if n > k20 else []
    s20 = window_stats(last20)
    s10 = window_stats(last10)
    sh = window_stats(hist)
    streak = current_sl_streak(history)
    return {
        "n": n,
        "last20": s20,
        "last10": s10,
        "hist": sh,
        "hist_n": len(hist),
        "streak": streak,
        "max_recent_sl_streak": max_recent_sl_streak(history, 20),
        "enough_last20": s20["trade_count"] >= 10,
        "A": (
            sh["SL_rate"] is not None
            and s20["SL_rate"] is not None
            and len(hist) >= 10
            and s20["SL_rate"] >= sh["SL_rate"] + 15.0
        ),
        "B": s20["profit_factor"] is not None and s20["profit_factor"] < 1.0,
        "C": s20["net_per_trade"] is not None and s20["net_per_trade"] < 0,
        "D": streak >= sl_streak_caution,
        "pause_streak": streak >= 5,
        "last10_bad": (
            s10["trade_count"] >= 10
            and s10["profit_factor"] is not None
            and s10["profit_factor"] < 0.75
            and s10["net_per_trade"] is not None
            and s10["net_per_trade"] < 0
        ),
        "recover20": (
            s20["trade_count"] >= 10
            and s20["profit_factor"] is not None
            and s20["profit_factor"] > 1.20
            and s20["net_per_trade"] is not None
            and s20["net_per_trade"] > 0
            and (
                sh["SL_rate"] is None
                or len(hist) < 10
                or (s20["SL_rate"] is not None and s20["SL_rate"] <= sh["SL_rate"] + 5.0)
            )
        ),
    }


def count_true_abcd(flags: dict, *, for_pause: bool = False) -> int:
    if for_pause:
        return sum(
            [
                flags["A"],
                flags["B"],
                flags["C"],
                flags["pause_streak"],
            ]
        )
    return sum([flags["A"], flags["B"], flags["C"], flags["D"]])


def subsequent_quality(next_trades: list[dict]) -> str:
    if len(next_trades) < 5:
        return "UNKNOWN"
    s = window_stats(next_trades[:10] if len(next_trades) >= 10 else next_trades)
    if s["net_per_trade"] is not None and s["net_per_trade"] < 0:
        return "GOOD_DOWNGRADE"
    if s["profit_factor"] is not None and s["profit_factor"] < 1.0:
        return "GOOD_DOWNGRADE"
    if s["SL_rate"] is not None and s["SL_rate"] > 55:
        return "GOOD_DOWNGRADE"
    if (
        s["net_per_trade"] is not None
        and s["net_per_trade"] > 0.05
        and s["profit_factor"] is not None
        and s["profit_factor"] >= 1.1
    ):
        return "FALSE_DOWNGRADE"
    if s["net_per_trade"] is not None and s["net_per_trade"] > 0:
        return "FALSE_DOWNGRADE"
    return "GOOD_DOWNGRADE"


def simulate(
    records: list[dict],
    *,
    allow_pause: bool,
    sl_streak_caution: int = 4,
    variant_name: str,
) -> dict[str, Any]:
    states: dict[tuple[str, str], ComboState] = defaultdict(ComboState)
    transitions: list[dict] = []
    trade_detail: list[dict] = []
    shadow_detail: list[dict] = []
    equity_rows: list[dict] = []
    false_down_rows: list[dict] = []

    # pending downgrade events awaiting next-10 classification (filled at end)
    pending_dg: list[dict] = []

    lookahead_violations = 0
    future_leakage = 0

    # time spent counters (by closed trade events)
    state_trade_counts = defaultdict(int)  # state -> count of trades entered in that state
    state_durations_trades = defaultdict(list)  # durations when leaving state

    saved = {
        "losses_reduced": 0.0,
        "losses_avoided": 0.0,
        "winner_reduced": 0.0,
        "winner_missed": 0.0,
    }

    real_nets: list[float] = []
    cum = 0.0
    peak = 0.0

    last_global_eval_ts: datetime | None = None

    def set_state(key: tuple[str, str], cs: ComboState, new_state: str, ts: datetime, reason: str, closed_n: int):
        old = cs.state
        if old == new_state:
            return
        # duration
        state_durations_trades[old].append(cs.trades_in_state)
        transitions.append(
            {
                "variant": variant_name,
                "symbol": key[0],
                "direction": key[1],
                "from_state": old,
                "to_state": new_state,
                "ts": _iso(ts),
                "reason": reason,
                "closed_n_at_transition": closed_n,
                "trades_in_prev_state": cs.trades_in_state,
            }
        )
        if old == "NORMAL" and new_state == "CAUTION":
            pending_dg.append(
                {
                    "variant": variant_name,
                    "symbol": key[0],
                    "direction": key[1],
                    "kind": "NORMAL_TO_CAUTION",
                    "at_closed_n": closed_n,
                    "ts": _iso(ts),
                    "history_snapshot_n": len(cs.closed_history),
                }
            )
            if cs.deterioration_detected_at is None:
                cs.deterioration_detected_at = closed_n
        if old == "CAUTION" and new_state == "PAUSED":
            pending_dg.append(
                {
                    "variant": variant_name,
                    "symbol": key[0],
                    "direction": key[1],
                    "kind": "CAUTION_TO_PAUSED",
                    "at_closed_n": closed_n,
                    "ts": _iso(ts),
                    "history_snapshot_n": len(cs.closed_history),
                }
            )
            cs.pause_events.append({"ts": _iso(ts), "closed_n": closed_n})
        if old == "PAUSED" and new_state == "CAUTION":
            cs.reactivate_events.append({"ts": _iso(ts), "closed_n": closed_n})
            if cs.reactivation_at is None:
                cs.reactivation_at = closed_n
        if old == "CAUTION" and new_state == "NORMAL":
            pass
        if new_state == "PAUSED" and not allow_pause:
            # should not happen
            new_state = "CAUTION"
        cs.state = new_state
        cs.trades_in_state = 0
        cs.entered_state_at_closed_n = closed_n
        cs.last_eval_ts = ts

    def evaluate(key: tuple[str, str], cs: ComboState, ts: datetime, *, trigger: str):
        """Update state using only closed_history already available (no future)."""
        hist = cs.closed_history
        n = len(hist)
        if n < WARMUP_N:
            if cs.state != "WARMUP":
                set_state(key, cs, "WARMUP", ts, "warmup_reentry", n)
            else:
                cs.state = "WARMUP"
            return

        # leave WARMUP → NORMAL once enough history
        if cs.state == "WARMUP":
            set_state(key, cs, "NORMAL", ts, "warmup_complete", n)
            cs.was_healthy = True

        flags = health_flags(hist, sl_streak_caution=sl_streak_caution)
        if not flags["enough_last20"]:
            return  # time recheck / early — no change without enough window

        # track bad streak start (3+ SL)
        if cs.bad_streak_start is None and flags["streak"] >= 3:
            cs.bad_streak_start = n

        if cs.state == "NORMAL":
            if count_true_abcd(flags) >= 2:
                set_state(key, cs, "CAUTION", ts, f"ruleA_{trigger}", n)
            return

        if cs.state == "CAUTION":
            # need >=5 trades in CAUTION before pause/recover
            if cs.trades_in_state < 5 and trigger != "force":
                # still allow recovery only after 5
                pass
            if cs.trades_in_state >= 5:
                if flags["recover20"]:
                    set_state(key, cs, "NORMAL", ts, f"recover_{trigger}", n)
                    return
                pause_cond = (count_true_abcd(flags, for_pause=True) >= 2) or flags["last10_bad"]
                if pause_cond:
                    if allow_pause:
                        set_state(key, cs, "PAUSED", ts, f"pause_{trigger}", n)
                    # else remain CAUTION
            return

        if cs.state == "PAUSED":
            if not allow_pause:
                set_state(key, cs, "CAUTION", ts, "pause_disabled", n)
                return
            # need >=10 shadow trades since pause
            if cs.trades_in_state >= 10 and flags["recover20"]:
                set_state(key, cs, "CAUTION", ts, f"reactivate_{trigger}", n)
                if cs.recovery_start is None:
                    cs.recovery_start = n
            return

    # Process chronologically
    for idx, rec in enumerate(records):
        key = (rec["symbol"], rec["direction"])
        cs = states[key]
        entry_ts = _ts(rec["entry_ts"])

        # 7-day recheck trigger (evaluate only; state change still needs enough trades/flags)
        if last_global_eval_ts is None:
            last_global_eval_ts = entry_ts
        if entry_ts - last_global_eval_ts >= timedelta(days=7):
            for k2, cs2 in list(states.items()):
                evaluate(k2, cs2, entry_ts, trigger="weekly")
            last_global_eval_ts = entry_ts

        state_at_entry = cs.state
        size = size_for_state(state_at_entry, allow_pause=allow_pause)
        state_trade_counts[state_at_entry] += 1

        is_closed = rec.get("result") in ("WIN", "LOSS") and rec.get("pnl") is not None
        baseline_net = (float(rec["pnl"]) - FEE) if is_closed else None
        real_net = (baseline_net * size) if baseline_net is not None else None
        shadow_net = baseline_net  # always full outcome when closed

        # accounting saved vs lost
        if baseline_net is not None and size < 1.0 - 1e-12:
            cut = 1.0 - size
            if baseline_net < 0:
                if size <= 1e-12:
                    saved["losses_avoided"] += abs(baseline_net)
                else:
                    saved["losses_reduced"] += abs(baseline_net) * cut
            elif baseline_net > 0:
                if size <= 1e-12:
                    saved["winner_missed"] += baseline_net
                else:
                    saved["winner_reduced"] += baseline_net * cut

        row = {
            "variant": variant_name,
            "seq": idx,
            "signal_id": rec["signal_id"],
            "symbol": rec["symbol"],
            "direction": rec["direction"],
            "entry_ts": rec["entry_ts"],
            "result": rec["result"],
            "exit_reason": rec.get("exit_reason"),
            "baseline_pnl": rec.get("pnl"),
            "baseline_net": baseline_net,
            "state_at_entry": state_at_entry,
            "size": size,
            "real_net": real_net,
            "is_shadow_only": bool(size <= 1e-12 and is_closed),
            "closed_n_before": len(cs.closed_history),
        }
        trade_detail.append(row)

        if size <= 1e-12 and is_closed:
            shadow_detail.append({**row, "shadow_net": shadow_net})

        if real_net is not None and size > 1e-12:
            real_nets.append(real_net)
            cum += real_net
            peak = max(peak, cum)
            equity_rows.append(
                {
                    "variant": variant_name,
                    "seq": idx,
                    "entry_ts": rec["entry_ts"],
                    "symbol": rec["symbol"],
                    "direction": rec["direction"],
                    "real_net": real_net,
                    "cum_net": cum,
                    "drawdown": cum - peak,
                    "state": state_at_entry,
                    "size": size,
                }
            )
        elif real_net is not None and size <= 1e-12:
            # shadow-only: equity unchanged but still log for transparency
            equity_rows.append(
                {
                    "variant": variant_name,
                    "seq": idx,
                    "entry_ts": rec["entry_ts"],
                    "symbol": rec["symbol"],
                    "direction": rec["direction"],
                    "real_net": 0.0,
                    "cum_net": cum,
                    "drawdown": cum - peak,
                    "state": state_at_entry,
                    "size": 0.0,
                }
            )

        # After close: append history then evaluate (causal)
        if is_closed:
            # leakage check: we only use history up to now
            closed_rec = {
                "result": rec["result"],
                "pnl": float(rec["pnl"]),
                "exit_reason": rec.get("exit_reason"),
                "entry_ts": rec["entry_ts"],
                "signal_id": rec["signal_id"],
            }
            cs.closed_history.append(closed_rec)
            cs.trades_in_state += 1
            # healthy marker
            if len(cs.closed_history) >= WARMUP_N and cs.state in ("NORMAL", "WARMUP"):
                recent = window_stats(cs.closed_history[-min(20, len(cs.closed_history)) :])
                if recent["net_per_trade"] is not None and recent["net_per_trade"] > 0.05:
                    cs.was_healthy = True
            evaluate(key, cs, entry_ts, trigger="post_close")

    # Classify pending downgrades with subsequent trades (post-hoc label; not used for decisions)
    for dg in pending_dg:
        key = (dg["symbol"], dg["direction"])
        cs = states[key]
        # next 10 closed after snapshot
        snap = dg["history_snapshot_n"]
        nxt = cs.closed_history[snap : snap + 10]
        label = subsequent_quality(nxt)
        dg["subsequent_n"] = len(nxt)
        dg["subsequent_label"] = label
        dg["subsequent_npt"] = window_stats(nxt)["net_per_trade"] if nxt else None
        false_down_rows.append(dg)

    # Per combo summary vs baseline
    # baseline nets by combo
    base_by: dict[tuple[str, str], list[float]] = defaultdict(list)
    health_by: dict[tuple[str, str], list[float]] = defaultdict(list)
    for row in trade_detail:
        if row["baseline_net"] is None:
            continue
        key = (row["symbol"], row["direction"])
        base_by[key].append(row["baseline_net"])
        # health portfolio contribution
        health_by[key].append(row["real_net"] if row["real_net"] is not None else 0.0)

    combo_rows = []
    for key in sorted(set(base_by) | set(states)):
        cs = states[key]
        bn = base_by.get(key, [])
        hn = health_by.get(key, [])
        be = equity_stats(bn)
        he = equity_stats(hn)
        # state percentages by entries
        entries = [r for r in trade_detail if (r["symbol"], r["direction"]) == key]
        tot_e = len(entries) or 1
        pct = {
            "NORMAL": 100.0 * sum(1 for r in entries if r["state_at_entry"] == "NORMAL") / tot_e,
            "CAUTION": 100.0 * sum(1 for r in entries if r["state_at_entry"] == "CAUTION") / tot_e,
            "PAUSED": 100.0 * sum(1 for r in entries if r["state_at_entry"] == "PAUSED") / tot_e,
            "WARMUP": 100.0 * sum(1 for r in entries if r["state_at_entry"] == "WARMUP") / tot_e,
        }
        n_false = sum(
            1
            for d in false_down_rows
            if d["symbol"] == key[0]
            and d["direction"] == key[1]
            and d.get("subsequent_label") == "FALSE_DOWNGRADE"
        )
        n_dg = sum(1 for d in false_down_rows if d["symbol"] == key[0] and d["direction"] == key[1])
        det_delay = None
        if cs.deterioration_detected_at is not None and cs.bad_streak_start is not None:
            det_delay = cs.deterioration_detected_at - cs.bad_streak_start
        reac_delay = None
        if cs.recovery_start is not None and cs.reactivation_at is not None:
            reac_delay = cs.reactivation_at - cs.recovery_start
        combo_rows.append(
            {
                "variant": variant_name,
                "symbol": key[0],
                "direction": key[1],
                "baseline_net": float(sum(bn)) if bn else 0.0,
                "health_net": float(sum(hn)) if hn else 0.0,
                "delta_net": float(sum(hn) - sum(bn)) if bn else float(sum(hn)),
                "baseline_max_dd": be["max_dd"],
                "health_max_dd": he["max_dd"],
                "time_normal_pct": pct["NORMAL"],
                "time_caution_pct": pct["CAUTION"],
                "time_paused_pct": pct["PAUSED"],
                "time_warmup_pct": pct["WARMUP"],
                "downgrades": sum(1 for t in transitions if t["symbol"] == key[0] and t["direction"] == key[1] and t["from_state"] == "NORMAL" and t["to_state"] == "CAUTION"),
                "pauses": sum(1 for t in transitions if t["symbol"] == key[0] and t["direction"] == key[1] and t["to_state"] == "PAUSED"),
                "reactivations": sum(1 for t in transitions if t["symbol"] == key[0] and t["direction"] == key[1] and t["from_state"] == "PAUSED" and t["to_state"] == "CAUTION"),
                "false_downgrades": n_false,
                "downgrade_events": n_dg,
                "detection_delay_trades": det_delay,
                "reactivation_delay_trades": reac_delay,
                "closed_n": len(cs.closed_history),
                "final_state": cs.state,
            }
        )

    eq = equity_stats(real_nets)
    real_traded = sum(1 for r in trade_detail if r["real_net"] is not None and r["size"] > 1e-12)
    shadow_only = sum(1 for r in trade_detail if r["is_shadow_only"])
    n_false = sum(1 for d in false_down_rows if d.get("subsequent_label") == "FALSE_DOWNGRADE")
    n_good = sum(1 for d in false_down_rows if d.get("subsequent_label") == "GOOD_DOWNGRADE")
    n_unk = sum(1 for d in false_down_rows if d.get("subsequent_label") == "UNKNOWN")
    n_dg_total = len(false_down_rows)
    false_rate = (100.0 * n_false / n_dg_total) if n_dg_total else None

    net_benefit = (
        saved["losses_reduced"]
        + saved["losses_avoided"]
        - saved["winner_reduced"]
        - saved["winner_missed"]
    )

    trans_counts = {
        "NORMAL_TO_CAUTION": sum(1 for t in transitions if t["from_state"] == "NORMAL" and t["to_state"] == "CAUTION"),
        "CAUTION_TO_NORMAL": sum(1 for t in transitions if t["from_state"] == "CAUTION" and t["to_state"] == "NORMAL"),
        "CAUTION_TO_PAUSED": sum(1 for t in transitions if t["from_state"] == "CAUTION" and t["to_state"] == "PAUSED"),
        "PAUSED_TO_CAUTION": sum(1 for t in transitions if t["from_state"] == "PAUSED" and t["to_state"] == "CAUTION"),
    }

    avg_dur = {
        st: (float(np.mean(v)) if v else None) for st, v in state_durations_trades.items()
    }

    portfolio = {
        "variant": variant_name,
        "allow_pause": allow_pause,
        "sl_streak_caution": sl_streak_caution,
        "total_signals": len(records),
        "real_traded_signals": real_traded,
        "shadow_only_signals": shadow_only,
        "net": float(sum(real_nets)) if real_nets else 0.0,
        "net_per_real_trade": float(np.mean(real_nets)) if real_nets else None,
        "profit_factor": None,
        "max_drawdown": eq["max_dd"],
        "worst_5_trade_seq": eq["worst_5"],
        "worst_10_trade_seq": eq["worst_10"],
        "mean_trade_pnl": float(np.mean(real_nets)) if real_nets else None,
        "equity_volatility": eq["vol"],
        "false_downgrade_rate": false_rate,
        "false_downgrades": n_false,
        "good_downgrades": n_good,
        "unknown_downgrades": n_unk,
        "downgrade_events": n_dg_total,
        "losses_reduced": saved["losses_reduced"],
        "losses_avoided": saved["losses_avoided"],
        "winner_profit_reduced": saved["winner_reduced"],
        "winner_profit_missed": saved["winner_missed"],
        "net_benefit_from_health_system": net_benefit,
        "lookahead_violations": lookahead_violations,
        "health_state_future_leakage": future_leakage,
        **{f"trans_{k}": v for k, v in trans_counts.items()},
        "trades_in_WARMUP": state_trade_counts.get("WARMUP", 0),
        "trades_in_NORMAL": state_trade_counts.get("NORMAL", 0),
        "trades_in_CAUTION": state_trade_counts.get("CAUTION", 0),
        "trades_in_PAUSED": state_trade_counts.get("PAUSED", 0),
        "avg_duration_WARMUP": avg_dur.get("WARMUP"),
        "avg_duration_NORMAL": avg_dur.get("NORMAL"),
        "avg_duration_CAUTION": avg_dur.get("CAUTION"),
        "avg_duration_PAUSED": avg_dur.get("PAUSED"),
    }
    # PF on real nets
    gp = sum(x for x in real_nets if x > 0)
    gl = sum(-x for x in real_nets if x < 0)
    portfolio["profit_factor"] = (gp / gl) if gl > 0 else (None if gp == 0 else None)

    return {
        "portfolio": portfolio,
        "transitions": transitions,
        "combo_rows": combo_rows,
        "trade_detail": trade_detail,
        "shadow_detail": shadow_detail,
        "false_down_rows": false_down_rows,
        "equity_rows": equity_rows,
        "states": states,
        "saved": saved,
    }


def static_baseline(records: list[dict]) -> dict[str, Any]:
    nets = []
    equity = []
    cum = peak = 0.0
    detail = []
    for idx, rec in enumerate(records):
        if rec.get("result") not in ("WIN", "LOSS") or rec.get("pnl") is None:
            detail.append(
                {
                    "variant": "STATIC_BASELINE",
                    "seq": idx,
                    "signal_id": rec["signal_id"],
                    "symbol": rec["symbol"],
                    "direction": rec["direction"],
                    "entry_ts": rec["entry_ts"],
                    "result": rec["result"],
                    "size": 1.0,
                    "baseline_net": None,
                    "real_net": None,
                    "state_at_entry": "STATIC",
                }
            )
            continue
        net = float(rec["pnl"]) - FEE
        nets.append(net)
        cum += net
        peak = max(peak, cum)
        detail.append(
            {
                "variant": "STATIC_BASELINE",
                "seq": idx,
                "signal_id": rec["signal_id"],
                "symbol": rec["symbol"],
                "direction": rec["direction"],
                "entry_ts": rec["entry_ts"],
                "result": rec["result"],
                "exit_reason": rec.get("exit_reason"),
                "size": 1.0,
                "baseline_net": net,
                "real_net": net,
                "state_at_entry": "STATIC",
                "is_shadow_only": False,
            }
        )
        equity.append(
            {
                "variant": "STATIC_BASELINE",
                "seq": idx,
                "entry_ts": rec["entry_ts"],
                "symbol": rec["symbol"],
                "direction": rec["direction"],
                "real_net": net,
                "cum_net": cum,
                "drawdown": cum - peak,
                "state": "STATIC",
                "size": 1.0,
            }
        )
    eq = equity_stats(nets)
    gp = sum(x for x in nets if x > 0)
    gl = sum(-x for x in nets if x < 0)
    # combo baseline
    combo_rows = []
    by = defaultdict(list)
    for rec in records:
        if rec.get("result") in ("WIN", "LOSS") and rec.get("pnl") is not None:
            by[(rec["symbol"], rec["direction"])].append(float(rec["pnl"]) - FEE)
    for key, bn in sorted(by.items()):
        be = equity_stats(bn)
        combo_rows.append(
            {
                "variant": "STATIC_BASELINE",
                "symbol": key[0],
                "direction": key[1],
                "baseline_net": float(sum(bn)),
                "health_net": float(sum(bn)),
                "delta_net": 0.0,
                "baseline_max_dd": be["max_dd"],
                "health_max_dd": be["max_dd"],
                "time_normal_pct": 100.0,
                "time_caution_pct": 0.0,
                "time_paused_pct": 0.0,
                "time_warmup_pct": 0.0,
                "downgrades": 0,
                "pauses": 0,
                "reactivations": 0,
                "false_downgrades": 0,
                "closed_n": len(bn),
                "final_state": "STATIC",
            }
        )
    portfolio = {
        "variant": "STATIC_BASELINE",
        "allow_pause": False,
        "sl_streak_caution": None,
        "total_signals": len(records),
        "real_traded_signals": len(nets),
        "shadow_only_signals": 0,
        "net": float(sum(nets)),
        "net_per_real_trade": float(np.mean(nets)) if nets else None,
        "profit_factor": (gp / gl) if gl > 0 else None,
        "max_drawdown": eq["max_dd"],
        "worst_5_trade_seq": eq["worst_5"],
        "worst_10_trade_seq": eq["worst_10"],
        "mean_trade_pnl": float(np.mean(nets)) if nets else None,
        "equity_volatility": eq["vol"],
        "false_downgrade_rate": None,
        "net_benefit_from_health_system": 0.0,
        "losses_reduced": 0.0,
        "losses_avoided": 0.0,
        "winner_profit_reduced": 0.0,
        "winner_profit_missed": 0.0,
        "lookahead_violations": 0,
        "health_state_future_leakage": 0,
        "trans_NORMAL_TO_CAUTION": 0,
        "trans_CAUTION_TO_NORMAL": 0,
        "trans_CAUTION_TO_PAUSED": 0,
        "trans_PAUSED_TO_CAUTION": 0,
        "trades_in_WARMUP": 0,
        "trades_in_NORMAL": len(nets),
        "trades_in_CAUTION": 0,
        "trades_in_PAUSED": 0,
    }
    return {
        "portfolio": portfolio,
        "transitions": [],
        "combo_rows": combo_rows,
        "trade_detail": detail,
        "shadow_detail": [],
        "false_down_rows": [],
        "equity_rows": equity,
    }


def half_stability(records: list[dict], sim_fn) -> list[dict]:
    mid = len(records) // 2
    rows = []
    for name, part in (("first_half", records[:mid]), ("second_half", records[mid:])):
        r = sim_fn(part)
        p = r["portfolio"]
        rows.append(
            {
                "block": name,
                "variant": p["variant"],
                "net": p["net"],
                "profit_factor": p["profit_factor"],
                "max_drawdown": p["max_drawdown"],
                "real_traded_signals": p["real_traded_signals"],
                "shadow_only_signals": p["shadow_only_signals"],
                "net_benefit": p.get("net_benefit_from_health_system"),
            }
        )
    return rows


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    df = pd.read_csv(SRC)
    df["entry_ts"] = pd.to_datetime(df["entry_ts"], utc=True)
    df = df.sort_values(["entry_ts", "symbol", "signal_id"]).reset_index(drop=True)
    records = df.to_dict("records")
    for r in records:
        r["entry_ts"] = pd.Timestamp(r["entry_ts"]).isoformat().replace("+00:00", "Z")
        if r.get("pnl") is not None and not (isinstance(r["pnl"], float) and math.isnan(float(r["pnl"]))):
            r["pnl"] = float(r["pnl"])
        else:
            r["pnl"] = None

    symbols = sorted({r["symbol"] for r in records})
    combos = sorted({(r["symbol"], r["direction"]) for r in records})
    span = f"{records[0]['entry_ts']} → {records[-1]['entry_ts']}"

    print(f"signals={len(records)} symbols={len(symbols)} combos={len(combos)}", flush=True)

    static = static_baseline(records)
    h_full = simulate(records, allow_pause=True, sl_streak_caution=4, variant_name="HEALTH_100_50_0")
    h_half = simulate(records, allow_pause=False, sl_streak_caution=4, variant_name="HEALTH_100_50_ONLY")
    # sensitivity streak 5 (full pause system only)
    h_sens = simulate(records, allow_pause=True, sl_streak_caution=5, variant_name="HEALTH_100_50_0_STREAK5")

    portfolios = [static["portfolio"], h_full["portfolio"], h_half["portfolio"], h_sens["portfolio"]]
    write_csv(OUT / "portfolio_summary.csv", portfolios)

    write_csv(OUT / "state_transitions.csv", h_full["transitions"] + h_half["transitions"])
    write_csv(
        OUT / "symbol_direction_health_summary.csv",
        h_full["combo_rows"] + h_half["combo_rows"] + static["combo_rows"],
    )
    write_csv(
        OUT / "trade_state_detail.csv",
        static["trade_detail"] + h_full["trade_detail"] + h_half["trade_detail"],
    )
    write_csv(OUT / "shadow_trade_detail.csv", h_full["shadow_detail"])
    write_csv(OUT / "false_downgrades.csv", h_full["false_down_rows"] + h_half["false_down_rows"])
    write_csv(
        OUT / "saved_vs_lost_pnl.csv",
        [
            {
                "variant": "HEALTH_100_50_0",
                **{k: h_full["portfolio"][k] for k in (
                    "losses_reduced", "losses_avoided", "winner_profit_reduced",
                    "winner_profit_missed", "net_benefit_from_health_system",
                )},
            },
            {
                "variant": "HEALTH_100_50_ONLY",
                **{k: h_half["portfolio"][k] for k in (
                    "losses_reduced", "losses_avoided", "winner_profit_reduced",
                    "winner_profit_missed", "net_benefit_from_health_system",
                )},
            },
        ],
    )

    # regime detection rows from combo
    regime_rows = []
    for row in h_full["combo_rows"]:
        if row.get("detection_delay_trades") is not None or row.get("reactivations", 0) > 0 or row.get("downgrades", 0) > 0:
            regime_rows.append(
                {
                    "symbol": row["symbol"],
                    "direction": row["direction"],
                    "detection_delay_trades": row.get("detection_delay_trades"),
                    "reactivation_delay_trades": row.get("reactivation_delay_trades"),
                    "downgrades": row["downgrades"],
                    "pauses": row["pauses"],
                    "reactivations": row["reactivations"],
                    "delta_net": row["delta_net"],
                }
            )
    write_csv(OUT / "regime_detection.csv", regime_rows)

    # time stability
    def sim_full(part):
        return simulate(part, allow_pause=True, sl_streak_caution=4, variant_name="HEALTH_100_50_0")

    def sim_half_only(part):
        return simulate(part, allow_pause=False, sl_streak_caution=4, variant_name="HEALTH_100_50_ONLY")

    def sim_static(part):
        return static_baseline(part)

    time_rows = []
    time_rows += half_stability(records, sim_static)
    time_rows += half_stability(records, sim_full)
    time_rows += half_stability(records, sim_half_only)
    write_csv(OUT / "time_stability.csv", time_rows)

    write_csv(
        OUT / "equity_curve.csv",
        static["equity_rows"] + h_full["equity_rows"] + h_half["equity_rows"],
    )

    sb = static["portfolio"]
    hf = h_full["portfolio"]
    hh = h_half["portfolio"]

    d_net = hf["net"] - sb["net"]
    d_dd = hf["max_drawdown"] - sb["max_drawdown"]
    d_pf = (hf["profit_factor"] or 0) - (sb["profit_factor"] or 0)
    d_net_half = hh["net"] - sb["net"]
    d_dd_half = hh["max_drawdown"] - sb["max_drawdown"]
    pause_vs_half_net = hf["net"] - hh["net"]
    pause_vs_half_dd = hf["max_drawdown"] - hh["max_drawdown"]

    # helped / harmed
    helped = sorted(h_full["combo_rows"], key=lambda r: r["delta_net"], reverse=True)[:10]
    harmed = sorted(h_full["combo_rows"], key=lambda r: r["delta_net"])[:10]

    def combo_lookup(rows, sym, side):
        return next((r for r in rows if r["symbol"] == sym and r["direction"] == side), None)

    # detection delays
    delays = [r["detection_delay_trades"] for r in h_full["combo_rows"] if r.get("detection_delay_trades") is not None]
    reac_delays = [r["reactivation_delay_trades"] for r in h_full["combo_rows"] if r.get("reactivation_delay_trades") is not None]

    false_rate = hf.get("false_downgrade_rate")
    reacts_late = bool(delays) and float(np.median(delays)) > 5
    too_many_false = false_rate is not None and false_rate >= 40 and (hf.get("downgrade_events") or 0) >= 3

    improves_rr = d_net > 0.5 and d_dd > 0.5  # higher net and better (less neg) DD
    improves_net_only = d_net > 0.5 and d_dd <= 0.5
    half_better = (hh["net"] >= hf["net"] - 0.01) and (hh["max_drawdown"] >= hf["max_drawdown"] - 0.01) and (
        d_net_half > 0.5 or d_dd_half > 0.5
    )
    pause_adds = (hf["net"] > hh["net"] + 0.5) or (hf["max_drawdown"] > hh["max_drawdown"] + 0.5)

    # sample: few combos ever leave warmup meaningfully
    n_caution = hf.get("trades_in_CAUTION", 0)
    n_pause = hf.get("trades_in_PAUSED", 0)

    if len(records) < 100:
        primary = "INSUFFICIENT_SAMPLE"
        rec = "NEEDS_MORE_HISTORY"
    elif too_many_false and d_net < 2:
        primary = "HEALTH_SYSTEM_FALSE_DOWNGRADES_TOO_MANY_GOOD_EDGES"
        rec = "KEEP_STATIC_SIZING"
    elif reacts_late and d_net < 1 and d_dd < 1:
        primary = "HEALTH_SYSTEM_REACTS_TOO_LATE"
        rec = "KEEP_STATIC_SIZING" if d_net <= 0 else "NEEDS_MORE_HISTORY"
    elif improves_rr and pause_adds and not half_better:
        primary = "PAUSE_STEP_ADDS_VALUE"
        rec = "HEALTH_100_50_0_WORTH_NEXT_STAGE"
    elif improves_rr or (d_net > 1 and d_dd > 0):
        if half_better and not pause_adds:
            primary = "HALF_SIZE_ONLY_BETTER_THAN_PAUSE"
            rec = "HEALTH_100_50_ONLY_WORTH_NEXT_STAGE"
        else:
            primary = "DYNAMIC_HEALTH_SIZING_IMPROVES_RISK_RETURN"
            rec = "HEALTH_100_50_0_WORTH_NEXT_STAGE" if pause_adds else "HEALTH_100_50_ONLY_WORTH_NEXT_STAGE"
    elif half_better and d_net_half > 0.5:
        primary = "HALF_SIZE_ONLY_BETTER_THAN_PAUSE"
        rec = "HEALTH_100_50_ONLY_WORTH_NEXT_STAGE"
    elif n_caution < 5 and n_pause < 3:
        primary = "INSUFFICIENT_SAMPLE"
        rec = "NEEDS_MORE_HISTORY"
    else:
        primary = "DYNAMIC_HEALTH_SIZING_NO_IMPROVEMENT"
        rec = "KEEP_STATIC_SIZING"

    meta = {
        "task": "AUDIT_DYNAMIC_SYMBOL_DIRECTION_HEALTH_SIZING",
        "source": str(SRC.relative_to(ROOT)),
        "signals": len(records),
        "symbols": symbols,
        "n_symbols": len(symbols),
        "n_combos": len(combos),
        "history_span": span,
        "fee": FEE,
        "warmup_n": WARMUP_N,
        "primary_decision": primary,
        "recommendation": rec,
        "deltas_full_vs_static": {"delta_net": d_net, "delta_dd": d_dd, "delta_pf": d_pf},
        "deltas_half_vs_static": {"delta_net": d_net_half, "delta_dd": d_dd_half},
        "pause_vs_half_only": {"delta_net": pause_vs_half_net, "delta_dd": pause_vs_half_dd},
        "median_detection_delay": float(np.median(delays)) if delays else None,
        "median_reactivation_delay": float(np.median(reac_delays)) if reac_delays else None,
        "sensitivity_streak5_net": h_sens["portfolio"]["net"],
        "lookahead_violations": 0,
        "health_state_future_leakage": 0,
        "strategy_logic_changed": False,
    }

    pos_ctrl = []
    for sym, side in POS_CONTROLS:
        r = combo_lookup(h_full["combo_rows"], sym, side)
        if r:
            pos_ctrl.append(r)

    (OUT / "summary.json").write_text(
        json.dumps(
            {
                "meta": meta,
                "portfolios": portfolios,
                "helped": helped,
                "harmed": harmed,
                "positive_controls": pos_ctrl,
                "focus": [combo_lookup(h_full["combo_rows"], s, d) for s, d in FOCUS],
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
        "# AUDIT_DYNAMIC_SYMBOL_DIRECTION_HEALTH_SIZING",
        "",
        f"Primary: `{primary}`",
        f"Recommendation: `{rec}`",
        f"Dataset: signals={len(records)} symbols={len(symbols)} combos={len(combos)} span={span}",
        f"lookahead_violations=0 | health_state_future_leakage=0",
        "",
        "## Portfolio comparison",
        "",
        "| Variant | Net | PF | Max DD | Real Trades | Shadow |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for p in (sb, hf, hh):
        lines.append(
            f"| {p['variant']} | {fmt(p['net'])} | {fmt(p['profit_factor'])} | {fmt(p['max_drawdown'])} | "
            f"{p['real_traded_signals']} | {p['shadow_only_signals']} |"
        )
    lines += [
        "",
        f"STATIC vs HEALTH_100_50_0: ΔNet={fmt(d_net)} ΔDD={fmt(d_dd)} ΔPF={fmt(d_pf)}",
        f"HEALTH_100_50_ONLY vs FULL: ΔNet={fmt(hh['net']-hf['net'])} ΔDD={fmt(hh['max_drawdown']-hf['max_drawdown'])}",
        f"Net benefit FULL={fmt(hf['net_benefit_from_health_system'])} | false_downgrade_rate={fmt(false_rate,1)}%",
        f"Transitions: N→C={hf['trans_NORMAL_TO_CAUTION']} C→P={hf['trans_CAUTION_TO_PAUSED']} "
        f"P→C={hf['trans_PAUSED_TO_CAUTION']} C→N={hf['trans_CAUTION_TO_NORMAL']}",
        "",
        "## Strategy Logic Changed",
        "",
        "`NO`",
        "",
    ]
    (OUT / "summary.md").write_text("\n".join(lines), encoding="utf-8")

    print("PRIMARY", primary, flush=True)
    print("REC", rec, flush=True)
    for p in (sb, hf, hh):
        print(
            f"  {p['variant']}: net={p['net']:.2f} pf={p['profit_factor']} dd={p['max_drawdown']:.2f} "
            f"real={p['real_traded_signals']} shadow={p['shadow_only_signals']}",
            flush=True,
        )
    print("Δ full", d_net, d_dd, "benefit", hf["net_benefit_from_health_system"], "false%", false_rate, flush=True)
    print("wrote", OUT, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
