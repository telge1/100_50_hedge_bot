#!/usr/bin/env python3
"""Phase-B trend/regime robustness audit (research-only, read-only).

Causal replay of the existing trend state machine vs transparent ground-truth
labels. Does not modify thresholds, transitions, live bots, or recovery logic.
Does not write into research/regime_scanner/results/.

CLI:
  PYTHONPATH=. python3 -m research.regime_scanner.trend_robustness_audit \\
    --symbol APTUSDT \\
    --output-dir research/regime_scanner/results_trend_robustness_phase_b
"""
from __future__ import annotations

import argparse
import csv
import gc
import hashlib
import json
import math
import resource
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Sequence

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
    min_hold_for,
    step_trend_state,
)
from research.regime_scanner.trend_state_policy import policy_for_state
from research.regime_scanner.trend_structure import has_hh_hl, has_lh_ll

# ---------------------------------------------------------------------------
# Windows (APTUSDT 5m)
# ---------------------------------------------------------------------------
LOAD_START = "2025-12-27T00:00:00+00:00"
ANALYZE_START = "2026-03-01T00:00:00+00:00"
ANALYZE_END = "2026-05-31T23:59:59+00:00"
LOAD_END = "2026-06-01T00:00:00+00:00"  # include May 31 23:55 close
MARCH_CASE_START = "2026-03-05T18:00:00+00:00"
MARCH_CASE_END = "2026-03-10T00:00:00+00:00"

DEFAULT_OUT = Path("research/regime_scanner/results_trend_robustness_phase_b")
BAR_MINUTES = 5

# SM state → audit class (no new SM states)
AUDIT_CLASS_MAP: dict[str, str] = {
    "strong_bullish": "UPTREND",
    "early_bullish": "UPTREND",
    "strong_bearish": "DOWNTREND",
    "early_bearish": "DOWNTREND",
    "neutral": "SIDEWAYS",
    "bottoming": "BOTTOMING",
    "topping": "TOPPING",
    "unavailable": "UNCLEAR",
    "bearish_warning": "UNCLEAR",
    "bullish_warning": "UNCLEAR",
    "bearish_weakening": "UNCLEAR",
    "bullish_weakening": "UNCLEAR",
}

GT_NET_48_TREND = 1.0
GT_NET_48_SIDEWAYS = 0.5
GT_NET_288_SIDEWAYS = 2.0
GT_ADX_MIN = 18.0

# Proposed policy (evaluate only — not wired live)
# BOTTOMING: long blocked, short allowed (documented assumption)
# TOPPING: short blocked, long allowed (mirror)
PROPOSED_POLICY: dict[str, tuple[bool, bool]] = {
    "UPTREND": (True, False),
    "DOWNTREND": (False, True),
    "SIDEWAYS": (False, False),
    "UNCLEAR": (False, False),
    "BOTTOMING": (False, True),
    "TOPPING": (True, False),
}


def _ts(v: object) -> pd.Timestamp:
    t = pd.Timestamp(v)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _iso(v: object | None) -> str | None:
    if v is None:
        return None
    return _ts(v).isoformat()


def _rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def _p(msg: str) -> None:
    print(f"{msg}  [rss≈{_rss_mb():.0f}MB]", flush=True)


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


def _finite(v: object) -> float | None:
    try:
        x = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _pctile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    arr = np.asarray(list(values), dtype=float)
    return float(np.percentile(arr, q))


def audit_class_for_state(state: str) -> str:
    return AUDIT_CLASS_MAP.get(str(state or "unavailable"), "UNCLEAR")


def proposed_policy_for_audit_class(audit_class: str) -> tuple[bool, bool]:
    return PROPOSED_POLICY.get(str(audit_class), (False, False))


def exclusive_bullish_structure(hh_hl: bool, lh_ll: bool) -> bool:
    return bool(hh_hl) and not bool(lh_ll)


def exclusive_bearish_structure(hh_hl: bool, lh_ll: bool) -> bool:
    return bool(lh_ll) and not bool(hh_hl)


def net_move_pct(closes: np.ndarray, end_idx: int, lookback: int) -> float | None:
    """Causal net move ending at end_idx (inclusive). Uses only past + current."""
    if end_idx < 0 or end_idx >= len(closes):
        return None
    start = end_idx - lookback
    if start < 0:
        return None
    c0 = float(closes[start])
    c1 = float(closes[end_idx])
    if not math.isfinite(c0) or not math.isfinite(c1) or c0 == 0.0:
        return None
    return (c1 - c0) / c0 * 100.0


def range_vs_atr(highs: np.ndarray, lows: np.ndarray, atr: float | None, end_idx: int, lookback: int) -> float | None:
    """Range over lookback / ATR — causal, past only."""
    if atr is None or atr <= 0 or end_idx < 0:
        return None
    start = end_idx - lookback + 1
    if start < 0:
        return None
    hi = float(np.nanmax(highs[start : end_idx + 1]))
    lo = float(np.nanmin(lows[start : end_idx + 1]))
    if not math.isfinite(hi) or not math.isfinite(lo):
        return None
    return (hi - lo) / float(atr)


def ground_truth_label(
    *,
    has_hh_hl_flag: bool,
    has_lh_ll_flag: bool,
    net_48: float | None,
    net_288: float | None,
    di_spread: float | None,
    adx: float | None,
) -> str:
    """Causal GT at decision_time. Never uses future bars."""
    n48 = net_48
    n288 = net_288
    ds = di_spread
    ax = adx
    if n48 is None or ds is None or ax is None:
        return "AMBIGUOUS"

    excl_up = exclusive_bullish_structure(has_hh_hl_flag, has_lh_ll_flag)
    excl_dn = exclusive_bearish_structure(has_hh_hl_flag, has_lh_ll_flag)

    if excl_up and n48 > GT_NET_48_TREND and ds > 0 and ax >= GT_ADX_MIN:
        return "CLEAR_UPTREND"
    if excl_dn and n48 < -GT_NET_48_TREND and ds < 0 and ax >= GT_ADX_MIN:
        return "CLEAR_DOWNTREND"
    if (
        abs(n48) < GT_NET_48_SIDEWAYS
        and n288 is not None
        and abs(n288) < GT_NET_288_SIDEWAYS
        and not excl_up
        and not excl_dn
    ):
        return "CLEAR_SIDEWAYS"
    return "AMBIGUOUS"


def htf_closed_only(src: pd.DataFrame, decision_ts: pd.Timestamp, close_col: str = "__close_time") -> pd.DataFrame:
    """Unit-testable helper: keep only HTF buckets whose close <= decision_time."""
    if src.empty or close_col not in src.columns:
        return src.drop(columns=[close_col], errors="ignore")
    out = src.loc[src[close_col] <= _ts(decision_ts)].drop(columns=[close_col], errors="ignore")
    return out.reset_index(drop=True)


def install_htf_cache(frame_5m: pd.DataFrame, end_decision: pd.Timestamp) -> None:
    """Causal HTF cache (adapted from trend_regime_four_class_audit; local to this module)."""
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
        return htf_closed_only(src, _ts(decision_time))

    tf_mod.aggregate_candles = cached_agg  # type: ignore[assignment]
    sm_mod.aggregate_candles = cached_agg  # type: ignore[assignment]

    def cached_update(rt, *, candles_5m, decision_time, cfg, scanner_cfg):
        from research.regime_scanner.trend_state_machine import _finite as sm_finite

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
            atr = sm_finite(last["atr"]) if "atr" in sub.columns else None
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


def load_analysis_frame(
    symbol: str,
    *,
    load_start: str = LOAD_START,
    load_end: str = LOAD_END,
    max_bars: int | None = None,
) -> pd.DataFrame:
    raw = load_symbol_candles(symbol)
    raw = raw.copy()
    raw["timestamp"] = pd.to_datetime(raw["timestamp"], utc=True)
    start = _ts(load_start)
    end = _ts(load_end)
    slice_ = raw[(raw["timestamp"] >= start) & (raw["timestamp"] < end)].copy()
    scfg = default_regime_scanner_config().with_timeframe("5m")
    ind = compute_indicator_frame(slice_, config=scfg).copy()
    ind["timestamp"] = pd.to_datetime(ind["timestamp"], utc=True)
    ind["decision_time"] = ind["timestamp"] + pd.Timedelta(minutes=BAR_MINUTES)
    # only fully closed relative to load_end
    ind = ind.loc[ind["decision_time"] <= end].reset_index(drop=True)
    if max_bars is not None and max_bars > 0:
        ind = ind.iloc[: int(max_bars)].reset_index(drop=True)
    return ind


def _event_type(ev: object | None) -> str | None:
    if ev is None:
        return None
    return getattr(ev, "event_type", None) or (ev.get("event_type") if isinstance(ev, dict) else None)


def _event_level(ev: object | None) -> float | None:
    if ev is None:
        return None
    if hasattr(ev, "level"):
        return _finite(getattr(ev, "level"))
    if isinstance(ev, dict):
        return _finite(ev.get("level"))
    return None


def stream_timeline(
    frame: pd.DataFrame,
    *,
    analyze_start: pd.Timestamp,
    analyze_end: pd.Timestamp,
    out_csv: Path,
    progress_every: int = 2000,
) -> dict[str, Any]:
    """Stream SM replay for all bars from load; write metrics rows for analyze window."""
    end_decision = _ts(frame["decision_time"].iloc[-1])
    install_htf_cache(frame, end_decision)

    scfg = default_regime_scanner_config().with_timeframe("5m")
    cfg = default_trend_state_config()
    pivots = find_confirmed_pivots(frame, config=scfg)
    rt = TrendRuntime()

    closes = frame["close"].to_numpy(dtype=float)
    highs = frame["high"].to_numpy(dtype=float) if "high" in frame.columns else closes.copy()
    lows = frame["low"].to_numpy(dtype=float) if "low" in frame.columns else closes.copy()

    fields = [
        "decision_time",
        "candle_timestamp",
        "close",
        "state",
        "previous_state",
        "audit_class",
        "gt_label",
        "age",
        "min_hold_remaining",
        "reasons",
        "bias_5m",
        "bias_15m",
        "bias_30m",
        "has_hh_hl",
        "has_lh_ll",
        "last_high_label",
        "last_low_label",
        "last_bos",
        "last_choch",
        "last_bos_level",
        "last_choch_level",
        "protective_low_level",
        "protective_high_level",
        "allow_long",
        "allow_short",
        "proposed_allow_long",
        "proposed_allow_short",
        "adx",
        "di_spread",
        "net_48",
        "net_288",
        "range_atr_48",
        "year_month",
    ]

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    n_written = 0
    n_warmup = 0
    with out_csv.open("w", newline="", encoding="utf-8") as fh:
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
            candle_ts = _ts(row["timestamp"])
            if decision_ts < analyze_start or candle_ts > analyze_end:
                n_warmup += 1
                if i % progress_every == 0:
                    _p(f"warmup/replay bar {i}/{len(frame)} state={rt.state}")
                continue

            st = str(snap.current_state)
            aclass = audit_class_for_state(st)
            prop_l, prop_s = proposed_policy_for_audit_class(aclass)
            pol = policy_for_state(st)
            hh = has_hh_hl(rt.structure_5m)
            lh = has_lh_ll(rt.structure_5m)
            n48 = net_move_pct(closes, i, 48)
            n288 = net_move_pct(closes, i, 288)
            adx = _finite(row.get("adx"))
            di = _finite(row.get("di_spread"))
            atr = _finite(row.get("atr"))
            gt = ground_truth_label(
                has_hh_hl_flag=hh,
                has_lh_ll_flag=lh,
                net_48=n48,
                net_288=n288,
                di_spread=di,
                adx=adx,
            )
            ym = f"{candle_ts.year:04d}-{candle_ts.month:02d}"
            w.writerow(
                {
                    "decision_time": snap.decision_time,
                    "candle_timestamp": _iso(candle_ts),
                    "close": row.get("close"),
                    "state": st,
                    "previous_state": snap.previous_state,
                    "audit_class": aclass,
                    "gt_label": gt,
                    "age": snap.age_5m_bars,
                    "min_hold_remaining": snap.min_hold_remaining,
                    "reasons": "|".join(snap.active_reasons),
                    "bias_5m": rt.structure_5m.current_structure_bias,
                    "bias_15m": rt.structure_15m.current_structure_bias,
                    "bias_30m": rt.structure_30m.current_structure_bias,
                    "has_hh_hl": hh,
                    "has_lh_ll": lh,
                    "last_high_label": rt.structure_5m.last_high_label,
                    "last_low_label": rt.structure_5m.last_low_label,
                    "last_bos": _event_type(rt.structure_5m.last_bos),
                    "last_choch": _event_type(rt.structure_5m.last_choch),
                    "last_bos_level": _event_level(rt.structure_5m.last_bos),
                    "last_choch_level": _event_level(rt.structure_5m.last_choch),
                    "protective_low_level": rt.structure_5m.protective_low_level,
                    "protective_high_level": rt.structure_5m.protective_high_level,
                    "allow_long": bool(pol.allow_long),
                    "allow_short": bool(pol.allow_short),
                    "proposed_allow_long": prop_l,
                    "proposed_allow_short": prop_s,
                    "adx": adx,
                    "di_spread": di,
                    "net_48": n48,
                    "net_288": n288,
                    "range_atr_48": range_vs_atr(highs, lows, atr, i, 48),
                    "year_month": ym,
                }
            )
            n_written += 1
            if i % progress_every == 0:
                _p(f"analyze bar {i}/{len(frame)} state={st} gt={gt} written={n_written}")

    meta = {
        "n_frame_bars": len(frame),
        "n_analyze_rows": n_written,
        "n_warmup_skipped_writes": n_warmup,
        "analyze_start": _iso(analyze_start),
        "analyze_end": _iso(analyze_end),
        "timeline_path": str(out_csv),
    }
    del pivots, rt
    gc.collect()
    return meta


def _as_bool_series(s: pd.Series) -> pd.Series:
    return s.map(lambda x: str(x).strip().lower() in {"1", "true", "t", "yes"})


def contiguous_episodes(mask: np.ndarray) -> list[tuple[int, int]]:
    """Return inclusive (start, end) index pairs where mask is True."""
    episodes: list[tuple[int, int]] = []
    n = len(mask)
    i = 0
    while i < n:
        if not mask[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and mask[j + 1]:
            j += 1
        episodes.append((i, j))
        i = j + 1
    return episodes


def compute_state_durations(states: Sequence[str]) -> list[tuple[str, int]]:
    """Run-length durations of consecutive equal states."""
    if not states:
        return []
    out: list[tuple[str, int]] = []
    cur = states[0]
    length = 1
    for s in states[1:]:
        if s == cur:
            length += 1
        else:
            out.append((cur, length))
            cur = s
            length = 1
    out.append((cur, length))
    return out


def count_transitions(states: Sequence[str], ages_at_end: Sequence[int] | None = None) -> list[dict[str, Any]]:
    """Count from→to transitions; median prior hold = age at last bar of prior state."""
    counts: Counter[tuple[str, str]] = Counter()
    holds: dict[tuple[str, str], list[int]] = defaultdict(list)
    for i in range(1, len(states)):
        a, b = states[i - 1], states[i]
        if a == b:
            continue
        counts[(a, b)] += 1
        if ages_at_end is not None:
            holds[(a, b)].append(int(ages_at_end[i - 1]))
        else:
            # reconstruct hold from run length ending at i-1
            hold = 1
            j = i - 2
            while j >= 0 and states[j] == a:
                hold += 1
                j -= 1
            holds[(a, b)].append(hold)
    rows = []
    for (a, b), c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        hs = holds[(a, b)]
        reverse = counts.get((b, a), 0)
        rows.append(
            {
                "from_state": a,
                "to_state": b,
                "count": c,
                "median_prior_hold_bars": float(np.median(hs)) if hs else None,
                "ping_pong_reverse_count": reverse,
                "is_ping_pong_pair": bool(reverse > 0 and c > 0),
            }
        )
    return rows


def detect_ping_pong(states: Sequence[str], max_gap: int = 6) -> list[dict[str, Any]]:
    """A→B→A within max_gap bars between transitions."""
    events: list[dict[str, Any]] = []
    i = 1
    while i < len(states):
        if states[i] == states[i - 1]:
            i += 1
            continue
        a, b = states[i - 1], states[i]
        # look ahead for return to A
        j = i + 1
        while j < len(states) and j - i <= max_gap:
            if states[j] != states[j - 1]:
                if states[j] == a and states[j - 1] == b:
                    events.append(
                        {
                            "index": i,
                            "pattern": f"{a}->{b}->{a}",
                            "gap_bars": j - i,
                            "state_a": a,
                            "state_b": b,
                        }
                    )
                    break
            j += 1
        i += 1
    return events


def detection_delays(
    df: pd.DataFrame,
    *,
    gt_label: str,
    match_audit: str,
    cfg=None,
) -> list[dict[str, Any]]:
    """Delays for CLEAR up/down contiguous episodes."""
    cfg = cfg or default_trend_state_config()
    mask = (df["gt_label"].astype(str) == gt_label).to_numpy()
    rows: list[dict[str, Any]] = []
    for ep_id, (s, e) in enumerate(contiguous_episodes(mask)):
        ref_start = _ts(df.iloc[s]["decision_time"])
        first_match_i = None
        first_stable_i = None
        for i in range(s, e + 1):
            row = df.iloc[i]
            if str(row["audit_class"]) != match_audit:
                continue
            if first_match_i is None:
                first_match_i = i
            age = int(row.get("age") or 0)
            st = str(row["state"])
            # stable if age already past min_hold, or age >= min_hold_for
            mh = min_hold_for(st, cfg)
            if age >= mh:
                first_stable_i = i
                break
        first_match_delay = None if first_match_i is None else first_match_i - s
        stable_delay = None if first_stable_i is None else first_stable_i - s
        rows.append(
            {
                "episode_id": ep_id,
                "gt_label": gt_label,
                "ref_start": _iso(ref_start),
                "ref_end": _iso(_ts(df.iloc[e]["decision_time"])),
                "episode_bars": e - s + 1,
                "first_matching_audit_class_time": (
                    None if first_match_i is None else _iso(_ts(df.iloc[first_match_i]["decision_time"]))
                ),
                "first_matching_state": None if first_match_i is None else df.iloc[first_match_i]["state"],
                "first_stable_time": (
                    None if first_stable_i is None else _iso(_ts(df.iloc[first_stable_i]["decision_time"]))
                ),
                "first_stable_state": None if first_stable_i is None else df.iloc[first_stable_i]["state"],
                "delay_first_match_candles": first_match_delay,
                "delay_first_match_minutes": None if first_match_delay is None else first_match_delay * BAR_MINUTES,
                "delay_stable_candles": stable_delay,
                "delay_stable_minutes": None if stable_delay is None else stable_delay * BAR_MINUTES,
                "missed": first_match_i is None,
            }
        )
    return rows


def delay_summary(delay_rows: list[dict[str, Any]], key: str = "delay_first_match_candles") -> dict[str, Any]:
    vals = [float(r[key]) for r in delay_rows if r.get(key) is not None]
    return {
        "n_episodes": len(delay_rows),
        "n_with_detection": len(vals),
        "n_missed": sum(1 for r in delay_rows if r.get("missed")),
        "median": _pctile(vals, 50),
        "p75": _pctile(vals, 75),
        "p90": _pctile(vals, 90),
        "max": (max(vals) if vals else None),
    }


def countertrend_exposure(df: pd.DataFrame) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for gt, bad_audit, long_side in (
        ("CLEAR_DOWNTREND", "UPTREND", True),
        ("CLEAR_UPTREND", "DOWNTREND", False),
    ):
        sub = df[df["gt_label"].astype(str) == gt]
        n = len(sub)
        if n == 0:
            rows.append(
                {
                    "gt_label": gt,
                    "n_bars": 0,
                    "share_bad_audit_class": None,
                    "share_proposed_countertrend_grant": None,
                    "share_existing_countertrend_grant": None,
                    "longest_false_grant_bars": 0,
                    "false_grant_runs": 0,
                }
            )
            continue
        bad = (sub["audit_class"].astype(str) == bad_audit).sum()
        if long_side:
            prop = _as_bool_series(sub["proposed_allow_long"])
            exist = _as_bool_series(sub["allow_long"])
        else:
            prop = _as_bool_series(sub["proposed_allow_short"])
            exist = _as_bool_series(sub["allow_short"])
        # longest contiguous true in prop (aligned to sub index order)
        prop_arr = prop.to_numpy(dtype=bool)
        longest = 0
        runs = 0
        for a, b in contiguous_episodes(prop_arr):
            runs += 1
            longest = max(longest, b - a + 1)
        rows.append(
            {
                "gt_label": gt,
                "n_bars": int(n),
                "bad_audit_class": bad_audit,
                "share_bad_audit_class": float(bad) / float(n),
                "n_bad_audit_class": int(bad),
                "share_proposed_countertrend_grant": float(prop.mean()),
                "share_existing_countertrend_grant": float(exist.mean()),
                "longest_false_grant_bars": int(longest),
                "false_grant_runs": int(runs),
            }
        )
    return rows


def sideways_false_trends(df: pd.DataFrame) -> list[dict[str, Any]]:
    sub = df[df["gt_label"].astype(str) == "CLEAR_SIDEWAYS"]
    if sub.empty:
        return [
            {
                "n_sideways_bars": 0,
                "false_uptrend_bars": 0,
                "false_downtrend_bars": 0,
                "false_uptrend_episodes": 0,
                "false_downtrend_episodes": 0,
                "mean_false_up_duration": None,
                "mean_false_down_duration": None,
                "median_false_up_duration": None,
                "median_false_down_duration": None,
                "state_switches": 0,
            }
        ]
    ac = sub["audit_class"].astype(str).to_numpy()
    up_mask = ac == "UPTREND"
    dn_mask = ac == "DOWNTREND"
    up_eps = contiguous_episodes(up_mask)
    dn_eps = contiguous_episodes(dn_mask)
    up_durs = [b - a + 1 for a, b in up_eps]
    dn_durs = [b - a + 1 for a, b in dn_eps]
    states = sub["state"].astype(str).tolist()
    switches = sum(1 for i in range(1, len(states)) if states[i] != states[i - 1])
    return [
        {
            "n_sideways_bars": int(len(sub)),
            "false_uptrend_bars": int(up_mask.sum()),
            "false_downtrend_bars": int(dn_mask.sum()),
            "false_uptrend_episodes": len(up_eps),
            "false_downtrend_episodes": len(dn_eps),
            "mean_false_up_duration": float(np.mean(up_durs)) if up_durs else None,
            "mean_false_down_duration": float(np.mean(dn_durs)) if dn_durs else None,
            "median_false_up_duration": float(np.median(up_durs)) if up_durs else None,
            "median_false_down_duration": float(np.median(dn_durs)) if dn_durs else None,
            "p90_false_up_duration": _pctile(up_durs, 90),
            "p90_false_down_duration": _pctile(dn_durs, 90),
            "state_switches": int(switches),
        }
    ]


def classification_error_stats(df: pd.DataFrame) -> dict[str, Any]:
    """Errors only on non-AMBIGUOUS GT. AMBIGUOUS never counted as FP/FN."""
    clear = df[df["gt_label"].astype(str) != "AMBIGUOUS"].copy()
    mapping = {
        "CLEAR_UPTREND": "UPTREND",
        "CLEAR_DOWNTREND": "DOWNTREND",
        "CLEAR_SIDEWAYS": "SIDEWAYS",
    }
    clear["expected_audit"] = clear["gt_label"].map(mapping)
    clear["match"] = clear["audit_class"].astype(str) == clear["expected_audit"].astype(str)
    by_gt: dict[str, Any] = {}
    for gt, exp in mapping.items():
        g = clear[clear["gt_label"] == gt]
        n = len(g)
        if n == 0:
            by_gt[gt] = {"n": 0, "match_rate": None, "mismatch_rate": None}
            continue
        m = int(g["match"].sum())
        by_gt[gt] = {
            "n": n,
            "matches": m,
            "mismatches": n - m,
            "match_rate": m / n,
            "mismatch_rate": (n - m) / n,
            "top_mismatches": dict(Counter(g.loc[~g["match"], "audit_class"].astype(str)).most_common(5)),
        }
    ambiguous_n = int((df["gt_label"].astype(str) == "AMBIGUOUS").sum())
    return {
        "n_total": int(len(df)),
        "n_ambiguous_excluded_from_errors": ambiguous_n,
        "n_clear": int(len(clear)),
        "overall_clear_match_rate": float(clear["match"].mean()) if len(clear) else None,
        "by_gt": by_gt,
    }


def build_state_distribution(df: pd.DataFrame) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    scopes: list[tuple[str, pd.DataFrame]] = [("overall", df)]
    for ym, g in df.groupby("year_month", sort=True):
        scopes.append((str(ym), g))
    for scope, g in scopes:
        n = len(g)
        for key in ("state", "audit_class"):
            durations = compute_state_durations(g[key].astype(str).tolist())
            dur_by: dict[str, list[int]] = defaultdict(list)
            for s, d in durations:
                dur_by[s].append(d)
            for val, cnt in Counter(g[key].astype(str)).most_common():
                ds = dur_by.get(val, [])
                rows.append(
                    {
                        "scope": scope,
                        "kind": key,
                        "label": val,
                        "count": int(cnt),
                        "pct": (100.0 * cnt / n) if n else 0.0,
                        "mean_duration_bars": float(np.mean(ds)) if ds else None,
                        "median_duration_bars": float(np.median(ds)) if ds else None,
                        "p90_duration_bars": _pctile(ds, 90),
                        "n_episodes": len(ds),
                    }
                )
    return rows


def monthly_summary(df: pd.DataFrame, err: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for ym, g in df.groupby("year_month", sort=True):
        n = len(g)
        ac = Counter(g["audit_class"].astype(str))
        gt = Counter(g["gt_label"].astype(str))
        local_err = classification_error_stats(g)
        rows.append(
            {
                "year_month": ym,
                "n_bars": n,
                "pct_UPTREND": 100.0 * ac.get("UPTREND", 0) / n,
                "pct_DOWNTREND": 100.0 * ac.get("DOWNTREND", 0) / n,
                "pct_SIDEWAYS": 100.0 * ac.get("SIDEWAYS", 0) / n,
                "pct_UNCLEAR": 100.0 * ac.get("UNCLEAR", 0) / n,
                "pct_BOTTOMING": 100.0 * ac.get("BOTTOMING", 0) / n,
                "pct_TOPPING": 100.0 * ac.get("TOPPING", 0) / n,
                "pct_CLEAR_UPTREND": 100.0 * gt.get("CLEAR_UPTREND", 0) / n,
                "pct_CLEAR_DOWNTREND": 100.0 * gt.get("CLEAR_DOWNTREND", 0) / n,
                "pct_CLEAR_SIDEWAYS": 100.0 * gt.get("CLEAR_SIDEWAYS", 0) / n,
                "pct_AMBIGUOUS": 100.0 * gt.get("AMBIGUOUS", 0) / n,
                "clear_match_rate": local_err.get("overall_clear_match_rate"),
                "n_state_switches": int(sum(1 for i in range(1, n) if g["state"].iloc[i] != g["state"].iloc[i - 1])),
            }
        )
    return rows


def march_case_study(df: pd.DataFrame, *, every_n: int = 1) -> list[dict[str, Any]]:
    start = _ts(MARCH_CASE_START)
    end = _ts(MARCH_CASE_END)
    dts = pd.to_datetime(df["decision_time"], utc=True)
    # include candle opens in [start, end)
    cts = pd.to_datetime(df["candle_timestamp"], utc=True)
    mask = (cts >= start) & (cts < end)
    sub = df.loc[mask].reset_index(drop=True)
    if every_n > 1:
        sub = sub.iloc[::every_n].reset_index(drop=True)
    rows = []
    for _, r in sub.iterrows():
        rows.append(
            {
                "timestamp": r["candle_timestamp"],
                "decision_time": r["decision_time"],
                "close": r["close"],
                "state_5m": r["state"],
                "previous_state": r["previous_state"],
                "audit_class": r["audit_class"],
                "gt_label": r["gt_label"],
                "bias_5m": r["bias_5m"],
                "bias_15m": r["bias_15m"],
                "bias_30m": r["bias_30m"],
                "last_high_label": r.get("last_high_label"),
                "last_low_label": r.get("last_low_label"),
                "has_hh_hl": r["has_hh_hl"],
                "has_lh_ll": r["has_lh_ll"],
                "last_bos": r["last_bos"],
                "last_choch": r["last_choch"],
                "last_bos_level": r.get("last_bos_level"),
                "last_choch_level": r.get("last_choch_level"),
                "protective_low_level": r.get("protective_low_level"),
                "protective_high_level": r.get("protective_high_level"),
                "proposed_audit_class": r["audit_class"],
                "final_state": r["state"],
                "transition_reason": r["reasons"],
                "allow_long_existing": r["allow_long"],
                "allow_short_existing": r["allow_short"],
                "proposed_allow_long": r["proposed_allow_long"],
                "proposed_allow_short": r["proposed_allow_short"],
                "age": r["age"],
                "adx": r["adx"],
                "di_spread": r["di_spread"],
                "net_48": r["net_48"],
            }
        )
    return rows


def root_cause_findings(df: pd.DataFrame, err: dict[str, Any]) -> list[dict[str, Any]]:
    """Concrete findings from measured mismatches + known policy deltas (no threshold changes)."""
    findings: list[dict[str, Any]] = []

    # Policy mismatches: existing vs proposed
    findings.append(
        {
            "finding_id": "pol_bottoming_long",
            "file": "research/regime_scanner/trend_state_policy.py",
            "function": "policy_for_state / _POLICY_TABLE['bottoming']",
            "condition": "bottoming: allow_long=True, allow_short=False",
            "effect": "Existing policy opens long during BOTTOMING; proposed Phase-B policy blocks long (short allowed).",
            "structural_vs_parameter": "structural_policy",
            "other_cases": "Any bottoming→early_bullish path; March recovery bottoms",
            "evidence": f"BOTTOMING bars={int((df['audit_class']=='BOTTOMING').sum())}",
        }
    )
    findings.append(
        {
            "finding_id": "pol_topping_short",
            "file": "research/regime_scanner/trend_state_policy.py",
            "function": "policy_for_state / _POLICY_TABLE['topping']",
            "condition": "topping: allow_long=False, allow_short=True",
            "effect": "Existing allows short in TOPPING; proposed blocks short (long allowed).",
            "structural_vs_parameter": "structural_policy",
            "other_cases": "Any topping episodes at swing highs",
            "evidence": f"TOPPING bars={int((df['audit_class']=='TOPPING').sum())}",
        }
    )
    findings.append(
        {
            "finding_id": "pol_neutral_both",
            "file": "research/regime_scanner/trend_state_policy.py",
            "function": "_POLICY_TABLE['neutral']",
            "condition": "neutral: allow_long=True, allow_short=True",
            "effect": "SIDEWAYS (neutral) allows both sides today; proposed blocks both.",
            "structural_vs_parameter": "structural_policy",
            "other_cases": "All quiet / post-invalidation neutral stretches",
            "evidence": f"SIDEWAYS bars={int((df['audit_class']=='SIDEWAYS').sum())}",
        }
    )
    findings.append(
        {
            "finding_id": "pol_unavailable_both",
            "file": "research/regime_scanner/trend_state_policy.py",
            "function": "_POLICY_TABLE['unavailable']",
            "condition": "unavailable: allow_long=True, allow_short=True",
            "effect": "UNCLEAR(unavailable) allows both; proposed blocks both.",
            "structural_vs_parameter": "structural_policy",
            "other_cases": "Warmup gaps and data_gap resets",
            "evidence": f"unavailable bars={int((df['state']=='unavailable').sum())}",
        }
    )
    findings.append(
        {
            "finding_id": "map_early_as_trend",
            "file": "research/regime_scanner/trend_robustness_audit.py",
            "function": "AUDIT_CLASS_MAP",
            "condition": "early_bullish/early_bearish mapped to UPTREND/DOWNTREND",
            "effect": "Early states counted as full trends — may look aggressive vs strong_* only.",
            "structural_vs_parameter": "audit_mapping",
            "other_cases": "All early→strong progressions",
            "evidence": (
                f"early_bullish={int((df['state']=='early_bullish').sum())}; "
                f"early_bearish={int((df['state']=='early_bearish').sum())}"
            ),
        }
    )

    by_gt = err.get("by_gt") or {}
    # CLEAR_DOWNTREND misclassified as UPTREND
    dn = by_gt.get("CLEAR_DOWNTREND") or {}
    top_dn = dn.get("top_mismatches") or {}
    if top_dn.get("UPTREND") or top_dn.get("SIDEWAYS") or top_dn.get("UNCLEAR"):
        findings.append(
            {
                "finding_id": "mis_clear_down",
                "file": "research/regime_scanner/trend_state_machine.py",
                "function": "_propose_transition / early→strong / warning gates",
                "condition": "HTF soft/hard gates + min_hold + structure labels required before early/strong bearish",
                "effect": f"During CLEAR_DOWNTREND mismatch_rate={dn.get('mismatch_rate')}; top={top_dn}",
                "structural_vs_parameter": "both (structure path + thresholds)",
                "other_cases": "Any fast impulse down before LH/LL + BOS chain completes",
                "evidence": json.dumps(dn, sort_keys=True),
            }
        )
    up = by_gt.get("CLEAR_UPTREND") or {}
    top_up = up.get("top_mismatches") or {}
    if top_up:
        findings.append(
            {
                "finding_id": "mis_clear_up",
                "file": "research/regime_scanner/trend_state_machine.py",
                "function": "_propose_transition",
                "condition": "Bullish progression needs HH/HL + bias + 15m ok + retest/indicators",
                "effect": f"During CLEAR_UPTREND mismatch_rate={up.get('mismatch_rate')}; top={top_up}",
                "structural_vs_parameter": "both",
                "other_cases": "Recoveries after bottoming",
                "evidence": json.dumps(up, sort_keys=True),
            }
        )
    sw = by_gt.get("CLEAR_SIDEWAYS") or {}
    top_sw = sw.get("top_mismatches") or {}
    if top_sw.get("UPTREND") or top_sw.get("DOWNTREND"):
        findings.append(
            {
                "finding_id": "mis_sideways_false_trend",
                "file": "research/regime_scanner/trend_structure.py",
                "function": "has_hh_hl / has_lh_ll / update_market_structure",
                "condition": "Local HH/HL or LH/LL inside a larger range can still trigger warning→early",
                "effect": f"CLEAR_SIDEWAYS false trends; top_mismatches={top_sw}",
                "structural_vs_parameter": "structural (swing labels local)",
                "other_cases": "Compressed ranges with small BOS",
                "evidence": json.dumps(sw, sort_keys=True),
            }
        )

    # Countertrend under existing policy during clear down
    cd = df[df["gt_label"].astype(str) == "CLEAR_DOWNTREND"]
    if len(cd):
        long_share = float(_as_bool_series(cd["allow_long"]).mean())
        if long_share > 0.05:
            findings.append(
                {
                    "finding_id": "exposure_long_in_clear_down",
                    "file": "research/regime_scanner/trend_state_policy.py",
                    "function": "would_block_long / policy_for_state",
                    "condition": "States mapped UNCLEAR/SIDEWAYS/BOTTOMING/UPTREND still allow_long under existing table",
                    "effect": f"Existing allow_long share during CLEAR_DOWNTREND={long_share:.3f}",
                    "structural_vs_parameter": "structural_policy",
                    "other_cases": "March 6 crash window and any clear down with delayed early_bearish",
                    "evidence": f"n={len(cd)}",
                }
            )
    return findings


def timeline_hash(path: Path, max_bytes: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        remaining = max_bytes
        while remaining > 0:
            chunk = fh.read(min(1 << 16, remaining))
            if not chunk:
                break
            h.update(chunk)
            remaining -= len(chunk)
    return h.hexdigest()


def analyze_timeline(timeline_csv: Path, out_dir: Path) -> dict[str, Any]:
    df = pd.read_csv(timeline_csv)
    if df.empty:
        raise RuntimeError(f"empty timeline: {timeline_csv}")
    for col in ("decision_time", "candle_timestamp"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], utc=True)

    err = classification_error_stats(df)
    dist = build_state_distribution(df)
    states = df["state"].astype(str).tolist()
    ages = df["age"].fillna(0).astype(int).tolist()
    transitions = count_transitions(states, ages_at_end=ages)
    ping = detect_ping_pong(states, max_gap=6)
    ping_rows = [
        {"pattern": p["pattern"], "count": c}
        for p, c in Counter(x["pattern"] for x in ping).most_common()
    ]
    delays_up = detection_delays(df, gt_label="CLEAR_UPTREND", match_audit="UPTREND")
    delays_dn = detection_delays(df, gt_label="CLEAR_DOWNTREND", match_audit="DOWNTREND")
    delay_rows = delays_up + delays_dn
    ct = countertrend_exposure(df)
    sw_false = sideways_false_trends(df)
    monthly = monthly_summary(df, err)
    march = march_case_study(df, every_n=1)
    roots = root_cause_findings(df, err)

    _write_csv(out_dir / "state_distribution.csv", dist)
    _write_csv(out_dir / "transition_matrix.csv", transitions)
    _write_csv(out_dir / "transition_ping_pong.csv", ping_rows)
    _write_csv(out_dir / "trend_detection_delays.csv", delay_rows)
    _write_csv(out_dir / "countertrend_exposure.csv", ct)
    _write_csv(out_dir / "sideways_false_trends.csv", sw_false)
    _write_csv(out_dir / "monthly_summary.csv", monthly)
    _write_csv(out_dir / "march_case_study.csv", march)
    _write_csv(out_dir / "root_cause_findings.csv", roots)

    summary = {
        "symbol_window": {
            "load_start": LOAD_START,
            "analyze_start": ANALYZE_START,
            "analyze_end": ANALYZE_END,
            "march_case": {"start": MARCH_CASE_START, "end": MARCH_CASE_END},
            "n_analyze_bars": int(len(df)),
            "t_min": _iso(df["decision_time"].iloc[0]),
            "t_max": _iso(df["decision_time"].iloc[-1]),
        },
        "audit_class_map": AUDIT_CLASS_MAP,
        "proposed_policy": {k: {"allow_long": v[0], "allow_short": v[1]} for k, v in PROPOSED_POLICY.items()},
        "ground_truth_rules": {
            "CLEAR_UPTREND": "has_hh_hl & !has_lh_ll & net_48>+1 & di_spread>0 & adx>=18",
            "CLEAR_DOWNTREND": "has_lh_ll & !has_hh_hl & net_48<-1 & di_spread<0 & adx>=18",
            "CLEAR_SIDEWAYS": "abs(net_48)<0.5 & abs(net_288)<2 & not exclusive trend structure",
            "AMBIGUOUS": "otherwise — never scored as FP/FN",
        },
        "gt_counts": dict(Counter(df["gt_label"].astype(str))),
        "audit_class_counts": dict(Counter(df["audit_class"].astype(str))),
        "state_counts": dict(Counter(df["state"].astype(str))),
        "classification_vs_gt": err,
        "detection_delays": {
            "CLEAR_UPTREND_first_match": delay_summary(delays_up, "delay_first_match_candles"),
            "CLEAR_UPTREND_stable": delay_summary(delays_up, "delay_stable_candles"),
            "CLEAR_DOWNTREND_first_match": delay_summary(delays_dn, "delay_first_match_candles"),
            "CLEAR_DOWNTREND_stable": delay_summary(delays_dn, "delay_stable_candles"),
        },
        "countertrend_exposure": ct,
        "sideways_false_trends": sw_false,
        "ping_pong": {
            "n_events_gap_le_6": len(ping),
            "top_patterns": ping_rows[:15],
        },
        "transition_n_pairs": len(transitions),
        "timeline_sha256_prefix": timeline_hash(timeline_csv)[:16],
        "notes": [
            "default_trend_state_config(enabled=False) — SM still steps transitions (enabled unused).",
            "Proposed policy is evaluated only; existing policy_for_state recorded separately.",
            "Outputs under results_trend_robustness_phase_b only; results/ not touched.",
        ],
    }
    _write_json(out_dir / "summary.json", summary)
    write_readme(out_dir, summary)
    return summary


def write_readme(out_dir: Path, summary: dict[str, Any]) -> None:
    sw = summary.get("symbol_window") or {}
    err = summary.get("classification_vs_gt") or {}
    delays = summary.get("detection_delays") or {}
    lines = [
        "# Trend / Regime Robustness — Phase B Results",
        "",
        "Read-only causal audit of the existing trend state machine vs transparent ground truth.",
        "No live wiring. No threshold changes. No writes into `research/regime_scanner/results/`.",
        "",
        "## Window",
        f"- Load/warmup from `{sw.get('load_start')}`",
        f"- Analyze `{sw.get('analyze_start')}` → `{sw.get('analyze_end')}`",
        f"- Bars analyzed: **{sw.get('n_analyze_bars')}**",
        f"- March case: `{MARCH_CASE_START}` → `{MARCH_CASE_END}`",
        "",
        "## Ground truth",
        "- CLEAR_UPTREND / CLEAR_DOWNTREND / CLEAR_SIDEWAYS / AMBIGUOUS",
        "- AMBIGUOUS is never counted as FP/FN",
        "",
        "## Audit-class map",
        "- UPTREND ← strong_bullish, early_bullish",
        "- DOWNTREND ← strong_bearish, early_bearish",
        "- SIDEWAYS ← neutral",
        "- BOTTOMING / TOPPING ← same SM states",
        "- UNCLEAR ← unavailable, warnings, weakenings, other",
        "",
        "## Headline classification (clear GT only)",
        f"- Clear bars: {err.get('n_clear')}; ambiguous excluded: {err.get('n_ambiguous_excluded_from_errors')}",
        f"- Overall clear match rate: {err.get('overall_clear_match_rate')}",
        "",
        "## Detection delays (candles)",
        f"- CLEAR_UPTREND first match: {delays.get('CLEAR_UPTREND_first_match')}",
        f"- CLEAR_DOWNTREND first match: {delays.get('CLEAR_DOWNTREND_first_match')}",
        "",
        "## Files",
        "- `summary.json`, `state_distribution.csv`, `transition_matrix.csv`, `transition_ping_pong.csv`",
        "- `trend_detection_delays.csv`, `countertrend_exposure.csv`, `sideways_false_trends.csv`",
        "- `monthly_summary.csv`, `march_case_study.csv`, `root_cause_findings.csv`",
        "- `state_timeline_5m.csv` (streamed replay)",
        "",
    ]
    (out_dir / "README_results.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_audit(
    *,
    symbol: str = "APTUSDT",
    output_dir: Path | str = DEFAULT_OUT,
    analyze_start: str = ANALYZE_START,
    analyze_end: str = ANALYZE_END,
    max_bars: int | None = None,
    progress_every: int = 2000,
) -> dict[str, Any]:
    out = Path(output_dir)
    forbidden = Path("research/regime_scanner/results").resolve()
    resolved = out.resolve()
    if resolved == forbidden or forbidden in resolved.parents:
        raise ValueError(
            f"Refuse writing into research/regime_scanner/results/: {out}. "
            "Use results_trend_robustness_phase_b instead."
        )
    out.mkdir(parents=True, exist_ok=True)

    _p(f"loading {symbol} …")
    frame = load_analysis_frame(symbol, max_bars=max_bars)
    _p(f"loaded {len(frame)} bars ({_iso(frame['timestamp'].iloc[0])} … {_iso(frame['timestamp'].iloc[-1])})")

    a_start = _ts(analyze_start)
    # analyze_end as last candle open included: keep timestamps up to end-of-day May 31
    a_end = _ts(analyze_end)
    timeline = out / "state_timeline_5m.csv"
    meta = stream_timeline(
        frame,
        analyze_start=a_start,
        analyze_end=a_end,
        out_csv=timeline,
        progress_every=progress_every,
    )
    _p(f"timeline written: {meta}")
    summary = analyze_timeline(timeline, out)
    summary["stream_meta"] = meta
    _write_json(out / "summary.json", summary)
    _p(f"done → {out}")
    return summary


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Phase-B trend/regime robustness audit (read-only)")
    p.add_argument("--symbol", default="APTUSDT")
    p.add_argument("--output-dir", default=str(DEFAULT_OUT))
    p.add_argument("--analyze-start", default=ANALYZE_START)
    p.add_argument("--analyze-end", default=ANALYZE_END)
    p.add_argument("--max-bars", type=int, default=None)
    p.add_argument("--progress-every", type=int, default=2000)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argparser().parse_args(argv)
    run_audit(
        symbol=args.symbol,
        output_dir=Path(args.output_dir),
        analyze_start=args.analyze_start,
        analyze_end=args.analyze_end,
        max_bars=args.max_bars,
        progress_every=args.progress_every,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
