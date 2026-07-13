#!/usr/bin/env python3
"""Audit-only four-class market-regime labeling (research).

Does NOT modify trend_structure / trend_state_machine / trend_state_policy /
trend_zones. Does NOT wire production. Reuses prior SM timeline when present.

Classes: strong_bullish_trend | strong_bearish_trend | accumulation_range | transition_unclear

RAM-safe stepped:
  --step 0  feature frame (30m primary + 15m) + join SM timeline
  --step 1  run K0–K4 × H0–H4, write summaries + March case
  --step all

Example:
  PYTHONPATH=. python3 -u research/regime_scanner/market_regime_four_class_audit.py --step all
"""
from __future__ import annotations

import argparse
import csv
import gc
import hashlib
import json
import resource
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from research.regime_scanner.config import default_regime_scanner_config
from research.regime_scanner.data_loader import load_symbol_candles
from research.regime_scanner.indicators import compute_indicator_frame
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.timeframes import aggregate_candles, timeframe_timedelta

OUT = Path("research/regime_scanner/results/market_regime_four_class_audit")
CACHE = OUT / "_cache"
PRIOR_SM = Path("research/regime_scanner/results/trend_regime_four_class_audit/state_timeline_5m.csv")
PRIOR_FRAME = Path("research/regime_scanner/results/trend_regime_four_class_audit/_cache/frame_5m.parquet")

STRUCTURE = Path("research/regime_scanner/trend_structure.py")
MACHINE = Path("research/regime_scanner/trend_state_machine.py")
POLICY = Path("research/regime_scanner/trend_state_policy.py")
ZONES = Path("research/regime_scanner/trend_zones.py")

LOAD_START = "2025-12-27T00:00:00+00:00"
ANALYZE_START = "2026-01-01T00:00:00+00:00"
ANALYZE_END = "2026-03-15T00:00:00+00:00"
MARCH_START = "2026-03-05T00:00:00+00:00"
MARCH_END = "2026-03-10T00:00:00+00:00"

Regime = str  # one of four
CLASSES = (
    "strong_bullish_trend",
    "strong_bearish_trend",
    "accumulation_range",
    "transition_unclear",
)

SM_TO_K0 = {
    "strong_bullish": "strong_bullish_trend",
    "strong_bearish": "strong_bearish_trend",
    "neutral": "accumulation_range",
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

NO_LONG_REGIMES = {"strong_bearish_trend"}
NO_SHORT_REGIMES = {"strong_bullish_trend"}
# softer no_long also from transition when bearish-leaning — tracked separately as hint


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
    cols = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if v is None else v) for k, v in r.items()})


def _slope(arr: np.ndarray, n: int) -> float:
    if len(arr) < n + 1 or n <= 0:
        return float("nan")
    return float(arr[-1] - arr[-(n + 1)]) / n


def _feat_window(
    close: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    ema9: np.ndarray,
    ema20: np.ndarray,
    atr: np.ndarray,
    n: int,
) -> dict[str, float]:
    if len(close) < n + 1:
        return {}
    c = close[-(n + 1) :]
    h = high[-(n + 1) :]
    l = low[-(n + 1) :]
    e9 = ema9[-(n + 1) :]
    e20 = ema20[-(n + 1) :]
    a = float(atr[-1]) if atr[-1] == atr[-1] and atr[-1] > 0 else float("nan")
    rets = np.diff(c)
    net = float(c[-1] - c[0])
    path = float(np.sum(np.abs(rets)))
    de = abs(net) / path if path > 1e-12 else 0.0
    rng = float(np.max(h) - np.min(l))
    progress = abs(net) / rng if rng > 1e-12 else 0.0
    up = float(np.mean(rets > 0))
    dn = float(np.mean(rets < 0))
    # direction changes
    signs = np.sign(rets)
    flips = int(np.sum(signs[1:] * signs[:-1] < 0)) if len(signs) > 1 else 0
    # candle overlap: current range overlaps previous
    overlaps = 0
    for i in range(1, len(c)):
        if not (h[i] < l[i - 1] or l[i] > h[i - 1]):
            overlaps += 1
    overlap_rate = overlaps / max(len(c) - 1, 1)
    # MAE vs net direction
    if net >= 0:
        mae = float(c[0] - np.min(l))
    else:
        mae = float(np.max(h) - c[0])
    mae_atr = mae / a if a == a and a > 0 else float("nan")
    net_atr = net / a if a == a and a > 0 else float("nan")
    path_atr = path / a if a == a and a > 0 else float("nan")
    # rolling new highs/lows
    nh = nl = 0
    mx, mn = c[0], c[0]
    for x in c[1:]:
        if x > mx:
            nh += 1
            mx = x
        if x < mn:
            nl += 1
            mn = x
    # EMA geometry
    s9 = _slope(e9, n)
    s20 = _slope(e20, n)
    s9_atr = s9 / a if a == a and a > 0 else float("nan")
    s20_atr = s20 / a if a == a and a > 0 else float("nan")
    mid = max(n // 2, 1)
    s9_chg = _slope(e9[-(mid + 1) :], mid) - _slope(e9[: mid + 1], mid) if len(e9) > mid + 1 else float("nan")
    s20_chg = _slope(e20[-(mid + 1) :], mid) - _slope(e20[: mid + 1], mid) if len(e20) > mid + 1 else float("nan")
    sep = float(e9[-1] - e20[-1])
    sep_atr = sep / a if a == a and a > 0 else float("nan")
    sep_chg = float((e9[-1] - e20[-1]) - (e9[0] - e20[0]))
    sep_chg_atr = sep_chg / a if a == a and a > 0 else float("nan")
    above = float(np.mean((c[1:] > e9[1:]) & (c[1:] > e20[1:])))
    below = float(np.mean((c[1:] < e9[1:]) & (c[1:] < e20[1:])))
    mid_band = 0.5 * (e9[1:] + e20[1:])
    near = float(np.mean(np.abs(c[1:] - mid_band) <= (0.25 * a if a == a else 0)))
    crosses = int(np.sum(np.diff(np.sign(e9 - e20)) != 0))
    # bars since last cross
    sig = np.sign(e9 - e20)
    last_cross = 999
    for i in range(len(sig) - 1, 0, -1):
        if sig[i] != sig[i - 1] and sig[i] != 0 and sig[i - 1] != 0:
            last_cross = len(sig) - 1 - i
            break
    flat9 = abs(s9_atr) < 0.05 if s9_atr == s9_atr else False
    flat20 = abs(s20_atr) < 0.05 if s20_atr == s20_atr else False
    return {
        f"n{n}_net_return": net,
        f"n{n}_net_move_atr": net_atr,
        f"n{n}_gross_path_atr": path_atr,
        f"n{n}_directional_efficiency": de,
        f"n{n}_progress_vs_range": progress,
        f"n{n}_up_close_share": up,
        f"n{n}_down_close_share": dn,
        f"n{n}_mae_atr": mae_atr,
        f"n{n}_new_highs": nh,
        f"n{n}_new_lows": nl,
        f"n{n}_direction_flips": flips,
        f"n{n}_overlap_rate": overlap_rate,
        f"n{n}_ema9_slope": s9,
        f"n{n}_ema20_slope": s20,
        f"n{n}_ema9_slope_atr": s9_atr,
        f"n{n}_ema20_slope_atr": s20_atr,
        f"n{n}_ema9_slope_change": s9_chg,
        f"n{n}_ema20_slope_change": s20_chg,
        f"n{n}_ema9_minus_ema20": sep,
        f"n{n}_ema_sep_atr": sep_atr,
        f"n{n}_ema_sep_change_atr": sep_chg_atr,
        f"n{n}_share_above_both": above,
        f"n{n}_share_below_both": below,
        f"n{n}_share_near_ema_band": near,
        f"n{n}_ema_crosses": crosses,
        f"n{n}_bars_since_ema_cross": last_cross,
        f"n{n}_ema_flat": float(flat9 and flat20),
    }


@dataclass
class ClassResult:
    regime: Regime
    reasons: list[str]
    no_long_context: bool
    no_short_context: bool
    bearish_hint: bool
    bullish_hint: bool


def _g(row: dict[str, Any], key: str, default: float = 0.0) -> float:
    v = row.get(key, default)
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    if x != x:
        return default
    return x


# --- Classifiers (clear reason codes) ---

def classify_k0(row: dict[str, Any]) -> ClassResult:
    st = str(row.get("sm_state") or "unavailable")
    regime = SM_TO_K0.get(st, "transition_unclear")
    return ClassResult(
        regime=regime,
        reasons=[f"k0_map:{st}"],
        no_long_context=st in {"early_bearish", "strong_bearish", "bearish_weakening", "topping"},
        no_short_context=st in {"early_bullish", "strong_bullish", "bullish_weakening", "bottoming"},
        bearish_hint=st in {"bearish_warning", "early_bearish", "strong_bearish", "topping", "bearish_weakening"},
        bullish_hint=st in {"bullish_warning", "early_bullish", "strong_bullish", "bottoming", "bullish_weakening"},
    )


def classify_k1(row: dict[str, Any]) -> ClassResult:
    """EMA-only on 30m n12."""
    s9 = _g(row, "n12_ema9_slope_atr")
    s20 = _g(row, "n12_ema20_slope_atr")
    sep = _g(row, "n12_ema_sep_atr")
    below = _g(row, "n12_share_below_both")
    above = _g(row, "n12_share_above_both")
    crosses = _g(row, "n12_ema_crosses")
    flat = _g(row, "n12_ema_flat")
    reasons: list[str] = []
    bearish_hint = s9 < 0 and s20 < 0 and below >= 0.55
    bullish_hint = s9 > 0 and s20 > 0 and above >= 0.55
    if (
        s9 < -0.035
        and s20 < -0.03
        and sep < -0.05
        and below >= 0.75
        and crosses <= 1
    ):
        reasons = ["ema_slopes_down", "ema9_lt_ema20", f"below_both={below:.2f}", "few_crosses"]
        return ClassResult("strong_bearish_trend", reasons, True, False, True, False)
    if (
        s9 > 0.035
        and s20 > 0.03
        and sep > 0.05
        and above >= 0.75
        and crosses <= 1
    ):
        reasons = ["ema_slopes_up", "ema9_gt_ema20", f"above_both={above:.2f}", "few_crosses"]
        return ClassResult("strong_bullish_trend", reasons, False, True, False, True)
    if flat or (crosses >= 2 and abs(s20) < 0.03 and abs(s9) < 0.04):
        reasons = [f"ema_crosses={int(crosses)}", "flat_or_entangled_emas"]
        return ClassResult("accumulation_range", reasons, False, False, False, False)
    reasons = ["mixed_ema_geometry"]
    return ClassResult("transition_unclear", reasons, False, False, bearish_hint, bullish_hint)


def classify_k2(row: dict[str, Any]) -> ClassResult:
    """EMA + price progress."""
    base = classify_k1(row)
    de = _g(row, "n12_directional_efficiency")
    net_atr = _g(row, "n12_net_move_atr")
    prog = _g(row, "n12_progress_vs_range")
    below = _g(row, "n12_share_below_both")
    above = _g(row, "n12_share_above_both")
    s20 = _g(row, "n12_ema20_slope_atr")
    s9 = _g(row, "n12_ema9_slope_atr")
    sep = _g(row, "n12_ema_sep_atr")
    # Strong requires both progress AND EMA alignment (stricter than K1 alone)
    if (
        net_atr <= -1.0
        and de >= 0.32
        and prog >= 0.45
        and below >= 0.65
        and s20 < -0.015
        and s9 < -0.01
        and sep <= 0
    ):
        return ClassResult(
            "strong_bearish_trend",
            ["neg_net_atr", f"de={de:.2f}", f"prog={prog:.2f}", "ema_down", f"below={below:.2f}"],
            True,
            False,
            True,
            False,
        )
    if (
        net_atr >= 1.0
        and de >= 0.32
        and prog >= 0.45
        and above >= 0.65
        and s20 > 0.015
        and s9 > 0.01
        and sep >= 0
    ):
        return ClassResult(
            "strong_bullish_trend",
            ["pos_net_atr", f"de={de:.2f}", f"prog={prog:.2f}", "ema_up", f"above={above:.2f}"],
            False,
            True,
            False,
            True,
        )
    if abs(net_atr) < 0.35 and de < 0.22 and prog < 0.35:
        return ClassResult(
            "accumulation_range",
            [f"low_de={de:.2f}", f"small_net_atr={net_atr:.2f}", f"low_prog={prog:.2f}"],
            False,
            False,
            False,
            False,
        )
    if base.regime in {"strong_bullish_trend", "strong_bearish_trend"} and de < 0.28:
        return ClassResult(
            "transition_unclear",
            ["ema_trendish_but_insufficient_progress", *base.reasons],
            False,
            False,
            base.bearish_hint,
            base.bullish_hint,
        )
    if base.regime == "accumulation_range":
        return base
    return ClassResult(
        "transition_unclear",
        ["k2_residual", f"de={de:.2f}", f"net_atr={net_atr:.2f}", f"sep={sep:.2f}"],
        False,
        False,
        base.bearish_hint or (net_atr < -0.3 and s20 < 0),
        base.bullish_hint or (net_atr > 0.3 and s20 > 0),
    )


def classify_k3(row: dict[str, Any]) -> ClassResult:
    """Structure bias + EMA + progress. Soft HTF: reject strong against active opposite 30m pair."""
    r = classify_k2(row)
    bias30 = str(row.get("bias_30m") or "unknown")
    has_hh_hl = str(row.get("has_hh_hl_5m")) == "True"
    has_lh_ll = str(row.get("has_lh_ll_5m")) == "True"
    bias15 = str(row.get("bias_15m") or "unknown")
    # block strong bullish if 30m actively bearish LH/LL context via bias
    if r.regime == "strong_bullish_trend" and bias30 == "bearish" and not has_hh_hl:
        return ClassResult(
            "transition_unclear",
            ["k3_block_strong_bull_vs_30m_bearish", *r.reasons],
            False,
            False,
            False,
            True,
        )
    if r.regime == "strong_bearish_trend" and bias30 == "bullish" and not has_lh_ll:
        # soft: still allow if 15m also not bullish and progress strong
        de = _g(row, "n12_directional_efficiency")
        net = _g(row, "n12_net_move_atr")
        if bias15 == "bullish" or de < 0.35 or net > -1.0:
            return ClassResult(
                "transition_unclear",
                ["k3_soft_block_strong_bear_vs_30m_bullish", *r.reasons],
                True,  # still no_long hint if progress bearish
                False,
                True,
                False,
            )
    # Soft structure upgrades only — no_long remains tied to strong_bearish_trend
    if r.regime == "transition_unclear":
        de = _g(row, "n12_directional_efficiency")
        net = _g(row, "n12_net_move_atr")
        below = _g(row, "n12_share_below_both")
        above = _g(row, "n12_share_above_both")
        if net <= -1.1 and de >= 0.32 and below >= 0.65 and (has_lh_ll or bias30 != "bullish"):
            return ClassResult(
                "strong_bearish_trend",
                ["k3_upgrade_bear", f"lh_ll={has_lh_ll}", f"bias30={bias30}", *r.reasons],
                True,
                False,
                True,
                False,
            )
        if net >= 1.1 and de >= 0.32 and above >= 0.65 and (has_hh_hl or bias30 != "bearish"):
            return ClassResult(
                "strong_bullish_trend",
                ["k3_upgrade_bull", f"hh_hl={has_hh_hl}", f"bias30={bias30}", *r.reasons],
                False,
                True,
                False,
                True,
            )
    return ClassResult(r.regime, r.reasons, r.no_long_context, r.no_short_context, r.bearish_hint, r.bullish_hint)


def classify_k4(row: dict[str, Any]) -> ClassResult:
    """K3 + explicit range features."""
    r = classify_k3(row)
    de = _g(row, "n12_directional_efficiency")
    net = _g(row, "n12_net_move_atr")
    crosses = _g(row, "n12_ema_crosses")
    overlap = _g(row, "n12_overlap_rate")
    flips = _g(row, "n12_direction_flips")
    near = _g(row, "n12_share_near_ema_band")
    flat = _g(row, "n12_ema_flat")
    # demote false strong to range
    if r.regime in {"strong_bullish_trend", "strong_bearish_trend"}:
        if de < 0.20 and overlap >= 0.70 and crosses >= 2:
            return ClassResult(
                "accumulation_range",
                ["k4_demote_strong_to_range", f"overlap={overlap:.2f}", f"crosses={int(crosses)}"],
                False,
                False,
                False,
                False,
            )
    if r.regime == "transition_unclear":
        if (
            abs(net) < 0.40
            and de < 0.25
            and (crosses >= 2 or flat >= 0.5)
            and overlap >= 0.65
            and flips >= 3
        ):
            return ClassResult(
                "accumulation_range",
                [
                    "k4_range_pack",
                    f"de={de:.2f}",
                    f"overlap={overlap:.2f}",
                    f"flips={int(flips)}",
                    f"near_ema={near:.2f}",
                ],
                False,
                False,
                False,
                False,
            )
    return r


CLASSIFIERS: dict[str, Callable[[dict[str, Any]], ClassResult]] = {
    "K0": classify_k0,
    "K1": classify_k1,
    "K2": classify_k2,
    "K3": classify_k3,
    "K4": classify_k4,
}


def apply_hysteresis(
    raw: list[ClassResult],
    mode: str,
) -> list[ClassResult]:
    """Apply confirmation bars / forced transition path."""
    if not raw:
        return []
    out: list[ClassResult] = []
    cur = raw[0].regime
    pending: Regime | None = None
    pend_count = 0
    out.append(raw[0])

    def need(dst: Regime, src: Regime) -> int:
        if mode == "H0":
            return 1
        if mode == "H1":
            return 2
        if mode == "H2":
            return 3
        if mode == "H3":
            # trend↔trend must pass transition
            trends = {"strong_bullish_trend", "strong_bearish_trend"}
            if src in trends and dst in trends and src != dst:
                return 999  # force via transition handled below
            return 2
        if mode == "H4":
            if dst in {"strong_bullish_trend", "strong_bearish_trend"}:
                return 2
            if dst == "accumulation_range":
                return 3
            return 2
        return 1

    for i in range(1, len(raw)):
        cand = raw[i]
        dst = cand.regime
        # H3: direct opposite trend → insert transition
        if mode == "H3":
            trends = {"strong_bullish_trend", "strong_bearish_trend"}
            if cur in trends and dst in trends and cur != dst:
                dst = "transition_unclear"
                cand = ClassResult(
                    "transition_unclear",
                    ["h3_force_via_transition", *cand.reasons],
                    cand.no_long_context,
                    cand.no_short_context,
                    cand.bearish_hint,
                    cand.bullish_hint,
                )
        if dst == cur:
            pending = None
            pend_count = 0
            out.append(ClassResult(cur, cand.reasons, cand.no_long_context, cand.no_short_context, cand.bearish_hint, cand.bullish_hint))
            continue
        if pending != dst:
            pending = dst
            pend_count = 1
        else:
            pend_count += 1
        req = need(dst, cur)
        if pend_count >= req:
            cur = dst
            pending = None
            pend_count = 0
            out.append(cand)
        else:
            # hold previous class, keep latest reasons tagged
            out.append(
                ClassResult(
                    cur,
                    [f"hyst_hold_{mode}:{pend_count}/{req}", *cand.reasons],
                    # no_long can still flip on hint for soft safety metric variants — keep held regime flags
                    cur in NO_LONG_REGIMES or cand.no_long_context and mode != "H0",
                    cur in NO_SHORT_REGIMES or cand.no_short_context and mode != "H0",
                    cand.bearish_hint,
                    cand.bullish_hint,
                )
            )
    # fix flags strictly from held regime (no soft hint→no_long bleed)
    fixed = []
    for r in out:
        nl = r.regime in NO_LONG_REGIMES
        ns = r.regime in NO_SHORT_REGIMES
        # K0 keeps SM-derived flags from raw when regime is transition but classifier said so
        if r.reasons and any(x.startswith("k0_map:") for x in r.reasons):
            nl = r.no_long_context
            ns = r.no_short_context
        fixed.append(ClassResult(r.regime, r.reasons, nl, ns, r.bearish_hint, r.bullish_hint))
    return fixed


def step0() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    hashes = {
        "trend_structure.py": _md5(STRUCTURE),
        "trend_state_machine.py": _md5(MACHINE),
        "trend_state_policy.py": _md5(POLICY),
        "trend_zones.py": _md5(ZONES),
    }
    _write_json(OUT / "hashes_before.json", hashes)
    _p(f"hashes_before={hashes}")

    # 5m frame
    if PRIOR_FRAME.exists():
        frame5 = pd.read_parquet(PRIOR_FRAME)
        _p(f"reused prior 5m frame n={len(frame5)}")
    else:
        raw = load_symbol_candles("APTUSDT")
        raw = raw.copy()
        raw["timestamp"] = pd.to_datetime(raw["timestamp"], utc=True)
        end = _ts(ANALYZE_END)
        sl = raw[(raw["timestamp"] >= _ts(LOAD_START)) & (raw["timestamp"] < end)]
        scfg = default_regime_scanner_config().with_timeframe("5m")
        frame5 = compute_indicator_frame(sl, config=scfg)
        frame5["timestamp"] = pd.to_datetime(frame5["timestamp"], utc=True)
        frame5["decision_time"] = frame5["timestamp"] + pd.Timedelta(minutes=5)
        frame5 = frame5.loc[frame5["decision_time"] <= end].reset_index(drop=True)

    frame5["timestamp"] = pd.to_datetime(frame5["timestamp"], utc=True)
    if "decision_time" not in frame5.columns:
        frame5["decision_time"] = frame5["timestamp"] + pd.Timedelta(minutes=5)
    else:
        frame5["decision_time"] = pd.to_datetime(frame5["decision_time"], utc=True)

    end = _ts(ANALYZE_END)
    # 30m aggregate
    scfg30 = default_regime_scanner_config().with_timeframe("30m")
    agg30 = aggregate_candles(
        frame5[["timestamp", "open", "high", "low", "close", "volume"]], "30m", end
    )
    ind30 = compute_indicator_frame(agg30, config=scfg30).copy()
    ind30["timestamp"] = pd.to_datetime(ind30["timestamp"], utc=True)
    ind30["decision_time"] = ind30["timestamp"] + timeframe_timedelta("30m")
    ind30 = ind30.loc[ind30["decision_time"] <= end].reset_index(drop=True)

    scfg15 = default_regime_scanner_config().with_timeframe("15m")
    agg15 = aggregate_candles(
        frame5[["timestamp", "open", "high", "low", "close", "volume"]], "15m", end
    )
    ind15 = compute_indicator_frame(agg15, config=scfg15).copy()
    ind15["timestamp"] = pd.to_datetime(ind15["timestamp"], utc=True)
    ind15["decision_time"] = ind15["timestamp"] + timeframe_timedelta("15m")
    ind15 = ind15.loc[ind15["decision_time"] <= end].reset_index(drop=True)

    # SM join at 30m decision times (asof)
    if not PRIOR_SM.exists():
        raise SystemExit(f"Missing SM timeline {PRIOR_SM} — run trend_regime_four_class_audit first")
    sm = pd.read_csv(PRIOR_SM)
    sm["decision_time"] = pd.to_datetime(sm["decision_time"], utc=True)
    sm = sm.sort_values("decision_time")

    close = ind30["close"].astype(float).to_numpy()
    high = ind30["high"].astype(float).to_numpy()
    low = ind30["low"].astype(float).to_numpy()
    ema9 = ind30["ema_9"].astype(float).to_numpy()
    ema20 = ind30["ema_20"].astype(float).to_numpy()
    atr = ind30["atr"].astype(float).to_numpy()

    # 15m arrays for confirmation slopes at matching times
    c15 = ind15["close"].astype(float).to_numpy()
    e9_15 = ind15["ema_9"].astype(float).to_numpy()
    e20_15 = ind15["ema_20"].astype(float).to_numpy()
    atr15 = ind15["atr"].astype(float).to_numpy()

    analyze_start = _ts(ANALYZE_START)
    rows: list[dict[str, Any]] = []
    for i in range(len(ind30)):
        dt = _ts(ind30.iloc[i]["decision_time"])
        if dt < analyze_start:
            continue
        feat: dict[str, Any] = {
            "decision_time": _iso(dt),
            "candle_timestamp": _iso(ind30.iloc[i]["timestamp"]),
            "close": float(close[i]),
            "atr": float(atr[i]) if atr[i] == atr[i] else None,
        }
        for n in (6, 12, 24):
            feat.update(_feat_window(close[: i + 1], high[: i + 1], low[: i + 1], ema9[: i + 1], ema20[: i + 1], atr[: i + 1], n))
        # 15m confirm: last closed 15m <= dt
        j = int(ind15["decision_time"].searchsorted(dt, side="right") - 1)
        if j >= 12:
            s9_15 = _slope(e9_15[: j + 1], 6)
            s20_15 = _slope(e20_15[: j + 1], 6)
            a15 = float(atr15[j]) if atr15[j] == atr15[j] and atr15[j] > 0 else float("nan")
            feat["m15_ema9_slope_atr_6"] = s9_15 / a15 if a15 == a15 else float("nan")
            feat["m15_ema20_slope_atr_6"] = s20_15 / a15 if a15 == a15 else float("nan")
            feat["m15_share_below_both_12"] = float(
                np.mean(
                    (c15[j - 11 : j + 1] < e9_15[j - 11 : j + 1])
                    & (c15[j - 11 : j + 1] < e20_15[j - 11 : j + 1])
                )
            )
        # SM asof
        idx = sm["decision_time"].searchsorted(dt, side="right") - 1
        if idx >= 0:
            srow = sm.iloc[int(idx)]
            feat["sm_state"] = srow["state"]
            feat["sm_previous_state"] = srow.get("previous_state")
            feat["allow_long"] = srow["allow_long"]
            feat["allow_short"] = srow["allow_short"]
            feat["bias_5m"] = srow.get("bias_5m")
            feat["bias_15m"] = srow.get("bias_15m")
            feat["bias_30m"] = srow.get("bias_30m")
            feat["has_hh_hl_5m"] = srow.get("has_hh_hl_5m")
            feat["has_lh_ll_5m"] = srow.get("has_lh_ll_5m")
            feat["sm_reasons"] = srow.get("reasons")
        rows.append(feat)

    path = CACHE / "regime_feature_rows_30m.csv"
    _write_csv(path, rows)
    _write_json(
        OUT / "feature_definitions.json",
        {
            "primary_tf": "30m",
            "confirm_tf": "15m",
            "windows": [6, 12, 24],
            "ema_fields": [
                "ema9/20 slope",
                "slope_atr",
                "slope_change",
                "sep_atr",
                "sep_change_atr",
                "share_above/below_both",
                "near_band",
                "crosses",
                "bars_since_cross",
                "flat",
            ],
            "progress_fields": [
                "net_return",
                "net_move_atr",
                "gross_path_atr",
                "directional_efficiency",
                "progress_vs_range",
                "up/down share",
                "mae_atr",
                "new highs/lows",
                "direction_flips",
                "overlap_rate",
            ],
            "structure_fields": ["bias_5m/15m/30m", "has_hh_hl_5m", "has_lh_ll_5m", "sm_state"],
            "note": "All features use only closed candles at decision_time.",
        },
    )
    _write_json(
        OUT / "variant_configurations.json",
        {
            "classifiers": {k: v.__doc__ for k, v in CLASSIFIERS.items()},
            "hysteresis": {
                "H0": "immediate",
                "H1": "2 bars confirm",
                "H2": "3 bars confirm",
                "H3": "2 bars + bullish↔bearish only via transition_unclear",
                "H4": "trend 2 bars / range 3 bars",
            },
            "classes": list(CLASSES),
        },
    )
    _p(f"step0 wrote {len(rows)} feature rows")
    del frame5, ind30, ind15, sm, rows
    gc.collect()


def _durations(labels: list[str]) -> list[int]:
    if not labels:
        return []
    durs = []
    cur = labels[0]
    n = 1
    for x in labels[1:]:
        if x == cur:
            n += 1
        else:
            durs.append(n)
            cur = x
            n = 1
    durs.append(n)
    return durs


def _pct(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    a = np.array(xs, dtype=float)
    return float(np.percentile(a, p))


def evaluate_variant(
    rows: list[dict[str, Any]],
    labels: list[ClassResult],
    kid: str,
    hid: str,
) -> dict[str, Any]:
    assert len(rows) == len(labels)
    # March metrics
    march_idx = [
        i
        for i, r in enumerate(rows)
        if _ts(MARCH_START) <= _ts(r["decision_time"]) <= _ts(MARCH_END)
    ]
    # clear bearish price path: from first bar on Mar6 where close < 0.99 down
    first_bearish_hint = None
    first_strong_bear = None
    first_no_long = None
    for i in march_idx:
        r, lab = rows[i], labels[i]
        if first_bearish_hint is None and lab.bearish_hint:
            first_bearish_hint = (r, lab)
        if first_strong_bear is None and lab.regime == "strong_bearish_trend":
            first_strong_bear = (r, lab)
        if first_no_long is None and lab.no_long_context:
            first_no_long = (r, lab)

    # premature: no_long on Mar5 morning/midday while price still elevated
    premature = 0
    for i in march_idx:
        r, lab = rows[i], labels[i]
        dt = _ts(r["decision_time"])
        if (
            dt < _ts("2026-03-05T16:00:00+00:00")
            and lab.no_long_context
            and float(r["close"]) >= 0.99
        ):
            premature += 1

    # delay vs selloff onset (05.03 16:00) — clearer than calendar Mar6
    crash_t = _ts("2026-03-05T16:00:00+00:00")
    def delay_h(pair):
        if pair is None:
            return None
        return (_ts(pair[0]["decision_time"]) - crash_t).total_seconds() / 3600.0
    # safety over full sample using feature heuristic as weak GT
    # clear_bearish: n12 net_atr<=-1 & de>=0.35 & below>=0.7
    # clear_bullish: opposite
    no_long_on_clear_bear = no_short_on_clear_bull = 0
    clear_bear = clear_bull = 0
    false_no_long_on_bull = false_no_short_on_bear = 0
    wrong_dir = trend_as_range = range_as_trend = 0
    for r, lab in zip(rows, labels):
        net = _g(r, "n12_net_move_atr")
        de = _g(r, "n12_directional_efficiency")
        below = _g(r, "n12_share_below_both")
        above = _g(r, "n12_share_above_both")
        is_clear_bear = net <= -1.0 and de >= 0.35 and below >= 0.65
        is_clear_bull = net >= 1.0 and de >= 0.35 and above >= 0.65
        is_clear_range = abs(net) < 0.3 and de < 0.20 and _g(r, "n12_overlap_rate") >= 0.7
        if is_clear_bear:
            clear_bear += 1
            if lab.no_long_context:
                no_long_on_clear_bear += 1
            if lab.regime == "strong_bullish_trend":
                wrong_dir += 1
            if lab.regime == "accumulation_range":
                trend_as_range += 1
            if not lab.no_long_context and lab.no_short_context:
                false_no_short_on_bear += 1
        if is_clear_bull:
            clear_bull += 1
            if lab.no_short_context:
                no_short_on_clear_bull += 1
            if lab.regime == "strong_bearish_trend":
                wrong_dir += 1
            if lab.regime == "accumulation_range":
                trend_as_range += 1
            if lab.no_long_context:
                false_no_long_on_bull += 1
        if is_clear_range and lab.regime in {"strong_bullish_trend", "strong_bearish_trend"}:
            range_as_trend += 1

    regs = [x.regime for x in labels]
    flips = sum(1 for a, b in zip(regs, regs[1:]) if a != b)
    durs = _durations(regs)
    counts = Counter(regs)

    # delay_h uses selloff onset Mar5 16:00 (defined above); keep mar6 aliases for CSV
    selloff_delay_no_long = delay_h(first_no_long)
    selloff_delay_strong = delay_h(first_strong_bear)
    mar6 = _ts("2026-03-06T00:00:00+00:00")

    def delay_vs(pair, ref: pd.Timestamp):
        if pair is None:
            return None
        return (_ts(pair[0]["decision_time"]) - ref).total_seconds() / 3600.0

    return {
        "variant": f"{kid}_{hid}",
        "classifier": kid,
        "hysteresis": hid,
        "n_bars": len(labels),
        "n_strong_bull": counts["strong_bullish_trend"],
        "n_strong_bear": counts["strong_bearish_trend"],
        "n_range": counts["accumulation_range"],
        "n_transition": counts["transition_unclear"],
        "share_transition": counts["transition_unclear"] / max(len(labels), 1),
        "regime_flip_count": flips,
        "median_regime_dur_bars": float(np.median(durs)) if durs else None,
        "p90_regime_dur_bars": _pct([float(x) for x in durs], 90),
        "clear_bear_bars": clear_bear,
        "no_long_during_clear_bear": no_long_on_clear_bear,
        "no_long_recall_clear_bear": (no_long_on_clear_bear / clear_bear) if clear_bear else None,
        "clear_bull_bars": clear_bull,
        "no_short_during_clear_bull": no_short_on_clear_bull,
        "no_short_recall_clear_bull": (no_short_on_clear_bull / clear_bull) if clear_bull else None,
        "false_no_long_on_clear_bull": false_no_long_on_bull,
        "false_no_short_on_clear_bear": false_no_short_on_bear,
        "wrong_direction": wrong_dir,
        "trend_as_range": trend_as_range,
        "range_as_trend": range_as_trend,
        "march_first_bearish_hint": None if first_bearish_hint is None else first_bearish_hint[0]["decision_time"],
        "march_first_strong_bear": None if first_strong_bear is None else first_strong_bear[0]["decision_time"],
        "march_first_no_long": None if first_no_long is None else first_no_long[0]["decision_time"],
        "march_delay_h_no_long_vs_selloff": selloff_delay_no_long,
        "march_delay_h_strong_vs_selloff": selloff_delay_strong,
        "march_delay_h_no_long_vs_mar6": delay_vs(first_no_long, mar6),
        "march_delay_h_strong_vs_mar6": delay_vs(first_strong_bear, mar6),
        "march_price_at_no_long": None if first_no_long is None else first_no_long[0]["close"],
        "march_price_at_strong": None if first_strong_bear is None else first_strong_bear[0]["close"],
        "march_sm_at_no_long": None if first_no_long is None else first_no_long[0].get("sm_state"),
        "march_reasons_no_long": None if first_no_long is None else "|".join(first_no_long[1].reasons),
        "march_premature_no_long_bars_mar5": premature,
        "score": 0.0,  # filled below
    }


def score_variant(s: dict[str, Any]) -> float:
    sc = 0.0
    n = max(float(s.get("n_bars") or 1), 1.0)
    # Prefer selloff-onset reference (Mar5 16:00): recognize during evening selloff / Mar6
    d = s.get("march_delay_h_no_long_vs_selloff")
    if d is None:
        sc -= 80
    else:
        d = float(d)
        if -1 <= d <= 18:
            sc += 50 - abs(d - 3) * 1.2
        elif 18 < d <= 48:
            sc += 12 - (d - 18) * 0.35
        elif d < -1:
            sc -= 35 + min(50.0, abs(d))
        else:
            sc -= 15 + min(35.0, d - 48)
    if s.get("march_premature_no_long_bars_mar5", 0) > 0:
        sc -= 12 * float(s["march_premature_no_long_bars_mar5"])
    ds = s.get("march_delay_h_strong_vs_selloff")
    if ds is not None and -1 <= float(ds) <= 24:
        sc += 12
    recall = s.get("no_long_recall_clear_bear")
    if recall is not None:
        sc += 30 * float(recall)
    sc -= 80 * (float(s.get("false_no_long_on_clear_bull") or 0) / n)
    sc -= 120 * (float(s.get("wrong_direction") or 0) / n)
    sc -= 60 * (float(s.get("range_as_trend") or 0) / n)
    sc -= 40 * (float(s.get("trend_as_range") or 0) / n)
    st = float(s.get("share_transition") or 0)
    if 0.15 <= st <= 0.75:
        sc += 6
    flips = float(s.get("regime_flip_count") or 0)
    flip_rate = flips / n
    if flip_rate > 0.20:
        sc -= 35
    elif flip_rate > 0.12:
        sc -= 15
    elif flip_rate > 0.08:
        sc -= 5
    elif flip_rate < 0.06:
        sc += 8
    # Stability bonus for confirmation hysteresis that still hits March on time
    if str(s.get("hysteresis")) in {"H1", "H2", "H4"} and d is not None and -1 <= float(d) <= 18:
        sc += 10
    if str(s.get("hysteresis")) in {"H1", "H4"} and flip_rate <= 0.15 and d is not None and -1 <= float(d) <= 18:
        sc += 8
    return sc


def variant_passes_gates(s: dict[str, Any], k0: dict[str, Any]) -> bool:
    dly = s.get("march_delay_h_no_long_vs_selloff")
    k0_delay = k0.get("march_delay_h_no_long_vs_selloff")
    if dly is None:
        return False
    earlier = k0_delay is None or float(dly) + 6 < float(k0_delay)
    during = float(dly) <= 32
    prem = int(s.get("march_premature_no_long_bars_mar5") or 0) <= 2
    flip_ok = float(s.get("regime_flip_count") or 0) / max(int(s.get("n_bars") or 1), 1) < 0.22
    recall_ok = float(s.get("no_long_recall_clear_bear") or 0) >= 0.45
    return earlier and during and prem and flip_ok and recall_ok


def step1() -> None:
    feat_path = CACHE / "regime_feature_rows_30m.csv"
    rows = list(csv.DictReader(feat_path.open()))
    _p(f"loaded {len(rows)} feature rows")

    hyst_modes = ["H0", "H1", "H2", "H3", "H4"]
    summaries = []
    # Keep best timelines in memory only for top variants + always K0_H0 and best
    timelines: dict[str, list[ClassResult]] = {}

    for kid, clf in CLASSIFIERS.items():
        raw = [clf(r) for r in rows]
        for hid in hyst_modes:
            labs = apply_hysteresis(raw, hid)
            s = evaluate_variant(rows, labs, kid, hid)
            s["score"] = score_variant(s)
            summaries.append(s)
            timelines[f"{kid}_{hid}"] = labs
            _p(
                f"{kid}_{hid}: score={s['score']:.1f} no_long@{s['march_first_no_long']} "
                f"delay_selloff_h={s['march_delay_h_no_long_vs_selloff']} "
                f"prem={s['march_premature_no_long_bars_mar5']} flips={s['regime_flip_count']} "
                f"recall_bear={s['no_long_recall_clear_bear']}"
            )
        del raw
        gc.collect()

    summaries.sort(key=lambda x: float(x["score"]), reverse=True)
    k0_ref = next(s for s in summaries if s["variant"] == "K0_H0")
    gated = [s for s in summaries if variant_passes_gates(s, k0_ref)]
    if gated:
        # Prefer gated variants: highest score among those that pass März/flicker gates
        gated.sort(key=lambda x: float(x["score"]), reverse=True)
        best = gated[0]
        _p(f"gated candidates={len(gated)}; prefer {best['variant']}")
    else:
        best = summaries[0]
        _p("no gated candidates; falling back to raw score leader")
    # Keep full ranking in CSV; mark selected
    for s in summaries:
        s["passes_gates"] = variant_passes_gates(s, k0_ref)
        s["selected"] = s["variant"] == best["variant"]
    summaries.sort(key=lambda x: (not x["selected"], -float(x["score"])))
    _write_csv(OUT / "variant_summary.csv", summaries)
    _write_csv(
        OUT / "hysteresis_comparison.csv",
        [s for s in summaries],
    )

    best_id = best["variant"]
    _p(f"best variant={best_id} score={best['score']:.1f}")

    # Full regime timeline for best + K0_H0 + K3_H1 + K4_H4
    export_ids = list(dict.fromkeys([best_id, "K0_H0", "K3_H1", "K3_H4", "K4_H1", "K2_H1"]))
    # primary timeline = best
    labs = timelines[best_id]
    timeline_rows = []
    disagree = []
    wrong_dir_rows = []
    range_err = []
    transitions = []
    prev = None
    for r, lab in zip(rows, labs):
        sm = str(r.get("sm_state") or "")
        mapped = SM_TO_K0.get(sm, "transition_unclear")
        dtype = ""
        if lab.regime == "strong_bearish_trend" and sm == "bullish_weakening":
            dtype = "regime_bearish__sm_bullish_weakening"
        elif lab.regime == "strong_bullish_trend" and sm == "bearish_weakening":
            dtype = "regime_bullish__sm_bearish_weakening"
        elif lab.regime == "accumulation_range" and sm in {"strong_bullish", "strong_bearish"}:
            dtype = "regime_range__sm_strong"
        elif lab.regime in {"strong_bullish_trend", "strong_bearish_trend"} and sm in {"bottoming", "topping"}:
            dtype = "regime_strong__sm_bottoming_topping"
        elif lab.regime != mapped:
            dtype = f"map_diff:{mapped}->{lab.regime}"
        row_out = {
            "decision_time": r["decision_time"],
            "close": r["close"],
            "market_regime_candidate": lab.regime,
            "current_trend_state": sm,
            "allow_long": r.get("allow_long"),
            "allow_short": r.get("allow_short"),
            "regime_no_long_context": lab.no_long_context,
            "regime_no_short_context": lab.no_short_context,
            "bearish_hint": lab.bearish_hint,
            "bullish_hint": lab.bullish_hint,
            "reason_codes": "|".join(lab.reasons),
            "disagreement_type": dtype,
            "n12_de": r.get("n12_directional_efficiency"),
            "n12_net_atr": r.get("n12_net_move_atr"),
            "n12_below_both": r.get("n12_share_below_both"),
            "n12_above_both": r.get("n12_share_above_both"),
            "bias_30m": r.get("bias_30m"),
            "variant": best_id,
        }
        timeline_rows.append(row_out)
        if dtype:
            disagree.append(row_out)
        net = _g(r, "n12_net_move_atr")
        if lab.regime == "strong_bullish_trend" and net < -0.8:
            wrong_dir_rows.append(row_out)
        if lab.regime == "strong_bearish_trend" and net > 0.8:
            wrong_dir_rows.append(row_out)
        if lab.regime in {"strong_bullish_trend", "strong_bearish_trend"} and _g(r, "n12_directional_efficiency") < 0.18:
            range_err.append({**row_out, "error": "trend_on_low_de"})
        if lab.regime == "accumulation_range" and abs(net) > 1.2 and _g(r, "n12_directional_efficiency") > 0.4:
            range_err.append({**row_out, "error": "range_on_strong_progress"})
        if prev is not None and prev != lab.regime:
            transitions.append(
                {
                    "decision_time": r["decision_time"],
                    "from_regime": prev,
                    "to_regime": lab.regime,
                    "close": r["close"],
                    "reasons": "|".join(lab.reasons),
                    "sm_state": sm,
                }
            )
        prev = lab.regime

    _write_csv(OUT / "regime_timeline.csv", timeline_rows)
    _write_csv(OUT / "regime_transitions.csv", transitions)
    _write_csv(OUT / "state_machine_disagreements.csv", disagree)
    _write_csv(OUT / "wrong_direction_cases.csv", wrong_dir_rows)
    _write_csv(OUT / "range_error_cases.csv", range_err)

    # March crash timeline for best + K0
    def march_tl(vid: str) -> list[dict[str, Any]]:
        labs_v = timelines[vid]
        out = []
        for r, lab in zip(rows, labs_v):
            if not (_ts(MARCH_START) <= _ts(r["decision_time"]) <= _ts(MARCH_END)):
                continue
            out.append(
                {
                    "decision_time": r["decision_time"],
                    "close": r["close"],
                    "variant": vid,
                    "regime": lab.regime,
                    "no_long_context": lab.no_long_context,
                    "bearish_hint": lab.bearish_hint,
                    "reasons": "|".join(lab.reasons),
                    "sm_state": r.get("sm_state"),
                    "sm_allow_long": r.get("allow_long"),
                    "n12_de": r.get("n12_directional_efficiency"),
                    "n12_net_atr": r.get("n12_net_move_atr"),
                    "n12_below": r.get("n12_share_below_both"),
                    "n12_ema20_slope_atr": r.get("n12_ema20_slope_atr"),
                    "bias_30m": r.get("bias_30m"),
                }
            )
        return out

    march = march_tl(best_id) + march_tl("K0_H0")
    _write_csv(OUT / "march_crash_timeline.csv", march)

    # Case studies: pick windows from feature heuristics
    def case_window(start: str, end: str, name: str) -> list[dict[str, Any]]:
        out = []
        for r, lab in zip(rows, labs):
            if _ts(start) <= _ts(r["decision_time"]) <= _ts(end):
                out.append(
                    {
                        "case": name,
                        "decision_time": r["decision_time"],
                        "close": r["close"],
                        "regime": lab.regime,
                        "sm_state": r.get("sm_state"),
                        "reasons": "|".join(lab.reasons),
                        "n12_de": r.get("n12_directional_efficiency"),
                        "n12_net_atr": r.get("n12_net_move_atr"),
                    }
                )
        return out

    # Find bullish / range / transition windows automatically
    bull_case = []
    bear_case = []
    range_case = []
    # scan for stretches
    for i, r in enumerate(rows):
        if _g(r, "n12_net_move_atr") >= 1.0 and _g(r, "n12_share_above_both") >= 0.7:
            bull_case.append(i)
        if _g(r, "n12_net_move_atr") <= -1.0 and _g(r, "n12_share_below_both") >= 0.7:
            bear_case.append(i)
        if abs(_g(r, "n12_net_move_atr")) < 0.25 and _g(r, "n12_directional_efficiency") < 0.2:
            range_case.append(i)

    def export_around(indices: list[int], name: str, path: Path) -> None:
        if not indices:
            _write_csv(path, [])
            return
        mid = indices[len(indices) // 2]
        lo = max(0, mid - 24)
        hi = min(len(rows) - 1, mid + 24)
        out = []
        for i in range(lo, hi + 1):
            lab = labs[i]
            r = rows[i]
            out.append(
                {
                    "case": name,
                    "decision_time": r["decision_time"],
                    "close": r["close"],
                    "regime": lab.regime,
                    "sm_state": r.get("sm_state"),
                    "reasons": "|".join(lab.reasons),
                    "n12_de": r.get("n12_directional_efficiency"),
                    "n12_net_atr": r.get("n12_net_move_atr"),
                }
            )
        _write_csv(path, out)

    export_around(bull_case, "bullish_trend", OUT / "bullish_case_study.csv")
    export_around(bear_case, "bearish_trend", OUT / "bearish_case_study.csv")
    export_around(range_case, "range", OUT / "range_case_study.csv")
    # choppy: many EMA crosses + low progress
    choppy_case = [
        i
        for i, r in enumerate(rows)
        if _g(r, "n12_ema_crosses") >= 3
        and abs(_g(r, "n12_net_move_atr")) < 0.45
        and _g(r, "n12_directional_efficiency") < 0.25
    ]
    export_around(choppy_case, "choppy_ema_crosses", OUT / "choppy_case_study.csv")
    # transition: around Mar 8 regime change for best
    _write_csv(
        OUT / "transition_case_study.csv",
        case_window("2026-03-07T00:00:00+00:00", "2026-03-09T12:00:00+00:00", "bullish_to_bearish_transition"),
    )
    # second transition window: post-crash bounce / unclear (bearish→bullish candidate)
    _write_csv(
        OUT / "transition_bearish_to_bullish_case_study.csv",
        case_window("2026-03-09T12:00:00+00:00", "2026-03-10T00:00:00+00:00", "bearish_to_bullish_candidate"),
    )

    # Decision J–N
    k = best["classifier"]
    dly = best.get("march_delay_h_no_long_vs_selloff")
    dly_mar6 = best.get("march_delay_h_no_long_vs_mar6")
    prem = int(best.get("march_premature_no_long_bars_mar5") or 0)
    recall = best.get("no_long_recall_clear_bear") or 0
    k0 = k0_ref
    k0_delay = k0.get("march_delay_h_no_long_vs_selloff")
    k0_delay_mar6 = k0.get("march_delay_h_no_long_vs_mar6")
    by_score = sorted(summaries, key=lambda x: float(x["score"]), reverse=True)

    ok_earlier = dly is not None and (k0_delay is None or float(dly) + 6 < float(k0_delay))
    # useful during clear selloff: no_long by end of Mar6 (≤32h after Mar5 16:00)
    ok_during_selloff = dly is not None and float(dly) <= 32
    ok_not_premature = prem <= 2
    ok_recall = float(recall) >= 0.45
    ok_flip = float(best["regime_flip_count"]) / max(int(best["n_bars"]), 1) < 0.22
    ok_range = int(best.get("range_as_trend") or 0) <= max(5, int(0.025 * int(best["n_bars"])))
    gated_ok = bool(best.get("passes_gates"))

    if not (ok_earlier and ok_during_selloff and ok_flip and gated_ok):
        decision, note = "N", "Keine robuste Vier-Klassen-Trennung / März-Nutzen unzureichend."
    elif k == "K0":
        decision, note = "J", "State-Machine-Mapping reicht nach kleiner Korrektur."
    elif k == "K1":
        decision, note = "K", "EMA-Basisebene reicht (EMA-only war best)."
    elif k in {"K2", "K3"}:
        decision, note = "L", "EMA + Price Progress nötig (Structure optional unterstützend)."
    elif k == "K4":
        decision, note = "M", "Range-Merkmale zusätzlich zwingend nötig."
    else:
        decision, note = "L", "EMA + Price Progress sinnvoll."

    # refine: if K1 best but K2/K3 close, prefer L
    if decision == "K" and any(
        s["classifier"] in {"K2", "K3"} and float(s["score"]) >= float(best["score"]) - 5 for s in by_score[:5]
    ):
        decision, note = "L", "EMA allein knapp; EMA+Progress(+Structure) robuster."

    # If K4 not needed (K2/K3 already ok_range), prefer L over M when K4 wins only by flicker bonus
    if decision == "M":
        best_k2k3 = [s for s in by_score if s["classifier"] in {"K2", "K3"} and s.get("passes_gates")]
        if best_k2k3 and float(best_k2k3[0]["score"]) >= float(best["score"]) - 8:
            decision, note = "L", "Range-Merkmale nicht zwingend; K2/K3 bereits ausreichend."

    if not (ok_not_premature and ok_recall and ok_range):
        if decision != "N":
            note += " Teilbedingungen (Premature/Recall/Range) nur teilweise erfüllt."

    hashes_after = {
        "trend_structure.py": _md5(STRUCTURE),
        "trend_state_machine.py": _md5(MACHINE),
        "trend_state_policy.py": _md5(POLICY),
        "trend_zones.py": _md5(ZONES),
    }
    hashes_before = json.loads((OUT / "hashes_before.json").read_text())
    assert hashes_before == hashes_after

    march_answer = (
        "JA — Basisebene liefert no_long während des Abverkaufs (Mar5 16:00+), "
        f"ohne Mar5-Vormittag-Premature ({prem} Bars), deutlich vor SM-K0."
        if ok_during_selloff and ok_not_premature and ok_earlier
        else (
            "TEILWEISE — früher als SM, aber Premature/Timing/Flicker nicht voll erfüllt."
            if ok_earlier
            else "NEIN — kein robuster Vorteil gegenüber SM-Mapping."
        )
    )

    rec = f"""# Final recommendation — four-class regime (audit-only)

**Decision: {decision}** — {note}

## Best variant

`{best_id}` score={best['score']:.1f}

- March first no_long: **{best.get('march_first_no_long')}** (price {best.get('march_price_at_no_long')})
- Delay vs selloff onset (Mar5 16:00): **{dly} h**
- Delay vs Mar6 00:00: **{dly_mar6} h**
- March first strong_bearish: {best.get('march_first_strong_bear')} (delay vs selloff {best.get('march_delay_h_strong_vs_selloff')} h)
- Premature no_long bars on Mar5 (<16:00, close≥0.99): {prem}
- Clear-bear no_long recall: {best.get('no_long_recall_clear_bear')}
- Regime flips: {best.get('regime_flip_count')} / {best.get('n_bars')} bars
- Class dwell: bull={best.get('n_strong_bull')} bear={best.get('n_strong_bear')} range={best.get('n_range')} transition={best.get('n_transition')}
- Reasons at first no_long: {best.get('march_reasons_no_long')}
- SM at first no_long: {best.get('march_sm_at_no_long')}

## vs K0 (SM map)

- K0 first no_long: {k0.get('march_first_no_long')} delay_vs_selloff={k0_delay} h (vs Mar6={k0_delay_mar6} h)
- Earlier than SM path: **{ok_earlier}**

## Pflichtfrage März

{march_answer}

→ Details: `march_crash_timeline.csv`

## Next research (still no prod)

1. Freeze best classifier+hysteresis thresholds on more symbols
2. Keep SM lifecycle separate; regime layer only answers market location
3. Do not wire zones/entries yet
4. Optional: soft no_long from bearish_hint+transition for crash days

## Hashes unchanged

{json.dumps(hashes_after, indent=2)}
"""
    (OUT / "final_recommendation.md").write_text(rec, encoding="utf-8")

    readme = f"""# Market Regime Four-Class Audit (audit-only)

**Decision: {decision}** — {note}

Best: `{best_id}`

## Scope

- New audit only: `market_regime_four_class_audit.py`
- No productive `market_regime.py`
- SM not replaced; zones not used
- Hashes unchanged for structure/machine/policy/zones

## Classes

{list(CLASSES)}

## Variants

K0 SM-map · K1 EMA · K2 EMA+Progress · K3 Structure+EMA+Progress · K4 +Range
H0–H4 hysteresis — see `variant_configurations.json`

## Key March result (best)

| Event | Time | Price |
|---|---|---|
| first bearish_hint | {best.get('march_first_bearish_hint')} | |
| first no_long_context | {best.get('march_first_no_long')} | {best.get('march_price_at_no_long')} |
| first strong_bearish | {best.get('march_first_strong_bear')} | {best.get('march_price_at_strong')} |
| delay no_long vs selloff (Mar5 16:00) | {dly} h | |
| delay no_long vs Mar6 | {dly_mar6} h | |
| SM at no_long | {best.get('march_sm_at_no_long')} | |

## Pflichtfrage

{march_answer}

K0 delay vs selloff: {k0_delay} h

## Artifacts

See directory listing; primary: `variant_summary.csv`, `regime_timeline.csv`, `march_crash_timeline.csv`, `final_recommendation.md`
"""
    (OUT / "README.md").write_text(readme, encoding="utf-8")
    _write_json(
        OUT / "decision.json",
        {
            "decision": decision,
            "note": note,
            "march_answer": march_answer,
            "best": best,
            "k0": k0,
            "top5": by_score[:5],
            "gated_count": len(gated),
            "hashes": hashes_after,
            "export_ids": export_ids,
            "checks": {
                "ok_earlier": ok_earlier,
                "ok_during_selloff": ok_during_selloff,
                "ok_not_premature": ok_not_premature,
                "ok_recall": ok_recall,
                "ok_flip": ok_flip,
                "ok_range": ok_range,
                "gated_ok": gated_ok,
            },
        },
    )
    _p(f"DONE decision={decision} best={best_id}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", required=True, help="0|1|all")
    args = ap.parse_args()
    step = str(args.step).lower()
    if step == "all":
        step0()
        gc.collect()
        step1()
        return
    if step == "0":
        step0()
    elif step == "1":
        step1()
    else:
        raise SystemExit("step must be 0|1|all")


if __name__ == "__main__":
    main()
