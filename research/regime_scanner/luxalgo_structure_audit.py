#!/usr/bin/env python3
"""Read-only LuxAlgo structure reference audit (isolated research).

Attribution: structure semantics from Smart Money Concepts [LuxAlgo],
CC BY-NC-SA 4.0. See luxalgo_structure_reference.py header.

Does not modify production modules, policy, or state machine.
Does not stage or commit.
"""
from __future__ import annotations

import csv
import hashlib
import json
import resource
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.config import default_regime_scanner_config
from research.regime_scanner.data_loader import feather_path_for_symbol, load_symbol_candles
from research.regime_scanner.indicators import compute_indicator_frame
from research.regime_scanner.luxalgo_structure_reference import (
    BEARISH,
    BULLISH,
    run_lux_structure,
)
from research.regime_scanner.market_regime import (
    MarketRegimeClassifier,
    compute_market_regime_features,
    default_market_regime_config,
)
from research.regime_scanner.market_regime_macro_context_audit import aggregate_closed_htf
from research.regime_scanner.market_regime_macro_stability_audit import apply_s2
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.swings import find_confirmed_pivots
from research.regime_scanner.trend_structure import (
    MarketStructureState,
    default_trend_structure_config,
    update_market_structure,
)

OUT = Path("research/regime_scanner/results/luxalgo_structure_audit")
ROOT = Path("research/regime_scanner")

PROTECTED = {
    "market_regime.py": "1e79f30af2ddf95c3f91c1b1a012cded",
    "trend_structure.py": "4976cbd9921e9df58dcfaace5cb125a2",
    "trend_state_machine.py": "3a8ed63f60f86ec29bf05e7831bb3349",
    "trend_state_policy.py": "412f672652b66c93b7d44d4b692da2aa",
    "trend_zones.py": "6378f736a184e51efe070ebd2c2d969c",
}

LOAD_START = "2025-12-27T00:00:00+00:00"
AUDIT_START = "2026-01-06T00:00:00+00:00"
AUDIT_END = "2026-03-16T23:59:00+00:00"

FOCUS_WINDOWS = [
    ("2026-01-13", "2026-01-15"),
    ("2026-01-17", "2026-01-19"),
    ("2026-01-27", "2026-01-31"),
    ("2026-02-05", "2026-02-07"),
    ("2026-03-05", "2026-03-10"),
]

VARIANTS: list[dict[str, Any]] = [
    {"key": "30m_i5_s50", "tf": "30m", "minutes": 30, "internal": 5, "swing": 50, "timeline": "structure_timeline_30m.csv"},
    {"key": "4h_i5_s20", "tf": "4h", "minutes": 240, "internal": 5, "swing": 20, "timeline": "structure_timeline_4h_len20.csv"},
    {"key": "4h_i5_s30", "tf": "4h", "minutes": 240, "internal": 5, "swing": 30, "timeline": "structure_timeline_4h_len30.csv"},
    {"key": "4h_i5_s50", "tf": "4h", "minutes": 240, "internal": 5, "swing": 50, "timeline": "structure_timeline_4h_len50.csv"},
]


def _ts(v: object) -> pd.Timestamp:
    t = pd.Timestamp(v)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _iso(v: object | None) -> str | None:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    return _ts(v).isoformat()


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _p(msg: str) -> None:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    print(f"{msg}  [rss≈{rss:.0f}MB]", flush=True)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(json_safe(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
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
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if v is None else v) for k, v in r.items()})


def data_qc(raw: pd.DataFrame) -> dict[str, Any]:
    path = feather_path_for_symbol("APTUSDT")
    ts = pd.to_datetime(raw["timestamp"], utc=True)
    deltas = ts.diff().dropna()
    gaps = deltas[deltas != pd.Timedelta(minutes=5)]
    return {
        "feather_path": str(path),
        "feather_exists": path.exists(),
        "earliest_timestamp": _iso(ts.iloc[0]),
        "latest_timestamp": _iso(ts.iloc[-1]),
        "candle_count": int(len(raw)),
        "duplicate_timestamps": int(ts.duplicated().sum()),
        "gap_count": int(len(gaps)),
        "sorted_ascending": bool(ts.is_monotonic_increasing),
        "warmup_from": LOAD_START,
        "audit_start": AUDIT_START,
        "audit_end": AUDIT_END,
    }


def filter_audit(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    a, b = _ts(AUDIT_START), _ts(AUDIT_END)
    out = []
    for r in rows:
        dt = _ts(r["event_decision_timestamp"])
        if a <= dt <= b:
            out.append(r)
    return out


def extract_swing_events(rows: list[dict[str, Any]], variant: str) -> list[dict[str, Any]]:
    events = []
    for r in rows:
        if r.get("swing_new_pivot_high") or r.get("swing_new_pivot_low"):
            events.append(
                {
                    "variant": variant,
                    "event_type": "swing_pivot_high" if r.get("swing_new_pivot_high") else "swing_pivot_low",
                    "swing_point_type": r.get("swing_point_type") or "",
                    "pivot_candle_timestamp": r.get("pivot_candle_timestamp"),
                    "confirmation_timestamp": r.get("confirmation_timestamp"),
                    "event_decision_timestamp": r.get("event_decision_timestamp"),
                    "level": r.get("swing_pivot_high") if r.get("swing_new_pivot_high") else r.get("swing_pivot_low"),
                    "timeframe": r.get("timeframe"),
                    "close": r.get("close"),
                }
            )
    return events


def extract_bos_choch(rows: list[dict[str, Any]], variant: str, scope: str = "swing") -> list[dict[str, Any]]:
    mapping = {
        f"{scope}_bullish_bos": "bullish_bos",
        f"{scope}_bearish_bos": "bearish_bos",
        f"{scope}_bullish_choch": "bullish_choch",
        f"{scope}_bearish_choch": "bearish_choch",
    }
    events = []
    for r in rows:
        for flag, etype in mapping.items():
            if r.get(flag):
                events.append(
                    {
                        "variant": variant,
                        "scope": scope,
                        "event_type": etype,
                        "direction": "bullish" if "bullish" in etype else "bearish",
                        "is_choch": "choch" in etype,
                        "event_decision_timestamp": r.get("event_decision_timestamp"),
                        "broken_level": r.get("broken_level"),
                        "bias_after": r.get(f"{scope}_bias"),
                        "timeframe": r.get("timeframe"),
                        "close": r.get("close"),
                        "swing_pivot_high": r.get("swing_pivot_high"),
                        "swing_pivot_low": r.get("swing_pivot_low"),
                    }
                )
    return events


def classify_structure_window(
    rows: list[dict[str, Any]],
    *,
    start: str,
    end: str,
    label: str,
) -> dict[str, Any]:
    """Answer structure questions for a calendar window (no backpaint)."""
    a, b = _ts(start), _ts(end)
    win = [r for r in rows if a <= _ts(r["event_decision_timestamp"]) <= b]
    pre = [r for r in rows if _ts(r["event_decision_timestamp"]) < a]

    last_lh = None
    for r in pre + win:
        if r.get("swing_point_type") == "LH" and r.get("swing_new_pivot_high"):
            last_lh = {
                "pivot_candle_timestamp": r.get("pivot_candle_timestamp"),
                "confirmation_timestamp": r.get("confirmation_timestamp"),
                "event_decision_timestamp": r.get("event_decision_timestamp"),
                "level": r.get("swing_pivot_high"),
            }

    bull_cross = None
    bull_choch = False
    bull_bos_after = False
    confirmed_hl = None
    fell_under_broken = False
    later_bear = None
    broken_level = None
    choch_ts = None

    for r in win:
        if r.get("swing_bullish_choch") or r.get("swing_bullish_bos"):
            if bull_cross is None:
                bull_cross = {
                    "event_decision_timestamp": r.get("event_decision_timestamp"),
                    "event_type": "bullish_choch" if r.get("swing_bullish_choch") else "bullish_bos",
                    "broken_level": r.get("broken_level"),
                    "close": r.get("close"),
                }
                broken_level = r.get("broken_level")
            if r.get("swing_bullish_choch"):
                bull_choch = True
                choch_ts = _ts(r["event_decision_timestamp"])
            if r.get("swing_bullish_bos") and choch_ts is not None and _ts(r["event_decision_timestamp"]) >= choch_ts:
                bull_bos_after = True
        if r.get("swing_point_type") == "HL" and r.get("swing_new_pivot_low"):
            if choch_ts is None or _ts(r["event_decision_timestamp"]) >= choch_ts:
                confirmed_hl = {
                    "pivot_candle_timestamp": r.get("pivot_candle_timestamp"),
                    "confirmation_timestamp": r.get("confirmation_timestamp"),
                    "level": r.get("swing_pivot_low"),
                }
        if broken_level is not None and float(r["close"]) < float(broken_level):
            fell_under_broken = True
        if r.get("swing_bearish_choch") or r.get("swing_bearish_bos"):
            later_bear = {
                "event_decision_timestamp": r.get("event_decision_timestamp"),
                "event_type": "bearish_choch" if r.get("swing_bearish_choch") else "bearish_bos",
                "broken_level": r.get("broken_level"),
            }

    if bull_choch and later_bear is not None:
        movement = "failed_reversal"
    elif bull_choch and confirmed_hl and bull_bos_after and not fell_under_broken:
        movement = "confirmed_reversal"
    elif bull_choch and fell_under_broken:
        movement = "failed_reversal" if later_bear is not None else "possible_reversal"
    elif bull_choch and (confirmed_hl or bull_bos_after):
        movement = "possible_reversal"
    elif bull_choch:
        movement = "possible_reversal"
    else:
        movement = "recovery"

    return {
        "window": label,
        "last_relevant_lower_high_before_rise": last_lh,
        "bullish_close_cross": bull_cross,
        "bullish_choch_produced": bull_choch,
        "bullish_bos_after_choch": bull_bos_after,
        "confirmed_higher_low": confirmed_hl,
        "price_fell_back_under_broken_level": fell_under_broken,
        "later_bearish_bos_or_choch": later_bear,
        "movement_label": movement,
        "n_bars_in_window": len(win),
    }


def classify_jan13_15(rows_4h: list[dict[str, Any]]) -> dict[str, Any]:
    """Backward-compatible wrapper for 4h Jan 13–15 answers."""
    out = classify_structure_window(
        rows_4h,
        start="2026-01-13T00:00:00+00:00",
        end="2026-01-15T23:59:59+00:00",
        label="2026-01-13_to_2026-01-15",
    )
    # keep prior key names expected by README
    out["last_relevant_4h_lower_high_before_rise"] = out.pop("last_relevant_lower_high_before_rise")
    out["n_4h_bars_in_window"] = out.pop("n_bars_in_window")
    return out


def run_existing_structure_timeline(htf: pd.DataFrame, timeframe: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Replay existing trend_structure on closed HTF bars (read-only)."""
    # Production scanner only wires 5m/15m/30m pivot maps; for 4h use explicit L/R=2.
    if timeframe == "30m":
        scfg = default_regime_scanner_config().with_timeframe("30m")
        pivots = find_confirmed_pivots(htf, config=scfg)
    else:
        scfg = default_regime_scanner_config()
        pivots = find_confirmed_pivots(htf, config=scfg, pivot_left=2, pivot_right=2)
    ind = htf.copy()
    ind["timestamp"] = pd.to_datetime(ind["timestamp"], utc=True)
    ind["decision_time"] = pd.to_datetime(ind["decision_time"], utc=True)
    cfg = default_trend_structure_config()
    state = MarketStructureState(timeframe=timeframe)
    bars: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    for i in range(len(ind)):
        row = ind.iloc[i]
        dt = _ts(row["decision_time"])
        state, evs = update_market_structure(
            state,
            candle=row,
            pivots=pivots,
            decision_time=dt,
            atr=None,
            cfg=cfg,
        )
        bars.append(
            {
                "event_decision_timestamp": _iso(dt),
                "bias": state.current_structure_bias,
                "last_bos": None if state.last_bos is None else state.last_bos.event_type,
                "last_choch": None if state.last_choch is None else state.last_choch.event_type,
                "protective_high": state.protective_high_level,
                "protective_low": state.protective_low_level,
                "close": float(row["close"]),
            }
        )
        for ev in evs:
            if ev.event_type in {
                "bullish_bos",
                "bearish_bos",
                "bullish_choch",
                "bearish_choch",
            }:
                events.append(
                    {
                        "event_decision_timestamp": _iso(ev.event_time),
                        "event_type": ev.event_type,
                        "direction": ev.direction,
                        "level": ev.level,
                        "source": "trend_structure",
                        "timeframe": timeframe,
                    }
                )
    return bars, events


def run_k2_h4_timeline(agg4: pd.DataFrame) -> list[dict[str, Any]]:
    scfg = default_regime_scanner_config()
    ind = compute_indicator_frame(agg4, config=scfg).copy()
    ind["timestamp"] = pd.to_datetime(agg4["timestamp"], utc=True).to_numpy()
    ind["decision_time"] = pd.to_datetime(agg4["decision_time"], utc=True).to_numpy()
    cfg = default_market_regime_config()
    clf = MarketRegimeClassifier(cfg)
    close = ind["close"].astype(float).to_numpy()
    high = ind["high"].astype(float).to_numpy()
    low = ind["low"].astype(float).to_numpy()
    ema9 = ind["ema_9"].astype(float).to_numpy()
    ema20 = ind["ema_20"].astype(float).to_numpy()
    atr = ind["atr"].astype(float).to_numpy()
    out: list[dict[str, Any]] = []
    for i in range(len(ind)):
        feat = compute_market_regime_features(
            close[: i + 1],
            high[: i + 1],
            low[: i + 1],
            ema9[: i + 1],
            ema20[: i + 1],
            atr[: i + 1],
            cfg=cfg,
        )
        if feat is None:
            continue
        dt = _ts(ind.iloc[i]["decision_time"])
        ctx = clf.update(decision_time=dt, features=feat)
        out.append(
            {
                "decision_time": _iso(dt),
                "regime": ctx.regime,
                "close": float(close[i]),
                "high": float(high[i]),
                "low": float(low[i]),
            }
        )
    return out


def compare_events(
    lux: list[dict[str, Any]],
    existing: list[dict[str, Any]],
    *,
    match_hours: float = 12.0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Pair LuxAlgo swing BOS/CHoCH vs existing structure events."""
    rows: list[dict[str, Any]] = []
    disagreements: list[dict[str, Any]] = []
    used_ex: set[int] = set()
    tol = pd.Timedelta(hours=match_hours)

    def _dir(et: str) -> str:
        return "bullish" if "bullish" in et else "bearish"

    def _kind(et: str) -> str:
        return "choch" if "choch" in et else "bos"

    for le in lux:
        lt = _ts(le["event_decision_timestamp"])
        best_i = None
        best_dt = None
        for i, ee in enumerate(existing):
            if i in used_ex:
                continue
            et = _ts(ee["event_decision_timestamp"])
            d = abs(et - lt)
            if d <= tol and (best_dt is None or d < best_dt):
                best_i = i
                best_dt = d
        if best_i is None:
            row = {
                "lux_event": le["event_type"],
                "lux_time": le["event_decision_timestamp"],
                "lux_level": le.get("broken_level"),
                "existing_event": None,
                "existing_time": None,
                "existing_level": None,
                "same_bos_direction": None,
                "same_choch_direction": None,
                "timestamp_delta_hours": None,
                "level_delta": None,
                "only_in": "luxalgo",
            }
            rows.append(row)
            disagreements.append(row)
            continue
        used_ex.add(best_i)
        ee = existing[best_i]
        same_dir = _dir(le["event_type"]) == _dir(ee["event_type"])
        same_kind = _kind(le["event_type"]) == _kind(ee["event_type"])
        lvl_l = le.get("broken_level")
        lvl_e = ee.get("level")
        level_delta = None
        if lvl_l is not None and lvl_e is not None:
            level_delta = float(lvl_l) - float(lvl_e)
        row = {
            "lux_event": le["event_type"],
            "lux_time": le["event_decision_timestamp"],
            "lux_level": lvl_l,
            "existing_event": ee["event_type"],
            "existing_time": ee["event_decision_timestamp"],
            "existing_level": lvl_e,
            "same_bos_direction": same_dir and same_kind and _kind(le["event_type"]) == "bos",
            "same_choch_direction": same_dir and same_kind and _kind(le["event_type"]) == "choch",
            "same_direction": same_dir,
            "same_kind": same_kind,
            "timestamp_delta_hours": None if best_dt is None else best_dt.total_seconds() / 3600.0,
            "level_delta": level_delta,
            "only_in": None if same_dir and same_kind else "disagree",
        }
        rows.append(row)
        if not (same_dir and same_kind):
            disagreements.append(row)

    for i, ee in enumerate(existing):
        if i in used_ex:
            continue
        row = {
            "lux_event": None,
            "lux_time": None,
            "lux_level": None,
            "existing_event": ee["event_type"],
            "existing_time": ee["event_decision_timestamp"],
            "existing_level": ee.get("level"),
            "same_bos_direction": None,
            "same_choch_direction": None,
            "timestamp_delta_hours": None,
            "level_delta": None,
            "only_in": "existing",
        }
        rows.append(row)
        disagreements.append(row)
    return rows, disagreements


def compare_s2(lux_rows: list[dict[str, Any]], s2_codes: list[int], s2_tl: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_dt = {_ts(r["decision_time"]): c for r, c in zip(s2_tl, s2_codes)}
    out = []
    for r in lux_rows:
        dt = _ts(r["event_decision_timestamp"])
        code = by_dt.get(dt)
        lux_bias = int(r.get("swing_bias") or 0)
        # S2 display: 1/3 bull, 2/4 bear, 5 range, 6 possible rev
        if code in (1, 3):
            s2_side = 1
        elif code in (2, 4):
            s2_side = -1
        else:
            s2_side = 0
        agree = (lux_bias == 0 and s2_side == 0) or (lux_bias * s2_side > 0)
        out.append(
            {
                "event_decision_timestamp": r["event_decision_timestamp"],
                "lux_swing_bias": lux_bias,
                "s2_display_code": code,
                "s2_side": s2_side,
                "direction_agree": agree,
                "lux_bullish_bos": r.get("swing_bullish_bos"),
                "lux_bearish_bos": r.get("swing_bearish_bos"),
                "lux_bullish_choch": r.get("swing_bullish_choch"),
                "lux_bearish_choch": r.get("swing_bearish_choch"),
                "close": r.get("close"),
            }
        )
    return out


def variant_stats(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    swing_ev = extract_bos_choch(rows, key, "swing")
    counts = defaultdict(int)
    for e in swing_ev:
        counts[e["event_type"]] += 1
    bias_changes = 0
    prev: int | None = None
    for r in rows:
        b = int(r.get("swing_bias") or 0)
        if b == 0:
            continue
        if prev is not None and b != prev:
            bias_changes += 1
        prev = b
    return {
        "variant": key,
        "n_bars": len(rows),
        "swing_bullish_bos": counts["bullish_bos"],
        "swing_bearish_bos": counts["bearish_bos"],
        "swing_bullish_choch": counts["bullish_choch"],
        "swing_bearish_choch": counts["bearish_choch"],
        "swing_bias_flips": bias_changes,
        "swing_pivots": sum(1 for r in rows if r.get("swing_new_pivot_high") or r.get("swing_new_pivot_low")),
    }


def write_pine_scripts(primary_4h: list[dict[str, Any]], rows_30m: list[dict[str, Any]]) -> None:
    """Minimal Pine v6 review scripts (structure display only)."""
    review = '''//@version=6
indicator("LuxAlgo Structure Reference Review", overlay=true, max_labels_count=200, max_lines_count=50)

// Research-only structure subset inspired by Smart Money Concepts [LuxAlgo]
// Original © LuxAlgo — CC BY-NC-SA 4.0 — https://creativecommons.org/licenses/by-nc-sa/4.0/
// Not affiliated with LuxAlgo. No OBs / FVGs / EQH / trading logic.

swingLength = input.int(50, "Swing length", minval=1)
showLabels = input.bool(false, "Show swing / BOS / CHoCH labels")
showBiasBg = input.bool(true, "Show swing bias background")
bgTransp = input.int(90, "Bias background transparency", minval=70, maxval=98)

BEARISH_LEG = 0
BULLISH_LEG = 1
BEARISH = -1
BULLISH = 1

leg(size) =>
    var int legState = BEARISH_LEG
    newLegHigh = high[size] > ta.highest(size)
    newLegLow = low[size] < ta.lowest(size)
    if newLegHigh
        legState := BEARISH_LEG
    else if newLegLow
        legState := BULLISH_LEG
    legState

var float swingHighLevel = na
var float swingLowLevel = na
var bool swingHighCrossed = false
var bool swingLowCrossed = false
var int swingBias = 0
var float lastSwingHigh = na
var float lastSwingLow = na

swingLeg = leg(swingLength)
newPivotHigh = ta.change(swingLeg) == -1
newPivotLow = ta.change(swingLeg) == 1

pivotHighPrice = high[swingLength]
pivotLowPrice = low[swingLength]

if newPivotHigh
    lastSwingHigh := swingHighLevel
    swingHighLevel := pivotHighPrice
    swingHighCrossed := false
if newPivotLow
    lastSwingLow := swingLowLevel
    swingLowLevel := pivotLowPrice
    swingLowCrossed := false

bullBreak = not na(swingHighLevel) and not swingHighCrossed and ta.crossover(close, swingHighLevel)
bearBreak = not na(swingLowLevel) and not swingLowCrossed and ta.crossunder(close, swingLowLevel)

bullChoch = false
bullBos = false
bearChoch = false
bearBos = false
if bullBreak
    bullChoch := swingBias == BEARISH
    bullBos := swingBias != BEARISH
    swingHighCrossed := true
    swingBias := BULLISH
if bearBreak
    bearChoch := swingBias == BULLISH
    bearBos := swingBias != BULLISH
    swingLowCrossed := true
    swingBias := BEARISH

plotshape(newPivotHigh, title="Swing High", style=shape.triangledown, location=location.abovebar, color=color.new(color.red, 20), size=size.tiny, offset=-swingLength)
plotshape(newPivotLow, title="Swing Low", style=shape.triangleup, location=location.belowbar, color=color.new(color.green, 20), size=size.tiny, offset=-swingLength)
plotshape(bullBos, title="Bull BOS", style=shape.circle, location=location.belowbar, color=color.new(color.teal, 0), size=size.tiny)
plotshape(bullChoch, title="Bull CHoCH", style=shape.diamond, location=location.belowbar, color=color.new(color.lime, 0), size=size.small)
plotshape(bearBos, title="Bear BOS", style=shape.circle, location=location.abovebar, color=color.new(color.maroon, 0), size=size.tiny)
plotshape(bearChoch, title="Bear CHoCH", style=shape.diamond, location=location.abovebar, color=color.new(color.red, 0), size=size.small)

bgcolor(showBiasBg ? (swingBias == BULLISH ? color.new(color.green, bgTransp) : swingBias == BEARISH ? color.new(color.red, bgTransp) : na) : na)

if showLabels and newPivotHigh
    label.new(bar_index - swingLength, pivotHighPrice, pivotHighPrice > nz(lastSwingHigh) ? "HH" : "LH", style=label.style_label_down, color=color.new(color.red, 70), textcolor=color.white, size=size.tiny)
if showLabels and newPivotLow
    label.new(bar_index - swingLength, pivotLowPrice, pivotLowPrice < nz(lastSwingLow) ? "LL" : "HL", style=label.style_label_up, color=color.new(color.green, 70), textcolor=color.white, size=size.tiny)
if showLabels and bullBos
    label.new(bar_index, low, "BOS+", style=label.style_label_up, color=color.new(color.teal, 60), textcolor=color.white, size=size.tiny)
if showLabels and bullChoch
    label.new(bar_index, low, "CHoCH+", style=label.style_label_up, color=color.new(color.lime, 50), textcolor=color.black, size=size.tiny)
if showLabels and bearBos
    label.new(bar_index, high, "BOS-", style=label.style_label_down, color=color.new(color.maroon, 60), textcolor=color.white, size=size.tiny)
if showLabels and bearChoch
    label.new(bar_index, high, "CHoCH-", style=label.style_label_down, color=color.new(color.red, 50), textcolor=color.white, size=size.tiny)
'''
    (OUT / "luxalgo_structure_reference_review.pine").write_text(review, encoding="utf-8")

    events_4h = extract_bos_choch(primary_4h, "4h_i5_s50", "swing")
    events_30 = extract_bos_choch(rows_30m, "30m_i5_s50", "swing")
    jan_lo, jan_hi = _ts("2026-01-12"), _ts("2026-01-20T23:59:59+00:00")
    jan_events = [
        ("4h50", e)
        for e in events_4h
        if jan_lo <= _ts(e["event_decision_timestamp"]) <= jan_hi
    ] + [
        ("30m50", e)
        for e in events_30
        if jan_lo <= _ts(e["event_decision_timestamp"]) <= jan_hi
    ]
    lines = [
        "//@version=6",
        'indicator("LuxAlgo Structure Jan 12-20 Review", overlay=true, max_labels_count=100)',
        "",
        "// Research slice 2026-01-12 .. 2026-01-20",
        "// Attribution: Smart Money Concepts [LuxAlgo] structure subset — CC BY-NC-SA 4.0",
        "// Markers = precomputed decision timestamps (no backpaint).",
        "",
        'showLabels = input.bool(true, "Show event labels")',
        'showWindow = input.bool(true, "Highlight Jan 12-20 window")',
        'show4h = input.bool(true, "Show 4h swing50 events")',
        'show30m = input.bool(true, "Show 30m swing50 events")',
        "",
        'inWin = time >= timestamp("UTC", 2026, 1, 12, 0, 0) and time <= timestamp("UTC", 2026, 1, 20, 23, 59)',
        "bgcolor(showWindow and inWin ? color.new(color.blue, 94) : na)",
        "",
    ]
    for src, e in jan_events:
        t = _ts(e["event_decision_timestamp"])
        tag = f"{src}_{e['event_type']}"
        lines.append(f"// {tag} @ {t.isoformat()} level={e.get('broken_level')}")
        y, m, d, h, mi = t.year, t.month, t.day, t.hour, t.minute
        gate = "show4h" if src.startswith("4h") else "show30m"
        cond = f"(year == {y} and month == {m} and dayofmonth == {d} and hour == {h} and minute == {mi})"
        col = "color.lime" if "bullish" in e["event_type"] else "color.red"
        shape = "shape.diamond" if "choch" in e["event_type"] else "shape.circle"
        loc = "location.belowbar" if "bullish" in e["event_type"] else "location.abovebar"
        lines.append(
            f'plotshape({gate} and {cond} and inWin, title="{tag}", style={shape}, location={loc}, color={col}, size=size.small)'
        )
        up = "bullish" in e["event_type"]
        lines.append(f"if showLabels and {gate} and {cond} and inWin")
        lines.append(
            f'    label.new(bar_index, {"low" if up else "high"}, "{tag}", '
            f'style=label.style_label_{"up" if up else "down"}, '
            f'color=color.new({col}, 40), textcolor=color.white, size=size.tiny)'
        )
    (OUT / "luxalgo_structure_reference_review_2026_01_12_20.pine").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def write_readme(
    qc: dict[str, Any],
    jan: dict[str, Any],
    decision: str,
    hashes_before: dict[str, str],
    hashes_after: dict[str, str],
) -> None:
    lines = [
        "# LuxAlgo structure reference audit",
        "",
        "## Attribution / license",
        "",
        "Structure semantics ported from **Smart Money Concepts [LuxAlgo]**.",
        "Original work © LuxAlgo, licensed under **CC BY-NC-SA 4.0**:",
        "https://creativecommons.org/licenses/by-nc-sa/4.0/",
        "",
        "This audit is non-commercial research only. No Order Blocks, FVGs, EQH/EQL,",
        "premium/discount, alerts, or trading logic were ported.",
        "",
        "## Scope",
        "",
        "- Isolated read-only reference under `research/regime_scanner/`",
        "- Closed 30m / 4h buckets from APTUSDT 5m feather",
        "- Warm-up from earliest available 2025-12-27; metrics only in audit window",
        "- Configs: 30m internal=5 swing=50; 4h swing ∈ {20,30,50}",
        "",
        "## Data QC",
        "",
        "```json",
        json.dumps(qc, indent=2),
        "```",
        "",
        "## Jan 13–15 focus (4h swing=50)",
        "",
        "```json",
        json.dumps(jan, indent=2, default=str),
        "```",
        "",
        "## Decision",
        "",
        f"**{decision}**",
        "",
        "## Protected hashes",
        "",
        f"- before: `{hashes_before}`",
        f"- after: `{hashes_after}`",
        "",
        "## Artifacts",
        "",
        "- structure_timeline_*.csv",
        "- swing_events_*.csv / bos_choch_events_*.csv",
        "- variant_comparison.csv / comparison_*.csv / disagreement_cases.csv",
        "- summary.json / audit_metadata.json",
        "- luxalgo_structure_reference_review.pine",
        "- luxalgo_structure_reference_review_2026_01_12_20.pine",
        "",
    ]
    (OUT / "README.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    hashes_before = {name: _md5(ROOT / name) for name in PROTECTED}
    for name, expected in PROTECTED.items():
        got = hashes_before[name]
        if got != expected:
            raise SystemExit(f"protected hash mismatch before audit: {name} {got} != {expected}")

    _p("load candles")
    raw = load_symbol_candles("APTUSDT")
    raw = raw.copy()
    raw["timestamp"] = pd.to_datetime(raw["timestamp"], utc=True)
    qc = data_qc(raw)
    _write_json(OUT / "data_qc.json", qc)
    _p(f"QC: n={qc['candle_count']} gaps={qc['gap_count']} path={qc['feather_path']}")

    end_wall = _ts(AUDIT_END)
    load_start = _ts(LOAD_START)
    sl = raw[(raw["timestamp"] >= load_start) & (raw["timestamp"] <= _ts("2026-03-16 23:55:00+00:00"))].copy()
    ohlcv5 = sl[["timestamp", "open", "high", "low", "close", "volume"]]

    _p("aggregate closed 30m / 4h")
    agg30 = aggregate_closed_htf(ohlcv5, 30, end_wall)
    agg4 = aggregate_closed_htf(ohlcv5, 240, end_wall)

    all_timelines: dict[str, list[dict[str, Any]]] = {}
    swing_events_30: list[dict[str, Any]] = []
    swing_events_4h: list[dict[str, Any]] = []
    bos_30: list[dict[str, Any]] = []
    bos_4h: list[dict[str, Any]] = []
    variant_rows: list[dict[str, Any]] = []

    for v in VARIANTS:
        frame = agg30 if v["tf"] == "30m" else agg4
        _p(f"run LuxAlgo structure {v['key']}")
        full = run_lux_structure(
            frame,
            timeframe=v["tf"],
            internal_size=int(v["internal"]),
            swing_size=int(v["swing"]),
        )
        audit_rows = filter_audit(full)
        all_timelines[v["key"]] = audit_rows
        _write_csv(OUT / v["timeline"], audit_rows)
        # focus window dumps
        for ws, we in FOCUS_WINDOWS:
            a, b = _ts(ws), _ts(we) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
            focus = [r for r in audit_rows if a <= _ts(r["event_decision_timestamp"]) <= b]
            _write_csv(OUT / f"focus_{ws}_{we}_{v['key']}.csv", focus)

        sev = extract_swing_events(audit_rows, v["key"])
        bev = extract_bos_choch(audit_rows, v["key"], "swing")
        if v["tf"] == "30m":
            swing_events_30.extend(sev)
            bos_30.extend(bev)
        else:
            swing_events_4h.extend(sev)
            bos_4h.extend(bev)
        variant_rows.append(variant_stats(audit_rows, v["key"]))

        # determinism check inline
        again = filter_audit(
            run_lux_structure(frame, timeframe=v["tf"], internal_size=int(v["internal"]), swing_size=int(v["swing"]))
        )
        if again != audit_rows:
            raise SystemExit(f"non-deterministic structure output for {v['key']}")

    _write_csv(OUT / "swing_events_30m.csv", swing_events_30)
    _write_csv(OUT / "swing_events_4h.csv", swing_events_4h)
    _write_csv(OUT / "bos_choch_events_30m.csv", bos_30)
    _write_csv(OUT / "bos_choch_events_4h.csv", bos_4h)
    _write_csv(OUT / "variant_comparison.csv", variant_rows)

    primary = all_timelines["4h_i5_s50"]
    jan_4h50 = classify_jan13_15(primary)
    jan_4h20 = classify_jan13_15(all_timelines["4h_i5_s20"])
    jan_4h30 = classify_jan13_15(all_timelines["4h_i5_s30"])
    jan_30m = classify_structure_window(
        all_timelines["30m_i5_s50"],
        start="2026-01-13T00:00:00+00:00",
        end="2026-01-15T23:59:59+00:00",
        label="2026-01-13_to_2026-01-15",
    )
    jan = {
        "primary_4h_swing50": jan_4h50,
        "4h_swing20": jan_4h20,
        "4h_swing30": jan_4h30,
        "30m_swing50": jan_30m,
        "explicit_4h_answers": {
            "last_relevant_4h_lower_high_before_rise": jan_4h50[
                "last_relevant_4h_lower_high_before_rise"
            ],
            "bullish_close_cross_4h50": jan_4h50["bullish_close_cross"],
            "bullish_choch_4h50": jan_4h50["bullish_choch_produced"],
            "bullish_bos_after_choch_4h50": jan_4h50["bullish_bos_after_choch"],
            "confirmed_hl_4h50": jan_4h50["confirmed_higher_low"],
            "fell_under_broken_4h50": jan_4h50["price_fell_back_under_broken_level"],
            "later_bear_4h50": jan_4h50["later_bearish_bos_or_choch"],
            "movement_4h50": jan_4h50["movement_label"],
            "movement_30m50": jan_30m["movement_label"],
            "note": (
                "4h swing lengths 20/30/50 show no bullish BOS/CHoCH inside Jan 13–15 "
                "(recovery / unconfirmed on HTF). 30m swing=50 prints bullish CHoCH then "
                "bearish CHoCH → failed_reversal — useful contrast to S2 macro-up paint."
            ),
        },
    }
    _write_json(OUT / "jan_13_15_structure_answers.json", jan)

    _p("compare vs existing trend_structure (4h)")
    _, existing_events = run_existing_structure_timeline(agg4, "4h")
    existing_audit = [
        e
        for e in existing_events
        if _ts(AUDIT_START) <= _ts(e["event_decision_timestamp"]) <= _ts(AUDIT_END)
    ]
    lux_events = extract_bos_choch(primary, "4h_i5_s50", "swing")
    cmp_struct, disagree = compare_events(lux_events, existing_audit)
    _write_csv(OUT / "comparison_existing_structure.csv", cmp_struct)

    _p("compare vs S2 macro + K2_H4")
    k2_tl = run_k2_h4_timeline(agg4)
    k2_audit = [r for r in k2_tl if _ts(AUDIT_START) <= _ts(r["decision_time"]) <= _ts(AUDIT_END)]
    # rebuild full (warm) for S2 then filter
    s2_codes_full = apply_s2(k2_tl)
    s2_pairs = [
        (r, c)
        for r, c in zip(k2_tl, s2_codes_full)
        if _ts(AUDIT_START) <= _ts(r["decision_time"]) <= _ts(AUDIT_END)
    ]
    s2_tl = [r for r, _ in s2_pairs]
    s2_codes = [c for _, c in s2_pairs]
    cmp_s2 = compare_s2(primary, s2_codes, s2_tl)
    _write_csv(OUT / "comparison_s2_macro.csv", cmp_s2)

    # K2 local disagreement when LuxAlgo CHoCH fires but K2 stays opposite strong
    for e in lux_events:
        dt = _ts(e["event_decision_timestamp"])
        k2 = next((r for r in k2_audit if _ts(r["decision_time"]) == dt), None)
        if k2 is None:
            continue
        lux_bull = e["direction"] == "bullish"
        k2_reg = k2["regime"]
        conflict = (lux_bull and k2_reg == "strong_bearish_trend") or (
            (not lux_bull) and k2_reg == "strong_bullish_trend"
        )
        if conflict and e.get("is_choch"):
            disagree.append(
                {
                    "lux_event": e["event_type"],
                    "lux_time": e["event_decision_timestamp"],
                    "lux_level": e.get("broken_level"),
                    "existing_event": f"k2_h4:{k2_reg}",
                    "existing_time": k2["decision_time"],
                    "existing_level": None,
                    "same_bos_direction": False,
                    "same_choch_direction": False,
                    "timestamp_delta_hours": 0.0,
                    "level_delta": None,
                    "only_in": "lux_vs_k2_h4",
                }
            )
    _write_csv(OUT / "disagreement_cases.csv", disagree)

    # Decision heuristic
    s2_agree = sum(1 for r in cmp_s2 if r["direction_agree"]) / max(1, len(cmp_s2))
    n_choch = sum(1 for e in lux_events if e.get("is_choch"))
    n_bos = len(lux_events) - n_choch
    matched = sum(1 for r in cmp_struct if r.get("only_in") is None)
    only_lux = sum(1 for r in cmp_struct if r.get("only_in") == "luxalgo")
    only_ex = sum(1 for r in cmp_struct if r.get("only_in") == "existing")

    choch_30 = sum(
        1
        for e in bos_30
        if e.get("is_choch")
    )
    jan_30_failed = jan_30m["movement_label"] == "failed_reversal"
    jan_4h_recovery = jan_4h50["movement_label"] == "recovery" and not jan_4h50["bullish_choch_produced"]

    if jan_30_failed and jan_4h_recovery and choch_30 >= 2:
        decision = "J"
        decision_reason = (
            "Reproducible LuxAlgo BOS/CHoCH: Jan 13–15 is HTF recovery (no 4h CHoCH) but "
            "30m failed_reversal (bullish then bearish CHoCH) — separates local bounce from "
            "confirmed macro reversal better than sticky S2 uptrend paint."
        )
    elif choch_30 >= 4 and n_choch + sum(
        1 for e in bos_4h if e.get("is_choch")
    ) >= 3:
        decision = "J"
        decision_reason = (
            "Enough reproducible CHoCH across 30m/4h variants to contrast recovery vs reversal."
        )
    elif choch_30 == 0 and n_choch == 0:
        decision = "N"
        decision_reason = "No swing CHoCH across audited variants."
    else:
        decision = "U"
        decision_reason = (
            "Semantics OK and some CHoCH exist, but Jan 13–15 separation vs S2 is incomplete "
            "or length-50 4h is too sparse for a firm adopt/reject call."
        )
    write_pine_scripts(primary, all_timelines["30m_i5_s50"])

    hashes_after = {name: _md5(ROOT / name) for name in PROTECTED}
    for name, expected in PROTECTED.items():
        if hashes_after[name] != expected or hashes_after[name] != hashes_before[name]:
            raise SystemExit(f"protected module hash changed: {name}")

    summary = {
        "decision": decision,
        "decision_reason": decision_reason,
        "data_qc": qc,
        "variant_comparison": variant_rows,
        "jan_13_15": jan,
        "comparison_vs_existing": {
            "matched_same_kind_dir": matched,
            "only_luxalgo": only_lux,
            "only_existing": only_ex,
            "lux_swing_bos": n_bos,
            "lux_swing_choch": n_choch,
        },
        "s2_direction_agree_rate": s2_agree,
        "k2_h4_bars": len(k2_audit),
        "protected_hashes_before": hashes_before,
        "protected_hashes_after": hashes_after,
        "license": "CC BY-NC-SA 4.0 (LuxAlgo Smart Money Concepts structure subset attribution)",
    }
    _write_json(OUT / "summary.json", summary)
    meta = {
        "audit_window": {"start": AUDIT_START, "end": AUDIT_END},
        "warmup_from": LOAD_START,
        "variants": VARIANTS,
        "aggregation": "closed buckets only via aggregate_closed_htf; no lookahead",
        "pine_semantics": {
            "ta.highest(size)": "max(high[0]..high[size-1]) relative; excludes high[size]",
            "pivot_lag_bars": "size",
            "timestamps": [
                "pivot_candle_timestamp",
                "confirmation_timestamp",
                "event_decision_timestamp",
            ],
            "no_backpainting_in_decision_outputs": True,
        },
        "attribution": "Smart Money Concepts [LuxAlgo] — CC BY-NC-SA 4.0",
        "protected_hashes": hashes_after,
        "imports_into_policy_or_state_machine": False,
    }
    _write_json(OUT / "audit_metadata.json", meta)
    write_readme(qc, jan, f"{decision} — {decision_reason}", hashes_before, hashes_after)
    _p(f"done decision={decision}")


if __name__ == "__main__":
    main()
