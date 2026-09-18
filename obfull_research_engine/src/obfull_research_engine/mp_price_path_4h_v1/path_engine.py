"""Core excursion / target / first-hit / underwater computation on 1m candles."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from .candles import Candle1m, floor_minute_ns
from .geometry import (
    mae_from_extremes,
    mfe_from_extremes,
    signed_return_pct,
)
from .params import (
    FIRST_HIT_HORIZONS_MIN,
    HORIZONS_MIN,
    INITIAL_ADVERSE_BEFORE_MFE,
    MAE_STOPS_PCT,
    MFE_TARGETS_PCT,
    NS,
)


@dataclass
class PathState:
    candles: list[Candle1m]
    entry_candle_partial: bool
    start_idx: int  # index of entry candle in candles list


def select_path_candles(
    all_candles: Sequence[Candle1m],
    *,
    trigger_ts_ns: int,
    horizon_min: int = 240,
) -> PathState | None:
    """Candles from entry minute through horizon_min minutes (inclusive count)."""
    entry_open = floor_minute_ns(trigger_ts_ns)
    end_open = entry_open + int(horizon_min) * 60 * NS  # exclusive end of last needed bar+1?
    # Need horizon_min completed minutes of observation: bars with open in
    # [entry_open, entry_open + horizon_min * 60s)
    # i.e. horizon_min candles starting at entry minute.
    needed = int(horizon_min)
    # find start
    idx = None
    for i, c in enumerate(all_candles):
        if c.open_time_ns == entry_open:
            idx = i
            break
        if c.open_time_ns > entry_open:
            return None  # gap / missing entry candle
    if idx is None:
        return None
    end_idx = idx + needed  # exclusive
    if end_idx > len(all_candles):
        return None
    slice_c = list(all_candles[idx:end_idx])
    # continuity check
    for j in range(1, len(slice_c)):
        if slice_c[j].open_time_ns - slice_c[j - 1].open_time_ns != 60 * NS:
            return None
    partial = trigger_ts_ns > entry_open
    return PathState(candles=slice_c, entry_candle_partial=partial, start_idx=idx)


def _running_excursions(
    candles: Sequence[Candle1m],
    *,
    trade_side: str,
    trigger_price: float,
) -> list[dict[str, Any]]:
    run_mfe = 0.0
    run_mae = 0.0
    rows = []
    entry_ns = candles[0].open_time_ns
    for i, c in enumerate(candles):
        bar_mfe = mfe_from_extremes(
            trade_side=trade_side, trigger_price=trigger_price, high=c.high, low=c.low
        )
        bar_mae = mae_from_extremes(
            trade_side=trade_side, trigger_price=trigger_price, high=c.high, low=c.low
        )
        new_mfe = bar_mfe > run_mfe + 1e-15
        new_mae = bar_mae > run_mae + 1e-15
        if new_mfe:
            run_mfe = bar_mfe
        if new_mae:
            run_mae = bar_mae
        close_ret = signed_return_pct(
            trade_side=trade_side, trigger_price=trigger_price, future_close=c.close
        )
        rows.append(
            {
                "candle_ts_ns": c.open_time_ns,
                "minutes_since_trigger": i,  # bar index; entry bar = 0
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "running_mfe_pct": run_mfe,
                "running_mae_pct": run_mae,
                "signed_close_return_pct": close_ret,
                "underwater": close_ret < 0,
                "new_mfe_high": new_mfe,
                "new_mae_high": new_mae,
                "bar_mfe_pct": bar_mfe,
                "bar_mae_pct": bar_mae,
            }
        )
    return rows


def _first_index_mfe_at_least(rows: Sequence[dict[str, Any]], target: float) -> int | None:
    for i, r in enumerate(rows):
        if r["running_mfe_pct"] + 1e-15 >= target or r["bar_mfe_pct"] + 1e-15 >= target:
            return i
    return None


def _mae_before_index(
    rows: Sequence[dict[str, Any]],
    peak_i: int,
    *,
    inclusive: bool,
) -> tuple[float, int | None, bool]:
    """Max bar MAE before (exclusive) or through (inclusive) peak candle.

    Returns (mae_pct, mae_ts_ns_or_none, intrabar_ambiguous).
    """
    if peak_i < 0:
        return 0.0, None, False
    if inclusive:
        window = rows[: peak_i + 1]
        ambiguous = rows[peak_i]["bar_mae_pct"] > 1e-15 and rows[peak_i]["bar_mfe_pct"] > 1e-15
    else:
        window = rows[:peak_i]
        ambiguous = False
    if not window:
        return 0.0, None, ambiguous
    best = 0.0
    best_ts = None
    for r in window:
        if r["bar_mae_pct"] >= best:
            best = r["bar_mae_pct"]
            best_ts = r["candle_ts_ns"]
    # also track running mae within window for true max adverse
    run = 0.0
    run_ts = None
    for r in window:
        if r["bar_mae_pct"] > run:
            run = r["bar_mae_pct"]
            run_ts = r["candle_ts_ns"]
    return run, run_ts, ambiguous


def compute_horizon_row(
    rows: Sequence[dict[str, Any]],
    *,
    horizon_min: int,
    trade_side: str,
    trigger_price: float,
    entry_partial: bool,
) -> dict[str, Any]:
    n = int(horizon_min)
    if len(rows) < n:
        return {
            "horizon_min": horizon_min,
            "is_complete": False,
            "censor_reason": "INCOMPLETE_CANDLES",
            "mfe_pct": None,
            "mae_pct": None,
            "close_return_pct": None,
            "candle_count": len(rows),
            "entry_candle_partial": entry_partial,
        }
    sub = rows[:n]
    mfe = sub[-1]["running_mfe_pct"]
    mae = sub[-1]["running_mae_pct"]
    # first times
    mfe_i = next((i for i, r in enumerate(sub) if abs(r["running_mfe_pct"] - mfe) < 1e-12 or r["bar_mfe_pct"] >= mfe - 1e-15), 0)
    # more precise: first index where running_mfe reaches final mfe
    mfe_i = 0
    for i, r in enumerate(sub):
        if r["running_mfe_pct"] + 1e-15 >= mfe:
            mfe_i = i
            break
    mae_i = 0
    for i, r in enumerate(sub):
        if r["running_mae_pct"] + 1e-15 >= mae:
            mae_i = i
            break
    mae_ex, mae_ex_ts, amb_ex = _mae_before_index(sub, mfe_i, inclusive=False)
    mae_in, mae_in_ts, amb_in = _mae_before_index(sub, mfe_i, inclusive=True)

    # adverse/favorable first using first non-zero bar extremes across candles
    first_adv_i = next((i for i, r in enumerate(sub) if r["bar_mae_pct"] > 1e-15), None)
    first_fav_i = next((i for i, r in enumerate(sub) if r["bar_mfe_pct"] > 1e-15), None)
    adverse_first = None
    favorable_first = None
    if first_adv_i is not None and first_fav_i is not None:
        if first_adv_i < first_fav_i:
            adverse_first, favorable_first = True, False
        elif first_fav_i < first_adv_i:
            adverse_first, favorable_first = False, True
        else:
            adverse_first, favorable_first = None, None  # same candle ambiguous
    elif first_adv_i is not None:
        adverse_first, favorable_first = True, False
    elif first_fav_i is not None:
        adverse_first, favorable_first = False, True

    crossed = any(r["signed_close_return_pct"] >= 0 for r in sub[: mfe_i + 1]) and any(
        r["signed_close_return_pct"] < 0 for r in sub[: mfe_i + 1]
    )

    return {
        "horizon_min": horizon_min,
        "is_complete": True,
        "censor_reason": "",
        "mfe_pct": mfe,
        "mae_pct": mae,
        "close_return_pct": sub[-1]["signed_close_return_pct"],
        "time_to_mfe_minutes": float(mfe_i),
        "time_to_mae_minutes": float(mae_i),
        "mfe_timestamp_ns": sub[mfe_i]["candle_ts_ns"],
        "mae_timestamp_ns": sub[mae_i]["candle_ts_ns"],
        "candle_count": n,
        "entry_candle_partial": entry_partial,
        "mfe_peak_ts_ns": sub[mfe_i]["candle_ts_ns"],
        "mae_before_mfe_peak_exclusive_pct": mae_ex,
        "mae_before_mfe_peak_inclusive_pct": mae_in,
        "mae_before_mfe_peak_exclusive_ts_ns": mae_ex_ts,
        "mae_before_mfe_peak_inclusive_ts_ns": mae_in_ts,
        "minutes_to_mfe_peak": float(mfe_i),
        "minutes_underwater_before_mfe": float(
            sum(1 for r in sub[:mfe_i] if r["underwater"])
        ),
        "crossed_entry_before_mfe": bool(crossed),
        "adverse_first": adverse_first,
        "favorable_first": favorable_first,
        "intrabar_order_ambiguous": bool(amb_in and mfe_i >= 0),
    }


def compute_target_rows(
    rows: Sequence[dict[str, Any]],
    *,
    max_horizon_min: int = 240,
) -> list[dict[str, Any]]:
    out = []
    sub = rows[:max_horizon_min]
    for tgt in MFE_TARGETS_PCT:
        idx = _first_index_mfe_at_least(sub, tgt)
        if idx is None:
            out.append(
                {
                    "target_pct": tgt,
                    "target_reached": False,
                    "first_target_ts_ns": None,
                    "minutes_to_target": None,
                    "mae_before_target_exclusive_pct": None,
                    "mae_before_target_inclusive_pct": None,
                    "max_mae_before_target_pct": None,
                    "intrabar_order_ambiguous": False,
                    "horizon_required_minutes": None,
                }
            )
            continue
        mae_ex, _, _ = _mae_before_index(sub, idx, inclusive=False)
        mae_in, _, amb = _mae_before_index(sub, idx, inclusive=True)
        out.append(
            {
                "target_pct": tgt,
                "target_reached": True,
                "first_target_ts_ns": sub[idx]["candle_ts_ns"],
                "minutes_to_target": float(idx),
                "mae_before_target_exclusive_pct": mae_ex,
                "mae_before_target_inclusive_pct": mae_in,
                "max_mae_before_target_pct": mae_in,
                "intrabar_order_ambiguous": amb,
                "horizon_required_minutes": float(idx + 1),
            }
        )
    return out


def first_hit(
    rows: Sequence[dict[str, Any]],
    *,
    target_pct: float,
    stop_pct: float,
    horizon_min: int,
) -> dict[str, Any]:
    if len(rows) < horizon_min:
        return {
            "horizon_min": horizon_min,
            "target_pct": target_pct,
            "stop_pct": stop_pct,
            "result": "CENSORED",
            "minutes_to_decision": None,
            "intrabar_order_ambiguous": False,
        }
    sub = rows[:horizon_min]
    for i, r in enumerate(sub):
        hit_t = r["bar_mfe_pct"] + 1e-15 >= target_pct or r["running_mfe_pct"] + 1e-15 >= target_pct
        hit_s = r["bar_mae_pct"] + 1e-15 >= stop_pct or r["running_mae_pct"] + 1e-15 >= stop_pct
        # use bar extremes for same-candle ambiguity
        bar_t = r["bar_mfe_pct"] + 1e-15 >= target_pct
        bar_s = r["bar_mae_pct"] + 1e-15 >= stop_pct
        if bar_t and bar_s:
            return {
                "horizon_min": horizon_min,
                "target_pct": target_pct,
                "stop_pct": stop_pct,
                "result": "AMBIGUOUS",
                "minutes_to_decision": float(i),
                "intrabar_order_ambiguous": True,
            }
        if hit_t and not hit_s:
            return {
                "horizon_min": horizon_min,
                "target_pct": target_pct,
                "stop_pct": stop_pct,
                "result": "TARGET_FIRST",
                "minutes_to_decision": float(i),
                "intrabar_order_ambiguous": False,
            }
        if hit_s and not hit_t:
            return {
                "horizon_min": horizon_min,
                "target_pct": target_pct,
                "stop_pct": stop_pct,
                "result": "STOP_FIRST",
                "minutes_to_decision": float(i),
                "intrabar_order_ambiguous": False,
            }
    return {
        "horizon_min": horizon_min,
        "target_pct": target_pct,
        "stop_pct": stop_pct,
        "result": "NEITHER",
        "minutes_to_decision": None,
        "intrabar_order_ambiguous": False,
    }


def compute_underwater(
    rows: Sequence[dict[str, Any]],
    *,
    horizon_min: int = 240,
) -> dict[str, Any]:
    sub = rows[:horizon_min] if len(rows) >= horizon_min else rows
    complete = len(rows) >= horizon_min
    underwater_flags = [bool(r["underwater"]) for r in sub]
    total_uw = sum(underwater_flags)
    # max consecutive underwater
    max_run = cur = 0
    for u in underwater_flags:
        if u:
            cur += 1
            max_run = max(max_run, cur)
        else:
            cur = 0
    first_pos = next((i for i, r in enumerate(sub) if r["signed_close_return_pct"] > 0), None)
    # entry recovered = first close >= 0 after having been underwater
    recovered = None
    seen_uw = False
    for i, r in enumerate(sub):
        if r["underwater"]:
            seen_uw = True
        elif seen_uw and r["signed_close_return_pct"] >= 0:
            recovered = i
            break
    # crossings: sign changes of close return
    crossings = 0
    for i in range(1, len(sub)):
        a = sub[i - 1]["signed_close_return_pct"]
        b = sub[i]["signed_close_return_pct"]
        if (a < 0 <= b) or (a >= 0 > b):
            crossings += 1

    out: dict[str, Any] = {
        "is_complete_240": complete,
        "minutes_until_first_positive": None if first_pos is None else float(first_pos),
        "minutes_until_entry_recovered": None if recovered is None else float(recovered),
        "maximum_underwater_duration_minutes": float(max_run),
        "total_underwater_minutes": float(total_uw),
        "percent_of_horizon_underwater": (100.0 * total_uw / len(sub)) if sub else None,
        "number_of_entry_crossings": crossings,
    }
    for h in HORIZONS_MIN:
        key = f"return_at_{h}m_pct"
        if len(rows) >= h:
            out[key] = rows[h - 1]["signed_close_return_pct"]
        else:
            out[key] = None

    # initial adverse before first positive / MFE targets
    for thr in INITIAL_ADVERSE_BEFORE_MFE:
        label = "0pct" if thr == 0.0 else f"{thr:.2f}".replace(".", "p") + "pct"
        if thr == 0.0:
            idx = first_pos
        else:
            idx = _first_index_mfe_at_least(sub, thr)
        if idx is None:
            out[f"initial_adverse_before_mfe_{label}_pct"] = None
            out[f"max_initial_adverse_before_mfe_{label}_pct"] = None
        else:
            mae_ex, _, _ = _mae_before_index(sub, idx, inclusive=False)
            mae_in, _, _ = _mae_before_index(sub, idx, inclusive=True)
            out[f"initial_adverse_before_mfe_{label}_pct"] = mae_ex
            out[f"max_initial_adverse_before_mfe_{label}_pct"] = mae_in
    return out


def analyze_event_path(
    all_candles: Sequence[Candle1m],
    *,
    event_id: str,
    trigger_ts_ns: int,
    trigger_price: float,
    trade_side: str,
) -> dict[str, Any]:
    state = select_path_candles(all_candles, trigger_ts_ns=trigger_ts_ns, horizon_min=240)
    if state is None:
        return {
            "event_id": event_id,
            "ok": False,
            "censor_reason": "MISSING_OR_GAPPED_CANDLES",
            "path_rows": [],
            "horizons": [],
            "targets": [],
            "first_hits": [],
            "underwater": {},
        }
    rows = _running_excursions(
        state.candles, trade_side=trade_side, trigger_price=trigger_price
    )
    for r in rows:
        r["event_id"] = event_id
    horizons = [
        {
            "event_id": event_id,
            **compute_horizon_row(
                rows,
                horizon_min=h,
                trade_side=trade_side,
                trigger_price=trigger_price,
                entry_partial=state.entry_candle_partial,
            ),
        }
        for h in HORIZONS_MIN
    ]
    targets = [{"event_id": event_id, **t} for t in compute_target_rows(rows)]
    first_hits = []
    for h in FIRST_HIT_HORIZONS_MIN:
        for t in MFE_TARGETS_PCT:
            for s in MAE_STOPS_PCT:
                first_hits.append(
                    {"event_id": event_id, **first_hit(rows, target_pct=t, stop_pct=s, horizon_min=h)}
                )
    uw = {"event_id": event_id, **compute_underwater(rows)}
    return {
        "event_id": event_id,
        "ok": True,
        "censor_reason": "",
        "entry_candle_partial": state.entry_candle_partial,
        "path_rows": rows,
        "horizons": horizons,
        "targets": targets,
        "first_hits": first_hits,
        "underwater": uw,
    }
