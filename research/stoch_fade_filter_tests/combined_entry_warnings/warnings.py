"""Frozen W1–W4 flags and R0–R9 combinations. No outcome-driven retuning."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .config import STOCH_HIGH, STOCH_LOW, W3_THRESHOLD


def _missing(value: object) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        return True
    if isinstance(value, (float, np.floating)) and not np.isfinite(value):
        return True
    return False


def w1_5m_exhausted_in_trade_direction(direction: str, stoch_k: object) -> bool:
    """Exact previous 5m test: LONG K>80, SHORT K<20, missing K → False."""
    if _missing(stoch_k):
        return False
    k = float(stoch_k)
    if str(direction).upper() == "SHORT":
        return bool(k < STOCH_LOW)
    return bool(k > STOCH_HIGH)


def w2_1m_turning_against_trade(
    *,
    direction: str,
    k: object,
    d: object,
    k_prev: object,
    d_prev: object,
    cross_up: object,
    cross_down: object,
    phase: object,
) -> dict[str, Any]:
    """OR of three last-closed-1m conditions. Missing parts do not become False."""
    side = str(direction).upper()
    k_ok = not _missing(k)
    d_ok = not _missing(d)
    prev_ok = not _missing(k_prev) and not _missing(d_prev)
    phase_s = None if _missing(phase) else str(phase)

    if side == "LONG":
        cond_cross = False if _missing(cross_down) else bool(cross_down)
        cond_cross_known = not _missing(cross_down)
        cond_phase = phase_s == "OVERBOUGHT_TURNING_DOWN"
        cond_phase_known = phase_s is not None
        if k_ok and d_ok and prev_ok:
            kd = float(k) - float(d)
            kd_prev = float(k_prev) - float(d_prev)
            cond_spread = bool(float(k) < float(d) and kd < kd_prev)
            cond_spread_known = True
        else:
            cond_spread = False
            cond_spread_known = False
    else:
        cond_cross = False if _missing(cross_up) else bool(cross_up)
        cond_cross_known = not _missing(cross_up)
        cond_phase = phase_s == "OVERSOLD_TURNING_UP"
        cond_phase_known = phase_s is not None
        if k_ok and d_ok and prev_ok:
            kd = float(k) - float(d)
            kd_prev = float(k_prev) - float(d_prev)
            cond_spread = bool(float(k) > float(d) and kd > kd_prev)
            cond_spread_known = True
        else:
            cond_spread = False
            cond_spread_known = False

    any_true = bool(cond_cross or cond_spread or cond_phase)
    any_unknown = not (cond_cross_known and cond_spread_known and cond_phase_known)
    if any_true:
        flag: bool | None = True
    elif any_unknown:
        flag = None
    else:
        flag = False
    return {
        "w2_1m_turning_against_trade": flag,
        "w2_cross_against": cond_cross if cond_cross_known else None,
        "w2_spread_against": cond_spread if cond_spread_known else None,
        "w2_phase_against": cond_phase if cond_phase_known else None,
        "w2_missing": flag is None,
    }


def pre_entry_progress(
    *,
    direction: str,
    entry_price: object,
    wave_end_price: object,
    tp_price: object,
) -> dict[str, Any]:
    if _missing(entry_price) or _missing(wave_end_price) or _missing(tp_price):
        return {"pre_entry_progress": None, "pre_entry_progress_missing": True, "reason": "missing_input"}
    entry = float(entry_price)
    wave = float(wave_end_price)
    tp = float(tp_price)
    if str(direction).upper() == "LONG":
        denom = tp - wave
        numer = entry - wave
    else:
        denom = wave - tp
        numer = wave - entry
    if not np.isfinite(denom) or denom == 0:
        return {"pre_entry_progress": None, "pre_entry_progress_missing": True, "reason": "bad_denominator"}
    progress = numer / denom
    if not np.isfinite(progress):
        return {"pre_entry_progress": None, "pre_entry_progress_missing": True, "reason": "non_finite"}
    return {"pre_entry_progress": float(progress), "pre_entry_progress_missing": False, "reason": None}


def w3_pre_entry_tp_progress_ge_25pct(progress: object) -> bool | None:
    if _missing(progress):
        return None
    return bool(float(progress) >= W3_THRESHOLD)


def progress_bucket(progress: object) -> str:
    if _missing(progress):
        return "MISSING"
    x = float(progress)
    if x <= 0:
        return "<=0"
    if x < W3_THRESHOLD:
        return "(0,25%)"
    if x < 0.50:
        return "[25,50%)"
    if x < 0.75:
        return "[50,75%)"
    if x <= 1.00:
        return "[75,100%]"
    return ">100%"


def overlap_flags_for_symbol(frame: pd.DataFrame) -> pd.DataFrame:
    """W4 interval: previous_entry < current_entry < previous_exit. OPEN stays open."""
    out = frame.sort_values(["symbol", "entry_time", "signal_id"]).reset_index(drop=True)
    n = len(out)
    any_open = np.zeros(n, dtype=bool)
    same = np.zeros(n, dtype=bool)
    opp = np.zeros(n, dtype=bool)
    counts = np.zeros(n, dtype=int)
    symbols = out["symbol"].to_numpy()
    entries = pd.to_datetime(out["entry_time"], utc=True).to_numpy()
    exits = pd.to_datetime(out["exit_time"], utc=True).to_numpy()
    dirs = out["direction"].to_numpy()
    opens = out["is_open"].to_numpy() if "is_open" in out.columns else (out["outcome"].to_numpy() == "OPEN")
    for i in range(n):
        e_i = entries[i]
        n_open = 0
        same_hit = False
        opp_hit = False
        for j in range(i):
            if symbols[j] != symbols[i]:
                continue
            if not (entries[j] < e_i):
                continue
            still = bool(opens[j]) or (pd.notna(exits[j]) and exits[j] > e_i)
            if not still:
                continue
            n_open += 1
            if dirs[j] == dirs[i]:
                same_hit = True
            else:
                opp_hit = True
        counts[i] = n_open
        any_open[i] = n_open > 0
        same[i] = same_hit
        opp[i] = opp_hit
    out = out.copy()
    out["w4_symbol_trade_already_open"] = any_open
    out["w4_overlap_same_direction"] = same
    out["w4_overlap_opposite_direction"] = opp
    out["w4_n_open_same_symbol"] = counts
    return out


def warning_score(w1: object, w2: object, w3: object, w4: object) -> dict[str, Any]:
    flags = [w1, w2, w3, w4]
    names = ["W1", "W2", "W3", "W4"]
    missing = [names[i] for i, v in enumerate(flags) if v is None]
    n_true = int(sum(v is True for v in flags))
    n_false = int(sum(v is False for v in flags))
    n_missing = int(len(missing))
    return {
        "warning_score_true": n_true,
        "warning_score_false": n_false,
        "warning_score_missing": n_missing,
        "warning_score_complete": (n_true if n_missing == 0 else None),
        "warning_missing_components": ",".join(missing) if missing else "",
        "warning_any_missing": n_missing > 0,
    }


def apply_rules(w1: object, w2: object, w3: object, w4: object) -> dict[str, bool]:
    """Block only on known True. MISSING is not treated as a warning."""
    t1 = w1 is True
    t2 = w2 is True
    t3 = w3 is True
    t4 = w4 is True
    n_true = int(t1) + int(t2) + int(t3) + int(t4)
    return {
        "R0": False,
        "R1": t1,
        "R2": n_true >= 2,
        "R3": n_true >= 3,
        "R4": t1 and t2,
        "R5": t1 and t3,
        "R6": t1 and t2 and t3,
        "R7": t1 and t2 and t4,
        "R8": t2 and t3,
        "R9": t3 and t4,
    }


RULE_DESCRIPTIONS = {
    "R0": "Baseline, block nothing",
    "R1": "Block only W1 (5m exhausted)",
    "R2": "Block warning_score_true >= 2",
    "R3": "Block warning_score_true >= 3",
    "R4": "Block W1 AND W2",
    "R5": "Block W1 AND W3",
    "R6": "Block W1 AND W2 AND W3",
    "R7": "Block W1 AND W2 AND W4",
    "R8": "Block W2 AND W3",
    "R9": "Block W3 AND W4",
}
