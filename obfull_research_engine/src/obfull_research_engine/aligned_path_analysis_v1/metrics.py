"""Directional path metrics. Pure functions; no I/O; no HC class mutation."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from . import (
    CHOP_EXPANSION_PCT,
    CHOP_MIN_ZERO_CROSSES,
    FIRST_MOVE_FLOOR_PCT,
    HORIZONS_S,
    MARK_PCTS,
    MAX_HORIZON_S,
    PATH_COMPLETE_SLACK_MS,
    RELEVANT_MARK_PCT,
    SEQUENCE_CLASSES,
)

NA = None


def directional_return_pct(price: float, reference_price: float, direction: str) -> float:
    if reference_price <= 0 or price <= 0:
        raise ValueError("prices must be positive")
    d = str(direction).upper()
    if d == "BULLISH":
        return (price / reference_price - 1.0) * 100.0
    if d == "BEARISH":
        return (reference_price / price - 1.0) * 100.0
    raise ValueError(f"unsupported direction: {direction}")


def _iso(ts: pd.Timestamp | None) -> str | None:
    if ts is None or pd.isna(ts):
        return None
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    return t.isoformat().replace("+00:00", "Z")


def _ms_since(origin: pd.Timestamp, ts: pd.Timestamp | None) -> float | None:
    if ts is None or pd.isna(ts):
        return None
    return float((pd.Timestamp(ts) - pd.Timestamp(origin)).total_seconds() * 1000.0)


def _sign(val: float, *, floor: float) -> int:
    if val >= floor:
        return 1
    if val <= -floor:
        return -1
    return 0


def count_zero_crossings(returns: np.ndarray, *, floor: float = FIRST_MOVE_FLOOR_PCT) -> int:
    if returns.size < 2:
        return 0
    last = 0
    crosses = 0
    for val in returns:
        s = _sign(float(val), floor=floor)
        if s == 0:
            continue
        if last != 0 and s != last:
            crosses += 1
        last = s
    return crosses


def first_index_at_or_beyond(returns: np.ndarray, threshold: float) -> int | None:
    if returns.size == 0:
        return None
    if threshold >= 0:
        hits = np.where(returns >= threshold)[0]
    else:
        hits = np.where(returns <= threshold)[0]
    if hits.size == 0:
        return None
    return int(hits[0])


def _ratio(num: float | None, den: float | None) -> float | None:
    if num is None or den is None:
        return NA
    if den <= 0:
        return NA
    return float(num) / float(den)


def analyze_path(
    *,
    ts: np.ndarray,
    prices: np.ndarray,
    detection: pd.Timestamp,
    direction: str,
    reference_price: float,
    reference_price_ts: pd.Timestamp,
    price_source: str,
    path_resolution_ms: int,
    horizon_s: int = MAX_HORIZON_S,
    first_move_floor_pct: float = FIRST_MOVE_FLOOR_PCT,
) -> dict[str, Any]:
    """Compute path metrics on points with detection <= t <= detection+horizon."""
    detection = pd.Timestamp(detection)
    if detection.tzinfo is None:
        detection = detection.tz_localize("UTC")
    else:
        detection = detection.tz_convert("UTC")
    horizon_end = detection + pd.Timedelta(seconds=int(horizon_s))
    if ts.size == 0:
        return _empty_path(
            detection=detection,
            direction=direction,
            reference_price=reference_price,
            reference_price_ts=reference_price_ts,
            price_source=price_source,
            path_resolution_ms=path_resolution_ms,
            horizon_s=horizon_s,
            reason="NO_PATH_POINTS",
        )

    tser = pd.DatetimeIndex(pd.to_datetime(ts, utc=True))
    mask = np.asarray((tser >= detection) & (tser <= horizon_end), dtype=bool)
    t_use = tser[mask]
    p_use = np.asarray(prices, dtype=float)[mask]
    if t_use.size == 0:
        return _empty_path(
            detection=detection,
            direction=direction,
            reference_price=reference_price,
            reference_price_ts=reference_price_ts,
            price_source=price_source,
            path_resolution_ms=path_resolution_ms,
            horizon_s=horizon_s,
            reason="NO_PATH_POINTS_AFTER_DETECTION",
        )

    rets = np.array(
        [directional_return_pct(float(p), float(reference_price), direction) for p in p_use],
        dtype=float,
    )
    last_ts = pd.Timestamp(t_use[-1])
    closing = float(rets[-1])
    endpoint_age_ms = float((horizon_end - last_ts).total_seconds() * 1000.0)
    complete = bool(rets.size >= 1 and endpoint_age_ms <= float(PATH_COMPLETE_SLACK_MS))

    i_mfe = int(np.argmax(rets))
    i_mae = int(np.argmin(rets))
    mfe = float(rets[i_mfe])
    mae = float(rets[i_mae])
    mfe_ts = pd.Timestamp(t_use[i_mfe])
    mae_ts = pd.Timestamp(t_use[i_mae])
    mfe_before_mae = bool(i_mfe < i_mae) if i_mfe != i_mae else None
    mae_before_mfe = bool(i_mae < i_mfe) if i_mfe != i_mae else None

    i_profit = first_index_at_or_beyond(rets, float(first_move_floor_pct))
    i_adverse = first_index_at_or_beyond(rets, -float(first_move_floor_pct))
    i_first = None
    first_dir = "FLAT_OR_AMBIGUOUS"
    for i, val in enumerate(rets):
        if abs(float(val)) >= float(first_move_floor_pct):
            i_first = i
            first_dir = "PROFIT_FIRST" if val > 0 else "ADVERSE_FIRST"
            break

    first_profit_ts = pd.Timestamp(t_use[i_profit]) if i_profit is not None else None
    first_adverse_ts = pd.Timestamp(t_use[i_adverse]) if i_adverse is not None else None
    first_move_ts = pd.Timestamp(t_use[i_first]) if i_first is not None else None
    first_move_pct = float(rets[i_first]) if i_first is not None else None

    if i_profit is not None:
        mae_before_first_profit = float(np.min(rets[: i_profit + 1]))
        time_underwater = 0.0
        if i_adverse is not None and i_adverse < i_profit:
            time_underwater = float((t_use[i_profit] - t_use[i_adverse]).total_seconds() * 1000.0)
        longest_initial = _longest_initial_underwater_ms(t_use, rets, stop_idx=i_profit)
    else:
        mae_before_first_profit = float(np.min(rets))
        time_underwater = float((t_use[-1] - (t_use[i_adverse] if i_adverse is not None else t_use[0])).total_seconds() * 1000.0) if i_adverse is not None else 0.0
        longest_initial = _longest_initial_underwater_ms(t_use, rets, stop_idx=len(rets) - 1)

    marks = _mark_table(t_use, rets, detection)
    first_mark_side, first_mark_abs = _first_mark_overall(marks)
    symmetric = {abs_pct: _symmetric_winner(marks, abs_pct) for abs_pct in MARK_PCTS}

    mae_before_marks: dict[str, float | None] = {}
    for abs_pct in (0.05, 0.10, 0.20):
        key = f"{abs_pct:.2f}".replace(".", "_")
        i_hit = first_index_at_or_beyond(rets, abs_pct)
        if i_hit is None:
            mae_before_marks[key] = float(np.min(rets))
        else:
            mae_before_marks[key] = float(np.min(rets[: i_hit + 1]))

    post = _post_mfe(
        t_use=t_use,
        rets=rets,
        i_mfe=i_mfe,
        peak_mfe=mfe,
        closing=closing,
        detection=detection,
    )
    n_cross = count_zero_crossings(rets)
    seq = classify_sequence(
        first_direction=first_dir,
        first_profit=i_profit is not None,
        crossed_below_zero_after_mfe=bool(post["crossed_back_below_zero_after_mfe"]),
        crossed_below_zero_after_first_profit=_crossed_below_zero_after(t_use, rets, i_profit),
        relevant_winner=symmetric.get(RELEVANT_MARK_PCT) or first_dir,
        n_zero_crossings=n_cross,
        mfe_pct=mfe,
        mae_pct=mae,
        n_points=int(rets.size),
    )

    return {
        "detection_available_at": _iso(detection),
        "direction": str(direction).upper(),
        "reference_price": float(reference_price),
        "reference_price_ts": _iso(pd.Timestamp(reference_price_ts)),
        "price_source": price_source,
        "path_resolution_ms": int(path_resolution_ms),
        "horizon_seconds": int(horizon_s),
        "horizon_end_ts": _iso(horizon_end),
        "n_path_points": int(rets.size),
        "path_first_ts": _iso(pd.Timestamp(t_use[0])),
        "path_last_ts": _iso(last_ts),
        "path_complete": bool(complete),
        "first_nonzero_direction": first_dir,
        "first_move_pct": first_move_pct,
        "first_move_ts": _iso(first_move_ts),
        "first_profit_ts": _iso(first_profit_ts),
        "time_to_first_profit_ms": _ms_since(detection, first_profit_ts),
        "first_adverse_ts": _iso(first_adverse_ts),
        "time_to_first_adverse_ms": _ms_since(detection, first_adverse_ts),
        "mae_before_first_profit_pct": mae_before_first_profit,
        "mae_before_positive_0_05_pct": mae_before_marks["0_05"],
        "mae_before_positive_0_10_pct": mae_before_marks["0_10"],
        "mae_before_positive_0_20_pct": mae_before_marks["0_20"],
        "time_underwater_before_first_profit_ms": time_underwater,
        "longest_initial_underwater_duration_ms": longest_initial,
        "mfe_pct": mfe,
        "mfe_ts": _iso(mfe_ts),
        "time_to_mfe_ms": _ms_since(detection, mfe_ts),
        "mae_pct": mae,
        "mae_ts": _iso(mae_ts),
        "time_to_mae_ms": _ms_since(detection, mae_ts),
        "mfe_before_mae": mfe_before_mae,
        "mae_before_mfe": mae_before_mfe,
        "closing_directional_return_pct": closing,
        "peak_mfe_pct": post["peak_mfe_pct"],
        "post_mfe_min_return_pct": post["post_mfe_min_return_pct"],
        "giveback_from_mfe_pct": post["giveback_from_mfe_pct"],
        "giveback_fraction_of_mfe": post["giveback_fraction_of_mfe"],
        "retained_profit_at_horizon_pct": post["retained_profit_at_horizon_pct"],
        "retained_fraction_of_mfe": post["retained_fraction_of_mfe"],
        "crossed_back_below_zero_after_mfe": post["crossed_back_below_zero_after_mfe"],
        "time_from_mfe_to_zero_cross_ms": post["time_from_mfe_to_zero_cross_ms"],
        "post_mfe_adverse_excursion_pct": post["post_mfe_adverse_excursion_pct"],
        "n_zero_crossings": int(n_cross),
        "first_mark_side": first_mark_side,
        "first_mark_abs_pct": first_mark_abs,
        "symmetric_first_0_05": symmetric[0.05],
        "symmetric_first_0_10": symmetric[0.10],
        "symmetric_first_0_20": symmetric[0.20],
        "symmetric_first_0_30": symmetric[0.30],
        "symmetric_first_0_50": symmetric[0.50],
        "symmetric_first_0_75": symmetric[0.75],
        "symmetric_first_1_00": symmetric[1.00],
        "sequence_class": seq,
        "marks": marks,
        "path_status": "OK" if complete else "PARTIAL_HORIZON",
    }


def _longest_initial_underwater_ms(t_use: pd.DatetimeIndex, rets: np.ndarray, *, stop_idx: int) -> float:
    start = None
    for i in range(0, min(int(stop_idx) + 1, rets.size)):
        if rets[i] < 0:
            start = i
            break
    if start is None:
        return 0.0
    end = int(stop_idx)
    for i in range(start + 1, min(int(stop_idx) + 1, rets.size)):
        if rets[i] >= 0:
            end = i
            break
    return float((t_use[end] - t_use[start]).total_seconds() * 1000.0)


def _crossed_below_zero_after(t_use: pd.DatetimeIndex, rets: np.ndarray, i_profit: int | None) -> bool:
    if i_profit is None:
        return False
    if i_profit + 1 >= rets.size:
        return False
    return bool(np.any(rets[i_profit + 1 :] < 0))


def _post_mfe(
    *,
    t_use: pd.DatetimeIndex,
    rets: np.ndarray,
    i_mfe: int,
    peak_mfe: float,
    closing: float,
    detection: pd.Timestamp,
) -> dict[str, Any]:
    post = rets[i_mfe:]
    post_t = t_use[i_mfe:]
    post_min = float(np.min(post))
    giveback = float(peak_mfe - post_min)
    crossed = False
    cross_ms = NA
    if peak_mfe > 0:
        for j in range(1, post.size):
            if post[j] < 0:
                crossed = True
                cross_ms = float((post_t[j] - post_t[0]).total_seconds() * 1000.0)
                break
    post_adverse = float(min(0.0, post_min))
    return {
        "peak_mfe_pct": float(peak_mfe),
        "post_mfe_min_return_pct": post_min,
        "giveback_from_mfe_pct": giveback,
        "giveback_fraction_of_mfe": _ratio(giveback, peak_mfe if peak_mfe > 0 else None),
        "retained_profit_at_horizon_pct": float(closing),
        "retained_fraction_of_mfe": _ratio(closing, peak_mfe if peak_mfe > 0 else None),
        "crossed_back_below_zero_after_mfe": crossed,
        "time_from_mfe_to_zero_cross_ms": cross_ms,
        "post_mfe_adverse_excursion_pct": post_adverse,
    }


def _mark_key(abs_pct: float, side: str) -> str:
    token = f"{abs_pct:.2f}".replace(".", "_")
    return f"{side}_{token}"


def _mark_table(t_use: pd.DatetimeIndex, rets: np.ndarray, detection: pd.Timestamp) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for abs_pct in MARK_PCTS:
        for side, thr in (("profit", abs_pct), ("adverse", -abs_pct)):
            idx = first_index_at_or_beyond(rets, thr)
            reached = idx is not None
            ts = pd.Timestamp(t_use[idx]) if reached else None
            out[_mark_key(abs_pct, side)] = {
                "abs_pct": abs_pct,
                "side": side,
                "threshold_pct": thr,
                "reached": reached,
                "first_reached_at": _iso(ts),
                "time_to_reach_ms": _ms_since(detection, ts),
            }
    return out


def _first_mark_overall(marks: dict[str, dict[str, Any]]) -> tuple[str | None, float | None]:
    best = None
    for rec in marks.values():
        if not rec.get("reached"):
            continue
        ms = rec.get("time_to_reach_ms")
        if ms is None:
            continue
        cand = (float(ms), float(rec["abs_pct"]), 0 if rec["side"] == "profit" else 1, rec)
        if best is None or cand[:3] < best[:3]:
            best = cand
    if best is None:
        return None, None
    rec = best[3]
    return ("PROFIT" if rec["side"] == "profit" else "ADVERSE"), float(rec["abs_pct"])


def _symmetric_winner(marks: dict[str, dict[str, Any]], abs_pct: float) -> str:
    p = marks[_mark_key(abs_pct, "profit")]
    a = marks[_mark_key(abs_pct, "adverse")]
    pr, ar = p.get("reached"), a.get("reached")
    if not pr and not ar:
        return "NEITHER"
    if pr and not ar:
        return "PROFIT"
    if ar and not pr:
        return "ADVERSE"
    pms, ams = p.get("time_to_reach_ms"), a.get("time_to_reach_ms")
    if pms is None and ams is None:
        return "TIE"
    if pms is None:
        return "ADVERSE"
    if ams is None:
        return "PROFIT"
    if float(pms) < float(ams):
        return "PROFIT"
    if float(ams) < float(pms):
        return "ADVERSE"
    return "TIE"


def classify_sequence(
    *,
    first_direction: str,
    first_profit: bool,
    crossed_below_zero_after_mfe: bool,
    crossed_below_zero_after_first_profit: bool,
    relevant_winner: str,
    n_zero_crossings: int,
    mfe_pct: float,
    mae_pct: float,
    n_points: int,
) -> str:
    if n_points < 2:
        return "FLAT_OR_INSUFFICIENT_DATA"
    expansion = max(abs(float(mfe_pct)), abs(float(mae_pct)))
    if n_zero_crossings >= CHOP_MIN_ZERO_CROSSES and expansion < CHOP_EXPANSION_PCT:
        return "CHOP_AROUND_ENTRY"
    relevant = str(relevant_winner)
    profit_first = first_direction == "PROFIT_FIRST" or relevant == "PROFIT"
    adverse_first = first_direction == "ADVERSE_FIRST" or relevant == "ADVERSE"
    # Prefer the relevant ±0.10 mark when it exists; otherwise first_nonzero.
    if relevant == "PROFIT":
        profit_first, adverse_first = True, False
    elif relevant == "ADVERSE":
        profit_first, adverse_first = False, True
    elif first_direction == "PROFIT_FIRST":
        profit_first, adverse_first = True, False
    elif first_direction == "ADVERSE_FIRST":
        profit_first, adverse_first = False, True
    else:
        return "FLAT_OR_INSUFFICIENT_DATA"
    gave_back = bool(crossed_below_zero_after_first_profit or crossed_below_zero_after_mfe)
    if profit_first:
        if gave_back:
            return "DIRECT_PROFIT_GIVEN_BACK"
        return "DIRECT_PROFIT_HELD"
    if first_profit:
        return "ADVERSE_THEN_PROFIT"
    return "ADVERSE_NO_RECOVERY"


def _empty_path(
    *,
    detection: pd.Timestamp,
    direction: str,
    reference_price: float | None,
    reference_price_ts: pd.Timestamp | None,
    price_source: str,
    path_resolution_ms: int,
    horizon_s: int,
    reason: str,
) -> dict[str, Any]:
    marks = {}
    for abs_pct in MARK_PCTS:
        for side in ("profit", "adverse"):
            marks[_mark_key(abs_pct, side)] = {
                "abs_pct": abs_pct,
                "side": side,
                "threshold_pct": abs_pct if side == "profit" else -abs_pct,
                "reached": False,
                "first_reached_at": None,
                "time_to_reach_ms": None,
            }
    return {
        "detection_available_at": _iso(detection),
        "direction": str(direction).upper(),
        "reference_price": reference_price,
        "reference_price_ts": _iso(reference_price_ts) if reference_price_ts is not None else None,
        "price_source": price_source,
        "path_resolution_ms": int(path_resolution_ms),
        "horizon_seconds": int(horizon_s),
        "horizon_end_ts": _iso(detection + pd.Timedelta(seconds=int(horizon_s))),
        "n_path_points": 0,
        "path_first_ts": None,
        "path_last_ts": None,
        "path_complete": False,
        "first_nonzero_direction": "FLAT_OR_AMBIGUOUS",
        "first_move_pct": None,
        "first_move_ts": None,
        "first_profit_ts": None,
        "time_to_first_profit_ms": None,
        "first_adverse_ts": None,
        "time_to_first_adverse_ms": None,
        "mae_before_first_profit_pct": None,
        "mae_before_positive_0_05_pct": None,
        "mae_before_positive_0_10_pct": None,
        "mae_before_positive_0_20_pct": None,
        "time_underwater_before_first_profit_ms": None,
        "longest_initial_underwater_duration_ms": None,
        "mfe_pct": None,
        "mfe_ts": None,
        "time_to_mfe_ms": None,
        "mae_pct": None,
        "mae_ts": None,
        "time_to_mae_ms": None,
        "mfe_before_mae": None,
        "mae_before_mfe": None,
        "closing_directional_return_pct": None,
        "peak_mfe_pct": None,
        "post_mfe_min_return_pct": None,
        "giveback_from_mfe_pct": None,
        "giveback_fraction_of_mfe": None,
        "retained_profit_at_horizon_pct": None,
        "retained_fraction_of_mfe": None,
        "crossed_back_below_zero_after_mfe": None,
        "time_from_mfe_to_zero_cross_ms": None,
        "post_mfe_adverse_excursion_pct": None,
        "n_zero_crossings": 0,
        "first_mark_side": None,
        "first_mark_abs_pct": None,
        "symmetric_first_0_05": "NEITHER",
        "symmetric_first_0_10": "NEITHER",
        "symmetric_first_0_20": "NEITHER",
        "symmetric_first_0_30": "NEITHER",
        "symmetric_first_0_50": "NEITHER",
        "symmetric_first_0_75": "NEITHER",
        "symmetric_first_1_00": "NEITHER",
        "sequence_class": "FLAT_OR_INSUFFICIENT_DATA",
        "marks": marks,
        "path_status": reason,
    }


def analyze_all_horizons(**kwargs: Any) -> list[dict[str, Any]]:
    rows = []
    for hz in HORIZONS_S:
        rows.append(analyze_path(horizon_s=int(hz), **kwargs))
    return rows


assert set(SEQUENCE_CLASSES)
