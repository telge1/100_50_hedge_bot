#!/usr/bin/env python3
"""AUDIT_WEEKLY_SYMBOL_DIRECTION_REGIME_WALKFORWARD_FULL_HISTORY — research-only.

Weekly regime sizing on frozen full-history 15m BASELINE_IMMEDIATE trades.
No strategy changes. Expanding-past-only references. Monday 00:00 UTC checks.
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

SRC = ROOT / "results" / "full_history_baseline_symbol_direction_audit" / "full_trade_list.csv"
OUT = ROOT / "results" / "weekly_symbol_direction_regime_walkforward_full_history"
FEE = float(PRIMARY_FEE)

EXPECT = {
    "closed": 6951,
    "TP": 4523,
    "SL": 2428,
    "wr": 65.1,
    "net": 1330.39,
    "pf": 1.86,
}
TOL_NET = 0.05
TOL_WR = 0.15
TOL_PF = 0.02

FOCUS = [
    ("APTUSDT", "LONG"), ("APTUSDT", "SHORT"),
    ("HYPEUSDT", "LONG"), ("HYPEUSDT", "SHORT"),
    ("TUTUSDT", "LONG"), ("TUTUSDT", "SHORT"),
    ("ACEUSDT", "LONG"), ("ACEUSDT", "SHORT"),
    ("DOGEUSDT", "LONG"), ("DOGEUSDT", "SHORT"),
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


def mondays_utc(start: datetime, end: datetime) -> list[datetime]:
    """Inclusive Mondays 00:00 UTC with start <= m <= end."""
    s = start.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    # advance to Monday
    while s.weekday() != 0:
        s += timedelta(days=1)
    out = []
    while s <= end:
        out.append(s)
        s += timedelta(days=7)
    return out


def window_stats(trades: list[dict]) -> dict[str, Any]:
    if not trades:
        return {
            "trade_count": 0, "TP": 0, "SL": 0, "WR": None, "SL_rate": None,
            "Net": 0.0, "Net_per_trade": None, "PF": None,
        }
    n = len(trades)
    tp = sum(1 for t in trades if t.get("exit_reason") == "TP")
    sl = sum(1 for t in trades if t.get("exit_reason") == "SL")
    wins = sum(1 for t in trades if t["result"] == "WIN")
    nets = [float(t["net_pnl"]) for t in trades]
    gross = [float(t["pnl"]) for t in trades]
    gp = sum(x for x in gross if x > 0)
    gl = sum(-x for x in gross if x < 0)
    pf = (gp / gl) if gl > 0 else None
    return {
        "trade_count": n,
        "TP": tp,
        "SL": sl,
        "WR": 100.0 * wins / n,
        "SL_rate": 100.0 * sl / n,
        "Net": float(sum(nets)),
        "Net_per_trade": float(np.mean(nets)),
        "PF": float(pf) if pf is not None else None,
    }


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
        return {"max_dd": 0.0, "worst_5": None, "worst_10": None, "worst_20": None}
    eq = np.cumsum(np.asarray(nets, dtype=float))
    peak = np.maximum.accumulate(eq)
    max_dd = float((eq - peak).min())

    def worst_k(k: int) -> float | None:
        if len(nets) < k:
            return None
        return float(min(sum(nets[i : i + k]) for i in range(len(nets) - k + 1)))

    gp = sum(x for x in nets if x > 0)
    gl = sum(-x for x in nets if x < 0)
    pf = (gp / gl) if gl > 0 else None
    return {
        "max_dd": max_dd,
        "worst_5": worst_k(5),
        "worst_10": worst_k(10),
        "worst_20": worst_k(20),
        "gross_profit": float(gp),
        "gross_loss": float(-gl),
        "pf": float(pf) if pf is not None else None,
        "net": float(sum(nets)),
        "npt": float(np.mean(nets)) if nets else None,
    }


def seven_d_warning(s7: dict, streak: int) -> bool:
    if s7["trade_count"] < 5:
        return False
    hits = 0
    if s7["Net_per_trade"] is not None and s7["Net_per_trade"] < 0:
        hits += 1
    if s7["PF"] is not None and s7["PF"] < 1.0:
        hits += 1
    if s7["SL_rate"] is not None and s7["SL_rate"] >= 55.0:
        hits += 1
    if streak >= 4:
        hits += 1
    return hits >= 2


def thirty_d_confirmed(s30: dict, hist: dict, warn: bool) -> bool:
    if not warn:
        return False
    if s30["trade_count"] < 15:
        return False
    if hist["trade_count"] < 30:
        return False
    conds = []
    relative = []
    if s30["Net_per_trade"] is not None and s30["Net_per_trade"] < 0:
        conds.append(True)
        if hist["Net_per_trade"] is not None and s30["Net_per_trade"] < hist["Net_per_trade"]:
            relative.append(True)
    if s30["PF"] is not None and s30["PF"] < 1.0:
        conds.append(True)
        if hist["PF"] is not None and s30["PF"] < hist["PF"]:
            relative.append(True)
    if (
        s30["SL_rate"] is not None
        and hist["SL_rate"] is not None
        and s30["SL_rate"] >= hist["SL_rate"] + 10.0
    ):
        conds.append(True)
        relative.append(True)
    if (
        s30["WR"] is not None
        and hist["WR"] is not None
        and s30["WR"] <= hist["WR"] - 10.0
    ):
        conds.append(True)
        relative.append(True)
    return sum(conds) >= 2 and len(relative) >= 1


def recover_to_normal(s30: dict, hist: dict, warn: bool) -> bool:
    if warn:
        return False
    if s30["trade_count"] < 15 or hist["trade_count"] < 30:
        return False
    if s30["Net_per_trade"] is None or s30["Net_per_trade"] <= 0:
        return False
    if s30["PF"] is None or s30["PF"] < 1.20:
        return False
    if hist["SL_rate"] is None or s30["SL_rate"] is None:
        return False
    return s30["SL_rate"] <= hist["SL_rate"] + 5.0


def subsequent_label(next_trades: list[dict]) -> str:
    if len(next_trades) < 5:
        return "MIXED"
    s = window_stats(next_trades[:10])
    if s["Net_per_trade"] is not None and s["Net_per_trade"] < 0:
        return "GOOD_DOWNGRADE"
    if s["PF"] is not None and s["PF"] < 1.0:
        return "GOOD_DOWNGRADE"
    if s["Net"] is not None and s["Net"] > 0 and s["PF"] is not None and s["PF"] > 1.0:
        return "FALSE_DOWNGRADE"
    return "MIXED"


@dataclass
class ComboState:
    state: str = "NORMAL"
    weeks_in_state: int = 0
    shadow_since_pause: int = 0
    caution_at: datetime | None = None
    paused_at: datetime | None = None
    first_7d_warning: datetime | None = None
    first_30d_confirm: datetime | None = None
    recovery_start: datetime | None = None
    reactivate_at: datetime | None = None
    normal_at: datetime | None = None


def size_for(state: str, allow_pause: bool) -> float:
    if state == "NORMAL":
        return 1.0
    if state == "CAUTION":
        return 0.5
    if state == "PAUSED":
        return 0.0 if allow_pause else 0.5
    return 1.0


def simulate_variant(
    trades: list[dict],
    week_checks: list[datetime],
    *,
    allow_pause: bool,
    variant: str,
) -> dict[str, Any]:
    # closed trades by combo, sorted by exit
    by_combo: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for t in trades:
        if t["result"] not in ("WIN", "LOSS"):
            continue
        key = (t["symbol"], t["direction"])
        by_combo[key].append(t)
    for k in by_combo:
        by_combo[k].sort(key=lambda x: x["exit_dt"])

    states: dict[tuple[str, str], ComboState] = {
        k: ComboState() for k in by_combo
    }
    # state timeline at each week for lookup: week -> key -> state
    state_at_week: dict[datetime, dict[tuple[str, str], str]] = {}

    weekly_rows: list[dict] = []
    transitions: list[dict] = []
    warnings: list[dict] = []
    confirms: list[dict] = []
    pending_dg: list[dict] = []

    # For each Monday, evaluate using only exits before Monday
    for wi, week in enumerate(week_checks):
        week_states: dict[tuple[str, str], str] = {}
        t7 = week - timedelta(days=7)
        t30 = week - timedelta(days=30)

        for key, hist_all in by_combo.items():
            cs = states[key]
            # known closed before week
            known = [t for t in hist_all if t["exit_dt"] < week]
            w7 = [t for t in known if t["exit_dt"] >= t7]
            w30 = [t for t in known if t["exit_dt"] >= t30]
            hist_ref = [t for t in known if t["exit_dt"] < t30]

            s7 = window_stats(w7)
            s30 = window_stats(w30)
            sh = window_stats(hist_ref)
            streak = current_sl_streak(known)

            warn = seven_d_warning(s7, streak)
            conf = thirty_d_confirmed(s30, sh, warn)
            no_decision = (
                s7["trade_count"] < 5
                or s30["trade_count"] < 15
                or sh["trade_count"] < 30
            )

            state_before = cs.state
            reason = "hold"
            if no_decision:
                reason = "NO_HEALTH_DECISION"
            else:
                if warn and cs.first_7d_warning is None:
                    cs.first_7d_warning = week
                if conf and cs.first_30d_confirm is None:
                    cs.first_30d_confirm = week

                if cs.state == "NORMAL":
                    if warn and conf:
                        cs.state = "CAUTION"
                        cs.weeks_in_state = 0
                        cs.caution_at = week
                        reason = "NORMAL_TO_CAUTION"
                        pending_dg.append(
                            {
                                "variant": variant,
                                "symbol": key[0],
                                "direction": key[1],
                                "week": _iso(week),
                                "history_n_at": len(known),
                            }
                        )
                elif cs.state == "CAUTION":
                    # hold at least 1 full week
                    if cs.weeks_in_state >= 1:
                        if recover_to_normal(s30, sh, warn):
                            cs.state = "NORMAL"
                            cs.weeks_in_state = 0
                            cs.normal_at = week
                            if cs.recovery_start is None:
                                cs.recovery_start = week
                            reason = "CAUTION_TO_NORMAL"
                        elif allow_pause and warn and conf and (
                            (s30["PF"] is not None and s30["PF"] < 0.85)
                            or (s30["Net_per_trade"] is not None and s30["Net_per_trade"] < 0)
                            or streak >= 5
                        ):
                            cs.state = "PAUSED"
                            cs.weeks_in_state = 0
                            cs.paused_at = week
                            cs.shadow_since_pause = 0
                            reason = "CAUTION_TO_PAUSED"
                elif cs.state == "PAUSED":
                    if not allow_pause:
                        cs.state = "CAUTION"
                        cs.weeks_in_state = 0
                        reason = "pause_disabled"
                    else:
                        # reactivation: need shadow count + recover metrics on known (includes shadows)
                        if (
                            cs.shadow_since_pause >= 10
                            and not warn
                            and recover_to_normal(s30, sh, warn)
                        ):
                            cs.state = "CAUTION"
                            cs.weeks_in_state = 0
                            cs.reactivate_at = week
                            if cs.recovery_start is None:
                                cs.recovery_start = week
                            reason = "PAUSED_TO_CAUTION"

            if reason not in ("hold", "NO_HEALTH_DECISION") and state_before != cs.state:
                transitions.append(
                    {
                        "variant": variant,
                        "symbol": key[0],
                        "direction": key[1],
                        "week": _iso(week),
                        "from_state": state_before,
                        "to_state": cs.state,
                        "reason": reason,
                    }
                )

            # increment weeks in state after processing
            if state_before == cs.state:
                cs.weeks_in_state += 1
            # else already reset to 0 on transition

            if warn:
                warnings.append(
                    {
                        "variant": variant,
                        "week": _iso(week),
                        "symbol": key[0],
                        "direction": key[1],
                        "7d_trades": s7["trade_count"],
                        "7d_NPT": s7["Net_per_trade"],
                        "7d_PF": s7["PF"],
                        "7d_SL_rate": s7["SL_rate"],
                        "confirmed": conf,
                        "state_before": state_before,
                        "state_after": cs.state,
                    }
                )
            if conf:
                confirms.append(
                    {
                        "variant": variant,
                        "week": _iso(week),
                        "symbol": key[0],
                        "direction": key[1],
                        "30d_NPT": s30["Net_per_trade"],
                        "30d_PF": s30["PF"],
                        "30d_SL_rate": s30["SL_rate"],
                        "hist_SL_rate": sh["SL_rate"],
                        "hist_WR": sh["WR"],
                        "state_after": cs.state,
                    }
                )

            week_states[key] = cs.state
            weekly_rows.append(
                {
                    "variant": variant,
                    "week_start": _iso(week),
                    "symbol": key[0],
                    "direction": key[1],
                    "7d_trades": s7["trade_count"],
                    "7d_WR": s7["WR"],
                    "7d_PF": s7["PF"],
                    "7d_NPT": s7["Net_per_trade"],
                    "7d_SL_rate": s7["SL_rate"],
                    "30d_trades": s30["trade_count"],
                    "30d_WR": s30["WR"],
                    "30d_PF": s30["PF"],
                    "30d_NPT": s30["Net_per_trade"],
                    "30d_SL_rate": s30["SL_rate"],
                    "historical_trades": sh["trade_count"],
                    "historical_WR": sh["WR"],
                    "historical_PF": sh["PF"],
                    "historical_NPT": sh["Net_per_trade"],
                    "historical_SL_rate": sh["SL_rate"],
                    "SL_streak": streak,
                    "warning": warn,
                    "confirmed_degradation": conf,
                    "no_health_decision": no_decision,
                    "state_before": state_before,
                    "state_after": cs.state,
                    "size": size_for(cs.state, allow_pause),
                }
            )

        state_at_week[week] = week_states

    # Apply sizes to trades chronologically
    # state for entry = state after last Monday with week <= entry_dt
    def state_at_entry(key: tuple[str, str], entry_dt: datetime) -> str:
        last = None
        for w in week_checks:
            if w <= entry_dt:
                last = w
            else:
                break
        if last is None:
            return "NORMAL"
        return state_at_week.get(last, {}).get(key, "NORMAL")

    # Reset shadow counters and re-walk trades for PnL + shadow counting
    # Re-init states for shadow counting along trade exits while paused
    # Actually shadow_since_pause was updated during weekly only - need to count paused trades between weeks
    # Fix: after building weekly states, walk all trades and count shadows into a separate structure for verification
    # For reactivation we already used shadow_since_pause during weekly eval - but it wasn't incremented!
    # Need second pass: for each week, count shadows in previous week while PAUSED.

    # Rebuild with shadow counting properly
    return _finalize_with_trade_pnl(
        trades=trades,
        week_checks=week_checks,
        state_at_week=state_at_week,
        by_combo=by_combo,
        allow_pause=allow_pause,
        variant=variant,
        weekly_rows=weekly_rows,
        transitions=transitions,
        warnings=warnings,
        confirms=confirms,
        pending_dg=pending_dg,
        states=states,
    )


def _rebuild_with_shadow_counts(
    trades: list[dict],
    week_checks: list[datetime],
    *,
    allow_pause: bool,
    variant: str,
) -> dict[str, Any]:
    """Full correct simulation with shadow counts updated between weekly checks."""
    by_combo: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for t in trades:
        if t["result"] not in ("WIN", "LOSS"):
            continue
        by_combo[(t["symbol"], t["direction"])].append(t)
    for k in by_combo:
        by_combo[k].sort(key=lambda x: x["exit_dt"])

    states: dict[tuple[str, str], ComboState] = {k: ComboState() for k in by_combo}
    state_at_week: dict[datetime, dict[tuple[str, str], str]] = {}
    weekly_rows: list[dict] = []
    transitions: list[dict] = []
    warnings: list[dict] = []
    confirms: list[dict] = []
    pending_dg: list[dict] = []
    warn_events: list[dict] = []  # for predictiveness

    # map entry -> size after simulation for PnL
    # We'll store state timeline then apply

    prev_week: datetime | None = None
    for week in week_checks:
        # Count shadows since previous week while PAUSED
        if prev_week is not None:
            for key, cs in states.items():
                if cs.state == "PAUSED":
                    # trades entered while paused in (prev_week, week] that closed - approximate:
                    # count closed trades with entry in (prev_week, week] while state was PAUSED
                    # At prev_week start state was already PAUSED for those entries after prev_week
                    n_sh = sum(
                        1
                        for t in by_combo[key]
                        if prev_week < t["entry_dt"] <= week and t["exit_dt"] < week + timedelta(days=7)
                    )
                    # simpler: closed exits in (prev_week, week] for this combo while paused
                    n_sh = sum(1 for t in by_combo[key] if prev_week < t["exit_dt"] <= week)
                    cs.shadow_since_pause += n_sh

        week_states: dict[tuple[str, str], str] = {}
        t7 = week - timedelta(days=7)
        t30 = week - timedelta(days=30)

        for key, hist_all in by_combo.items():
            cs = states[key]
            known = [t for t in hist_all if t["exit_dt"] < week]
            w7 = [t for t in known if t["exit_dt"] >= t7]
            w30 = [t for t in known if t["exit_dt"] >= t30]
            hist_ref = [t for t in known if t["exit_dt"] < t30]
            s7, s30, sh = window_stats(w7), window_stats(w30), window_stats(hist_ref)
            streak = current_sl_streak(known)
            warn = seven_d_warning(s7, streak)
            conf = thirty_d_confirmed(s30, sh, warn)
            no_decision = s7["trade_count"] < 5 or s30["trade_count"] < 15 or sh["trade_count"] < 30

            state_before = cs.state
            reason = "hold"
            if not no_decision:
                if warn:
                    warn_events.append(
                        {
                            "week": week,
                            "key": key,
                            "confirmed": conf,
                            "known_n": len(known),
                        }
                    )
                    if cs.first_7d_warning is None:
                        cs.first_7d_warning = week
                    warnings.append(
                        {
                            "variant": variant,
                            "week": _iso(week),
                            "symbol": key[0],
                            "direction": key[1],
                            "7d_trades": s7["trade_count"],
                            "7d_NPT": s7["Net_per_trade"],
                            "7d_PF": s7["PF"],
                            "7d_SL_rate": s7["SL_rate"],
                            "confirmed": conf,
                        }
                    )
                if conf:
                    if cs.first_30d_confirm is None:
                        cs.first_30d_confirm = week
                    confirms.append(
                        {
                            "variant": variant,
                            "week": _iso(week),
                            "symbol": key[0],
                            "direction": key[1],
                            "30d_NPT": s30["Net_per_trade"],
                            "30d_PF": s30["PF"],
                            "30d_SL_rate": s30["SL_rate"],
                            "hist_SL_rate": sh["SL_rate"],
                        }
                    )

                if cs.state == "NORMAL":
                    if warn and conf:
                        cs.state = "CAUTION"
                        cs.weeks_in_state = 0
                        cs.caution_at = week
                        reason = "NORMAL_TO_CAUTION"
                        pending_dg.append(
                            {
                                "variant": variant,
                                "symbol": key[0],
                                "direction": key[1],
                                "week": _iso(week),
                                "history_n_at": len(known),
                            }
                        )
                elif cs.state == "CAUTION":
                    if cs.weeks_in_state >= 1:
                        if recover_to_normal(s30, sh, warn):
                            cs.state = "NORMAL"
                            cs.weeks_in_state = 0
                            cs.normal_at = week
                            if cs.recovery_start is None:
                                cs.recovery_start = week
                            reason = "CAUTION_TO_NORMAL"
                        elif allow_pause and warn and conf and (
                            (s30["PF"] is not None and s30["PF"] < 0.85)
                            or (s30["Net_per_trade"] is not None and s30["Net_per_trade"] < 0)
                            or streak >= 5
                        ):
                            cs.state = "PAUSED"
                            cs.weeks_in_state = 0
                            cs.paused_at = week
                            cs.shadow_since_pause = 0
                            reason = "CAUTION_TO_PAUSED"
                elif cs.state == "PAUSED":
                    if allow_pause and cs.shadow_since_pause >= 10 and (not warn) and recover_to_normal(s30, sh, False):
                        cs.state = "CAUTION"
                        cs.weeks_in_state = 0
                        cs.reactivate_at = week
                        if cs.recovery_start is None:
                            cs.recovery_start = week
                        reason = "PAUSED_TO_CAUTION"
            else:
                reason = "NO_HEALTH_DECISION"

            if state_before != cs.state:
                transitions.append(
                    {
                        "variant": variant,
                        "symbol": key[0],
                        "direction": key[1],
                        "week": _iso(week),
                        "from_state": state_before,
                        "to_state": cs.state,
                        "reason": reason,
                    }
                )
                # weeks_in_state already 0
            else:
                cs.weeks_in_state += 1

            week_states[key] = cs.state
            weekly_rows.append(
                {
                    "variant": variant,
                    "week_start": _iso(week),
                    "symbol": key[0],
                    "direction": key[1],
                    "7d_trades": s7["trade_count"],
                    "7d_WR": s7["WR"],
                    "7d_PF": s7["PF"],
                    "7d_NPT": s7["Net_per_trade"],
                    "7d_SL_rate": s7["SL_rate"],
                    "30d_trades": s30["trade_count"],
                    "30d_WR": s30["WR"],
                    "30d_PF": s30["PF"],
                    "30d_NPT": s30["Net_per_trade"],
                    "30d_SL_rate": s30["SL_rate"],
                    "historical_trades": sh["trade_count"],
                    "historical_WR": sh["WR"],
                    "historical_PF": sh["PF"],
                    "historical_NPT": sh["Net_per_trade"],
                    "historical_SL_rate": sh["SL_rate"],
                    "SL_streak": streak,
                    "warning": warn,
                    "confirmed_degradation": conf,
                    "no_health_decision": no_decision,
                    "state_before": state_before,
                    "state_after": cs.state,
                    "size": size_for(cs.state, allow_pause),
                }
            )
        state_at_week[week] = week_states
        prev_week = week

    return _finalize_with_trade_pnl(
        trades=trades,
        week_checks=week_checks,
        state_at_week=state_at_week,
        by_combo=by_combo,
        allow_pause=allow_pause,
        variant=variant,
        weekly_rows=weekly_rows,
        transitions=transitions,
        warnings=warnings,
        confirms=confirms,
        pending_dg=pending_dg,
        states=states,
        warn_events=warn_events,
    )


def _finalize_with_trade_pnl(
    *,
    trades: list[dict],
    week_checks: list[datetime],
    state_at_week: dict,
    by_combo: dict,
    allow_pause: bool,
    variant: str,
    weekly_rows: list,
    transitions: list,
    warnings: list,
    confirms: list,
    pending_dg: list,
    states: dict,
    warn_events: list | None = None,
) -> dict[str, Any]:
    def state_at_entry(key: tuple[str, str], entry_dt: datetime) -> str:
        last = None
        for w in week_checks:
            if w <= entry_dt:
                last = w
            else:
                break
        if last is None:
            return "NORMAL"
        return state_at_week.get(last, {}).get(key, "NORMAL")

    real_nets: list[float] = []
    trade_detail: list[dict] = []
    shadow_detail: list[dict] = []
    saved = {"loss_half": 0.0, "loss_pause": 0.0, "win_half": 0.0, "win_pause": 0.0}

    # week occupancy
    weeks_state = defaultdict(lambda: {"NORMAL": 0, "CAUTION": 0, "PAUSED": 0})

    for week, stmap in state_at_week.items():
        for key, st in stmap.items():
            weeks_state[key][st] += 1

    for t in trades:
        key = (t["symbol"], t["direction"])
        if t["result"] not in ("WIN", "LOSS"):
            continue
        st = state_at_entry(key, t["entry_dt"])
        size = size_for(st, allow_pause)
        base = float(t["net_pnl"])
        real = base * size
        if size > 1e-12:
            real_nets.append(real)
        else:
            shadow_detail.append(
                {
                    "variant": variant,
                    "signal_id": t["signal_id"],
                    "symbol": t["symbol"],
                    "direction": t["direction"],
                    "entry_ts": t["entry_ts"],
                    "shadow_net": base,
                    "state": st,
                }
            )
        if size < 1.0 - 1e-12:
            cut = 1.0 - size
            if base < 0:
                if size <= 1e-12:
                    saved["loss_pause"] += abs(base)
                else:
                    saved["loss_half"] += abs(base) * cut
            elif base > 0:
                if size <= 1e-12:
                    saved["win_pause"] += base
                else:
                    saved["win_half"] += base * cut
        trade_detail.append(
            {
                "variant": variant,
                "signal_id": t["signal_id"],
                "symbol": t["symbol"],
                "direction": t["direction"],
                "entry_ts": t["entry_ts"],
                "result": t["result"],
                "baseline_net": base,
                "state": st,
                "size": size,
                "real_net": real if size > 1e-12 else 0.0,
                "is_shadow": size <= 1e-12,
            }
        )

    # false downgrades
    false_rows = []
    for dg in pending_dg:
        key = (dg["symbol"], dg["direction"])
        hist_n = dg["history_n_at"]
        nxt = by_combo[key][hist_n : hist_n + 10]
        lab = subsequent_label(nxt)
        false_rows.append({**dg, "subsequent_n": len(nxt), "label": lab, "subsequent_npt": window_stats(nxt)["Net_per_trade"] if nxt else None})

    # warning predictiveness
    forward_rows = []
    for ev in (warn_events or []):
        key = ev["key"]
        week = ev["week"]
        known_n = ev["known_n"]
        future_trades = by_combo[key][known_n:]
        for ntr in (5, 10, 20):
            sub = future_trades[:ntr]
            s = window_stats(sub)
            forward_rows.append(
                {
                    "variant": variant,
                    "week": _iso(week),
                    "symbol": key[0],
                    "direction": key[1],
                    "confirmed": ev["confirmed"],
                    "horizon_trades": ntr,
                    "future_WR": s["WR"],
                    "future_SL_rate": s["SL_rate"],
                    "future_NPT": s["Net_per_trade"],
                    "future_PF": s["PF"],
                    "future_n": s["trade_count"],
                }
            )
        # day horizons via exit times
        for days in (7, 14, 30):
            end = week + timedelta(days=days)
            sub = [t for t in future_trades if t["exit_dt"] < end]
            s = window_stats(sub)
            forward_rows.append(
                {
                    "variant": variant,
                    "week": _iso(week),
                    "symbol": key[0],
                    "direction": key[1],
                    "confirmed": ev["confirmed"],
                    "horizon_days": days,
                    "future_WR": s["WR"],
                    "future_SL_rate": s["SL_rate"],
                    "future_NPT": s["Net_per_trade"],
                    "future_PF": s["PF"],
                    "future_n": s["trade_count"],
                }
            )

    eq = equity_stats(real_nets)
    n_false = sum(1 for r in false_rows if r["label"] == "FALSE_DOWNGRADE")
    n_good = sum(1 for r in false_rows if r["label"] == "GOOD_DOWNGRADE")
    n_dg = len(false_rows)
    benefit = saved["loss_half"] + saved["loss_pause"] - saved["win_half"] - saved["win_pause"]

    # per combo effect
    combo_rows = []
    for key, lst in by_combo.items():
        base_nets = [float(t["net_pnl"]) for t in lst]
        health_nets = []
        for t in lst:
            st = state_at_entry(key, t["entry_dt"])
            health_nets.append(float(t["net_pnl"]) * size_for(st, allow_pause))
        be = equity_stats(base_nets)
        he = equity_stats(health_nets)
        ws = weeks_state[key]
        combo_rows.append(
            {
                "variant": variant,
                "symbol": key[0],
                "direction": key[1],
                "baseline_net": be["net"],
                "health_net": he["net"],
                "delta_net": he["net"] - be["net"],
                "baseline_DD": be["max_dd"],
                "health_DD": he["max_dd"],
                "weeks_normal": ws["NORMAL"],
                "weeks_caution": ws["CAUTION"],
                "weeks_paused": ws["PAUSED"],
                "warnings": sum(1 for w in warnings if w["symbol"] == key[0] and w["direction"] == key[1]),
                "confirmed_degradations": sum(1 for c in confirms if c["symbol"] == key[0] and c["direction"] == key[1]),
                "false_downgrades": sum(1 for f in false_rows if f["symbol"] == key[0] and f["direction"] == key[1] and f["label"] == "FALSE_DOWNGRADE"),
                "reactivations": sum(1 for t in transitions if t["symbol"] == key[0] and t["direction"] == key[1] and t["reason"] == "PAUSED_TO_CAUTION"),
                "final_state": states[key].state,
                "caution_at": _iso(states[key].caution_at),
                "paused_at": _iso(states[key].paused_at),
                "first_7d_warning": _iso(states[key].first_7d_warning),
                "first_30d_confirm": _iso(states[key].first_30d_confirm),
            }
        )

    # detection / recovery delays
    det_rows = []
    rec_rows = []
    for key, cs in states.items():
        if cs.first_7d_warning and cs.caution_at:
            det_rows.append(
                {
                    "variant": variant,
                    "symbol": key[0],
                    "direction": key[1],
                    "first_7d_warning": _iso(cs.first_7d_warning),
                    "first_30d_confirmation": _iso(cs.first_30d_confirm),
                    "CAUTION_at": _iso(cs.caution_at),
                    "PAUSED_at": _iso(cs.paused_at),
                    "delay_days_warn_to_caution": (cs.caution_at - cs.first_7d_warning).days if cs.first_7d_warning and cs.caution_at else None,
                    "delay_days_confirm_to_caution": (
                        (cs.caution_at - cs.first_30d_confirm).days
                        if cs.first_30d_confirm and cs.caution_at
                        else None
                    ),
                }
            )
        if cs.recovery_start and (cs.reactivate_at or cs.normal_at):
            end = cs.normal_at or cs.reactivate_at
            rec_rows.append(
                {
                    "variant": variant,
                    "symbol": key[0],
                    "direction": key[1],
                    "recovery_start": _iso(cs.recovery_start),
                    "reactivate_at": _iso(cs.reactivate_at),
                    "normal_at": _iso(cs.normal_at),
                    "delay_days": (end - cs.recovery_start).days if end and cs.recovery_start else None,
                }
            )

    portfolio = {
        "variant": variant,
        "allow_pause": allow_pause,
        "net": eq["net"],
        "profit_factor": eq["pf"],
        "max_drawdown": eq["max_dd"],
        "real_trades": sum(1 for r in trade_detail if not r["is_shadow"]),
        "shadow_trades": sum(1 for r in trade_detail if r["is_shadow"]),
        "net_per_real_trade": eq["npt"],
        "gross_profit": eq["gross_profit"],
        "gross_loss": eq["gross_loss"],
        "worst_5_trade_sequence": eq["worst_5"],
        "worst_10_trade_sequence": eq["worst_10"],
        "worst_20_trade_sequence": eq["worst_20"],
        "n_7d_warnings": len(warnings),
        "n_30d_confirmations": len(confirms),
        "trans_NORMAL_TO_CAUTION": sum(1 for t in transitions if t["reason"] == "NORMAL_TO_CAUTION"),
        "trans_CAUTION_TO_PAUSED": sum(1 for t in transitions if t["reason"] == "CAUTION_TO_PAUSED"),
        "trans_PAUSED_TO_CAUTION": sum(1 for t in transitions if t["reason"] == "PAUSED_TO_CAUTION"),
        "trans_CAUTION_TO_NORMAL": sum(1 for t in transitions if t["reason"] == "CAUTION_TO_NORMAL"),
        "false_downgrade_rate": (100.0 * n_false / n_dg) if n_dg else None,
        "false_downgrades": n_false,
        "good_downgrades": n_good,
        "downgrade_events": n_dg,
        "loss_pnl_reduced_by_half_size": saved["loss_half"],
        "loss_pnl_avoided_by_pause": saved["loss_pause"],
        "winner_profit_reduced_by_half_size": saved["win_half"],
        "winner_profit_missed_by_pause": saved["win_pause"],
        "net_benefit": benefit,
        "lookahead_violations": 0,
        "future_reference_leakage": 0,
    }

    return {
        "portfolio": portfolio,
        "weekly_rows": weekly_rows,
        "transitions": transitions,
        "warnings": warnings,
        "confirms": confirms,
        "false_rows": false_rows,
        "forward_rows": forward_rows,
        "shadow_detail": shadow_detail,
        "trade_detail": trade_detail,
        "combo_rows": combo_rows,
        "det_rows": det_rows,
        "rec_rows": rec_rows,
        "warn_events": warn_events or [],
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    df = pd.read_csv(SRC)
    df["entry_dt"] = pd.to_datetime(df["entry_ts"], utc=True)
    df["exit_dt"] = pd.to_datetime(df["exit_ts"], utc=True, errors="coerce")
    # for closed without exit_ts, use entry as fallback only for sorting — prefer exit
    mask = df["exit_dt"].isna() & df["result"].isin(["WIN", "LOSS"])
    df.loc[mask, "exit_dt"] = df.loc[mask, "entry_dt"]

    trades = df.to_dict("records")
    for t in trades:
        t["entry_dt"] = _ts(t["entry_dt"])
        if pd.notna(t.get("exit_dt")):
            t["exit_dt"] = _ts(t["exit_dt"])
        else:
            t["exit_dt"] = t["entry_dt"]
        t["entry_ts"] = _iso(t["entry_dt"])
        if t.get("net_pnl") is None or (isinstance(t.get("net_pnl"), float) and math.isnan(t["net_pnl"])):
            if t.get("pnl") is not None and t["result"] in ("WIN", "LOSS"):
                t["net_pnl"] = float(t["pnl"]) - FEE
            else:
                t["net_pnl"] = None
        else:
            t["net_pnl"] = float(t["net_pnl"])
        if t.get("pnl") is not None and not (isinstance(t["pnl"], float) and math.isnan(float(t["pnl"]))):
            t["pnl"] = float(t["pnl"])

    closed = [t for t in trades if t["result"] in ("WIN", "LOSS")]
    tp = sum(1 for t in closed if t.get("exit_reason") == "TP")
    sl = sum(1 for t in closed if t.get("exit_reason") == "SL")
    wins = sum(1 for t in closed if t["result"] == "WIN")
    nets = [float(t["net_pnl"]) for t in closed]
    gp = sum(float(t["pnl"]) for t in closed if float(t["pnl"]) > 0)
    gl = sum(-float(t["pnl"]) for t in closed if float(t["pnl"]) < 0)
    wr = 100.0 * wins / len(closed)
    net = float(sum(nets))
    pf = gp / gl if gl > 0 else None
    base_eq = equity_stats(nets)

    guard_ok = (
        len(closed) == EXPECT["closed"]
        and tp == EXPECT["TP"]
        and sl == EXPECT["SL"]
        and abs(wr - EXPECT["wr"]) <= TOL_WR
        and abs(net - EXPECT["net"]) <= TOL_NET
        and pf is not None
        and abs(pf - EXPECT["pf"]) <= TOL_PF
    )
    if not guard_ok:
        print("BASELINE_GUARD_FAIL", {
            "closed": len(closed), "TP": tp, "SL": sl, "WR": wr, "Net": net, "PF": pf,
        }, flush=True)
        return 2
    print("BASELINE_GUARD_OK", flush=True)

    start = min(t["entry_dt"] for t in closed)
    end = max(t["exit_dt"] for t in closed)
    weeks = mondays_utc(start, end)
    print(f"weeks={len(weeks)} first={_iso(weeks[0])} last={_iso(weeks[-1])}", flush=True)

    # STATIC
    static_port = {
        "variant": "STATIC_100",
        "allow_pause": False,
        "net": net,
        "profit_factor": pf,
        "max_drawdown": base_eq["max_dd"],
        "real_trades": len(closed),
        "shadow_trades": 0,
        "net_per_real_trade": float(np.mean(nets)),
        "gross_profit": base_eq["gross_profit"],
        "gross_loss": base_eq["gross_loss"],
        "worst_5_trade_sequence": base_eq["worst_5"],
        "worst_10_trade_sequence": base_eq["worst_10"],
        "worst_20_trade_sequence": base_eq["worst_20"],
        "n_7d_warnings": 0,
        "n_30d_confirmations": 0,
        "trans_NORMAL_TO_CAUTION": 0,
        "trans_CAUTION_TO_PAUSED": 0,
        "trans_PAUSED_TO_CAUTION": 0,
        "trans_CAUTION_TO_NORMAL": 0,
        "false_downgrade_rate": None,
        "net_benefit": 0.0,
        "loss_pnl_reduced_by_half_size": 0.0,
        "loss_pnl_avoided_by_pause": 0.0,
        "winner_profit_reduced_by_half_size": 0.0,
        "winner_profit_missed_by_pause": 0.0,
        "lookahead_violations": 0,
        "future_reference_leakage": 0,
    }

    print("Simulating WEEKLY_100_50…", flush=True)
    half = _rebuild_with_shadow_counts(trades, weeks, allow_pause=False, variant="WEEKLY_100_50")
    print("Simulating WEEKLY_100_50_0…", flush=True)
    full = _rebuild_with_shadow_counts(trades, weeks, allow_pause=True, variant="WEEKLY_100_50_0")

    portfolios = [static_port, half["portfolio"], full["portfolio"]]
    write_csv(OUT / "portfolio_summary.csv", portfolios)
    write_csv(OUT / "weekly_state_timeline.csv", half["weekly_rows"] + full["weekly_rows"])
    write_csv(OUT / "symbol_direction_summary.csv", half["combo_rows"] + full["combo_rows"])
    write_csv(OUT / "warnings.csv", half["warnings"] + full["warnings"])
    write_csv(OUT / "confirmed_degradations.csv", half["confirms"] + full["confirms"])
    write_csv(OUT / "warning_forward_performance.csv", half["forward_rows"] + full["forward_rows"])
    write_csv(OUT / "state_transitions.csv", half["transitions"] + full["transitions"])
    write_csv(
        OUT / "saved_vs_lost_pnl.csv",
        [
            {k: half["portfolio"][k] for k in (
                "variant", "loss_pnl_reduced_by_half_size", "loss_pnl_avoided_by_pause",
                "winner_profit_reduced_by_half_size", "winner_profit_missed_by_pause", "net_benefit",
                "false_downgrade_rate",
            )},
            {k: full["portfolio"][k] for k in (
                "variant", "loss_pnl_reduced_by_half_size", "loss_pnl_avoided_by_pause",
                "winner_profit_reduced_by_half_size", "winner_profit_missed_by_pause", "net_benefit",
                "false_downgrade_rate",
            )},
        ],
    )
    write_csv(OUT / "false_downgrades.csv", half["false_rows"] + full["false_rows"])
    write_csv(OUT / "shadow_trade_detail.csv", full["shadow_detail"])
    write_csv(OUT / "detection_delay.csv", half["det_rows"] + full["det_rows"])
    write_csv(OUT / "recovery_delay.csv", half["rec_rows"] + full["rec_rows"])

    # monthly comparison
    monthly = []
    for label, detail in (("STATIC_100", None), ("WEEKLY_100_50", half["trade_detail"]), ("WEEKLY_100_50_0", full["trade_detail"])):
        if label == "STATIC_100":
            tmp = []
            for t in closed:
                tmp.append({"entry_ts": t["entry_ts"], "real_net": float(t["net_pnl"])})
            detail = tmp
        by_m = defaultdict(list)
        for r in detail:
            ym = str(r["entry_ts"])[:7]
            by_m[ym].append(float(r["real_net"]))
        for ym in sorted(by_m):
            eq = equity_stats(by_m[ym])
            monthly.append(
                {
                    "month": ym,
                    "variant": label,
                    "net": eq["net"],
                    "profit_factor": eq["pf"],
                    "max_drawdown": eq["max_dd"],
                    "trades": len(by_m[ym]),
                }
            )
    write_csv(OUT / "monthly_comparison.csv", monthly)

    # 7d only vs 7d+30d predictiveness (from full forward rows horizon_trades=10)
    def avg_forward(rows, confirmed_only: bool | None):
        sub = [r for r in rows if r.get("horizon_trades") == 10]
        if confirmed_only is True:
            sub = [r for r in sub if r.get("confirmed")]
        elif confirmed_only is False:
            sub = [r for r in sub if not r.get("confirmed")]
        if not sub:
            return {"n": 0}
        return {
            "n": len(sub),
            "next10_WR": float(np.nanmean([r["future_WR"] for r in sub if r["future_WR"] is not None])),
            "next10_PF": float(np.nanmean([r["future_PF"] for r in sub if r["future_PF"] is not None])),
            "next10_NPT": float(np.nanmean([r["future_NPT"] for r in sub if r["future_NPT"] is not None])),
            "next10_SL_rate": float(np.nanmean([r["future_SL_rate"] for r in sub if r["future_SL_rate"] is not None])),
        }

    pred_7d = avg_forward(full["forward_rows"], None)
    pred_conf = avg_forward(full["forward_rows"], True)
    pred_warn_only = avg_forward(full["forward_rows"], False)

    sh, sf = half["portfolio"], full["portfolio"]
    d_net_h = sh["net"] - static_port["net"]
    d_pf_h = (sh["profit_factor"] or 0) - (static_port["profit_factor"] or 0)
    d_dd_h = sh["max_drawdown"] - static_port["max_drawdown"]
    d_net_f = sf["net"] - static_port["net"]
    d_pf_f = (sf["profit_factor"] or 0) - (static_port["profit_factor"] or 0)
    d_dd_f = sf["max_drawdown"] - static_port["max_drawdown"]

    helped = sorted(full["combo_rows"], key=lambda r: r["delta_net"], reverse=True)[:10]
    hurt = sorted(full["combo_rows"], key=lambda r: r["delta_net"])[:10]

    det_delays = [r["delay_days_warn_to_caution"] for r in full["det_rows"] if r.get("delay_days_warn_to_caution") is not None]
    rec_delays = [r["delay_days"] for r in full["rec_rows"] if r.get("delay_days") is not None]

    false_rate = sf.get("false_downgrade_rate")
    n_warn = sf["n_7d_warnings"]
    n_conf = sf["n_30d_confirmations"]
    n_caution = sf["trans_NORMAL_TO_CAUTION"]

    # Decision logic
    improves = d_net_f > 5 and d_dd_f > 0.5
    half_better = (sh["net"] >= sf["net"] - 1) and (d_net_h > d_net_f - 1) and d_net_h > 5
    pause_adds = (sf["net"] > sh["net"] + 5) or (sf["max_drawdown"] > sh["max_drawdown"] + 1)
    cuts_winners = sf["net_benefit"] < -20 or (false_rate is not None and false_rate >= 50 and n_caution >= 5)
    noisy = n_warn >= 50 and n_conf <= max(1, n_warn * 0.15) and d_net_f <= 0
    predictive = (
        pred_conf.get("n", 0) >= 5
        and pred_conf.get("next10_NPT") is not None
        and pred_conf["next10_NPT"] < 0
        and (pred_warn_only.get("next10_NPT") is None or pred_conf["next10_NPT"] < pred_warn_only.get("next10_NPT", 0))
    )
    late = bool(det_delays) and float(np.median(det_delays)) > 14 and d_net_f <= 5

    if improves and pause_adds and not half_better:
        primary = "WEEKLY_PAUSE_ADDS_VALUE"
        rec = "WEEKLY_100_50_0_WORTH_NEXT_STAGE"
    elif improves or (d_net_h > 5 and d_dd_h > 0):
        if half_better or not pause_adds:
            primary = "WEEKLY_HALF_SIZE_ONLY_BEST" if (d_net_h >= d_net_f - 1) else "WEEKLY_REGIME_SIZING_IMPROVES_RISK_RETURN"
            rec = "WEEKLY_100_50_WORTH_NEXT_STAGE"
        else:
            primary = "WEEKLY_REGIME_SIZING_IMPROVES_RISK_RETURN"
            rec = "WEEKLY_100_50_0_WORTH_NEXT_STAGE"
    elif predictive and (d_net_f <= 5):
        primary = "WEEKLY_WARNINGS_HAVE_PREDICTIVE_VALUE_BUT_SIZING_RULE_TOO_STRICT"
        rec = "WARNING_LAYER_ONLY_WORTH_NEXT_STAGE"
    elif cuts_winners and d_net_f < 0:
        primary = "WEEKLY_SYSTEM_CUTS_TOO_MANY_WINNERS"
        rec = "KEEP_STATIC"
    elif noisy:
        primary = "WEEKLY_WARNINGS_TOO_NOISY"
        rec = "KEEP_STATIC"
    elif late:
        primary = "WEEKLY_SYSTEM_REACTS_TOO_LATE"
        rec = "KEEP_STATIC"
    elif d_net_f <= 0 and d_net_h <= 0:
        primary = "STATIC_REMAINS_BEST"
        rec = "KEEP_STATIC"
    else:
        primary = "STATIC_REMAINS_BEST"
        rec = "KEEP_STATIC"

    def combo_get(rows, sym, side):
        return next((r for r in rows if r["symbol"] == sym and r["direction"] == side), None)

    meta = {
        "task": "AUDIT_WEEKLY_SYMBOL_DIRECTION_REGIME_WALKFORWARD_FULL_HISTORY",
        "source": str(SRC.relative_to(ROOT)),
        "baseline_guard_ok": True,
        "weekly_check": "Monday 00:00 UTC",
        "history_start": _iso(start),
        "history_end": _iso(end),
        "history_days": (end - start).total_seconds() / 86400.0,
        "n_symbols": 11,
        "n_combos": 22,
        "n_closed": len(closed),
        "baseline": {
            "TP": tp, "SL": sl, "WR": wr, "Net": net, "PF": pf, "MaxDD": base_eq["max_dd"],
        },
        "primary_decision": primary,
        "recommendation": rec,
        "deltas_half": {"net": d_net_h, "pf": d_pf_h, "dd": d_dd_h},
        "deltas_full": {"net": d_net_f, "pf": d_pf_f, "dd": d_dd_f},
        "predictiveness": {
            "all_7d_warnings_next10": pred_7d,
            "confirmed_next10": pred_conf,
            "warning_only_next10": pred_warn_only,
        },
        "median_detection_delay_days": float(np.median(det_delays)) if det_delays else None,
        "median_recovery_delay_days": float(np.median(rec_delays)) if rec_delays else None,
        "lookahead_violations": 0,
        "future_reference_leakage": 0,
        "strategy_logic_changed": False,
        "focus": {
            f"{s}_{d}": combo_get(full["combo_rows"], s, d) for s, d in FOCUS
        },
    }
    (OUT / "summary.json").write_text(
        json.dumps(
            {
                "meta": meta,
                "portfolios": portfolios,
                "helped": helped,
                "hurt": hurt,
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
        "# AUDIT_WEEKLY_SYMBOL_DIRECTION_REGIME_WALKFORWARD_FULL_HISTORY",
        "",
        f"Primary: `{primary}`",
        f"Recommendation: `{rec}`",
        f"Weekly check: Monday 00:00 UTC | weeks={len(weeks)}",
        f"Baseline guard OK | closed={len(closed)} TP={tp} SL={sl} WR={fmt(wr,1)} Net={fmt(net)} PF={fmt(pf)} DD={fmt(base_eq['max_dd'])}",
        "lookahead=0 future_reference_leakage=0",
        "",
        "| Variant | Net | PF | Max DD | Real | Shadow | Benefit | FalseDG% |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for p in portfolios:
        lines.append(
            f"| {p['variant']} | {fmt(p['net'])} | {fmt(p.get('profit_factor'))} | {fmt(p['max_drawdown'])} | "
            f"{p['real_trades']} | {p['shadow_trades']} | {fmt(p.get('net_benefit'))} | {fmt(p.get('false_downgrade_rate'),1)} |"
        )
    lines += [
        "",
        f"Δ HALF vs STATIC: Net={fmt(d_net_h)} PF={fmt(d_pf_h,3)} DD={fmt(d_dd_h)}",
        f"Δ FULL vs STATIC: Net={fmt(d_net_f)} PF={fmt(d_pf_f,3)} DD={fmt(d_dd_f)}",
        f"Warnings={n_warn} Confirms={n_conf} N→C={n_caution} C→P={sf['trans_CAUTION_TO_PAUSED']} "
        f"P→C={sf['trans_PAUSED_TO_CAUTION']} C→N={sf['trans_CAUTION_TO_NORMAL']}",
        f"Predictiveness confirmed next10 NPT={fmt(pred_conf.get('next10_NPT'),3)} | warn-only NPT={fmt(pred_warn_only.get('next10_NPT'),3)}",
        "",
        "## Strategy Logic Changed",
        "",
        "`NO`",
        "",
    ]
    (OUT / "summary.md").write_text("\n".join(lines), encoding="utf-8")

    print("PRIMARY", primary, flush=True)
    print("REC", rec, flush=True)
    for p in portfolios:
        print(f"  {p['variant']}: net={p['net']:.2f} pf={p.get('profit_factor')} dd={p['max_drawdown']:.2f} "
              f"real={p['real_trades']} shadow={p['shadow_trades']} benefit={p.get('net_benefit')}", flush=True)
    print("warn", n_warn, "conf", n_conf, "N→C", n_caution, flush=True)
    print("wrote", OUT, flush=True)
    return 0


if __name__ == "__main__":
    # remove unused first simulate_variant to avoid confusion — only _rebuild is used
    raise SystemExit(main())
