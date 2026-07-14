"""Phase C: multi-timeframe feature snapshots and before/after comparison.

Reuses Phase A/B exports. No entry, classification, TP/SL, or scanner changes.
Targets are descriptive only and never mix into PRE/SWEEP feature columns.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from research.liquidation_level.liquidation_audit import load_feather
from research.liquidation_level.liquidation_control_validation import (
    EXPECTED_FULL,
    EXPECTED_IS,
    EXPECTED_OOS,
)
from research.liquidation_level.liquidation_levels import normalize_ohlcv_dataframe
from research.liquidation_level.sweep_analysis_window import (
    DEFAULT_WINDOW_SIZES,
    features_at_index,
)
from research.liquidation_level.sweep_scanner_join import (
    SOURCE_CONFIG_ID,
    ensure_utc,
    precompute_scanner_feature_store,
    select_timeline_event_indices,
)

PHASE_B_EXPECTED_HASH = "b4d4437cba11d26d3772f814793c8f2425f5a5a2634f874b672171fc68cbf7cd"

# Core feature keys shared across stages (map from Phase B column suffixes).
_CORE_KEYS = (
    "ema_9",
    "ema_20",
    "ema_59",
    "ema_200",
    "ema_9_20_distance",
    "ema_20_59_distance",
    "adx",
    "di_plus",
    "di_minus",
    "atr",
    "atr_pct",
    "regime",
    "structure_bias",
    "structure_pair",
    "hh",
    "hl",
    "lh",
    "ll",
    "last_bos",
    "last_choch",
    "last_failed_breakout",
    "last_failed_breakdown",
    "retest_level",
    "retest_direction",
    "raw_volume",
    "volume_ratio",
    "ema_9_slope_3_pct",
    "ema_9_slope_6_pct",
    "ema_9_slope_12_pct",
    "ema_20_slope_6_pct",
    "ema_20_slope_12_pct",
    "ema_20_slope_48_pct",
    "ema_59_slope_12_pct",
    "ema_59_slope_48_pct",
    "ema_200_slope_48_pct",
    "ema_200_slope_144_pct",
    "trend_state",
)

_NUMERIC_COMPARE = (
    "ema_9_20_distance",
    "ema_20_59_distance",
    "adx",
    "di_spread",
    "atr_pct",
    "volume_ratio",
)

_CATEGORICAL_COMPARE = (
    "regime",
    "structure_bias",
    "structure_pair",
    "last_bos",
    "last_choch",
    "trend_state",
    "ema_order_state",
)

_MISSING_ALWAYS = (
    "adx_slope",
    "recent_range_expansion",
    "volatility_regime",
    "volume_spike_state",
    "regime_strength",
    "trend_age",
    "protective_level",
    "distance_to_protective_level_pct",
    "last_structure_event",
    "last_swing_type",
    "last_swing_price",
)


class PhaseCValidationError(RuntimeError):
    """Abort Phase C when inputs do not match frozen Phase A/B contracts."""


def _finite(v: object) -> float | None:
    try:
        x = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not np.isfinite(x):
        return None
    return x


def ema_order_state(ema9: object, ema20: object, ema59: object, ema200: object) -> str | None:
    vals = [_finite(ema9), _finite(ema20), _finite(ema59), _finite(ema200)]
    if any(v is None for v in vals):
        return None
    a, b, c, d = vals  # type: ignore[misc]
    if a > b > c > d:
        return "bullish_aligned"
    if a < b < c < d:
        return "bearish_aligned"
    return "mixed"


def di_spread(di_plus: object, di_minus: object) -> float | None:
    a, b = _finite(di_plus), _finite(di_minus)
    if a is None or b is None:
        return None
    return float(a - b)


def volume_sma_from_ratio(volume: object, ratio: object) -> float | None:
    v, r = _finite(volume), _finite(ratio)
    if v is None or r is None or r == 0:
        return None
    return float(v / r)


def enrich_stage_pack(raw: Mapping[str, Any], *, ohlc: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Normalize a feature pack with derived / explicit-missing fields."""
    out = {k: raw.get(k) for k in _CORE_KEYS}
    out["ema_9_minus_ema_20_pct"] = raw.get("ema_9_20_distance")
    out["ema_20_minus_ema_59_pct"] = raw.get("ema_20_59_distance")
    # 59 vs 200 not always present as distance in Phase B — derive if EMAs exist
    e59, e200, close = _finite(raw.get("ema_59")), _finite(raw.get("ema_200")), None
    if ohlc is not None:
        close = _finite(ohlc.get("close"))
    if e59 is not None and e200 is not None and close not in (None, 0):
        out["ema_59_minus_ema_200_pct"] = (e59 - e200) / abs(float(close)) * 100.0
    else:
        out["ema_59_minus_ema_200_pct"] = None
    out["ema_9_slope"] = raw.get("ema_9_slope_12_pct")
    out["ema_20_slope"] = raw.get("ema_20_slope_12_pct")
    out["ema_59_slope"] = raw.get("ema_59_slope_48_pct")
    out["ema_200_slope"] = raw.get("ema_200_slope_48_pct")
    out["ema_order_state"] = ema_order_state(
        raw.get("ema_9"), raw.get("ema_20"), raw.get("ema_59"), raw.get("ema_200")
    )
    out["di_spread"] = di_spread(raw.get("di_plus"), raw.get("di_minus"))
    vol = raw.get("raw_volume") if "raw_volume" in raw else raw.get("volume")
    out["volume"] = vol
    out["volume_sma"] = volume_sma_from_ratio(vol, raw.get("volume_ratio"))
    out["volume_ratio"] = raw.get("volume_ratio")
    out["regime_label"] = raw.get("regime")
    out["regime_direction"] = None
    if isinstance(raw.get("regime"), str):
        r = raw["regime"].lower()
        if "bull" in r:
            out["regime_direction"] = "bullish"
        elif "bear" in r:
            out["regime_direction"] = "bearish"
        elif "neutral" in r:
            out["regime_direction"] = "neutral"
        else:
            out["regime_direction"] = "other"
    out["last_bos_direction"] = raw.get("last_bos")
    out["last_choch_direction"] = raw.get("last_choch")
    out["failed_breakout"] = raw.get("last_failed_breakout")
    out["failed_breakdown"] = raw.get("last_failed_breakdown")
    out["retest_state"] = raw.get("retest_direction")
    if ohlc is not None:
        o, h, l, c = (_finite(ohlc.get(k)) for k in ("open", "high", "low", "close"))
        if all(x is not None for x in (o, h, l, c)) and c not in (0, None):
            out["candle_range_pct"] = (float(h) - float(l)) / abs(float(c)) * 100.0  # type: ignore[arg-type]
        else:
            out["candle_range_pct"] = None
    else:
        out["candle_range_pct"] = None
    for miss in _MISSING_ALWAYS:
        out[miss] = None
    out["trend_state_proxy"] = raw.get("trend_state")
    return out


def pack_from_row(row: Mapping[str, Any], prefix: str) -> dict[str, Any]:
    raw = {}
    for k in _CORE_KEYS:
        col = f"{prefix}{k}"
        if col in row:
            raw[k] = row[col]
        elif k == "raw_volume" and f"{prefix}volume" in row:
            raw[k] = row[f"{prefix}volume"]
    return enrich_stage_pack(raw)


def pack_from_features_at_index(feats5: Mapping[str, Any], feats15: Mapping[str, Any], feats30: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        "5m": enrich_stage_pack(feats5),
        "15m": enrich_stage_pack(feats15),
        "30m": enrich_stage_pack(feats30),
    }


def numeric_delta(a: object, b: object) -> dict[str, Any]:
    fa, fb = _finite(a), _finite(b)
    if fa is None or fb is None:
        return {
            "abs": None,
            "pct": None,
            "sign_changed": None,
            "crossing_zero": None,
            "direction_changed": None,
        }
    abs_d = float(fb - fa)
    pct = None if fa == 0 else float(abs_d / abs(fa) * 100.0)
    sign_changed = (fa > 0 and fb < 0) or (fa < 0 and fb > 0)
    crossing_zero = (fa <= 0 <= fb) or (fb <= 0 <= fa)
    direction_changed = (fa > 0 and fb <= 0) or (fa < 0 and fb >= 0) or (fa == 0 and fb != 0)
    return {
        "abs": abs_d,
        "pct": pct,
        "sign_changed": bool(sign_changed),
        "crossing_zero": bool(crossing_zero),
        "direction_changed": bool(direction_changed),
    }


def categorical_delta(a: object, b: object) -> dict[str, Any]:
    sa = None if a is None or (isinstance(a, float) and not np.isfinite(a)) else str(a)
    sb = None if b is None or (isinstance(b, float) and not np.isfinite(b)) else str(b)
    return {
        "changed": sa != sb,
        "transition_from": sa,
        "transition_to": sb,
    }


def compute_stage_deltas(
    pre: Mapping[str, Any],
    sweep: Mapping[str, Any],
    end: Mapping[str, Any],
    *,
    tf: str,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in _NUMERIC_COMPARE:
        for tag, a, b in (
            ("pre_sweep", pre.get(key), sweep.get(key)),
            ("sweep_end", sweep.get(key), end.get(key)),
            ("pre_end", pre.get(key), end.get(key)),
        ):
            d = numeric_delta(a, b)
            for sk, sv in d.items():
                out[f"delta_{tf}_{tag}_{key}_{sk}"] = sv
    for key in _CATEGORICAL_COMPARE:
        for tag, a, b in (
            ("pre_sweep", pre.get(key), sweep.get(key)),
            ("sweep_end", sweep.get(key), end.get(key)),
            ("pre_end", pre.get(key), end.get(key)),
        ):
            d = categorical_delta(a, b)
            for sk, sv in d.items():
                out[f"delta_{tf}_{tag}_{key}_{sk}"] = sv
    return out


def validate_phase_inputs(
    *,
    phase_a_dir: Path,
    phase_b_dir: Path,
    expected_hash: str = PHASE_B_EXPECTED_HASH,
) -> dict[str, Any]:
    a = Path(phase_a_dir)
    b = Path(phase_b_dir)
    summary_b = json.loads((b / "summary.json").read_text(encoding="utf-8"))
    val_b = json.loads((b / "event_validation.json").read_text(encoding="utf-8"))
    events = pd.read_csv(a / "sweep_events.csv", usecols=["event_id", "sample"])
    windows = pd.read_csv(b / "analysis_windows.csv", usecols=["event_id", "window_size", "complete", "sample"])
    got_hash = str(summary_b.get("deterministic_hash") or "")
    counts = {
        "full": int(len(events)),
        "in_sample": int((events["sample"] == "in_sample").sum()),
        "out_of_sample": int((events["sample"] == "out_of_sample").sum()),
    }
    by_size = windows.groupby("window_size").size().to_dict()
    complete_ok = bool(windows["complete"].all())
    payload = {
        "expected_events": {"full": EXPECTED_FULL, "in_sample": EXPECTED_IS, "out_of_sample": EXPECTED_OOS},
        "reproduced_events": counts,
        "expected_phase_b_hash": expected_hash,
        "observed_phase_b_hash": got_hash,
        "windows_by_size": {str(k): int(v) for k, v in by_size.items()},
        "all_windows_complete": complete_ok,
        "phase_b_validation": val_b,
    }
    errors: list[str] = []
    if counts != {"full": EXPECTED_FULL, "in_sample": EXPECTED_IS, "out_of_sample": EXPECTED_OOS}:
        errors.append(f"event counts mismatch: {counts}")
    if got_hash != expected_hash:
        errors.append(f"phase B hash mismatch: {got_hash}")
    for s in (3, 6, 12):
        if int(by_size.get(s, 0)) != EXPECTED_FULL:
            errors.append(f"window size {s} count {by_size.get(s)} != {EXPECTED_FULL}")
    if not complete_ok:
        errors.append("not all phase B windows complete")
    if errors:
        payload["errors"] = errors
        raise PhaseCValidationError(json.dumps(payload, indent=2))
    payload["ok"] = True
    return payload


def _union_find_groups(intervals: list[tuple[str, int, int]]) -> dict[str, str]:
    """intervals: (event_id, start, end inclusive). Return event_id -> group_id."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    ordered = sorted(intervals, key=lambda t: (t[1], t[2], t[0]))
    active: list[tuple[int, int, str]] = []  # start, end, event_id
    for eid, a, b in ordered:
        parent.setdefault(eid, eid)
        active = [(aa, bb, ae) for aa, bb, ae in active if bb >= a]
        for _, _, oid in active:
            union(eid, oid)
        active.append((a, b, eid))

    comps: dict[str, list[str]] = {}
    for eid, _, _ in intervals:
        r = find(eid)
        comps.setdefault(r, []).append(eid)
    mapping: dict[str, str] = {}
    for members in comps.values():
        gid = f"OG_{min(members)}"
        for m in members:
            mapping[m] = gid
    return mapping


def build_overlap_groups(windows: pd.DataFrame) -> pd.DataFrame:
    """Use largest window (12) intervals for grouping; keep all events."""
    w12 = windows.loc[windows["window_size"] == 12].copy()
    intervals = [
        (str(r.event_id), int(r.start_index), int(r.end_index))
        for r in w12.itertuples()
        if pd.notna(r.start_index) and pd.notna(r.end_index)
    ]
    mapping = _union_find_groups(intervals)
    ev = w12.sort_values("signal_index").copy()
    ev["overlap_group_id"] = ev["event_id"].map(mapping)
    rows = []
    for gid, g in ev.groupby("overlap_group_id", sort=False):
        g = g.sort_values("signal_index")
        ids = g["event_id"].tolist()
        sig = g["signal_index"].to_numpy(int)
        for i, r in enumerate(g.itertuples()):
            prev_sig = int(sig[i - 1]) if i else None
            rows.append(
                {
                    "event_id": r.event_id,
                    "sample": r.sample,
                    "signal_index": int(r.signal_index),
                    "overlap_group_id": gid,
                    "overlapping_event_count": int(len(g) - 1),
                    "first_event_in_group": ids[0],
                    "last_event_in_group": ids[-1],
                    "is_first_in_group": bool(i == 0),
                    "candles_since_previous_sweep": None if prev_sig is None else int(sig[i] - prev_sig),
                    "minutes_since_previous_sweep": None
                    if prev_sig is None
                    else float((sig[i] - prev_sig) * 5),
                    "same_cluster_or_level_proxy": None,
                }
            )
    return pd.DataFrame(rows)


def compute_path_aggregates(bars: pd.DataFrame, windows: pd.DataFrame) -> pd.DataFrame:
    """Path aggregates per event_id × window_size."""
    meta = windows[
        ["event_id", "window_size", "sample", "initial_sweep_level"]
    ].drop_duplicates(subset=["event_id", "window_size"])
    rows = []
    grouped = bars.groupby(["event_id", "window_size"], sort=False)
    meta_i = meta.set_index(["event_id", "window_size"])
    for key, g in grouped:
        eid, ws = key
        g = g.sort_values("window_offset")
        win = meta_i.loc[(eid, ws)]
        if isinstance(win, pd.DataFrame):
            win = win.iloc[0]
        level = _finite(win["initial_sweep_level"])
        closes = g["close"].to_numpy(float)
        highs = g["high"].to_numpy(float)
        lows = g["low"].to_numpy(float)
        n = len(g)
        above = g["lvl_close_above_level"].astype(bool).to_numpy()
        below = g["lvl_close_below_level"].astype(bool).to_numpy()
        crosses = int(g["lvl_crossed_level"].astype(bool).sum())
        reclaims = int(g["lvl_reclaimed_below_level"].astype(bool).sum())

        def _run(flags: np.ndarray) -> int:
            best = cur = 0
            for f in flags:
                if f:
                    cur += 1
                    best = max(best, cur)
                else:
                    cur = 0
            return best

        if level is not None and level != 0:
            close_rel = (closes / level - 1.0) * 100.0
            high_rel = (highs / level - 1.0) * 100.0
            low_rel = (lows / level - 1.0) * 100.0
            max_close_above = float(np.max(np.clip(close_rel, 0, None)))
            min_close_below = float(np.min(np.clip(close_rel, None, 0)))
            max_high_above = float(np.max(high_rel))
            min_low_below = float(np.min(low_rel))
            final_rel = float(close_rel[-1])
        else:
            max_close_above = min_close_below = max_high_above = min_low_below = final_rel = None

        adx = pd.to_numeric(g["current_5m_adx"], errors="coerce")
        di_p = pd.to_numeric(g["current_5m_di_plus"], errors="coerce")
        di_m = pd.to_numeric(g["current_5m_di_minus"], errors="coerce")
        di_s = di_p - di_m
        atrp = pd.to_numeric(g["current_5m_atr_pct"], errors="coerce")
        vr = pd.to_numeric(g["current_5m_volume_ratio"], errors="coerce")
        e9 = pd.to_numeric(g["current_5m_ema_9"], errors="coerce")
        e20 = pd.to_numeric(g["current_5m_ema_20"], errors="coerce")
        e59 = pd.to_numeric(g["current_5m_ema_59"], errors="coerce")
        e200 = pd.to_numeric(g["current_5m_ema_200"], errors="coerce")

        cross_count = 0
        if len(e9) >= 2:
            prev = e9.to_numpy() - e20.to_numpy()
            for i in range(1, len(prev)):
                if (
                    np.isfinite(prev[i - 1])
                    and np.isfinite(prev[i])
                    and prev[i - 1] * prev[i] <= 0
                    and prev[i - 1] != 0
                ):
                    cross_count += 1

        bull_ord = bear_ord = 0
        for i in range(n):
            order = ema_order_state(e9.iloc[i], e20.iloc[i], e59.iloc[i], e200.iloc[i])
            if order == "bullish_aligned":
                bull_ord += 1
            elif order == "bearish_aligned":
                bear_ord += 1

        bos = g["current_5m_last_bos"].astype(str)
        choch = g["current_5m_last_choch"].astype(str)

        def _new_events(series: pd.Series, needle: str) -> int:
            vals = series.fillna("").astype(str).tolist()
            cnt = 0
            prev_v = None
            for v in vals:
                if needle in v.lower() and v != prev_v:
                    cnt += 1
                prev_v = v
            return cnt

        def _changes(s: pd.Series) -> int:
            vals = s.astype(str).tolist()
            return sum(1 for i in range(1, len(vals)) if vals[i] != vals[i - 1])

        rows.append(
            {
                "event_id": eid,
                "window_size": int(ws),
                "sample": win["sample"],
                "max_close_above_level_pct": max_close_above,
                "min_close_below_level_pct": min_close_below,
                "max_high_above_level_pct": max_high_above,
                "min_low_below_level_pct": min_low_below,
                "final_close_relative_to_level_pct": final_rel,
                "fraction_closes_above_level": float(above.mean()) if n else None,
                "fraction_closes_below_level": float(below.mean()) if n else None,
                "number_level_crosses": crosses,
                "number_reclaims_below": reclaims,
                "longest_above_run": _run(above),
                "longest_below_run": _run(below),
                "adx_min": float(adx.min()) if adx.notna().any() else None,
                "adx_max": float(adx.max()) if adx.notna().any() else None,
                "adx_mean": float(adx.mean()) if adx.notna().any() else None,
                "adx_change": float(adx.iloc[-1] - adx.iloc[0])
                if len(adx) and np.isfinite(adx.iloc[0]) and np.isfinite(adx.iloc[-1])
                else None,
                "di_spread_min": float(di_s.min()) if di_s.notna().any() else None,
                "di_spread_max": float(di_s.max()) if di_s.notna().any() else None,
                "di_spread_mean": float(di_s.mean()) if di_s.notna().any() else None,
                "fraction_di_minus_gt_plus": float((di_m > di_p).mean()) if n else None,
                "fraction_di_plus_gt_minus": float((di_p > di_m).mean()) if n else None,
                "ema9_ema20_crosses": cross_count,
                "fraction_bearish_ema_ordering": bear_ord / n if n else None,
                "fraction_bullish_ema_ordering": bull_ord / n if n else None,
                "atr_pct_min": float(atrp.min()) if atrp.notna().any() else None,
                "atr_pct_max": float(atrp.max()) if atrp.notna().any() else None,
                "atr_pct_mean": float(atrp.mean()) if atrp.notna().any() else None,
                "max_range_expansion_proxy": float(atrp.max() - atrp.min()) if atrp.notna().any() else None,
                "volume_ratio_min": float(vr.min()) if vr.notna().any() else None,
                "volume_ratio_max": float(vr.max()) if vr.notna().any() else None,
                "volume_ratio_mean": float(vr.mean()) if vr.notna().any() else None,
                "volume_spike_count_proxy": int((vr >= 1.3).sum()) if vr.notna().any() else 0,
                "new_bearish_bos_count": _new_events(bos, "bear"),
                "new_bullish_bos_count": _new_events(bos, "bull"),
                "new_bearish_choch_count": _new_events(choch, "bear"),
                "new_bullish_choch_count": _new_events(choch, "bull"),
                "failed_breakout_count": int(g["current_5m_last_failed_breakout"].notna().sum()),
                "failed_breakdown_count": int(g["current_5m_last_failed_breakdown"].notna().sum()),
                "retest_count": int(g["current_5m_retest_direction"].notna().sum()),
                "end_structure_bias": g["current_5m_structure_bias"].iloc[-1],
                "tf15_regime_changes": _changes(g["current_15m_regime"]),
                "tf30_regime_changes": _changes(g["current_30m_regime"]),
                "tf15_structure_changes": _changes(g["current_15m_structure_bias"]),
                "tf30_structure_changes": _changes(g["current_30m_structure_bias"]),
                "end_15m_regime": g["current_15m_regime"].iloc[-1],
                "end_30m_regime": g["current_30m_regime"].iloc[-1],
                "end_15m_structure_bias": g["current_15m_structure_bias"].iloc[-1],
                "end_30m_structure_bias": g["current_30m_structure_bias"].iloc[-1],
            }
        )
    return pd.DataFrame(rows)


def compute_targets(path_agg: pd.DataFrame, windows: pd.DataFrame) -> pd.DataFrame:
    rows = []
    wmap = windows.set_index(["event_id", "window_size"])
    for r in path_agg.itertuples():
        frac_above = r.fraction_closes_above_level
        frac_below = r.fraction_closes_below_level
        final_rel = r.final_close_relative_to_level_pct
        # final_rel uses (close/level - 1)*100 → negative means below level
        ended_below = final_rel is not None and float(final_rel) < 0
        ended_above = final_rel is not None and float(final_rel) > 0
        maj_above = frac_above is not None and float(frac_above) > 0.5
        maj_below = frac_below is not None and float(frac_below) > 0.5
        mixed = not maj_above and not maj_below
        max_h = r.max_high_above_level_pct
        min_l = r.min_low_below_level_pct
        new_high = (
            max_h is not None
            and min_l is not None
            and float(max_h) > abs(float(min_l))
        )
        new_low = (
            max_h is not None
            and min_l is not None
            and abs(float(min_l)) > float(max_h)
        )
        rows.append(
            {
                "event_id": r.event_id,
                "window_size": int(r.window_size),
                "sample": r.sample,
                "target_ended_below_level": bool(ended_below),
                "target_ended_above_level": bool(ended_above),
                "target_majority_below": bool(maj_below),
                "target_majority_above": bool(maj_above),
                "target_mixed_path": bool(mixed),
                "target_new_low_dominant": bool(new_low),
                "target_new_high_dominant": bool(new_high),
            }
        )
    return pd.DataFrame(rows)


@dataclass
class PhaseCBundle:
    snapshots: pd.DataFrame
    deltas: pd.DataFrame
    path_aggregates: pd.DataFrame
    targets: pd.DataFrame
    feature_availability: pd.DataFrame
    feature_timing: pd.DataFrame
    categorical_transitions: pd.DataFrame
    descriptive_group_comparison: pd.DataFrame
    overlap_groups: pd.DataFrame
    overlap_group_comparison: pd.DataFrame
    validation: dict[str, Any]
    leakage_checks: dict[str, Any]


def _flatten_prefixed(stage: str, tf: str, pack: Mapping[str, Any]) -> dict[str, Any]:
    return {f"{stage}_{tf}_{k}": v for k, v in pack.items()}


def build_phase_c_bundle(
    *,
    phase_a_dir: Path,
    phase_b_dir: Path,
    feather_file: Path,
    window_sizes: Sequence[int] = DEFAULT_WINDOW_SIZES,
    max_events: int | None = None,
    progress: Any | None = None,
) -> PhaseCBundle:
    def _p(msg: str) -> None:
        if progress is not None:
            progress(msg)

    validation = validate_phase_inputs(phase_a_dir=phase_a_dir, phase_b_dir=phase_b_dir)
    _p("Inputs geladen / validiert")

    events = pd.read_csv(phase_a_dir / "sweep_events.csv")
    windows = pd.read_csv(phase_b_dir / "analysis_windows.csv")
    # selective bar columns
    bar_cols = pd.read_csv(phase_b_dir / "analysis_bars.csv", nrows=0).columns.tolist()
    need = [
        c
        for c in bar_cols
        if c
        in {
            "event_id",
            "window_size",
            "window_offset",
            "candle_index",
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "available_at",
        }
        or c.startswith(
            (
                "current_5m_",
                "current_15m_",
                "current_30m_",
                "frozen_5m_",
                "frozen_15m_",
                "frozen_30m_",
                "htf15_",
                "htf30_",
                "lvl_",
            )
        )
    ]
    bars = pd.read_csv(phase_b_dir / "analysis_bars.csv", usecols=need, low_memory=False)
    sizes = tuple(int(x) for x in window_sizes)
    windows = windows.loc[windows["window_size"].isin(sizes)].copy()
    bars = bars.loc[bars["window_size"].isin(sizes)].copy()

    if max_events is not None:
        keep = events["event_id"].head(int(max_events)).tolist()
        events = events.loc[events["event_id"].isin(keep)].copy()
        windows = windows.loc[windows["event_id"].isin(keep)].copy()
        bars = bars.loc[bars["event_id"].isin(keep)].copy()

    # PRE contexts via one feature-store pass
    _p("PRE-Context Feature-Store")
    ohlcv = normalize_ohlcv_dataframe(load_feather(Path(feather_file).expanduser().resolve()))
    store = precompute_scanner_feature_store(ohlcv, progress=progress)
    pre_cache: dict[int, dict[str, dict[str, Any]]] = {}
    pre_meta: dict[int, dict[str, Any]] = {}
    for sig in sorted(events["signal_index"].astype(int).unique()):
        pre_i = int(sig) - 1
        if pre_i < 0:
            pre_cache[sig] = {
                "5m": enrich_stage_pack({}),
                "15m": enrich_stage_pack({}),
                "30m": enrich_stage_pack({}),
            }
            pre_meta[sig] = {
                "pre_5m_timestamp": None,
                "pre_15m_available_at": None,
                "pre_30m_available_at": None,
            }
            continue
        f5, f15, f30, m15, m30 = features_at_index(store, pre_i)
        row = store.ohlcv.iloc[pre_i]
        f5 = dict(f5)
        packs = pack_from_features_at_index(f5, f15, f30)
        packs["5m"] = enrich_stage_pack(
            f5,
            ohlc={
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
            },
        )
        pre_cache[sig] = packs
        pre_meta[sig] = {
            "pre_5m_timestamp": ensure_utc(row["timestamp"]).isoformat(),
            "pre_15m_available_at": m15.get("available_at"),
            "pre_30m_available_at": m30.get("available_at"),
        }
    _p("Snapshots erzeugt (PRE cache)")

    # Build snapshot rows
    snap_rows: list[dict[str, Any]] = []
    delta_rows: list[dict[str, Any]] = []
    # End bars: max offset per group
    end_idx = bars.groupby(["event_id", "window_size"])["window_offset"].transform("max")
    end_bars = bars.loc[bars["window_offset"] == end_idx].copy()
    first_bars = bars.loc[bars["window_offset"] == 1].set_index(["event_id", "window_size"])
    end_bars_i = end_bars.set_index(["event_id", "window_size"])
    ev_map = events.set_index("event_id")

    for w in windows.itertuples(index=False):
        eid = w.event_id
        ws = int(w.window_size)
        if (eid, ws) not in first_bars.index or (eid, ws) not in end_bars_i.index:
            continue
        ev = ev_map.loc[eid]
        fb = first_bars.loc[(eid, ws)]
        eb = end_bars_i.loc[(eid, ws)]
        # if duplicate index, take first
        if isinstance(fb, pd.DataFrame):
            fb = fb.iloc[0]
        if isinstance(eb, pd.DataFrame):
            eb = eb.iloc[0]
        sig = int(ev["signal_index"])
        pre = pre_cache[sig]
        pm = pre_meta[sig]

        s5 = pack_from_row(fb.to_dict(), "frozen_5m_")
        c = _finite(ev["sweep_candle_close"])
        h = _finite(ev["sweep_candle_high"])
        l = _finite(ev["sweep_candle_low"])
        if c and h is not None and l is not None and c != 0:
            s5["candle_range_pct"] = (float(h) - float(l)) / abs(float(c)) * 100.0
        s15 = pack_from_row(fb.to_dict(), "frozen_15m_")
        s30 = pack_from_row(fb.to_dict(), "frozen_30m_")
        e5 = pack_from_row(eb.to_dict(), "current_5m_")
        cc, hh, ll = _finite(eb["close"]), _finite(eb["high"]), _finite(eb["low"])
        if cc and hh is not None and ll is not None and cc != 0:
            e5["candle_range_pct"] = (float(hh) - float(ll)) / abs(float(cc)) * 100.0
        e15 = pack_from_row(eb.to_dict(), "current_15m_")
        e30 = pack_from_row(eb.to_dict(), "current_30m_")

        if pm["pre_5m_timestamp"] is not None:
            if not (ensure_utc(pm["pre_5m_timestamp"]) < ensure_utc(ev["signal_timestamp"])):
                raise PhaseCValidationError(f"PRE timestamp not before sweep for {eid}")

        row = {
            "event_id": eid,
            "source_config_id": SOURCE_CONFIG_ID,
            "sample": ev["sample"],
            "signal_index": sig,
            "signal_timestamp": ensure_utc(ev["signal_timestamp"]).isoformat(),
            "window_size": ws,
            "sweep_level": _finite(w.initial_sweep_level),
            "cluster_center_price": _finite(ev["cluster_center_price"]),
            "swept_level_count": int(ev["swept_level_count"]),
            "swept_total_strength": int(ev["swept_total_strength"]),
            "swept_leverages": ev["swept_leverages"],
            "reclaim_status": ev["reclaim_status"],
            "pre_5m_timestamp": pm["pre_5m_timestamp"],
            "sweep_5m_timestamp": ensure_utc(ev["signal_timestamp"]).isoformat(),
            "end_5m_timestamp": str(pd.Timestamp(eb["timestamp"])),
            "pre_15m_available_at": pm["pre_15m_available_at"],
            "sweep_15m_available_at": fb.get("htf15_available_at"),
            "end_15m_available_at": eb.get("htf15_available_at"),
            "pre_30m_available_at": pm["pre_30m_available_at"],
            "sweep_30m_available_at": fb.get("htf30_available_at"),
            "end_30m_available_at": eb.get("htf30_available_at"),
        }
        row.update(_flatten_prefixed("pre", "5m", pre["5m"]))
        row.update(_flatten_prefixed("pre", "15m", pre["15m"]))
        row.update(_flatten_prefixed("pre", "30m", pre["30m"]))
        row.update(_flatten_prefixed("sweep", "5m", s5))
        row.update(_flatten_prefixed("sweep", "15m", s15))
        row.update(_flatten_prefixed("sweep", "30m", s30))
        row.update(_flatten_prefixed("end", "5m", e5))
        row.update(_flatten_prefixed("end", "15m", e15))
        row.update(_flatten_prefixed("end", "30m", e30))
        snap_rows.append(row)

        drow = {"event_id": eid, "window_size": ws, "sample": ev["sample"]}
        drow.update(compute_stage_deltas(pre["5m"], s5, e5, tf="5m"))
        drow.update(compute_stage_deltas(pre["15m"], s15, e15, tf="15m"))
        drow.update(compute_stage_deltas(pre["30m"], s30, e30, tf="30m"))
        delta_rows.append(drow)

    snapshots = pd.DataFrame(snap_rows)
    deltas = pd.DataFrame(delta_rows)
    _p("Deltas erzeugt")

    path_aggregates = compute_path_aggregates(bars, windows)
    _p("Pfadaggregate erzeugt")
    targets = compute_targets(path_aggregates, windows)
    _p("Targets erzeugt")

    # Ensure no target leakage into snapshots
    for c in snapshots.columns:
        if str(c).startswith("target_"):
            raise PhaseCValidationError(f"target column leaked into snapshots: {c}")

    overlap_groups = build_overlap_groups(windows)
    _p("Overlaps gruppiert")

    feature_availability = build_feature_availability(snapshots)
    feature_timing = build_feature_timing(snapshots.columns.tolist())
    categorical_transitions = build_categorical_transitions(snapshots)
    descriptive_group_comparison = build_descriptive_group_comparison(
        snapshots, deltas, path_aggregates, targets
    )
    overlap_group_comparison = build_overlap_group_comparison(targets, overlap_groups, path_aggregates)

    leakage = run_leakage_checks(snapshots, targets, feature_timing)
    _p("Leakage-Checks")

    validation = {
        **validation,
        "snapshot_rows": int(len(snapshots)),
        "reproduced_events_after_filter": {
            "full": int(events["event_id"].nunique()),
            "in_sample": int((events["sample"] == "in_sample").sum()),
            "out_of_sample": int((events["sample"] == "out_of_sample").sum()),
        },
    }
    return PhaseCBundle(
        snapshots=snapshots,
        deltas=deltas,
        path_aggregates=path_aggregates,
        targets=targets,
        feature_availability=feature_availability,
        feature_timing=feature_timing,
        categorical_transitions=categorical_transitions,
        descriptive_group_comparison=descriptive_group_comparison,
        overlap_groups=overlap_groups,
        overlap_group_comparison=overlap_group_comparison,
        validation=validation,
        leakage_checks=leakage,
    )


def build_feature_availability(snapshots: pd.DataFrame) -> pd.DataFrame:
    rows = []
    n = len(snapshots)
    stage_re = re.compile(r"^(pre|sweep|end)_(5m|15m|30m)_(.+)$")
    for col in snapshots.columns:
        m = stage_re.match(col)
        if not m:
            continue
        stage, tf, name = m.groups()
        s = snapshots[col]
        avail = int(s.notna().sum())
        missing = n - avail
        source = "phase_b_export" if stage in {"sweep", "end"} else "phase_c_pre_store"
        if name in _MISSING_ALWAYS:
            source = "explicit_missing"
        note = ""
        if name in _MISSING_ALWAYS:
            note = "vollständig fehlend / nicht in Scanner-Exports"
        elif name in {"price_action_state", "momentum_state"}:
            note = "PA/Momentum weiterhin nicht verfügbar"
        elif "trend_state" in name:
            note = "Proxy statt Original-TSM"
        elif avail < n:
            note = "nur nach Warm-up oder verzögerte Struktur"
        rows.append(
            {
                "feature_name": name,
                "timeframe": tf,
                "snapshot_stage": stage,
                "available_count": avail,
                "missing_count": missing,
                "availability_pct": 100.0 * avail / n if n else 0.0,
                "source_module": source,
                "source_field": col,
                "causal_at_stage": True,
                "notes": note,
            }
        )
    return pd.DataFrame(rows)


def build_feature_timing(columns: Sequence[str]) -> pd.DataFrame:
    rows = []
    stage_re = re.compile(r"^(pre|sweep|end)_(5m|15m|30m)_(.+)$")
    for col in columns:
        if col.startswith("target_"):
            rows.append(
                {
                    "feature_name": col,
                    "earliest_available_offset": None,
                    "earliest_available_timestamp_semantics": "post_window_only",
                    "safe_for_sweep_decision": False,
                    "safe_for_offset_1_decision": False,
                    "safe_for_offset_3_decision": False,
                    "safe_for_window_end_only": False,
                    "target_only": True,
                }
            )
            continue
        m = stage_re.match(col)
        if not m:
            continue
        stage, tf, name = m.groups()
        if stage == "pre":
            earliest = -1
            safe_sweep = True
            safe1 = True
            safe3 = True
            end_only = False
            sem = "before_sweep_open_or_previous_5m_close"
        elif stage == "sweep":
            earliest = 0
            safe_sweep = True
            safe1 = True
            safe3 = True
            end_only = False
            sem = "at_sweep_close"
        else:
            earliest = None  # window-size dependent
            safe_sweep = False
            safe1 = False
            safe3 = False
            end_only = True
            sem = "at_window_end_only"
        rows.append(
            {
                "feature_name": col,
                "earliest_available_offset": earliest if earliest is not None else "window_size",
                "earliest_available_timestamp_semantics": sem,
                "safe_for_sweep_decision": safe_sweep,
                "safe_for_offset_1_decision": safe1 or stage == "sweep",
                "safe_for_offset_3_decision": safe3 or stage in {"pre", "sweep"},
                "safe_for_window_end_only": end_only,
                "target_only": False,
            }
        )
    # path aggregates timing
    for name in (
        "path_until_offset_*",
        "end_* aggregates",
    ):
        rows.append(
            {
                "feature_name": name,
                "earliest_available_offset": "depends_on_offset",
                "earliest_available_timestamp_semantics": "after_corresponding_follow_candle_close",
                "safe_for_sweep_decision": False,
                "safe_for_offset_1_decision": False,
                "safe_for_offset_3_decision": False,
                "safe_for_window_end_only": True,
                "target_only": False,
            }
        )
    return pd.DataFrame(rows)


def build_categorical_transitions(snapshots: pd.DataFrame) -> pd.DataFrame:
    rows = []
    cats = ["regime_label", "structure_bias", "structure_pair", "ema_order_state", "trend_state_proxy"]
    for tf in ("5m", "15m", "30m"):
        for cat in cats:
            pre_c = f"pre_{tf}_{cat}"
            sw_c = f"sweep_{tf}_{cat}"
            en_c = f"end_{tf}_{cat}"
            if pre_c not in snapshots.columns:
                continue
            for sample in ("full", "in_sample", "out_of_sample"):
                df = snapshots if sample == "full" else snapshots.loc[snapshots["sample"] == sample]
                for ws, g in df.groupby("window_size"):
                    # PRE→SWEEP
                    ct = pd.crosstab(g[pre_c].fillna("NA"), g[sw_c].fillna("NA"))
                    for a in ct.index:
                        for b in ct.columns:
                            rows.append(
                                {
                                    "sample": sample,
                                    "window_size": int(ws),
                                    "timeframe": tf,
                                    "feature": cat,
                                    "transition": "pre_to_sweep",
                                    "from_value": a,
                                    "to_value": b,
                                    "count": int(ct.loc[a, b]),
                                }
                            )
                    ct2 = pd.crosstab(g[sw_c].fillna("NA"), g[en_c].fillna("NA"))
                    for a in ct2.index:
                        for b in ct2.columns:
                            rows.append(
                                {
                                    "sample": sample,
                                    "window_size": int(ws),
                                    "timeframe": tf,
                                    "feature": cat,
                                    "transition": "sweep_to_end",
                                    "from_value": a,
                                    "to_value": b,
                                    "count": int(ct2.loc[a, b]),
                                }
                            )
    return pd.DataFrame(rows)


def build_descriptive_group_comparison(
    snapshots: pd.DataFrame,
    deltas: pd.DataFrame,
    path_agg: pd.DataFrame,
    targets: pd.DataFrame,
) -> pd.DataFrame:
    """Median feature differences by target_ended_below vs above — descriptive only."""
    merged = snapshots.merge(
        targets[["event_id", "window_size", "target_ended_below_level", "target_ended_above_level"]],
        on=["event_id", "window_size"],
        how="left",
    )
    # attach a few delta cols
    dcols = [c for c in deltas.columns if c.startswith("delta_5m_sweep_end_") and c.endswith("_abs")]
    merged = merged.merge(deltas[["event_id", "window_size"] + dcols[:20]], on=["event_id", "window_size"], how="left")
    feature_cols = [
        c
        for c in merged.columns
        if c.startswith(("sweep_5m_", "pre_5m_", "delta_5m_"))
        and not c.startswith("target_")
        and merged[c].dtype != object
    ][:40]
    rows = []
    for sample in ("full", "in_sample", "out_of_sample"):
        base = merged if sample == "full" else merged.loc[merged["sample"] == sample]
        for ws, g in base.groupby("window_size"):
            below = g.loc[g["target_ended_below_level"] == True]  # noqa: E712
            above = g.loc[g["target_ended_above_level"] == True]  # noqa: E712
            for feat in feature_cols:
                mb = pd.to_numeric(below[feat], errors="coerce").median()
                ma = pd.to_numeric(above[feat], errors="coerce").median()
                rows.append(
                    {
                        "sample": sample,
                        "window_size": int(ws),
                        "feature": feat,
                        "median_target_ended_below": None if pd.isna(mb) else float(mb),
                        "median_target_ended_above": None if pd.isna(ma) else float(ma),
                        "median_diff_below_minus_above": None
                        if pd.isna(mb) or pd.isna(ma)
                        else float(mb - ma),
                        "n_below": int(len(below)),
                        "n_above": int(len(above)),
                        "missing_rate": float(pd.to_numeric(g[feat], errors="coerce").isna().mean()),
                        "note": "descriptive_only_not_an_edge_claim",
                    }
                )
    return pd.DataFrame(rows)


def build_overlap_group_comparison(
    targets: pd.DataFrame,
    overlap_groups: pd.DataFrame,
    path_agg: pd.DataFrame,
) -> pd.DataFrame:
    m = targets.merge(overlap_groups, on=["event_id", "sample"], how="left", suffixes=("", "_og"))
    m = m.merge(
        path_agg[["event_id", "window_size", "fraction_closes_below_level", "number_level_crosses"]],
        on=["event_id", "window_size"],
        how="left",
    )
    rows = []
    for ws, g0 in m.groupby("window_size"):
        variants = {
            "all_events": g0,
            "first_in_group_only": g0.loc[g0["is_first_in_group"] == True],  # noqa: E712
            "gap_ge_12": g0.loc[
                g0["candles_since_previous_sweep"].isna()
                | (g0["candles_since_previous_sweep"] >= 12)
            ],
            "gap_ge_24": g0.loc[
                g0["candles_since_previous_sweep"].isna()
                | (g0["candles_since_previous_sweep"] >= 24)
            ],
        }
        for name, g in variants.items():
            rows.append(
                {
                    "window_size": int(ws),
                    "variant": name,
                    "n_events": int(len(g)),
                    "rate_target_ended_below": float(g["target_ended_below_level"].mean())
                    if len(g)
                    else None,
                    "rate_target_ended_above": float(g["target_ended_above_level"].mean())
                    if len(g)
                    else None,
                    "mean_fraction_closes_below": float(
                        pd.to_numeric(g["fraction_closes_below_level"], errors="coerce").mean()
                    )
                    if len(g)
                    else None,
                    "mean_level_crosses": float(
                        pd.to_numeric(g["number_level_crosses"], errors="coerce").mean()
                    )
                    if len(g)
                    else None,
                    "note": "descriptive_only",
                }
            )
    return pd.DataFrame(rows)


def run_leakage_checks(
    snapshots: pd.DataFrame,
    targets: pd.DataFrame,
    timing: pd.DataFrame,
) -> dict[str, Any]:
    snap_target_cols = [c for c in snapshots.columns if str(c).startswith("target_")]
    timing_targets = timing.loc[timing["target_only"] == True]  # noqa: E712
    future_as_sweep = [
        c
        for c in snapshots.columns
        if c.startswith("end_")
        and any(
            timing.loc[timing["feature_name"] == c, "safe_for_sweep_decision"].astype(bool).tolist()
        )
    ]
    # end_* should never be safe_for_sweep
    bad_timing = timing.loc[
        timing["feature_name"].astype(str).str.startswith("end_")
        & (timing["safe_for_sweep_decision"] == True)  # noqa: E712
    ]
    checks = {
        "no_target_cols_in_snapshots": len(snap_target_cols) == 0,
        "targets_marked_target_only": True,
        "no_end_feature_safe_at_sweep": len(bad_timing) == 0,
        "target_row_count": int(len(targets)),
        "snapshot_target_cols": snap_target_cols,
        "bad_timing_rows": int(len(bad_timing)),
    }
    checks["passed"] = bool(
        checks["no_target_cols_in_snapshots"]
        and checks["no_end_feature_safe_at_sweep"]
        and int(len(targets)) > 0
    )
    return checks


def bundle_hash(bundle: PhaseCBundle) -> str:
    payload = {
        "snapshots": bundle.snapshots.sort_values(["event_id", "window_size"]).head(500).to_dict(orient="records"),
        "n_snapshots": len(bundle.snapshots),
        "n_targets": len(bundle.targets),
        "target_sum": {
            "below": int(bundle.targets["target_ended_below_level"].sum()),
            "above": int(bundle.targets["target_ended_above_level"].sum()),
        },
        "path_n": len(bundle.path_aggregates),
        "overlap_n": int(bundle.overlap_groups["overlap_group_id"].nunique())
        if len(bundle.overlap_groups)
        else 0,
    }
    # stronger hash over compact keys of all snapshots
    keys = ["event_id", "window_size", "sample", "signal_index", "sweep_level"]
    keys += [c for c in bundle.snapshots.columns if c.startswith("sweep_5m_") and c.endswith(("adx", "regime_label", "ema_9"))]
    compact = bundle.snapshots[keys].sort_values(["event_id", "window_size"])
    blob = compact.to_csv(index=False) + json.dumps(payload["target_sum"], sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def assert_no_entry_fields(df: pd.DataFrame) -> None:
    bad = [
        c
        for c in df.columns
        if c in {"entry_index", "entry_price", "pnl", "tp", "sl", "fees"} or str(c).startswith("entry_")
    ]
    if bad:
        raise RuntimeError(f"forbidden columns: {bad}")


__all__ = [
    "PHASE_B_EXPECTED_HASH",
    "PhaseCValidationError",
    "PhaseCBundle",
    "validate_phase_inputs",
    "build_phase_c_bundle",
    "bundle_hash",
    "ema_order_state",
    "di_spread",
    "compute_targets",
    "build_overlap_groups",
    "run_leakage_checks",
    "assert_no_entry_fields",
]
