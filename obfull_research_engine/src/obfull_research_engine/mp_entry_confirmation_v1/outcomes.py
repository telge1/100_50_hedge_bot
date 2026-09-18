"""Target / stop ordering and post-entry path metrics (percent)."""

from __future__ import annotations

from typing import Any, Sequence

from obfull_research_engine.mp_price_path_4h_v1.candles import Candle1m, floor_minute_ns
from obfull_research_engine.mp_price_path_4h_v1.geometry import (
    mae_from_extremes,
    mfe_from_extremes,
    signed_return_pct,
)

from .params import (
    CLOSE_RETURN_HORIZONS_MIN,
    DIAGNOSTIC_PAIRS,
    MAX_HOLDING_MINUTES,
    NS,
    PATH_HORIZONS_MIN,
    STOP_PCT,
    TARGET_PCT,
)


def target_price(*, trade_side: str, entry_price: float, target_pct: float = TARGET_PCT) -> float:
    side = trade_side.upper()
    if side == "LONG":
        return entry_price * (1.0 + target_pct / 100.0)
    if side == "SHORT":
        return entry_price * (1.0 - target_pct / 100.0)
    raise ValueError(trade_side)


def stop_price(*, trade_side: str, entry_price: float, stop_pct: float = STOP_PCT) -> float:
    side = trade_side.upper()
    if side == "LONG":
        return entry_price * (1.0 - stop_pct / 100.0)
    if side == "SHORT":
        return entry_price * (1.0 + stop_pct / 100.0)
    raise ValueError(trade_side)


def bar_hits_target(trade_side: str, entry_price: float, high: float, low: float, target_pct: float) -> bool:
    tp = target_price(trade_side=trade_side, entry_price=entry_price, target_pct=target_pct)
    if trade_side.upper() == "LONG":
        return high + 1e-15 >= tp
    return low - 1e-15 <= tp


def bar_hits_stop(trade_side: str, entry_price: float, high: float, low: float, stop_pct: float) -> bool:
    sp = stop_price(trade_side=trade_side, entry_price=entry_price, stop_pct=stop_pct)
    if trade_side.upper() == "LONG":
        return low - 1e-15 <= sp
    return high + 1e-15 >= sp


def evaluate_target_stop(
    candles: Sequence[Candle1m],
    *,
    trade_side: str,
    entry_price: float,
    target_pct: float = TARGET_PCT,
    stop_pct: float = STOP_PCT,
    max_holding_minutes: int = MAX_HOLDING_MINUTES,
) -> dict[str, Any]:
    """Walk 1m bars from entry; return TARGET_FIRST / STOP_FIRST / NEITHER / AMBIGUOUS / CENSORED."""
    if not candles:
        return {
            "result": "CENSORED",
            "censor_reason": "NO_CANDLES",
            "target_pct": target_pct,
            "stop_pct": stop_pct,
            "minutes_to_target": None,
            "minutes_to_stop": None,
            "hit_ts_ns": None,
        }
    n = min(len(candles), int(max_holding_minutes))
    if len(candles) < int(max_holding_minutes):
        # still evaluate available bars; mark censored if neither hit
        censored_incomplete = True
    else:
        censored_incomplete = False
    for i in range(n):
        c = candles[i]
        ht = bar_hits_target(trade_side, entry_price, c.high, c.low, target_pct)
        hs = bar_hits_stop(trade_side, entry_price, c.high, c.low, stop_pct)
        if ht and hs:
            return {
                "result": "AMBIGUOUS",
                "conservative_result": "STOP_FIRST",
                "target_pct": target_pct,
                "stop_pct": stop_pct,
                "minutes_to_target": i,
                "minutes_to_stop": i,
                "hit_ts_ns": c.open_time_ns,
                "censor_reason": None,
            }
        if ht:
            return {
                "result": "TARGET_FIRST",
                "conservative_result": "TARGET_FIRST",
                "target_pct": target_pct,
                "stop_pct": stop_pct,
                "minutes_to_target": i,
                "minutes_to_stop": None,
                "hit_ts_ns": c.open_time_ns,
                "censor_reason": None,
            }
        if hs:
            return {
                "result": "STOP_FIRST",
                "conservative_result": "STOP_FIRST",
                "target_pct": target_pct,
                "stop_pct": stop_pct,
                "minutes_to_target": None,
                "minutes_to_stop": i,
                "hit_ts_ns": c.open_time_ns,
                "censor_reason": None,
            }
    if censored_incomplete:
        return {
            "result": "CENSORED",
            "conservative_result": "CENSORED",
            "target_pct": target_pct,
            "stop_pct": stop_pct,
            "minutes_to_target": None,
            "minutes_to_stop": None,
            "hit_ts_ns": None,
            "censor_reason": "INCOMPLETE_HOLDING_WINDOW",
            "close_return_pct": signed_return_pct(
                trade_side=trade_side, trigger_price=entry_price, future_close=candles[n - 1].close
            )
            if n
            else None,
        }
    close_ret = signed_return_pct(
        trade_side=trade_side, trigger_price=entry_price, future_close=candles[n - 1].close
    )
    return {
        "result": "NEITHER",
        "conservative_result": "NEITHER",
        "target_pct": target_pct,
        "stop_pct": stop_pct,
        "minutes_to_target": None,
        "minutes_to_stop": None,
        "hit_ts_ns": None,
        "censor_reason": None,
        "close_return_pct": close_ret,
    }


def select_entry_path(
    all_candles: Sequence[Candle1m],
    *,
    entry_ts_ns: int,
    horizon_min: int = MAX_HOLDING_MINUTES,
) -> list[Candle1m] | None:
    entry_open = floor_minute_ns(entry_ts_ns)
    # entry_ts should already be a minute open; require exact match
    idx = None
    for i, c in enumerate(all_candles):
        if c.open_time_ns == entry_open:
            idx = i
            break
        if c.open_time_ns > entry_open:
            return None
    if idx is None:
        return None
    end = idx + int(horizon_min)
    if end > len(all_candles):
        # return available for censor handling
        slice_c = list(all_candles[idx:])
        if not slice_c:
            return None
        for j in range(1, len(slice_c)):
            if slice_c[j].open_time_ns - slice_c[j - 1].open_time_ns != 60 * NS:
                return None
        return slice_c
    slice_c = list(all_candles[idx:end])
    for j in range(1, len(slice_c)):
        if slice_c[j].open_time_ns - slice_c[j - 1].open_time_ns != 60 * NS:
            return None
    return slice_c


def compute_path_metrics(
    candles: Sequence[Candle1m],
    *,
    trade_side: str,
    entry_price: float,
    target_pct: float = TARGET_PCT,
) -> dict[str, Any]:
    run_mfe = 0.0
    run_mae = 0.0
    rows = []
    first_pos_i = None
    first_recover_i = None
    underwater_run = 0
    max_underwater = 0
    for i, c in enumerate(candles):
        bar_mfe = mfe_from_extremes(
            trade_side=trade_side, trigger_price=entry_price, high=c.high, low=c.low
        )
        bar_mae = mae_from_extremes(
            trade_side=trade_side, trigger_price=entry_price, high=c.high, low=c.low
        )
        run_mfe = max(run_mfe, bar_mfe)
        run_mae = max(run_mae, bar_mae)
        close_ret = signed_return_pct(
            trade_side=trade_side, trigger_price=entry_price, future_close=c.close
        )
        if first_pos_i is None and close_ret > 0:
            first_pos_i = i
        if close_ret < 0:
            underwater_run += 1
            max_underwater = max(max_underwater, underwater_run)
        else:
            if underwater_run > 0 and first_recover_i is None and close_ret >= 0:
                first_recover_i = i
            underwater_run = 0
        rows.append(
            {
                "minutes_since_entry": i,
                "candle_ts_ns": c.open_time_ns,
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "running_mfe_pct": run_mfe,
                "running_mae_pct": run_mae,
                "bar_mfe_pct": bar_mfe,
                "bar_mae_pct": bar_mae,
                "signed_close_return_pct": close_ret,
            }
        )

    def horizon_mfe_mae(h: int) -> tuple[float | None, float | None]:
        if len(rows) < h:
            return None, None
        return rows[h - 1]["running_mfe_pct"], rows[h - 1]["running_mae_pct"]

    out: dict[str, Any] = {"path_rows": rows}
    for h in PATH_HORIZONS_MIN:
        mfe, mae = horizon_mfe_mae(h)
        out[f"mfe_{h}m_pct"] = mfe
        out[f"mae_{h}m_pct"] = mae
    for h in CLOSE_RETURN_HORIZONS_MIN:
        if len(rows) >= h:
            out[f"close_return_{h}m_pct"] = rows[h - 1]["signed_close_return_pct"]
        else:
            out[f"close_return_{h}m_pct"] = None

    # time to target_pct MFE
    t041 = None
    for r in rows:
        if r["running_mfe_pct"] + 1e-15 >= target_pct or r["bar_mfe_pct"] + 1e-15 >= target_pct:
            t041 = int(r["minutes_since_entry"])
            break
    mae_before = 0.0
    if t041 is not None:
        before = rows[:t041]
        if before:
            mae_before = float(max(x["running_mae_pct"] for x in before))
            mae_before = max(mae_before, float(max(x["bar_mae_pct"] for x in before)))
    out["minutes_to_0_41_pct"] = t041
    out["mae_before_0_41_pct"] = mae_before if t041 is not None else None
    out["max_underwater_minutes"] = max_underwater
    out["minutes_to_first_positive_return"] = first_pos_i
    out["minutes_to_entry_recovery"] = first_recover_i
    return out


def evaluate_all_pairs(
    candles: Sequence[Candle1m],
    *,
    trade_side: str,
    entry_price: float,
) -> dict[str, Any]:
    primary = evaluate_target_stop(
        candles,
        trade_side=trade_side,
        entry_price=entry_price,
        target_pct=TARGET_PCT,
        stop_pct=STOP_PCT,
    )
    diag = []
    for tp, sp in DIAGNOSTIC_PAIRS:
        diag.append(
            evaluate_target_stop(
                candles,
                trade_side=trade_side,
                entry_price=entry_price,
                target_pct=tp,
                stop_pct=sp,
            )
        )
    return {"primary": primary, "diagnostic": diag}
