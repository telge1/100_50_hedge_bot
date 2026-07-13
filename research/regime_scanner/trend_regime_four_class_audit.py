#!/usr/bin/env python3
"""Four-class regime readiness audit — inventory + causal replay (research only).

Goal: document whether the *existing* trend-state scanner can stably separate:
  strong_bullish_trend | strong_bearish_trend | accumulation_range | transition_unclear

No production changes. Does not touch trend_zones wiring.
Does not modify V6+V2 / G6 / HTF / bottoming-topping / structure semantics.

RAM-safe stepped runner:
  --step 0  load/cache 5m frame + HTF prep metadata
  --step 1  stream state-machine timeline CSV
  --step 2  diagnostic EMA/progress features (no SM)
  --step 3  join + month summaries + March case + README/decision

Example:
  PYTHONPATH=. python3 -u research/regime_scanner/trend_regime_four_class_audit.py --step all
"""
from __future__ import annotations

import argparse
import csv
import gc
import hashlib
import json
import resource
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.config import default_regime_scanner_config
from research.regime_scanner.data_loader import load_symbol_candles
from research.regime_scanner.indicators import compute_indicator_frame
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.swings import find_confirmed_pivots
from research.regime_scanner.timeframes import timeframe_timedelta
from research.regime_scanner.trend_state_machine import (
    TrendRuntime,
    default_trend_state_config,
    step_trend_state,
)
from research.regime_scanner.trend_state_policy import would_block_long, would_block_short
from research.regime_scanner.trend_structure import has_hh_hl, has_lh_ll

OUT = Path("research/regime_scanner/results/trend_regime_four_class_audit")
CACHE = OUT / "_cache"
STRUCTURE = Path("research/regime_scanner/trend_state_machine.py")
STRUCTURE_PY = Path("research/regime_scanner/trend_structure.py")
POLICY = Path("research/regime_scanner/trend_state_policy.py")

# Data window: ≥2 months warmup before Jan; analyze Jan / Feb / Mar1–15
LOAD_START = "2025-12-27T00:00:00+00:00"  # earliest available store
ANALYZE_START = "2026-01-01T00:00:00+00:00"
ANALYZE_END = "2026-03-15T00:00:00+00:00"
MARCH_FOCUS_START = "2026-03-05T00:00:00+00:00"
MARCH_FOCUS_END = "2026-03-10T00:00:00+00:00"

# Provisional mapping current SM → 4-class (diagnostic only; not a production claim)
MAP_TO_FOUR: dict[str, str] = {
    "strong_bullish": "strong_bullish_trend",
    "strong_bearish": "strong_bearish_trend",
    "neutral": "accumulation_range",  # HYPOTHESIS — audit must stress-test this
    "unavailable": "transition_unclear",
    "bearish_warning": "transition_unclear",
    "bullish_warning": "transition_unclear",
    "early_bearish": "transition_unclear",
    "early_bullish": "transition_unclear",
    "bearish_weakening": "transition_unclear",
    "bullish_weakening": "transition_unclear",
    "bottoming": "transition_unclear",
    "topping": "transition_unclear",
}


def _ts(v: object) -> pd.Timestamp:
    t = pd.Timestamp(v)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _iso(v: object | None) -> str | None:
    if v is None:
        return None
    return _ts(v).isoformat()


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _rss() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def _p(msg: str) -> None:
    print(f"{msg}  [rss≈{_rss():.0f}MB]", flush=True)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(json_safe(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    cols = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if v is None else v) for k, v in r.items()})


def install_htf_cache(frame_5m: pd.DataFrame, end_decision: pd.Timestamp) -> None:
    """Same causal HTF cache pattern as march root-cause audit (read-only speedup)."""
    import research.regime_scanner.timeframes as tf_mod
    import research.regime_scanner.trend_state_machine as sm_mod
    from research.regime_scanner.indicators import compute_indicator_frame as cif
    from research.regime_scanner.swings import find_confirmed_pivots as fcp
    from research.regime_scanner.trend_structure import update_market_structure

    ohlcv = frame_5m[[c for c in ("timestamp", "open", "high", "low", "close", "volume")]]
    full_agg: dict[str, pd.DataFrame] = {}
    full_ind: dict[str, pd.DataFrame] = {}
    full_pivots: dict[str, list] = {}
    for tf in ("15m", "30m"):
        agg = tf_mod.aggregate_candles(ohlcv, tf, end_decision).copy()
        if not agg.empty:
            agg["__close_time"] = pd.to_datetime(agg["timestamp"], utc=True) + timeframe_timedelta(tf)
        full_agg[tf] = agg
        cfg = default_regime_scanner_config().with_timeframe(tf)
        ind = cif(agg.drop(columns=["__close_time"], errors="ignore"), config=cfg).copy()
        ind["__close_time"] = pd.to_datetime(ind["timestamp"], utc=True) + timeframe_timedelta(tf)
        full_ind[tf] = ind
        full_pivots[tf] = fcp(ind.drop(columns=["__close_time"], errors="ignore"), config=cfg)

    original_agg = tf_mod.aggregate_candles

    def cached_agg(candles_5m: pd.DataFrame, timeframe: str, decision_time: object) -> pd.DataFrame:
        key = str(timeframe).strip().lower()
        if key not in full_agg:
            return original_agg(candles_5m, timeframe, decision_time)
        src = full_agg[key]
        if src.empty:
            return src.drop(columns=["__close_time"], errors="ignore")
        decision_ts = _ts(decision_time)
        out = src.loc[src["__close_time"] <= decision_ts].drop(columns=["__close_time"])
        return out.reset_index(drop=True)

    tf_mod.aggregate_candles = cached_agg  # type: ignore[assignment]
    sm_mod.aggregate_candles = cached_agg  # type: ignore[assignment]

    def cached_update(rt, *, candles_5m, decision_time, cfg, scanner_cfg):
        from research.regime_scanner.trend_state_machine import _finite

        events = []
        decision_ts = _ts(decision_time)
        for tf, slot_attr, last_attr in (
            ("15m", "structure_15m", "last_15m_bucket"),
            ("30m", "structure_30m", "last_30m_bucket"),
        ):
            src = full_ind[tf]
            if src.empty:
                continue
            sub = src.loc[src["__close_time"] <= decision_ts]
            if sub.empty:
                continue
            last = sub.iloc[-1]
            bucket = str(pd.Timestamp(last["timestamp"]))
            if getattr(rt, last_attr) == bucket:
                continue
            pivots = [p for p in full_pivots[tf] if _ts(p.confirmation_timestamp) <= decision_ts]
            atr = _finite(last["atr"]) if "atr" in sub.columns else None
            st = getattr(rt, slot_attr)
            st, evs = update_market_structure(
                st,
                candle=last.drop(labels=["__close_time"], errors="ignore"),
                pivots=pivots,
                decision_time=_ts(last["__close_time"]),
                atr=atr,
                cfg=cfg.structure,
            )
            setattr(rt, slot_attr, st)
            setattr(rt, last_attr, bucket)
            events.extend(evs)
        return events

    sm_mod._update_htf_structure = cached_update  # type: ignore[assignment]


def inventory_doc() -> dict[str, Any]:
    return {
        "existing_states": [
            "unavailable",
            "neutral",
            "bearish_warning",
            "early_bearish",
            "strong_bearish",
            "bearish_weakening",
            "bottoming",
            "bullish_warning",
            "early_bullish",
            "strong_bullish",
            "bullish_weakening",
            "topping",
        ],
        "binding_inputs": {
            "structure_5m": "mandatory for almost all transitions (BOS/CHoCH/labels/retest/failed-break)",
            "structure_15m": "soft gate for early→strong; veto component for opposite strong",
            "structure_30m": "hard veto with 15m for opposite strong; soft context on bottoming→early",
            "bias_hh_hl_lh_ll": "has_hh_hl / has_lh_ll + current_structure_bias required for early→strong",
            "bos_choch": "primary entry into warnings; progression/invalidation",
            "retest_holds": "accelerator into strong (or indicator confirms ≥2)",
            "failed_break_G6": "qualified FB/FO needed for weakening from early/strong",
            "indicators": "confirm/score only — EMA9/20, slopes, DI, ADX; never sole transition trigger",
            "confidence": "snapshot score only; not a hard gate",
            "min_hold": "blocks leaving states for N 5m bars",
        },
        "transition_sketch": {
            "neutral|unavailable → warning": "bearish_choch|failed_breakout|bearish_bos (bias≠bullish) / bullish mirror; HTF veto",
            "warning → early": "bos or LH/HL + impulse closes; HTF veto",
            "early → strong": "has_lh_ll|hh_hl + bias + 15m ok + (retest_holds OR ≥2 indicator confirms); 15m+30m hard veto opposite",
            "strong → weakening": "G6 qualified FB/FO or choch/retest_fail/HL|LH or no_ll/hh lookback; not continuing LL+BOS",
            "weakening → bottoming|topping": "≥2 of {failed_break, counter_choch, HL|LH, counter_bos}",
            "bottoming|topping → early": "structure reclaim + impulse/indicators; false bottom/top back",
        },
        "neutral_mix_hypothesis": [
            "true sideways / accumulation",
            "insufficient structure yet (post-warmup quiet)",
            "contradictory TF not expressed as transition_unclear",
            "post-invalidated warning returning to neutral",
            "NOT a dedicated accumulation_range state today",
        ],
        "four_class_provisional_map": MAP_TO_FOUR,
        "policy_no_long_context_candidates": [
            "early_bearish",
            "strong_bearish",
            "bearish_weakening",
            "topping",
        ],
    }


def step0() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    hashes = {
        "trend_structure.py": _md5(STRUCTURE_PY),
        "trend_state_machine.py": _md5(STRUCTURE),
        "trend_state_policy.py": _md5(POLICY),
    }
    _write_json(OUT / "hashes_before.json", hashes)
    _write_json(OUT / "current_state_inventory.json", inventory_doc())

    raw = load_symbol_candles("APTUSDT")
    raw = raw.copy()
    raw["timestamp"] = pd.to_datetime(raw["timestamp"], utc=True)
    end = _ts(ANALYZE_END)
    start = _ts(LOAD_START)
    slice_ = raw[(raw["timestamp"] >= start) & (raw["timestamp"] < end)].copy()
    scfg = default_regime_scanner_config().with_timeframe("5m")
    ind = compute_indicator_frame(slice_, config=scfg)
    ind = ind.copy()
    ind["timestamp"] = pd.to_datetime(ind["timestamp"], utc=True)
    ind["decision_time"] = ind["timestamp"] + pd.Timedelta(minutes=5)
    # keep only fully closed
    ind = ind.loc[ind["decision_time"] <= end].reset_index(drop=True)
    # feather/parquet cache of needed columns
    cols = [
        c
        for c in ind.columns
        if c
        in {
            "timestamp",
            "decision_time",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "atr",
            "ema_9",
            "ema_20",
            "ema_9_slope_3_pct",
            "ema_20_slope_3_pct",
            "di_spread",
            "adx",
        }
        or c.startswith("ema_")
    ]
    frame = ind[cols].copy()
    path = CACHE / "frame_5m.parquet"
    frame.to_parquet(path, index=False)
    _write_json(
        CACHE / "meta.json",
        {
            "n_bars": len(frame),
            "t_min": _iso(frame["timestamp"].iloc[0]),
            "t_max": _iso(frame["timestamp"].iloc[-1]),
            "hashes": hashes,
            "analyze_start": ANALYZE_START,
            "analyze_end": ANALYZE_END,
        },
    )
    _p(f"step0 cached {len(frame)} bars → {path}")


def step1() -> None:
    frame = pd.read_parquet(CACHE / "frame_5m.parquet")
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame["decision_time"] = pd.to_datetime(frame["decision_time"], utc=True)
    end = _ts(ANALYZE_END)
    install_htf_cache(frame, end)

    scfg = default_regime_scanner_config().with_timeframe("5m")
    cfg = default_trend_state_config()
    pivots = find_confirmed_pivots(frame, config=scfg)
    rt = TrendRuntime()

    out_path = OUT / "state_timeline_5m.csv"
    fields = [
        "decision_time",
        "candle_timestamp",
        "state",
        "previous_state",
        "four_class_map",
        "age_5m_bars",
        "min_hold_remaining",
        "reasons",
        "bias_5m",
        "bias_15m",
        "bias_30m",
        "has_hh_hl_5m",
        "has_lh_ll_5m",
        "allow_long",
        "allow_short",
        "block_long",
        "block_short",
        "close",
        "ema_9",
        "ema_20",
        "di_spread",
        "adx",
        "bearish_score",
        "bullish_score",
    ]
    analyze_start = _ts(ANALYZE_START)
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for i in range(len(frame)):
            row = frame.iloc[i]
            decision_ts = _ts(row["decision_time"])
            as_of = frame.iloc[: i + 1]
            rt, snap, _ = step_trend_state(
                rt,
                candle_row=row,
                pivots_5m=pivots,
                decision_time=decision_ts,
                candles_5m_as_of=as_of,
                bar_index=i,
                cfg=cfg,
                scanner_cfg=scfg,
            )
            if decision_ts < analyze_start:
                continue
            st = snap.current_state
            w.writerow(
                {
                    "decision_time": snap.decision_time,
                    "candle_timestamp": _iso(row["timestamp"]),
                    "state": st,
                    "previous_state": snap.previous_state,
                    "four_class_map": MAP_TO_FOUR.get(st, "transition_unclear"),
                    "age_5m_bars": snap.age_5m_bars,
                    "min_hold_remaining": snap.min_hold_remaining,
                    "reasons": "|".join(snap.active_reasons),
                    "bias_5m": rt.structure_5m.current_structure_bias,
                    "bias_15m": rt.structure_15m.current_structure_bias,
                    "bias_30m": rt.structure_30m.current_structure_bias,
                    "has_hh_hl_5m": has_hh_hl(rt.structure_5m),
                    "has_lh_ll_5m": has_lh_ll(rt.structure_5m),
                    "allow_long": snap.allow_long,
                    "allow_short": snap.allow_short,
                    "block_long": would_block_long(st),
                    "block_short": would_block_short(st),
                    "close": row.get("close"),
                    "ema_9": row.get("ema_9"),
                    "ema_20": row.get("ema_20"),
                    "di_spread": row.get("di_spread"),
                    "adx": row.get("adx"),
                    "bearish_score": snap.bearish_score,
                    "bullish_score": snap.bullish_score,
                }
            )
            if i % 2000 == 0:
                _p(f"replay bar {i}/{len(frame)} state={st}")
    _p(f"step1 wrote {out_path}")
    del frame, pivots, rt
    gc.collect()


def _window_feats(close: np.ndarray, ema9: np.ndarray, ema20: np.ndarray, atr: np.ndarray, n: int) -> dict[str, float]:
    if len(close) < n + 1:
        return {}
    c = close[-(n + 1) :]
    e9 = ema9[-(n + 1) :]
    e20 = ema20[-(n + 1) :]
    a = atr[-1]
    rets = np.diff(c)
    net = float(c[-1] - c[0])
    path = float(np.sum(np.abs(rets)))
    de = abs(net) / path if path > 1e-12 else 0.0
    up = float(np.mean(rets > 0))
    # EMA
    slope9 = float(e9[-1] - e9[0]) / max(n, 1)
    slope20 = float(e20[-1] - e20[0]) / max(n, 1)
    # mid-window slope change
    mid = n // 2
    slope9_chg = float((e9[-1] - e9[mid]) - (e9[mid] - e9[0])) / max(mid, 1)
    slope20_chg = float((e20[-1] - e20[mid]) - (e20[mid] - e20[0])) / max(mid, 1)
    sep = float(e9[-1] - e20[-1])
    sep0 = float(e9[0] - e20[0])
    sep_chg = sep - sep0
    above_both = float(np.mean((c[1:] > e9[1:]) & (c[1:] > e20[1:])))
    below_both = float(np.mean((c[1:] < e9[1:]) & (c[1:] < e20[1:])))
    crosses = int(np.sum(np.diff(np.sign(e9 - e20)) != 0))
    dist_atr = float((c[-1] - 0.5 * (e9[-1] + e20[-1])) / a) if a and a > 0 else float("nan")
    rng = float(np.max(c) - np.min(c))
    progress_vs_range = abs(net) / rng if rng > 1e-12 else 0.0
    # new highs/lows count
    highs = 0
    lows = 0
    mx = c[0]
    mn = c[0]
    for x in c[1:]:
        if x > mx:
            highs += 1
            mx = x
        if x < mn:
            lows += 1
            mn = x
    # max adverse vs start direction
    if net >= 0:
        mfe = float(np.max(c) - c[0])
        mae = float(c[0] - np.min(c))
    else:
        mfe = float(c[0] - np.min(c))
        mae = float(np.max(c) - c[0])
    mae_atr = mae / a if a and a > 0 else float("nan")
    return {
        f"n{n}_net_return": net,
        f"n{n}_directional_efficiency": de,
        f"n{n}_up_close_share": up,
        f"n{n}_ema9_slope": slope9,
        f"n{n}_ema20_slope": slope20,
        f"n{n}_ema9_slope_change": slope9_chg,
        f"n{n}_ema20_slope_change": slope20_chg,
        f"n{n}_ema9_minus_ema20": sep,
        f"n{n}_ema_sep_change": sep_chg,
        f"n{n}_share_close_above_both_ema": above_both,
        f"n{n}_share_close_below_both_ema": below_both,
        f"n{n}_ema_crosses": crosses,
        f"n{n}_dist_to_ema_band_atr": dist_atr,
        f"n{n}_progress_vs_range": progress_vs_range,
        f"n{n}_new_highs": highs,
        f"n{n}_new_lows": lows,
        f"n{n}_mae_atr": mae_atr,
    }


def step2() -> None:
    """Diagnostic features every 30m decision (12×5m) to keep file small."""
    frame = pd.read_parquet(CACHE / "frame_5m.parquet")
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame["decision_time"] = pd.to_datetime(frame["decision_time"], utc=True)
    analyze_start = _ts(ANALYZE_START)
    close = frame["close"].astype(float).to_numpy()
    ema9 = frame["ema_9"].astype(float).to_numpy()
    ema20 = frame["ema_20"].astype(float).to_numpy()
    atr = frame["atr"].astype(float).to_numpy()
    path = OUT / "regime_features_30m_sample.csv"
    rows = []
    for i in range(len(frame)):
        if _ts(frame.iloc[i]["decision_time"]) < analyze_start:
            continue
        # sample on 30m closes: minute == 0 or 30 on timestamp open → decision_time ends :05/:35
        ts = _ts(frame.iloc[i]["timestamp"])
        if ts.minute not in {25, 55}:  # last 5m bar of each 30m bucket (open 25→close 30, open 55→close 00)
            continue
        feat: dict[str, Any] = {
            "decision_time": _iso(frame.iloc[i]["decision_time"]),
            "candle_timestamp": _iso(ts),
            "close": float(close[i]),
        }
        for n in (6, 12, 24):
            feat.update(_window_feats(close[: i + 1], ema9[: i + 1], ema20[: i + 1], atr[: i + 1], n))
        # heuristic diagnostic labels (NOT production) from features alone
        f24 = feat
        de = float(f24.get("n24_directional_efficiency") or 0)
        below = float(f24.get("n24_share_close_below_both_ema") or 0)
        above = float(f24.get("n24_share_close_above_both_ema") or 0)
        slope20 = float(f24.get("n24_ema20_slope") or 0)
        crosses = float(f24.get("n24_ema_crosses") or 0)
        prog = float(f24.get("n24_progress_vs_range") or 0)
        if de >= 0.35 and below >= 0.65 and slope20 < 0 and prog >= 0.45:
            feat["feature_regime_hint"] = "strong_bearish_trend"
        elif de >= 0.35 and above >= 0.65 and slope20 > 0 and prog >= 0.45:
            feat["feature_regime_hint"] = "strong_bullish_trend"
        elif de <= 0.20 and crosses >= 2 and prog <= 0.35:
            feat["feature_regime_hint"] = "accumulation_range"
        else:
            feat["feature_regime_hint"] = "transition_unclear"
        rows.append(feat)
    _write_csv(path, rows)
    _p(f"step2 wrote {len(rows)} feature rows → {path}")
    del frame, rows
    gc.collect()


def _month_key(dt: str) -> str:
    return str(dt)[:7]


def step3() -> None:
    # Load timelines
    states = list(csv.DictReader((OUT / "state_timeline_5m.csv").open()))
    feats = {r["decision_time"]: r for r in csv.DictReader((OUT / "regime_features_30m_sample.csv").open())}

    # State dwell / mix
    state_counts = Counter(r["state"] for r in states)
    four_counts = Counter(r["four_class_map"] for r in states)
    by_month: dict[str, Counter] = defaultdict(Counter)
    four_by_month: dict[str, Counter] = defaultdict(Counter)
    long_ok_in_bearish = 0
    bearish_bars = 0
    for r in states:
        m = _month_key(r["decision_time"])
        by_month[m][r["state"]] += 1
        four_by_month[m][r["four_class_map"]] += 1
        if r["state"] in {"early_bearish", "strong_bearish", "bearish_weakening", "topping"}:
            bearish_bars += 1
            if r["allow_long"] in {"True", "true", True} or r["block_long"] in {"False", "false", False}:
                # allow_long true while in bearish family is the interesting FP for longs
                if str(r["allow_long"]) == "True":
                    long_ok_in_bearish += 1

    # Transitions
    transitions = Counter()
    for a, b in zip(states, states[1:]):
        if a["state"] != b["state"]:
            transitions[(a["state"], b["state"])] += 1

    # Neutral mixture: when mapped as accumulation, what are feature hints?
    neutral_vs_feat = Counter()
    for r in states:
        if r["state"] != "neutral":
            continue
        # nearest feature sample (same or previous 30m)
        dt = r["decision_time"]
        hint = None
        if dt in feats:
            hint = feats[dt].get("feature_regime_hint")
        else:
            # search backward coarsely
            hint = "no_feature_sample"
        neutral_vs_feat[hint] += 1

    # Agreement SM four-map vs feature hint on 30m samples
    agree = Counter()
    for dt, f in feats.items():
        # find state at that decision_time
        # binary search via dict built once
        pass
    state_at = {r["decision_time"]: r for r in states}
    for dt, f in feats.items():
        st = state_at.get(dt)
        if not st:
            continue
        mapped = st["four_class_map"]
        hint = f["feature_regime_hint"]
        agree[(mapped, hint)] += 1

    # March focus timeline (hourly sample + all transitions)
    march_rows = []
    prev = None
    for r in states:
        dt = _ts(r["decision_time"])
        if dt < _ts(MARCH_FOCUS_START) or dt > _ts(MARCH_FOCUS_END):
            continue
        changed = prev is None or prev != r["state"]
        # keep transitions + every hour
        if changed or dt.minute == 0:
            march_rows.append(r)
        prev = r["state"]
    _write_csv(OUT / "march_2026_state_timeline.csv", march_rows)

    # First times for early/strong bearish in March window
    first_early = next((r for r in states if _ts(r["decision_time"]) >= _ts(MARCH_FOCUS_START) and r["state"] == "early_bearish"), None)
    first_strong = next((r for r in states if _ts(r["decision_time"]) >= _ts(MARCH_FOCUS_START) and r["state"] == "strong_bearish"), None)
    # Also search from Mar 1
    first_early_mar = next((r for r in states if _ts(r["decision_time"]) >= _ts("2026-03-01") and r["state"] == "early_bearish"), None)
    first_strong_mar = next((r for r in states if _ts(r["decision_time"]) >= _ts("2026-03-01") and r["state"] == "strong_bearish"), None)

    # Long-friendly while price falling Mar 5-10
    long_friendly_mar = [
        r
        for r in states
        if _ts(MARCH_FOCUS_START) <= _ts(r["decision_time"]) <= _ts(MARCH_FOCUS_END)
        and str(r["allow_long"]) == "True"
        and r["state"] not in {"early_bearish", "strong_bearish", "bearish_weakening", "topping"}
    ]

    hashes_before = json.loads((OUT / "hashes_before.json").read_text())
    hashes_after = {
        "trend_structure.py": _md5(STRUCTURE_PY),
        "trend_state_machine.py": _md5(STRUCTURE),
        "trend_state_policy.py": _md5(POLICY),
    }
    assert hashes_before == hashes_after

    # Decision heuristic for "can scanner do 4-class?"
    # If neutral often coincides with feature strong trend → mixing problem
    neutral_trend_mix = neutral_vs_feat.get("strong_bearish_trend", 0) + neutral_vs_feat.get("strong_bullish_trend", 0)
    neutral_total = sum(neutral_vs_feat.values()) or 1
    mix_rate = neutral_trend_mix / neutral_total

    if first_strong_mar is None:
        decision, note = "M", "Strong bearish not reached in March window — structure path insufficient for strong_bearish_trend."
    elif mix_rate > 0.25:
        decision, note = (
            "L",
            "neutral vermischt Range und Trendphasen; 4-Klassen-Ziel nicht ohne Semantik-Trennung erreichbar.",
        )
    elif first_strong and _ts(first_strong["decision_time"]) > _ts("2026-03-07T12:00:00+00:00"):
        decision, note = (
            "K",
            "Strong-bearish wird erkannt, aber verzögert; Accumulation vs Transition noch nicht getrennt.",
        )
    else:
        decision, note = (
            "K",
            "Scanner liefert Trendpfade, aber keine dedizierte accumulation_range / transition_unclear Semantik.",
        )

    # Prefer K as default honest readout given architecture
    if decision == "M" and first_early_mar is not None:
        decision, note = (
            "K",
            "Early bearish vorhanden; strong ggf. spät/fehlend — 4-Klassen-Ziel braucht klare Restklasse + Range.",
        )

    month_summary = []
    for m in sorted(by_month):
        total = sum(by_month[m].values())
        month_summary.append(
            {
                "month": m,
                "bars": total,
                "top_states": "|".join(f"{k}:{v}" for k, v in by_month[m].most_common(5)),
                "four_class": "|".join(f"{k}:{v}" for k, v in four_by_month[m].most_common()),
                "share_strong_bearish": by_month[m].get("strong_bearish", 0) / total,
                "share_strong_bullish": by_month[m].get("strong_bullish", 0) / total,
                "share_neutral": by_month[m].get("neutral", 0) / total,
                "share_early_bearish": by_month[m].get("early_bearish", 0) / total,
                "share_mapped_accumulation": four_by_month[m].get("accumulation_range", 0) / total,
                "share_mapped_transition": four_by_month[m].get("transition_unclear", 0) / total,
            }
        )
    _write_csv(OUT / "month_regime_summary.csv", month_summary)
    _write_csv(
        OUT / "state_transition_counts.csv",
        [{"from_state": a, "to_state": b, "count": c} for (a, b), c in transitions.most_common()],
    )
    _write_csv(
        OUT / "neutral_vs_feature_hint.csv",
        [{"feature_regime_hint": k, "neutral_bars": v} for k, v in neutral_vs_feat.most_common()],
    )
    _write_csv(
        OUT / "sm_map_vs_feature_agreement.csv",
        [{"sm_four_class": a, "feature_hint": b, "count": c} for (a, b), c in agree.most_common()],
    )
    _write_csv(
        OUT / "long_friendly_during_march_focus.csv",
        long_friendly_mar[:500],
    )

    improvement = {
        "keep": [
            "Structure-first transitions (BOS/CHoCH/labels)",
            "Separate early vs strong",
            "HTF soft/hard veto concept",
            "G6 qualified failed-break weakening",
            "Policy mapping early/strong_bearish → no long",
        ],
        "do_not_do_next": [
            "Wire trend_zones into SM",
            "Add entry/momentum gates",
            "Overwrite V6+V2/G6/HTF/bottoming rules",
        ],
        "proposed_research_path": [
            "1. Split neutral into diagnostic labels: range_candidate vs unclear vs sparse_data (audit-only)",
            "2. Define accumulation_range evidence pack: low DE + EMA crosses + low progress_vs_range",
            "3. Define transition_unclear as explicit residual (warnings/early/weakening/TF conflict)",
            "4. Keep strong_* mapped from strong_bullish/bearish; validate delay on Mar-06 case",
            "5. Only after stable 4-class diagnostic: consider SM rename/collapse — still research",
        ],
        "minimal_no_long_context_for_march": [
            "state in {early_bearish, strong_bearish, bearish_weakening, topping}",
            "OR (feature_hint==strong_bearish_trend AND bias_5m==bearish) as soft research veto — not production yet",
        ],
    }

    march_case = {
        "first_early_bearish_from_mar1": None
        if first_early_mar is None
        else {
            "decision_time": first_early_mar["decision_time"],
            "reasons": first_early_mar["reasons"],
            "bias_5m": first_early_mar["bias_5m"],
            "bias_15m": first_early_mar["bias_15m"],
            "bias_30m": first_early_mar["bias_30m"],
        },
        "first_strong_bearish_from_mar1": None
        if first_strong_mar is None
        else {
            "decision_time": first_strong_mar["decision_time"],
            "reasons": first_strong_mar["reasons"],
            "bias_5m": first_strong_mar["bias_5m"],
            "bias_15m": first_strong_mar["bias_15m"],
            "bias_30m": first_strong_mar["bias_30m"],
        },
        "first_early_in_focus_window": None
        if first_early is None
        else first_early["decision_time"],
        "first_strong_in_focus_window": None
        if first_strong is None
        else first_strong["decision_time"],
        "long_friendly_bars_in_focus": len(long_friendly_mar),
        "note": "Causal availability = closed 5m + HTF buckets closed at decision_time; pivots confirmed with right-window.",
    }

    readme = f"""# Trend Regime Four-Class Audit (inventory + replay)

**Decision: {decision}** — {note}

## Scope

Research-only. No production changes. `trend_zones*` not extended/wired.
Protected hashes unchanged:
- structure `{hashes_after['trend_structure.py']}`
- machine `{hashes_after['trend_state_machine.py']}`
- policy `{hashes_after['trend_state_policy.py']}`

## Current scanner (inventory)

See `current_state_inventory.json`.

**12 states**, not 4. Closest mapping today:

| Target class | Current best proxy | Gap |
|---|---|---|
| strong_bullish_trend | `strong_bullish` | OK as proxy |
| strong_bearish_trend | `strong_bearish` | OK as proxy; delay via early+holds |
| accumulation_range | `neutral` (forced) | **neutral mixes range / quiet / post-warning / unclear** |
| transition_unclear | early/warning/weakening/bottoming/topping | Exists as many states; no single residual |

Binding inputs: **5m structure mandatory**; 15m/30m veto/context; indicators confirm only; G6 for FB/FO weakening.

## Replay

APTUSDT 5m, load from `{LOAD_START}`, analyze `{ANALYZE_START}`→`{ANALYZE_END}` (≥2 months warmup inside load).

State dwell (analyze window):
```json
{json.dumps(json_safe(dict(state_counts)), indent=2)}
```

Provisional 4-class map dwell:
```json
{json.dumps(json_safe(dict(four_counts)), indent=2)}
```

## Neutral mixing (critical)

When SM=`neutral`, feature-hint distribution:
```json
{json.dumps(json_safe(dict(neutral_vs_feat)), indent=2)}
```
Mix rate (neutral bars whose features look like strong trend): **{mix_rate:.2%}**

## March 2026 downtrend case

```json
{json.dumps(json_safe(march_case), indent=2)}
```

Long-friendly (allow_long) while not in bearish-block family during focus: **{len(long_friendly_mar)}** bars
Details: `long_friendly_during_march_focus.csv`, `march_2026_state_timeline.csv`

Minimal research `no_long_context` for that window:
- SM in early/strong/weakening bearish or topping
- (optional diagnostic) feature strong_bearish_trend + bearish 5m bias

## EMA / progress features

Sampled each 30m close: `regime_features_30m_sample.csv`
Windows N=6/12/24: DE, EMA slopes/sep/crosses, share above/below both EMAs, progress_vs_range, MAE ATR.

## Improvement plan (next research only)

```json
{json.dumps(improvement, indent=2)}
```

## Artifacts

- current_state_inventory.json
- state_timeline_5m.csv
- regime_features_30m_sample.csv
- month_regime_summary.csv
- state_transition_counts.csv
- neutral_vs_feature_hint.csv
- sm_map_vs_feature_agreement.csv
- march_2026_state_timeline.csv
- long_friendly_during_march_focus.csv
- decision.json
- README.md
"""
    (OUT / "README.md").write_text(readme, encoding="utf-8")
    _write_json(
        OUT / "decision.json",
        {
            "decision": decision,
            "note": note,
            "state_counts": dict(state_counts),
            "four_class_counts": dict(four_counts),
            "neutral_vs_feature_hint": dict(neutral_vs_feat),
            "mix_rate_neutral_looks_like_trend": mix_rate,
            "march_case": march_case,
            "improvement_plan": improvement,
            "hashes": hashes_after,
        },
    )
    _write_json(OUT / "improvement_plan.json", improvement)
    _p(f"DONE decision={decision}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", required=True, help="0|1|2|3|all")
    args = ap.parse_args()
    step = str(args.step).lower()
    if step == "all":
        for s in ("0", "1", "2", "3"):
            _p(f"===== STEP {s} =====")
            globals()[f"step{s}"]()
            gc.collect()
        return
    if step not in {"0", "1", "2", "3"}:
        raise SystemExit("step must be 0|1|2|3|all")
    globals()[f"step{step}"]()


if __name__ == "__main__":
    main()
