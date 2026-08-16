"""BE50 path simulation — freeze fractal_wave_fade_be50_july_2026/simulate.py."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from signal_generator.strategy.wave_fade.parameters import BE_FRAC, FEE_PCT
from signal_generator.strategy.wave_fade.tpsl import tpsl_for_tf


def _utc(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_convert("UTC") if t.tzinfo else t.tz_localize("UTC")


def trade_levels(tr: pd.Series) -> dict[str, float]:
    epx = float(tr["entry_price"])
    side = str(tr["side"])
    ftp, fsl = tpsl_for_tf(str(tr["highest_tf_reached"]), extra_4h=False)
    if side == "LONG":
        tp = epx * (1.0 + ftp / 100.0)
        sl = epx * (1.0 - fsl / 100.0)
        be_trig = epx + BE_FRAC * (tp - epx)
    else:
        tp = epx * (1.0 - ftp / 100.0)
        sl = epx * (1.0 + fsl / 100.0)
        be_trig = epx - BE_FRAC * (epx - tp)
    return {
        "entry": epx,
        "tp": tp,
        "sl": sl,
        "be_trigger": be_trig,
        "tp_pct": ftp,
        "sl_pct": fsl,
    }


def gross_from_exit(side: str, entry: float, exit_px: float) -> float:
    if side == "LONG":
        return (exit_px / entry - 1.0) * 100.0
    return (entry - exit_px) / entry * 100.0


def _exit(tr, exit_time, exit_px, reason, gross, triggered, trig_time, ambiguity) -> dict[str, Any]:
    net = float(gross) - FEE_PCT
    return {
        "be50_triggered": bool(triggered),
        "be50_trigger_time": trig_time,
        "be50_exit_time": exit_time,
        "be50_exit_price": float(exit_px),
        "be50_reason": reason
        if not (ambiguity == "AMBIGUOUS_INTRABAR" and reason in ("SL", "BE"))
        else reason,
        "be50_gross_pct": float(gross),
        "be50_net_pct": float(net),
        "ambiguity_flag": ambiguity or "",
    }


def simulate_be50_trade(
    tr: pd.Series,
    c1m: pd.DataFrame,
    levels: dict[str, float],
) -> dict[str, Any]:
    """Walk 1m bars from entry until exit under BE50 (conservative intrabar)."""
    side = str(tr["side"])
    entry = levels["entry"]
    tp = levels["tp"]
    sl0 = levels["sl"]
    be_trig = levels["be_trigger"]
    et = _utc(tr["entry_time"])

    ts = pd.to_datetime(c1m["timestamp"], utc=True)
    mask = ts >= et
    xt_cap = _utc(tr["exit_time"]) + pd.Timedelta(days=10)
    sub = c1m.loc[mask & (ts <= xt_cap)].reset_index(drop=True)
    if sub.empty or pd.to_datetime(sub.iloc[0]["timestamp"], utc=True) != et:
        return {
            "be50_triggered": False,
            "be50_trigger_time": None,
            "be50_exit_time": None,
            "be50_exit_price": None,
            "be50_reason": "DATA_MISSING",
            "be50_gross_pct": None,
            "be50_net_pct": None,
            "ambiguity_flag": "ENTRY_BAR_MISSING",
        }

    armed = False
    arm_time = None
    ambiguity = ""

    highs = sub["high"].astype(float).to_numpy()
    lows = sub["low"].astype(float).to_numpy()
    times = pd.to_datetime(sub["timestamp"], utc=True)

    for i in range(len(sub)):
        h, l = float(highs[i]), float(lows[i])
        tbar = times.iloc[i]

        if side == "LONG":
            hit_be_trig = h >= be_trig
            hit_tp = h >= tp
            hit_sl0 = l <= sl0
            hit_be = l <= entry

            if not armed:
                if hit_sl0 and (hit_be_trig or hit_tp):
                    ambiguity = "AMBIGUOUS_INTRABAR"
                    gross = -levels["sl_pct"]
                    return _exit(tr, tbar, sl0, "SL", gross, False, None, ambiguity)
                if hit_sl0:
                    gross = -levels["sl_pct"]
                    return _exit(tr, tbar, sl0, "SL", gross, False, None, ambiguity)
                if hit_tp and hit_be_trig:
                    gross = levels["tp_pct"]
                    return _exit(tr, tbar, tp, "TP", gross, True, tbar, ambiguity)
                if hit_tp:
                    gross = levels["tp_pct"]
                    return _exit(tr, tbar, tp, "TP", gross, False, None, ambiguity)
                if hit_be_trig:
                    armed = True
                    arm_time = tbar
                    if hit_tp and hit_be:
                        ambiguity = "AMBIGUOUS_INTRABAR"
                        return _exit(tr, tbar, entry, "BE", 0.0, True, arm_time, ambiguity)
                    if hit_tp:
                        gross = levels["tp_pct"]
                        return _exit(tr, tbar, tp, "TP", gross, True, arm_time, ambiguity)
                    if hit_be:
                        return _exit(tr, tbar, entry, "BE", 0.0, True, arm_time, ambiguity)
                    continue
            else:
                if hit_tp and hit_be:
                    ambiguity = "AMBIGUOUS_INTRABAR"
                    return _exit(tr, tbar, entry, "BE", 0.0, True, arm_time, ambiguity)
                if hit_be:
                    return _exit(tr, tbar, entry, "BE", 0.0, True, arm_time, ambiguity)
                if hit_tp:
                    gross = levels["tp_pct"]
                    return _exit(tr, tbar, tp, "TP", gross, True, arm_time, ambiguity)

        else:  # SHORT
            hit_be_trig = l <= be_trig
            hit_tp = l <= tp
            hit_sl0 = h >= sl0
            hit_be = h >= entry

            if not armed:
                if hit_sl0 and (hit_be_trig or hit_tp):
                    ambiguity = "AMBIGUOUS_INTRABAR"
                    gross = -levels["sl_pct"]
                    return _exit(tr, tbar, sl0, "SL", gross, False, None, ambiguity)
                if hit_sl0:
                    gross = -levels["sl_pct"]
                    return _exit(tr, tbar, sl0, "SL", gross, False, None, ambiguity)
                if hit_tp and hit_be_trig:
                    gross = levels["tp_pct"]
                    return _exit(tr, tbar, tp, "TP", gross, True, tbar, ambiguity)
                if hit_tp:
                    gross = levels["tp_pct"]
                    return _exit(tr, tbar, tp, "TP", gross, False, None, ambiguity)
                if hit_be_trig:
                    armed = True
                    arm_time = tbar
                    if hit_tp and hit_be:
                        ambiguity = "AMBIGUOUS_INTRABAR"
                        return _exit(tr, tbar, entry, "BE", 0.0, True, arm_time, ambiguity)
                    if hit_tp:
                        gross = levels["tp_pct"]
                        return _exit(tr, tbar, tp, "TP", gross, True, arm_time, ambiguity)
                    if hit_be:
                        return _exit(tr, tbar, entry, "BE", 0.0, True, arm_time, ambiguity)
                    continue
            else:
                if hit_tp and hit_be:
                    ambiguity = "AMBIGUOUS_INTRABAR"
                    return _exit(tr, tbar, entry, "BE", 0.0, True, arm_time, ambiguity)
                if hit_be:
                    return _exit(tr, tbar, entry, "BE", 0.0, True, arm_time, ambiguity)
                if hit_tp:
                    gross = levels["tp_pct"]
                    return _exit(tr, tbar, tp, "TP", gross, True, arm_time, ambiguity)

    last = sub.iloc[-1]
    px = float(last["close"])
    gross = gross_from_exit(side, entry, px)
    return _exit(
        tr,
        times.iloc[-1],
        px,
        "TIMEOUT",
        gross,
        armed,
        arm_time,
        ambiguity or "PATH_EXHAUSTED",
    )


def prepare_book(c1m: pd.DataFrame) -> dict[str, Any]:
    """Freeze simulate_fast.prepare_book."""
    ts = pd.to_datetime(c1m["timestamp"], utc=True)
    return {
        "df": c1m.reset_index(drop=True),
        "ts_ns": ts.astype("int64").to_numpy(),
    }


def simulate_be50_trade_fast(
    tr: pd.Series, book: dict[str, Any], levels: dict[str, float]
) -> dict[str, Any]:
    """Freeze simulate_be50_trade_fast."""
    et = pd.Timestamp(tr["entry_time"])
    if et.tzinfo is None:
        et = et.tz_localize("UTC")
    else:
        et = et.tz_convert("UTC")
    xt_cap = pd.Timestamp(tr["exit_time"])
    if xt_cap.tzinfo is None:
        xt_cap = xt_cap.tz_localize("UTC")
    else:
        xt_cap = xt_cap.tz_convert("UTC")
    xt_cap = xt_cap + pd.Timedelta(days=10)

    ts_ns = book["ts_ns"]
    i0 = int(np.searchsorted(ts_ns, et.value, side="left"))
    i1 = int(np.searchsorted(ts_ns, xt_cap.value, side="right"))
    sub = book["df"].iloc[i0:i1].reset_index(drop=True)
    return simulate_be50_trade(tr, sub, levels)
