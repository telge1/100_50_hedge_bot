#!/usr/bin/env python3
"""Diagnostic-only: do visual APTUSDT 30m PA points match production structure pivots?

No production changes. Uses aggregate_candles + find_confirmed_pivots + update_market_structure.
"""
from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pandas as pd

from research.regime_scanner.config import default_regime_scanner_config
from research.regime_scanner.data_loader import load_symbol_candles
from research.regime_scanner.indicators import compute_indicator_frame
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.structure import classify_swing_structure
from research.regime_scanner.swings import ConfirmedPivot, find_confirmed_pivots
from research.regime_scanner.timeframes import aggregate_candles, timeframe_timedelta
from research.regime_scanner.trend_structure import (
    MarketStructureState,
    default_trend_structure_config,
    has_hh_hl,
    has_lh_ll,
    update_market_structure,
)

OUT = Path("research/regime_scanner/results/trend_structure_30m_point_audit")
MACHINE = Path("research/regime_scanner/trend_state_machine.py")
STRUCTURE = Path("research/regime_scanner/trend_structure.py")
POLICY = Path("research/regime_scanner/trend_state_policy.py")

REPLAY_START = "2025-12-20T00:00:00+00:00"
REPLAY_END = "2026-03-15T00:00:00+00:00"

REQUESTED: list[dict[str, Any]] = [
    {"timestamp": "2026-02-25T20:00:00+00:00", "visual_role": "Higher High"},
    {"timestamp": "2026-02-27T09:00:00+00:00", "visual_role": "Lower High"},
    {"timestamp": "2026-02-28T12:30:00+00:00", "visual_role": "Higher Low"},
    {"timestamp": "2026-03-03T19:00:00+00:00", "visual_role": "Lower High"},
    {"timestamp": "2026-03-05T16:30:00+00:00", "visual_role": "markant Low (open)"},
    {"timestamp": "2026-03-08T04:00:00+00:00", "visual_role": "markant Low (open)"},
    {"timestamp": "2026-03-10T15:00:00+00:00", "visual_role": "markant High (open)"},
    {"timestamp": "2026-03-12T12:30:00+00:00", "visual_role": "markant Low (open)"},
]

LABEL_MAP = {
    "Higher High": "higher_high",
    "Lower High": "lower_high",
    "Higher Low": "higher_low",
    "Lower Low": "lower_low",
}


def _ts(v: object) -> pd.Timestamp:
    t = pd.Timestamp(v)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _iso(v: object) -> str:
    return _ts(v).isoformat()


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _p(msg: str) -> None:
    print(msg, flush=True)


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    cols = fields or list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in cols})


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(json_safe(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_5m(start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    raw = load_symbol_candles("APTUSDT")
    raw = raw.copy()
    raw["timestamp"] = pd.to_datetime(raw["timestamp"], utc=True)
    # need bars whose opens are before end; aggregation needs closed groups
    slice_ = raw[(raw["timestamp"] >= start) & (raw["timestamp"] < end)].copy()
    return slice_.reset_index(drop=True)


def build_30m(frame_5m: pd.DataFrame, end: pd.Timestamp) -> tuple[pd.DataFrame, list[ConfirmedPivot], Any]:
    scfg = default_regime_scanner_config().with_timeframe("30m")
    agg = aggregate_candles(frame_5m, "30m", end)
    if agg.empty:
        raise RuntimeError("empty 30m aggregate")
    ind = compute_indicator_frame(agg, config=scfg)
    ind = ind.copy()
    ind["timestamp"] = pd.to_datetime(ind["timestamp"], utc=True)
    ind["close_time"] = ind["timestamp"] + timeframe_timedelta("30m")
    pivots = find_confirmed_pivots(ind, config=scfg)
    return ind, pivots, scfg


def local_extremum_check(ind: pd.DataFrame, idx: int, side: str, left: int = 2, right: int = 2) -> dict[str, Any]:
    """Strict local extremum vs neighbors (same inequality as pivot rule), may be unconfirmed."""
    if idx < 0 or idx >= len(ind):
        return {"is_local_extremum": False, "reason": "index_oob"}
    highs = ind["high"].astype(float).to_numpy()
    lows = ind["low"].astype(float).to_numpy()
    if side == "high":
        h = float(highs[idx])
        lo = max(0, idx - left)
        hi = min(len(ind), idx + 1 + right)
        left_ok = bool((highs[lo:idx] < h).all()) if idx > lo else False
        right_ok = bool((highs[idx + 1 : hi] < h).all()) if hi > idx + 1 else False
        equal_neighbor = bool(
            any(abs(float(highs[j]) - h) < 1e-12 for j in list(range(lo, idx)) + list(range(idx + 1, hi)))
        )
        return {
            "is_local_extremum": left_ok and right_ok and (idx - lo) >= left and (hi - idx - 1) >= right,
            "left_strictly_lower": left_ok,
            "right_strictly_lower": right_ok,
            "equal_neighbor_present": equal_neighbor,
            "price": h,
            "side": "high",
            "window_complete": (idx - lo) >= left and (hi - idx - 1) >= right,
        }
    l = float(lows[idx])
    lo = max(0, idx - left)
    hi = min(len(ind), idx + 1 + right)
    left_ok = bool((lows[lo:idx] > l).all()) if idx > lo else False
    right_ok = bool((lows[idx + 1 : hi] > l).all()) if hi > idx + 1 else False
    equal_neighbor = bool(
        any(abs(float(lows[j]) - l) < 1e-12 for j in list(range(lo, idx)) + list(range(idx + 1, hi)))
    )
    return {
        "is_local_extremum": left_ok and right_ok and (idx - lo) >= left and (hi - idx - 1) >= right,
        "left_strictly_higher": left_ok,
        "right_strictly_higher": right_ok,
        "equal_neighbor_present": equal_neighbor,
        "price": l,
        "side": "low",
        "window_complete": (idx - lo) >= left and (hi - idx - 1) >= right,
    }


def replay_30m_structure(
    ind: pd.DataFrame, pivots: list[ConfirmedPivot]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Causal walk matching HTF update: one closed 30m bar at a time."""
    cfg = default_trend_structure_config()
    state = MarketStructureState(timeframe="30m")
    event_rows: list[dict[str, Any]] = []
    candle_rows: list[dict[str, Any]] = []
    # map pivot_timestamp -> label assignment record when first applied
    pivot_label_at: dict[str, dict[str, Any]] = {}
    # track which pivots are last_confirmed / used for bias/pair
    usage_by_pivot_ts: dict[str, dict[str, Any]] = {}

    for i in range(len(ind)):
        row = ind.iloc[i]
        close_time = _ts(row["close_time"])
        atr = float(row["atr"]) if "atr" in ind.columns and pd.notna(row.get("atr")) else None
        prev_bias = state.current_structure_bias
        prev_pair = (
            f"{state.last_high_label}/{state.last_low_label}"
            if state.last_high_label or state.last_low_label
            else ""
        )
        state, evs = update_market_structure(
            state,
            candle=row,
            pivots=pivots,
            decision_time=close_time,
            atr=atr,
            cfg=cfg,
        )
        for ev in evs:
            event_rows.append(
                {
                    "event_time": _iso(ev.event_time),
                    "event_type": ev.event_type,
                    "level": ev.level,
                    "direction": ev.direction,
                    "reference_pivot_time": (
                        None if ev.reference_pivot_time is None else _iso(ev.reference_pivot_time)
                    ),
                    "reference_pivot_price": ev.reference_pivot_price,
                    "reason_codes": "|".join(ev.reason_codes or ()),
                    "bias_after": state.current_structure_bias,
                    "pair_after": f"{state.last_high_label}/{state.last_low_label}",
                    "candle_open": _iso(row["timestamp"]),
                }
            )
            if ev.event_type in {
                "higher_high",
                "lower_high",
                "equal_high",
                "higher_low",
                "lower_low",
                "equal_low",
            }:
                pts = ev.reference_pivot_time
                if pts is not None:
                    key = _iso(pts)
                    if key not in pivot_label_at:
                        pivot_label_at[key] = {
                            "label": ev.event_type,
                            "event_available_timestamp": _iso(ev.event_time),
                            "level": ev.level,
                        }

        # usage snapshot
        for attr, role in (
            ("last_confirmed_swing_high", "last_confirmed_high"),
            ("last_confirmed_swing_low", "last_confirmed_low"),
            ("last_higher_high", "last_hh"),
            ("last_higher_low", "last_hl"),
            ("last_lower_high", "last_lh"),
            ("last_lower_low", "last_ll"),
            ("protective_high_pivot", "protective_high"),
            ("protective_low_pivot", "protective_low"),
        ):
            p = getattr(state, attr)
            if p is None:
                continue
            k = _iso(p.pivot_timestamp)
            usage_by_pivot_ts.setdefault(k, {"roles": set(), "first_seen_close": _iso(close_time)})
            usage_by_pivot_ts[k]["roles"].add(role)

        candle_rows.append(
            {
                "candle_open": _iso(row["timestamp"]),
                "close_time": _iso(close_time),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "bias": state.current_structure_bias,
                "last_high_label": state.last_high_label,
                "last_low_label": state.last_low_label,
                "has_hh_hl": has_hh_hl(state),
                "has_lh_ll": has_lh_ll(state),
                "events_this_bar": "|".join(e.event_type for e in evs),
                "bias_changed": state.current_structure_bias != prev_bias,
                "pair_before": prev_pair,
                "pair_after": f"{state.last_high_label}/{state.last_low_label}",
            }
        )

    # freeze roles as strings
    for k, v in usage_by_pivot_ts.items():
        v["roles"] = sorted(v["roles"])
    return event_rows, candle_rows, {"labels": pivot_label_at, "usage": usage_by_pivot_ts}


def nearest_pivots(
    pivots: list[ConfirmedPivot], ts: pd.Timestamp, side: str | None = None
) -> tuple[ConfirmedPivot | None, ConfirmedPivot | None]:
    cands = pivots if side is None else [p for p in pivots if p.pivot_type == side]
    before = None
    after = None
    for p in sorted(cands, key=lambda x: _ts(x.pivot_timestamp)):
        pt = _ts(p.pivot_timestamp)
        if pt < ts:
            before = p
        elif pt > ts and after is None:
            after = p
    return before, after


def expected_side(visual_role: str) -> str | None:
    if visual_role in LABEL_MAP:
        lab = LABEL_MAP[visual_role]
        return "high" if lab.endswith("high") else "low"
    low = visual_role.lower()
    # prefer explicit Low/High tokens at end of open-class labels
    if "low" in low and "high" not in low:
        return "low"
    if "high" in low and "low" not in low:
        return "high"
    if low.endswith("low") or " low" in low:
        return "low"
    if low.endswith("high") or " high" in low:
        return "high"
    return None


def audit_point(
    req: dict[str, Any],
    ind: pd.DataFrame,
    pivots: list[ConfirmedPivot],
    label_map: dict[str, dict[str, Any]],
    usage_map: dict[str, dict[str, Any]],
    event_rows: list[dict[str, Any]],
    scfg: Any,
) -> dict[str, Any]:
    ts = _ts(req["timestamp"])
    visual = req["visual_role"]
    expected_label = LABEL_MAP.get(visual)
    side_hint = expected_side(visual)
    if side_hint is None:
        # open class — decide from candle vs neighbors later
        side_hint = None

    # locate candle
    matches = ind.index[ind["timestamp"] == ts].tolist()
    if not matches:
        # try floor
        return {
            "requested_timestamp": _iso(ts),
            "requested_visual_role": visual,
            "status": "NOT_DETECTED",
            "explanation": "no_30m_candle_at_requested_open",
            "exact_pivot_found": False,
        }
    idx = int(matches[0])
    candle = ind.iloc[idx]
    req_high = float(candle["high"])
    req_low = float(candle["low"])

    # find exact pivot(s) at this open
    exact = [p for p in pivots if _ts(p.pivot_timestamp) == ts]
    if side_hint:
        exact_side = [p for p in exact if p.pivot_type == side_hint]
    else:
        exact_side = exact

    # local extremum both sides for open-class points
    loc_high = local_extremum_check(ind, idx, "high", left=scfg.pivot_left, right=scfg.pivot_right)
    loc_low = local_extremum_check(ind, idx, "low", left=scfg.pivot_left, right=scfg.pivot_right)

    # infer likely side for open labels
    if side_hint is None:
        if loc_low["is_local_extremum"] and not loc_high["is_local_extremum"]:
            side_hint = "low"
        elif loc_high["is_local_extremum"] and not loc_low["is_local_extremum"]:
            side_hint = "high"
        elif "Low" in visual:
            side_hint = "low"
        elif "High" in visual:
            side_hint = "high"

    pivot = None
    if exact_side:
        pivot = exact_side[0]
    elif exact:
        pivot = exact[0]

    prev_same = None
    price_rel = None
    detected_type = None
    detected_price = None
    pivot_candle_ts = ""
    pivot_conf_ts = ""
    event_avail = ""
    if pivot is not None:
        pivot_candle_ts = _iso(pivot.pivot_timestamp)
        pivot_conf_ts = _iso(pivot.confirmation_timestamp)
        # event available = close of confirmation candle = conf_open + 30m
        event_avail = _iso(_ts(pivot.confirmation_timestamp) + timeframe_timedelta("30m"))
        detected_price = float(pivot.price)
        # previous same-side among confirmed pivots before this
        same = [p for p in pivots if p.pivot_type == pivot.pivot_type and _ts(p.pivot_timestamp) < ts]
        if same:
            prev_same = same[-1]
            pack = classify_swing_structure(
                {"price": prev_same.price},
                {"price": pivot.price},
                side=pivot.pivot_type,  # type: ignore[arg-type]
                epsilon_pct=0.01,
            )
            detected_type = pack["structure_type"]
            price_rel = pack["price_distance_pct"]
        # prefer label from structure walk if present
        lab = label_map.get(_iso(pivot.pivot_timestamp))
        if lab:
            detected_type = lab["label"]
            event_avail = lab["event_available_timestamp"]

    # nearby if not exact
    before, after = nearest_pivots(pivots, ts, side=side_hint)
    nearby = None
    ts_diff_candles = None
    ts_diff_minutes = None
    if pivot is None:
        # choose closer of before/after
        cands = []
        if before is not None:
            cands.append(before)
        if after is not None:
            cands.append(after)
        if cands:
            nearby = min(cands, key=lambda p: abs((_ts(p.pivot_timestamp) - ts).total_seconds()))
            delta = _ts(nearby.pivot_timestamp) - ts
            ts_diff_minutes = int(delta.total_seconds() // 60)
            ts_diff_candles = int(ts_diff_minutes // 30)

    # structure events referencing this candle / nearby window
    win_lo = ts - pd.Timedelta(hours=12)
    win_hi = ts + pd.Timedelta(hours=12)
    related_events = [
        e
        for e in event_rows
        if (
            (e.get("reference_pivot_time") and abs((_ts(e["reference_pivot_time"]) - ts).total_seconds()) < 60)
            or (win_lo <= _ts(e["event_time"]) <= win_hi and e.get("candle_open") == _iso(ts))
        )
    ]
    bos_choch_after = [
        e
        for e in event_rows
        if e["event_type"] in {
            "bullish_bos",
            "bearish_bos",
            "bullish_choch",
            "bearish_choch",
            "failed_breakout",
            "failed_breakdown",
        }
        and _ts(e["event_time"]) >= ts
        and _ts(e["event_time"]) <= ts + pd.Timedelta(hours=24)
    ]

    usage = usage_map.get(_iso(ts) if pivot else "", {})
    used_ctx = bool(usage.get("roles"))
    # state machine uses 30m context via bias/pair — approximate: used if last_* or protective
    used_sm = used_ctx

    # why not a pivot?
    suppress_reason = ""
    if pivot is None and side_hint:
        loc = loc_high if side_hint == "high" else loc_low
        if not loc.get("window_complete"):
            suppress_reason = "right_confirmation_window_incomplete_at_series_end_or_edge"
        elif loc.get("equal_neighbor_present") and not loc.get("is_local_extremum"):
            suppress_reason = "equal_neighbor_blocks_strict_pivot"
        elif not loc.get("is_local_extremum"):
            suppress_reason = "not_strict_local_extremum_vs_L2_R2"
        else:
            # is local extremum but not in pivots list — confirmation not yet or filtered
            # check if would become pivot with enough right bars
            if idx + scfg.pivot_right >= len(ind):
                suppress_reason = "awaiting_right_confirmation_bars"
            else:
                suppress_reason = "local_extremum_but_not_in_confirmed_pivot_list"

    # status
    status = "NOT_DETECTED"
    explanation = ""
    letter = "E"

    if pivot is not None:
        exact_pivot_found = True
        conf_delay_bars = (
            int((_ts(pivot.confirmation_timestamp) - _ts(pivot.pivot_timestamp)).total_seconds() // 1800)
        )
        if expected_label and detected_type == expected_label:
            status = "EXACT_MATCH"
            letter = "A"
            explanation = f"exact {pivot.pivot_type} pivot @ high/low price; label={detected_type}"
        elif expected_label and detected_type and detected_type != expected_label:
            status = "MATCH_DIFFERENT_LABEL"
            letter = "B"
            explanation = f"pivot exact but label {detected_type} != expected {expected_label}"
        elif expected_label is None and detected_type:
            status = "EXACT_MATCH"
            letter = "A"
            explanation = f"open-class point is confirmed pivot labeled {detected_type}"
        elif expected_label is None and detected_type is None:
            status = "EXACT_MATCH"
            letter = "C"
            explanation = "pivot exists but no prior same-side pivot yet for HH/HL/LH/LL"
        else:
            status = "DELAYED_CONFIRMATION_ONLY"
            letter = "C"
            explanation = f"pivot at candle; confirmed after {conf_delay_bars} bars (pivot_right={scfg.pivot_right})"
        if conf_delay_bars > 0 and status == "EXACT_MATCH":
            # still note confirmation latency is by design
            explanation += f"; confirmation_latency={conf_delay_bars} bars / {conf_delay_bars*30}m"
    else:
        exact_pivot_found = False
        if nearby is not None and abs(ts_diff_candles or 99) <= 2:
            status = "NEARBY_MATCH"
            letter = "D"
            explanation = (
                f"no exact pivot; nearest {nearby.pivot_type} @ {_iso(nearby.pivot_timestamp)} "
                f"({ts_diff_candles} bars / {ts_diff_minutes}m); {suppress_reason}"
            )
            # classify nearby
            same = [
                p
                for p in pivots
                if p.pivot_type == nearby.pivot_type
                and _ts(p.pivot_timestamp) < _ts(nearby.pivot_timestamp)
            ]
            if same:
                pack = classify_swing_structure(
                    {"price": same[-1].price},
                    {"price": nearby.price},
                    side=nearby.pivot_type,  # type: ignore[arg-type]
                )
                detected_type = pack["structure_type"]
                detected_price = float(nearby.price)
                prev_same = same[-1]
                price_rel = pack["price_distance_pct"]
            pivot_candle_ts = _iso(nearby.pivot_timestamp)
            pivot_conf_ts = _iso(nearby.confirmation_timestamp)
            event_avail = _iso(_ts(nearby.confirmation_timestamp) + timeframe_timedelta("30m"))
        elif suppress_reason in {
            "not_strict_local_extremum_vs_L2_R2",
            "equal_neighbor_blocks_strict_pivot",
        }:
            status = "VISUAL_POINT_NOT_VALID_UNDER_CURRENT_RULE"
            letter = "F"
            explanation = suppress_reason
        elif suppress_reason == "awaiting_right_confirmation_bars":
            status = "DELAYED_CONFIRMATION_ONLY"
            letter = "C"
            explanation = suppress_reason
        else:
            status = "NOT_DETECTED"
            letter = "E"
            explanation = suppress_reason or "no_pivot"

    # structural role notes for open-class
    structural_role = ""
    if expected_label is None:
        roles = []
        if side_hint == "low":
            if loc_low["is_local_extremum"]:
                roles.append("local_swing_low")
            if pivot and detected_type:
                roles.append(f"structure_label:{detected_type}")
            elif not pivot:
                roles.append("not_confirmed_swing_pivot")
        if side_hint == "high":
            if loc_high["is_local_extremum"]:
                roles.append("local_swing_high")
            if pivot and detected_type:
                roles.append(f"structure_label:{detected_type}")
            elif not pivot:
                roles.append("not_confirmed_swing_pivot")
        # failed break / retest referencing this level?
        for e in related_events:
            if e["event_type"] in {
                "failed_breakout",
                "failed_breakdown",
                "bullish_retest_holds",
                "bearish_retest_holds",
                "retest_fails",
            }:
                roles.append(e["event_type"])
        if usage.get("roles"):
            roles.append("used_in_30m_context:" + ",".join(usage["roles"]))
        structural_role = "|".join(roles) if roles else "unclassified_visual_extreme"

    # price source check
    price_source_ok = None
    if pivot is not None:
        if pivot.pivot_type == "high":
            price_source_ok = abs(float(pivot.price) - req_high) < 1e-12
        else:
            price_source_ok = abs(float(pivot.price) - req_low) < 1e-12

    return {
        "requested_timestamp": _iso(ts),
        "requested_visual_role": visual,
        "requested_high": req_high,
        "requested_low": req_low,
        "exact_pivot_found": exact_pivot_found,
        "pivot_candle_timestamp": pivot_candle_ts,
        "pivot_confirmed_timestamp": pivot_conf_ts,
        "event_available_timestamp": event_avail,
        "detected_type": detected_type or "",
        "detected_price": detected_price if detected_price is not None else "",
        "previous_same_side_pivot_timestamp": (
            _iso(prev_same.pivot_timestamp) if prev_same is not None else ""
        ),
        "previous_same_side_pivot_price": float(prev_same.price) if prev_same is not None else "",
        "price_relation_pct": price_rel if price_rel is not None else "",
        "timestamp_difference_candles": ts_diff_candles if ts_diff_candles is not None else 0,
        "timestamp_difference_minutes": ts_diff_minutes if ts_diff_minutes is not None else 0,
        "structure_event_types": "|".join(sorted({e["event_type"] for e in related_events})),
        "bos_or_choch_afterward": "|".join(
            f"{e['event_type']}@{e['event_time']}" for e in bos_choch_after[:8]
        ),
        "used_by_30m_context": used_ctx,
        "used_by_state_machine": used_sm,
        "usage_roles": "|".join(usage.get("roles") or []),
        "status": status,
        "letter_grade": letter,
        "explanation": explanation,
        "structural_role": structural_role,
        "price_uses_high_or_low_not_close": price_source_ok,
        "local_high_extremum": loc_high.get("is_local_extremum"),
        "local_low_extremum": loc_low.get("is_local_extremum"),
        "suppress_reason": suppress_reason,
        "side_hint": side_hint or "",
        "pivot_left": scfg.pivot_left,
        "pivot_right": scfg.pivot_right,
    }


def nearby_window_rows(
    req_ts: str,
    ind: pd.DataFrame,
    pivots: list[ConfirmedPivot],
    event_rows: list[dict[str, Any]],
    candle_rows: list[dict[str, Any]],
    label_map: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ts = _ts(req_ts)
    lo = ts - pd.Timedelta(hours=12)
    hi = ts + pd.Timedelta(hours=12)
    piv_by_ts = {_iso(p.pivot_timestamp): p for p in pivots}
    candles_out = []
    for c in candle_rows:
        ot = _ts(c["candle_open"])
        if not (lo <= ot <= hi):
            continue
        p = piv_by_ts.get(c["candle_open"])
        label = ""
        conf_lat = ""
        if p is not None:
            lab = label_map.get(c["candle_open"])
            label = lab["label"] if lab else "(pivot_no_label_yet)"
            conf_lat = int(
                (_ts(p.confirmation_timestamp) - _ts(p.pivot_timestamp)).total_seconds() // 60
            )
        candles_out.append(
            {
                "requested_center": req_ts,
                **c,
                "is_requested": ot == ts,
                "pivot_type": "" if p is None else p.pivot_type,
                "pivot_price": "" if p is None else p.price,
                "pivot_label": label,
                "pivot_confirmation_timestamp": "" if p is None else _iso(p.confirmation_timestamp),
                "pivot_confirmation_latency_minutes": conf_lat,
            }
        )
    events_out = []
    for e in event_rows:
        et = _ts(e["event_time"])
        if lo <= et <= hi:
            events_out.append({"requested_center": req_ts, **e})
    return candles_out, events_out


def run() -> dict[str, Any]:
    OUT.mkdir(parents=True, exist_ok=True)
    hashes_before = {
        "trend_state_machine_md5": _md5(MACHINE),
        "trend_structure_md5": _md5(STRUCTURE),
        "trend_state_policy_md5": _md5(POLICY),
    }
    _p("=== 30m visual point audit ===")
    _p(json.dumps(hashes_before))

    start = _ts(REPLAY_START)
    end = _ts(REPLAY_END)
    _p("[load] 5m")
    frame5 = load_5m(start, end)
    _p(f"[load] 5m bars={len(frame5)}")
    ind, pivots, scfg = build_30m(frame5, end)
    _p(f"[30m] candles={len(ind)} pivots={len(pivots)} L={scfg.pivot_left} R={scfg.pivot_right}")

    event_rows, candle_rows, meta = replay_30m_structure(ind, pivots)
    label_map = meta["labels"]
    usage_map = meta["usage"]

    # full pivot sequence with labels
    pivot_seq = []
    for p in sorted(pivots, key=lambda x: (_ts(x.pivot_timestamp), x.pivot_type)):
        same = [
            q
            for q in pivots
            if q.pivot_type == p.pivot_type and _ts(q.pivot_timestamp) < _ts(p.pivot_timestamp)
        ]
        prev = same[-1] if same else None
        lab = label_map.get(_iso(p.pivot_timestamp), {})
        if prev is not None:
            pack = classify_swing_structure(
                {"price": prev.price},
                {"price": p.price},
                side=p.pivot_type,  # type: ignore[arg-type]
            )
            computed = pack["structure_type"]
            rel = pack["price_distance_pct"]
        else:
            computed = ""
            rel = ""
        usage = usage_map.get(_iso(p.pivot_timestamp), {})
        pivot_seq.append(
            {
                "pivot_candle_timestamp": _iso(p.pivot_timestamp),
                "pivot_confirmed_timestamp": _iso(p.confirmation_timestamp),
                "event_available_timestamp": lab.get(
                    "event_available_timestamp",
                    _iso(_ts(p.confirmation_timestamp) + timeframe_timedelta("30m")),
                ),
                "pivot_type": p.pivot_type,
                "price": p.price,
                "label_from_structure_walk": lab.get("label", computed),
                "label_from_prev_same_side": computed,
                "previous_same_side_timestamp": _iso(prev.pivot_timestamp) if prev else "",
                "previous_same_side_price": float(prev.price) if prev else "",
                "price_relation_pct": rel,
                "confirmation_latency_minutes": int(
                    (_ts(p.confirmation_timestamp) - _ts(p.pivot_timestamp)).total_seconds() // 60
                ),
                "used_roles": "|".join(usage.get("roles") or []),
                "pivot_index": p.pivot_index,
                "confirmation_index": p.confirmation_index,
            }
        )
    _write_csv(OUT / "full_30m_pivot_sequence.csv", pivot_seq)

    audit_rows = []
    nearby_candles_all = []
    nearby_events_all = []
    for req in REQUESTED:
        row = audit_point(req, ind, pivots, label_map, usage_map, event_rows, scfg)
        audit_rows.append(row)
        nc, ne = nearby_window_rows(
            row["requested_timestamp"], ind, pivots, event_rows, candle_rows, label_map
        )
        nearby_candles_all.extend(nc)
        nearby_events_all.extend(ne)
        _p(
            f"[{row['requested_timestamp']}] {row['status']} / {row['letter_grade']} "
            f"type={row.get('detected_type')} — {row.get('explanation')}"
        )

    fields = [
        "requested_timestamp",
        "requested_visual_role",
        "requested_high",
        "requested_low",
        "exact_pivot_found",
        "pivot_candle_timestamp",
        "pivot_confirmed_timestamp",
        "event_available_timestamp",
        "detected_type",
        "detected_price",
        "previous_same_side_pivot_timestamp",
        "previous_same_side_pivot_price",
        "price_relation_pct",
        "timestamp_difference_candles",
        "timestamp_difference_minutes",
        "structure_event_types",
        "bos_or_choch_afterward",
        "used_by_30m_context",
        "used_by_state_machine",
        "usage_roles",
        "status",
        "letter_grade",
        "explanation",
        "structural_role",
        "price_uses_high_or_low_not_close",
        "local_high_extremum",
        "local_low_extremum",
        "suppress_reason",
        "side_hint",
        "pivot_left",
        "pivot_right",
    ]
    _write_csv(OUT / "requested_points_audit.csv", audit_rows, fields)
    _write_json(OUT / "requested_points_audit.json", audit_rows)
    _write_csv(OUT / "nearby_30m_candles.csv", nearby_candles_all)
    _write_csv(OUT / "nearby_30m_structure_events.csv", nearby_events_all)

    # summary stats
    grades = {r["letter_grade"]: 0 for r in audit_rows}
    for r in audit_rows:
        grades[r["letter_grade"]] = grades.get(r["letter_grade"], 0) + 1
    exact = sum(1 for r in audit_rows if r["status"] == "EXACT_MATCH")
    nearby = sum(1 for r in audit_rows if r["status"] == "NEARBY_MATCH")
    expected_ok = sum(
        1
        for r in audit_rows
        if LABEL_MAP.get(r["requested_visual_role"])
        and r.get("detected_type") == LABEL_MAP[r["requested_visual_role"]]
        and r["exact_pivot_found"]
    )
    used = [r for r in audit_rows if r.get("used_by_30m_context")]

    hashes_after = {
        "trend_state_machine_md5": _md5(MACHINE),
        "trend_structure_md5": _md5(STRUCTURE),
        "trend_state_policy_md5": _md5(POLICY),
    }

    summary = {
        "hashes_before": hashes_before,
        "hashes_after": hashes_after,
        "hashes_unchanged": hashes_before == hashes_after,
        "pivot_config_30m": {"left": scfg.pivot_left, "right": scfg.pivot_right},
        "n_30m_candles": len(ind),
        "n_confirmed_pivots": len(pivots),
        "letter_grades": grades,
        "exact_matches": exact,
        "nearby_matches": nearby,
        "expected_label_ok_among_named": expected_ok,
        "named_points": sum(1 for r in audit_rows if r["requested_visual_role"] in LABEL_MAP),
        "used_by_30m_context": [
            {"ts": r["requested_timestamp"], "roles": r.get("usage_roles"), "type": r.get("detected_type")}
            for r in used
        ],
        "per_point_letters": {
            r["requested_timestamp"]: {
                "letter": r["letter_grade"],
                "status": r["status"],
                "detected_type": r.get("detected_type"),
                "visual": r["requested_visual_role"],
            }
            for r in audit_rows
        },
        "systematic_notes": [
            "30m pivots use high/low with strict inequality; equal neighbors never pivot",
            "pivot_right=2 ⇒ confirmation 2×30m after pivot candle open; event available at confirm candle close",
            "HH/HL/LH/LL vs previous confirmed same-side pivot only (not vs any visual swing)",
            "No alternating high/low suppression in find_confirmed_pivots",
            "30m context bias/pair from last_high_label/last_low_label after structure walk",
        ],
        "code_change_needed": False,
        "recommendation": (
            "No production change. Visual naming often refers to chart swings that are not "
            "strict L2/R2 confirmed pivots or that compare to a different reference swing than "
            "the code's previous confirmed same-side pivot."
        ),
    }
    _write_json(OUT / "summary.json", summary)

    lines = [
        "# 30m Visual Point Audit — APTUSDT",
        "",
        f"Hashes unchanged: `{summary['hashes_unchanged']}`",
        f"30m pivot window: L={scfg.pivot_left} R={scfg.pivot_right}",
        f"30m candles: {len(ind)}, confirmed pivots: {len(pivots)}",
        "",
        "## Per-point grades",
        "",
        "| timestamp | visual | status | letter | detected | explanation |",
        "|---|---|---|---|---|---|",
    ]
    for r in audit_rows:
        lines.append(
            f"| {r['requested_timestamp']} | {r['requested_visual_role']} | {r['status']} | "
            f"{r['letter_grade']} | {r.get('detected_type','')} | {r.get('explanation','')[:80]} |"
        )
    lines.extend(
        [
            "",
            "## Totals",
            f"- Exact matches: {exact}/8",
            f"- Nearby: {nearby}/8",
            f"- Expected label OK (named points): {expected_ok}",
            f"- Used by 30m context: {len(used)}",
            "",
            "## Conclusion",
            summary["recommendation"],
            "",
            "Code change needed: **no** (diagnostic only).",
        ]
    )
    (OUT / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    _p(json.dumps({k: summary[k] for k in [
        "exact_matches", "nearby_matches", "expected_label_ok_among_named",
        "letter_grades", "code_change_needed", "hashes_unchanged",
    ]}, indent=2))
    _p("DONE")
    return summary


def main() -> int:
    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
