"""C3.5c APT pattern diagnostic audit (research-only, descriptive).

Links historical A6 Exit-A trades to causal indicator / pullback / structure /
regime context and compares winners vs losers.

Does NOT change SM, C3.4B, Pine, entry/exit rules, or promote filters.
No live/forward/DB work. No commits.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd
from scipy import stats

from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.pullback_entry_c3_5 import apply_pullback_entry, config_hash
from research.regime_scanner.pullback_entry_c3_5_diagnostics import baseline_a6
from research.regime_scanner.pullback_entry_c3_5c_realized_outcome_audit import (
    _filled_sorted,
    trades_exit_a_opposite_entry,
)
from research.regime_scanner.pullback_entry_c3_5c_robustness_audit import (
    DEFAULT_BASELINE_DIR,
    WARMUP_CALENDAR_DAYS,
    annotate_trades,
    assign_split,
    build_extended_tf_frame,
    closed_only,
    discover_5m_span,
    fixed_chrono_splits,
    generate_exit_a_trades,
    outlier_metrics,
)
from research.regime_scanner.trend_regime_classification_audit import (
    C2_BASELINE_HASH,
    assert_baseline_readonly,
)
from research.regime_scanner.trend_weakening_multi_bar_audit import assert_safe_output_dir

DEFAULT_OUT = Path(
    "research/regime_scanner/results/phase_c3_5_pullback_entry_state_machine/"
    "c35c_pattern_diagnostic_audit"
)

SYMBOL = "APTUSDT"
TIMEFRAME = "15m"
VARIANT = "A6"
EVENT_WINDOW = 10  # relative bars -10..+10 around trigger
NET_COL = "net_return_0_20_pct"
GROSS_COL = "gross_return_pct"
MIN_GROUP_N = 3
NEAR_ZERO_SLOPE_ATR = 0.05  # |slope_atr| < this → near_zero bucket

# Numeric entry features evaluated for winner/loser separation (pre-entry only).
NUMERIC_FEATURE_COLS: tuple[str, ...] = (
    "adx",
    "adx_change_1",
    "adx_change_3",
    "adx_change_5",
    "adx_slope_3",
    "adx_slope_5",
    "adx_rising_bars_last5",
    "plus_di",
    "minus_di",
    "di_spread_signed",
    "di_spread_abs",
    "di_alignment_correct",
    "di_alignment_age",
    "ema9_slope_1",
    "ema9_slope_3",
    "ema9_slope_5",
    "ema20_slope_1",
    "ema20_slope_3",
    "ema20_slope_5",
    "ema50_slope_3",
    "ema50_slope_5",
    "ema9_slope_3_atr",
    "ema20_slope_3_atr",
    "ema9_minus_ema20_pct",
    "ema20_minus_ema50_pct",
    "ema_band_width_pct",
    "ema_order_aligned",
    "cross_age_bars",
    "cross_distance_pct",
    "price_to_ema9_pct",
    "price_to_ema20_pct",
    "price_to_ema50_pct",
    "pullback_touch_in_band",
    "bars_arm_to_pullback",
    "bars_pullback_to_ready",
    "bars_ready_to_trigger",
    "bars_arm_to_trigger",
    "pullback_depth_pct",
    "pullback_depth_atr",
    "pullback_duration_bars",
    "rejection_wick_ratio",
    "confirmation_body_ratio",
    "breakout_distance_pct",
    "breakout_distance_atr",
    "chase_distance_atr",
    "failed_candidates_since_last_entry",
    "bars_since_external_bos",
    "distance_to_bos_level_pct",
    "distance_to_bos_level_atr",
    "distance_to_protected_level_pct",
    "distance_to_protected_level_atr",
    "major_direction",
    "micro_direction",
    "major_micro_alignment",
    "bars_since_internal_bos",
    "bars_since_choch",
    "structure_event_density_10",
    "structure_event_density_20",
    "atr",
    "atr_pct",
    "atr_pct_rolling_rank",
    "vol_range_pct_5",
    "vol_range_pct_10",
    "vol_range_pct_20",
    "ret_3",
    "ret_5",
    "ret_10",
    "trend_aligned",
)


# ---------------------------------------------------------------------------
# Small stats helpers
# ---------------------------------------------------------------------------


def _finite(x: Any, default: float = float("nan")) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v):
        return default
    return v


def _side_sign(side: Any) -> int:
    if isinstance(side, (int, np.integer)):
        return int(side)
    s = str(side).lower()
    if s in {"long", "1", "+1"}:
        return 1
    if s in {"short", "-1"}:
        return -1
    return int(side)


def _pf(rets: pd.Series) -> float | None:
    r = pd.to_numeric(rets, errors="coerce").dropna()
    if r.empty:
        return None
    gp = float(r[r > 0].sum())
    gl = float((-r[r <= 0]).sum())
    if gl <= 1e-15:
        return None if gp <= 0 else float("inf")
    return gp / gl


def cliffs_delta(x: Sequence[float], y: Sequence[float]) -> float | None:
    """Cliff's delta: P(x>y) - P(x<y). Positive ⇒ x tends larger than y."""
    a = np.asarray([v for v in x if math.isfinite(v)], dtype=float)
    b = np.asarray([v for v in y if math.isfinite(v)], dtype=float)
    if len(a) == 0 or len(b) == 0:
        return None
    # Efficient pairwise via broadcasting for small n (trades ~30)
    diff = a[:, None] - b[None, :]
    return float(np.mean(np.sign(diff)))


def standardized_mean_diff(x: Sequence[float], y: Sequence[float]) -> float | None:
    a = np.asarray([v for v in x if math.isfinite(v)], dtype=float)
    b = np.asarray([v for v in y if math.isfinite(v)], dtype=float)
    if len(a) < 1 or len(b) < 1:
        return None
    pooled = math.sqrt(((a.std(ddof=1) ** 2 if len(a) > 1 else 0.0) + (b.std(ddof=1) ** 2 if len(b) > 1 else 0.0)) / 2.0)
    if pooled < 1e-15:
        return 0.0 if abs(a.mean() - b.mean()) < 1e-15 else None
    return float((a.mean() - b.mean()) / pooled)


def mannwhitney_p(x: Sequence[float], y: Sequence[float]) -> float | None:
    a = [v for v in x if math.isfinite(v)]
    b = [v for v in y if math.isfinite(v)]
    if len(a) < 2 or len(b) < 2:
        return None
    try:
        res = stats.mannwhitneyu(a, b, alternative="two-sided")
        return float(res.pvalue)
    except ValueError:
        return None


def content_hash_frames(dfs: Mapping[str, pd.DataFrame]) -> str:
    h = hashlib.sha256()
    for name in sorted(dfs.keys()):
        h.update(name.encode())
        df = dfs[name]
        if df is None or df.empty:
            h.update(b"empty")
            continue
        h.update(pd.util.hash_pandas_object(df.fillna("__NA__"), index=True).values.tobytes())
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Causal frame enrichment (precomputed once)
# ---------------------------------------------------------------------------


def enrich_diagnostic_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Add causal diagnostic columns. All shifts are backward-looking only."""
    df = frame.copy()
    n = len(df)
    close = pd.to_numeric(df["close"], errors="coerce").astype(float)
    high = pd.to_numeric(df["high"], errors="coerce").astype(float)
    low = pd.to_numeric(df["low"], errors="coerce").astype(float)
    open_ = pd.to_numeric(df["open"], errors="coerce").astype(float)
    adx = pd.to_numeric(df["adx"], errors="coerce").astype(float)
    plus_di = pd.to_numeric(df["plus_di"], errors="coerce").astype(float)
    minus_di = pd.to_numeric(df["minus_di"], errors="coerce").astype(float)
    atr = pd.to_numeric(df.get("atr_14", df.get("atr")), errors="coerce").astype(float)
    atr = atr.replace(0, np.nan)
    ema9 = pd.to_numeric(df["ema_9"], errors="coerce").astype(float)
    ema20 = pd.to_numeric(df["ema_20"], errors="coerce").astype(float)
    ema50 = pd.to_numeric(df["ema_50"], errors="coerce").astype(float)

    df["atr"] = atr
    df["atr_pct"] = atr / close * 100.0
    # Rolling past-only percentile of atr_pct (expanding rank / window)
    win = min(200, max(20, n // 5)) if n else 20
    # Past-only rank within rolling window (exclude current via shift)
    past = df["atr_pct"].shift(1)
    roll_min = past.rolling(win, min_periods=max(10, win // 5)).min()
    roll_max = past.rolling(win, min_periods=max(10, win // 5)).max()
    span = (roll_max - roll_min).replace(0, np.nan)
    df["atr_pct_rolling_rank"] = (df["atr_pct"] - roll_min) / span

    for k in (1, 3, 5):
        df[f"adx_change_{k}"] = adx - adx.shift(k)
        df[f"adx_slope_{k}"] = (adx - adx.shift(k)) / float(k)
        df[f"ema9_slope_{k}"] = ema9 - ema9.shift(k)
        df[f"ema20_slope_{k}"] = ema20 - ema20.shift(k)
        if k in (3, 5):
            df[f"ema50_slope_{k}"] = ema50 - ema50.shift(k)

    # ATR-normalized slopes
    for col in ("ema9_slope_1", "ema9_slope_3", "ema9_slope_5", "ema20_slope_1", "ema20_slope_3", "ema20_slope_5", "ema50_slope_3", "ema50_slope_5"):
        if col in df.columns:
            df[f"{col}_atr"] = df[col] / atr

    # Rising bars in last 5 (count of positive 1-bar ADX changes ending at t)
    adx_up = (adx.diff() > 0).astype(float)
    df["adx_rising_bars_last5"] = adx_up.rolling(5, min_periods=1).sum()

    df["di_spread_raw"] = plus_di - minus_di  # +DI minus -DI (unsigned vs side)
    df["ema9_minus_ema20_pct"] = (ema9 - ema20) / close * 100.0
    df["ema20_minus_ema50_pct"] = (ema20 - ema50) / close * 100.0
    band_lo = np.minimum(ema9, ema20)
    band_hi = np.maximum(ema9, ema20)
    df["ema_band_width_pct"] = (band_hi - band_lo) / close * 100.0
    df["price_to_ema9_pct"] = (close - ema9) / close * 100.0
    df["price_to_ema20_pct"] = (close - ema20) / close * 100.0
    df["price_to_ema50_pct"] = (close - ema50) / close * 100.0

    # EMA cross age (same idea as enrich_indicators)
    spread = ema9 - ema20
    prev = spread.shift(1)
    bear_cross = (spread < 0) & (prev >= 0)
    bull_cross = (spread > 0) & (prev <= 0)
    cross_dir = np.full(n, "", dtype=object)
    cross_age = np.full(n, np.nan)
    cross_dist = np.full(n, np.nan)
    last_bear = -10_000
    last_bull = -10_000
    last_bear_px = np.nan
    last_bull_px = np.nan
    for i in range(n):
        if bool(bear_cross.iloc[i]):
            last_bear = i
            last_bear_px = float(close.iloc[i])
        if bool(bull_cross.iloc[i]):
            last_bull = i
            last_bull_px = float(close.iloc[i])
        if spread.iloc[i] < 0 and last_bear >= 0:
            cross_dir[i] = "bear"
            cross_age[i] = i - last_bear
            if math.isfinite(last_bear_px) and last_bear_px != 0:
                cross_dist[i] = (float(close.iloc[i]) - last_bear_px) / last_bear_px * 100.0
        elif spread.iloc[i] > 0 and last_bull >= 0:
            cross_dir[i] = "bull"
            cross_age[i] = i - last_bull
            if math.isfinite(last_bull_px) and last_bull_px != 0:
                cross_dist[i] = (float(close.iloc[i]) - last_bull_px) / last_bull_px * 100.0
    df["cross_direction"] = cross_dir
    df["cross_age_bars"] = cross_age
    df["cross_distance_pct"] = cross_dist

    # DI alignment age (bars since DI favored current major direction sign of di_spread)
    di_fav_long = plus_di > minus_di
    align_age = np.zeros(n, dtype=float)
    run = 0
    prev_fav: bool | None = None
    for i in range(n):
        fav = bool(di_fav_long.iloc[i]) if math.isfinite(float(plus_di.iloc[i])) else None
        if fav is None:
            align_age[i] = np.nan
            continue
        if prev_fav is None or fav != prev_fav:
            run = 0
        else:
            run += 1
        align_age[i] = run
        prev_fav = fav
    df["di_alignment_age_raw"] = align_age

    # Structure ages
    def _bool_col(name: str) -> pd.Series:
        if name not in df.columns:
            return pd.Series(False, index=df.index)
        return df[name].fillna(False).infer_objects(copy=False).astype(bool)

    ext_up = _bool_col("arm_edge_external_bull")
    ext_dn = _bool_col("arm_edge_external_bear")
    int_up = _bool_col("arm_edge_internal_bull")
    int_dn = _bool_col("arm_edge_internal_bear")
    choch_up = _bool_col("arm_edge_choch_bull")
    choch_dn = _bool_col("arm_edge_choch_bear")

    def _age_since(mask: pd.Series) -> np.ndarray:
        age = np.full(n, np.nan)
        last = -1
        for i in range(n):
            if bool(mask.iloc[i]):
                last = i
            if last >= 0:
                age[i] = i - last
        return age

    df["bars_since_external_bos_any"] = _age_since(ext_up | ext_dn)
    df["bars_since_external_bos_up"] = _age_since(ext_up)
    df["bars_since_external_bos_down"] = _age_since(ext_dn)
    df["bars_since_internal_bos_any"] = _age_since(int_up | int_dn)
    df["bars_since_internal_bos_up"] = _age_since(int_up)
    df["bars_since_internal_bos_down"] = _age_since(int_dn)
    df["bars_since_choch_any"] = _age_since(choch_up | choch_dn)
    df["bars_since_choch_up"] = _age_since(choch_up)
    df["bars_since_choch_down"] = _age_since(choch_dn)
    df["last_internal_bos_side"] = np.where(int_up, "bull", np.where(int_dn, "bear", ""))
    # forward-fill last side
    last_side = []
    cur = ""
    for i in range(n):
        if int_up.iloc[i]:
            cur = "bull"
        elif int_dn.iloc[i]:
            cur = "bear"
        last_side.append(cur)
    df["last_internal_bos_side"] = last_side
    last_choch = []
    cur = ""
    for i in range(n):
        if choch_up.iloc[i]:
            cur = "bull"
        elif choch_dn.iloc[i]:
            cur = "bear"
        last_choch.append(cur)
    df["last_choch_side"] = last_choch

    struct_evt = (
        ext_up.astype(int)
        + ext_dn.astype(int)
        + int_up.astype(int)
        + int_dn.astype(int)
        + choch_up.astype(int)
        + choch_dn.astype(int)
    )
    df["structure_event_density_10"] = struct_evt.rolling(10, min_periods=1).sum()
    df["structure_event_density_20"] = struct_evt.rolling(20, min_periods=1).sum()

    # Micro direction proxy from swing vs close
    msh = pd.to_numeric(df.get("micro_swing_high"), errors="coerce")
    msl = pd.to_numeric(df.get("micro_swing_low"), errors="coerce")
    micro_dir = np.zeros(n, dtype=int)
    for i in range(n):
        c = float(close.iloc[i])
        hi = float(msh.iloc[i]) if i < len(msh) and math.isfinite(_finite(msh.iloc[i])) else np.nan
        lo = float(msl.iloc[i]) if i < len(msl) and math.isfinite(_finite(msl.iloc[i])) else np.nan
        if math.isfinite(hi) and c >= hi:
            micro_dir[i] = 1
        elif math.isfinite(lo) and c <= lo:
            micro_dir[i] = -1
        elif i > 0:
            micro_dir[i] = micro_dir[i - 1]
    df["micro_direction"] = micro_dir

    maj = pd.to_numeric(df.get("major_direction"), errors="coerce").fillna(0).astype(int)
    df["major_micro_alignment"] = (np.sign(maj.to_numpy()) == np.sign(micro_dir)).astype(float)
    df.loc[maj.to_numpy() == 0, "major_micro_alignment"] = np.nan

    # Volatility / returns
    rng = (high - low) / close * 100.0
    df["vol_range_pct_5"] = rng.rolling(5, min_periods=1).mean()
    df["vol_range_pct_10"] = rng.rolling(10, min_periods=1).mean()
    df["vol_range_pct_20"] = rng.rolling(20, min_periods=1).mean()
    ret1 = close.pct_change(fill_method=None) * 100.0
    df["ret_3"] = ret1.rolling(3, min_periods=1).sum()
    df["ret_5"] = ret1.rolling(5, min_periods=1).sum()
    df["ret_10"] = ret1.rolling(10, min_periods=1).sum()

    # Candle geometry
    body = (close - open_).abs()
    full = (high - low).replace(0, np.nan)
    upper_wick = high - np.maximum(open_, close)
    lower_wick = np.minimum(open_, close) - low
    df["body_ratio"] = body / full
    df["upper_wick_ratio"] = upper_wick / full
    df["lower_wick_ratio"] = lower_wick / full

    # Regime proxies (descriptive only; not C2 SM)
    df["adx_regime"] = np.where(adx >= 25, "trend", "range")
    pss = df["protected_structure_state"].astype(str) if "protected_structure_state" in df.columns else pd.Series([""] * n)
    df["structure_state"] = pss
    df["regime"] = df["adx_regime"]

    df["pullback_touch_in_band"] = ((low <= band_hi) & (high >= band_lo)).astype(float)
    return df


# ---------------------------------------------------------------------------
# Trade labeling / outcomes
# ---------------------------------------------------------------------------


def label_trades(trades: pd.DataFrame, splits: Mapping[str, Any]) -> pd.DataFrame:
    """Annotate closed/open trades with outcome labels. Does not drop rows."""
    if trades.empty:
        return trades
    out = annotate_trades(trades, window_name="extended", splits=splits)
    out["entry_timestamp"] = pd.to_datetime(out["entry_timestamp"], utc=True)
    out["exit_timestamp"] = pd.to_datetime(out["exit_timestamp"], utc=True)
    out["month"] = out["entry_timestamp"].dt.strftime("%Y-%m")
    bar_minutes = float(out.attrs.get("bar_minutes", 15.0)) if hasattr(out, "attrs") else 15.0
    if "timeframe" in out.columns and len(out):
        tf0 = str(out["timeframe"].iloc[0])
        bar_minutes = {"5m": 5.0, "15m": 15.0, "1h": 60.0, "4h": 240.0}.get(tf0, 15.0)
    out["holding_minutes"] = out["holding_bars"].astype(float) * bar_minutes
    # net aliases
    if NET_COL not in out.columns and "net_return_0_20_pct" in out.columns:
        out[NET_COL] = out["net_return_0_20_pct"]
    out["gross_return_pct"] = out[GROSS_COL].astype(float)
    out["net_return_020_pct"] = out[NET_COL].astype(float)
    closed = out["closed"] == True  # noqa: E712
    out["winner_net020"] = False
    out["loser_net020"] = False
    out.loc[closed, "winner_net020"] = out.loc[closed, NET_COL] > 0
    out.loc[closed, "loser_net020"] = out.loc[closed, NET_COL] <= 0
    # Top-1 / Top-3 among closed only
    out["top1_trade"] = False
    out["top3_trade"] = False
    if closed.any():
        ranked = out.loc[closed, NET_COL].astype(float).sort_values(ascending=False)
        top_idx = list(ranked.index[:3])
        if top_idx:
            out.loc[top_idx[0], "top1_trade"] = True
        out.loc[top_idx, "top3_trade"] = True
    out["winner_without_top3_context"] = out["winner_net020"] & (~out["top3_trade"])
    # clipped return (descriptive)
    if closed.any():
        p95 = float(out.loc[closed, NET_COL].quantile(0.95))
        p05 = float(out.loc[closed, NET_COL].quantile(0.05))
        out["return_clipped_p95"] = out[NET_COL].clip(lower=p05, upper=p95)
    else:
        out["return_clipped_p95"] = out[NET_COL]
    med = float(out.loc[closed, NET_COL].median()) if closed.any() else 0.0
    out["above_median_net020"] = closed & (out[NET_COL] > med)
    out["below_median_net020"] = closed & (out[NET_COL] <= med)
    # rename MFE/MAE if present
    if "maximum_favorable_pct" in out.columns:
        out["mfe_pct"] = out["maximum_favorable_pct"]
        out["mae_pct"] = out["maximum_adverse_pct"]
    out["trade_id"] = [
        f"{pd.Timestamp(r.entry_timestamp).isoformat()}_{r.side}_{r.setup_id}"
        for r in out.itertuples(index=False)
    ]
    return out


def enrich_filled_from_entries(
    filled: Sequence[Mapping[str, Any]],
    entries: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Attach SM entry snapshot fields (pullback extremes, armed_price) onto fills."""
    by_setup = {int(e["setup_id"]): e for e in entries if e.get("setup_id") is not None}
    out: list[dict[str, Any]] = []
    for f in filled:
        row = dict(f)
        sid = f.get("setup_id")
        if sid is not None and int(sid) in by_setup:
            e = by_setup[int(sid)]
            for k in ("pullback_high", "pullback_low", "armed_price", "entry_reason", "adx", "atr_14"):
                if k in e and row.get(k) is None:
                    row[k] = e.get(k)
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# Feature extraction at trigger (pre-fill)
# ---------------------------------------------------------------------------


def _lifecycle_map(lives: Sequence[Mapping[str, Any]]) -> dict[int, dict[str, Any]]:
    return {int(x["setup_id"]): dict(x) for x in lives if x.get("setup_id") is not None}


def _failed_since_last_entry(
    lives: Sequence[Mapping[str, Any]],
    *,
    current_arm_bar: int | None,
    current_setup_id: int | None,
) -> int:
    if current_arm_bar is None:
        return 0
    # last successful fill bar before this arm
    filled = [x for x in lives if x.get("entry_created") and x.get("fill_bar") is not None]
    prev_fill = -1
    for x in filled:
        fb = int(x["fill_bar"])
        if fb < current_arm_bar:
            prev_fill = max(prev_fill, fb)
    n_fail = 0
    for x in lives:
        if current_setup_id is not None and int(x.get("setup_id") or -1) == int(current_setup_id):
            continue
        if x.get("entry_created"):
            continue
        ab = x.get("armed_bar")
        tb = x.get("terminal_bar")
        if ab is None:
            continue
        ab = int(ab)
        if ab <= prev_fill:
            continue
        if ab >= current_arm_bar:
            continue
        # ended without entry
        if tb is not None and int(tb) < current_arm_bar:
            n_fail += 1
        elif tb is None and not x.get("entry_created"):
            n_fail += 1
    return n_fail


def extract_trade_features(
    frame: pd.DataFrame,
    trades: pd.DataFrame,
    filled: Sequence[Mapping[str, Any]],
    lives: Sequence[Mapping[str, Any]],
) -> pd.DataFrame:
    """One row per trade with causal features at trigger bar (relative_bar=0)."""
    if trades.empty:
        return trades
    life_by_id = _lifecycle_map(lives)
    fill_by_key: dict[tuple[Any, str], dict[str, Any]] = {}
    for f in filled:
        key = (pd.Timestamp(f["fill_timestamp"]), f["side_name"])
        fill_by_key[key] = dict(f)

    rows: list[dict[str, Any]] = []
    for _, t in trades.iterrows():
        side = _side_sign(t["side"])
        et = pd.Timestamp(t["entry_timestamp"])
        fill = fill_by_key.get((et, t["side"]))
        # fallback match by setup_id
        if fill is None and t.get("setup_id") is not None:
            for f in filled:
                if f.get("setup_id") == t.get("setup_id") and f["side_name"] == t["side"]:
                    fill = f
                    break
        if fill is None:
            row = t.to_dict()
            row["feature_ok"] = False
            row["missing_reason"] = "fill_not_matched"
            rows.append(row)
            continue

        trigger_i = int(fill["trigger_bar"])
        fill_i = int(fill["fill_bar"])
        assert fill_i == trigger_i + 1, "fill must be next open after trigger"
        # Features use trigger close bar only (no fill bar open leakage beyond known entry price)
        fr = frame.iloc[trigger_i]
        life = life_by_id.get(int(t["setup_id"])) if pd.notna(t.get("setup_id")) else None
        if life is None:
            # try by trigger bar
            for L in lives:
                if L.get("trigger_bar") == trigger_i and L.get("direction") == ("long" if side > 0 else "short"):
                    life = L
                    break

        atr = max(_finite(fr.get("atr")), 1e-12)
        close = _finite(fr.get("close"))
        armed_px = _finite(life.get("armed_price") if life else fill.get("armed_price") if fill else np.nan)
        # pullback extremes from entry snapshot if present
        pb_hi = _finite(fill.get("pullback_high")) if fill.get("pullback_high") is not None else np.nan
        pb_lo = _finite(fill.get("pullback_low")) if fill.get("pullback_low") is not None else np.nan
        # from timeline row of trigger if available via frame — not stored; use life bars

        arm_bar = int(life["armed_bar"]) if life and life.get("armed_bar") is not None else None
        pb_bar = int(life["pullback_bar"]) if life and life.get("pullback_bar") is not None else None
        ready_bar = int(life["ready_bar"]) if life and life.get("ready_bar") is not None else None

        # Reconstruct pullback high/low from bars arm..ready if needed
        if life and arm_bar is not None:
            end_pb = ready_bar if ready_bar is not None else trigger_i
            start_pb = pb_bar if pb_bar is not None else arm_bar
            window = frame.iloc[start_pb : end_pb + 1]
            if len(window):
                if not math.isfinite(pb_hi):
                    pb_hi = float(window["high"].max())
                if not math.isfinite(pb_lo):
                    pb_lo = float(window["low"].min())

        if side < 0:
            depth_pct = (pb_hi - close) / close * 100.0 if math.isfinite(pb_hi) else np.nan
            depth_atr = (pb_hi - close) / atr if math.isfinite(pb_hi) else np.nan
            brk_level = pb_lo
            wick_ratio = _finite(fr.get("upper_wick_ratio"))
            di_signed = _finite(fr.get("minus_di")) - _finite(fr.get("plus_di"))
            di_align = 1.0 if _finite(fr.get("minus_di")) > _finite(fr.get("plus_di")) else 0.0
            ema_order = 1.0 if (
                _finite(fr.get("ema_9")) < _finite(fr.get("ema_20")) < _finite(fr.get("ema_50"))
            ) else 0.0
            # directional slopes: positive = in trade direction (down for short)
            dir_ema9_s3 = -_finite(fr.get("ema9_slope_3"))
            dir_ema20_s3 = -_finite(fr.get("ema20_slope_3"))
            dir_ema9_s3_atr = -_finite(fr.get("ema9_slope_3_atr"))
            bos_age = _finite(fr.get("bars_since_external_bos_down"))
            int_age = _finite(fr.get("bars_since_internal_bos_down"))
            choch_age = _finite(fr.get("bars_since_choch_down"))
            prot = _finite(fr.get("protected_high"))
            dist_prot_pct = (prot - close) / close * 100.0 if math.isfinite(prot) else np.nan
            dist_prot_atr = (prot - close) / atr if math.isfinite(prot) else np.nan
            cross_ok = str(fr.get("cross_direction")) == "bear"
        else:
            depth_pct = (close - pb_lo) / close * 100.0 if math.isfinite(pb_lo) else np.nan
            depth_atr = (close - pb_lo) / atr if math.isfinite(pb_lo) else np.nan
            brk_level = pb_hi
            wick_ratio = _finite(fr.get("lower_wick_ratio"))
            di_signed = _finite(fr.get("plus_di")) - _finite(fr.get("minus_di"))
            di_align = 1.0 if _finite(fr.get("plus_di")) > _finite(fr.get("minus_di")) else 0.0
            ema_order = 1.0 if (
                _finite(fr.get("ema_9")) > _finite(fr.get("ema_20")) > _finite(fr.get("ema_50"))
            ) else 0.0
            dir_ema9_s3 = _finite(fr.get("ema9_slope_3"))
            dir_ema20_s3 = _finite(fr.get("ema20_slope_3"))
            dir_ema9_s3_atr = _finite(fr.get("ema9_slope_3_atr"))
            bos_age = _finite(fr.get("bars_since_external_bos_up"))
            int_age = _finite(fr.get("bars_since_internal_bos_up"))
            choch_age = _finite(fr.get("bars_since_choch_up"))
            prot = _finite(fr.get("protected_low"))
            dist_prot_pct = (close - prot) / close * 100.0 if math.isfinite(prot) else np.nan
            dist_prot_atr = (close - prot) / atr if math.isfinite(prot) else np.nan
            cross_ok = str(fr.get("cross_direction")) == "bull"

        brk_dist_pct = abs(close - brk_level) / close * 100.0 if math.isfinite(brk_level) else np.nan
        brk_dist_atr = abs(close - brk_level) / atr if math.isfinite(brk_level) else np.nan
        chase = abs(close - armed_px) / atr if math.isfinite(armed_px) else np.nan
        bos_level = _finite(fr.get("active_external_break_level"))
        dist_bos_pct = abs(close - bos_level) / close * 100.0 if math.isfinite(bos_level) else np.nan
        dist_bos_atr = abs(close - bos_level) / atr if math.isfinite(bos_level) else np.nan

        maj = int(fr.get("major_direction") or 0)
        micro = int(fr.get("micro_direction") or 0)
        trend_aligned = 1.0 if maj != 0 and np.sign(maj) == np.sign(side) else (0.0 if maj != 0 else np.nan)

        def _bars(a: int | None, b: int | None) -> float:
            if a is None or b is None:
                return np.nan
            return float(b - a)

        row = t.to_dict()
        row.update(
            {
                "feature_ok": True,
                "feature_asof": "trigger_close",
                "post_entry_used_as_entry_feature": False,
                "trigger_bar": trigger_i,
                "fill_bar": fill_i,
                "entry_is_next_open": True,
                "adx": _finite(fr.get("adx")),
                "adx_change_1": _finite(fr.get("adx_change_1")),
                "adx_change_3": _finite(fr.get("adx_change_3")),
                "adx_change_5": _finite(fr.get("adx_change_5")),
                "adx_slope_3": _finite(fr.get("adx_slope_3")),
                "adx_slope_5": _finite(fr.get("adx_slope_5")),
                "adx_rising_bars_last5": _finite(fr.get("adx_rising_bars_last5")),
                "plus_di": _finite(fr.get("plus_di")),
                "minus_di": _finite(fr.get("minus_di")),
                "di_spread_signed": di_signed,
                "di_spread_abs": abs(di_signed) if math.isfinite(di_signed) else np.nan,
                "di_alignment_correct": di_align,
                "di_alignment_age": _finite(fr.get("di_alignment_age_raw")),
                "adx_bucket": _adx_bucket(_finite(fr.get("adx"))),
                "adx_rising_flag": "rising" if _finite(fr.get("adx_change_1")) > 0 else "flat_or_falling",
                "ema9": _finite(fr.get("ema_9")),
                "ema20": _finite(fr.get("ema_20")),
                "ema50": _finite(fr.get("ema_50")),
                "ema9_slope_1": _finite(fr.get("ema9_slope_1")),
                "ema9_slope_3": _finite(fr.get("ema9_slope_3")),
                "ema9_slope_5": _finite(fr.get("ema9_slope_5")),
                "ema20_slope_1": _finite(fr.get("ema20_slope_1")),
                "ema20_slope_3": _finite(fr.get("ema20_slope_3")),
                "ema20_slope_5": _finite(fr.get("ema20_slope_5")),
                "ema50_slope_3": _finite(fr.get("ema50_slope_3")),
                "ema50_slope_5": _finite(fr.get("ema50_slope_5")),
                "ema9_slope_3_atr": _finite(fr.get("ema9_slope_3_atr")),
                "ema20_slope_3_atr": _finite(fr.get("ema20_slope_3_atr")),
                "dir_ema9_slope_3": dir_ema9_s3,
                "dir_ema20_slope_3": dir_ema20_s3,
                "dir_ema9_slope_3_atr": dir_ema9_s3_atr,
                "ema9_minus_ema20_pct": _finite(fr.get("ema9_minus_ema20_pct")),
                "ema20_minus_ema50_pct": _finite(fr.get("ema20_minus_ema50_pct")),
                "ema_band_width_pct": _finite(fr.get("ema_band_width_pct")),
                "ema_order_aligned": ema_order,
                "cross_direction": fr.get("cross_direction"),
                "cross_direction_aligned": 1.0 if cross_ok else 0.0,
                "cross_age_bars": _finite(fr.get("cross_age_bars")),
                "cross_distance_pct": _finite(fr.get("cross_distance_pct")),
                "cross_age_bucket": _cross_age_bucket(_finite(fr.get("cross_age_bars")), fr.get("cross_direction")),
                "price_to_ema9_pct": _finite(fr.get("price_to_ema9_pct")),
                "price_to_ema20_pct": _finite(fr.get("price_to_ema20_pct")),
                "price_to_ema50_pct": _finite(fr.get("price_to_ema50_pct")),
                "pullback_touch_in_band": _finite(fr.get("pullback_touch_in_band")),
                "bars_arm_to_pullback": _bars(arm_bar, pb_bar),
                "bars_pullback_to_ready": _bars(pb_bar, ready_bar),
                "bars_ready_to_trigger": _bars(ready_bar, trigger_i),
                "bars_arm_to_trigger": _bars(arm_bar, trigger_i),
                "pullback_depth_pct": depth_pct,
                "pullback_depth_atr": depth_atr,
                "pullback_duration_bars": _bars(pb_bar, ready_bar if ready_bar is not None else trigger_i),
                "rejection_wick_ratio": wick_ratio,
                "confirmation_body_ratio": _finite(fr.get("body_ratio")),
                "breakout_distance_pct": brk_dist_pct,
                "breakout_distance_atr": brk_dist_atr,
                "chase_distance_atr": chase,
                "failed_candidates_since_last_entry": _failed_since_last_entry(
                    lives, current_arm_bar=arm_bar, current_setup_id=int(t["setup_id"]) if pd.notna(t.get("setup_id")) else None
                ),
                "arming_event_type": (life or {}).get("arming_type") or "external_bos",
                "external_bos_side": "bear" if side < 0 else "bull",
                "bars_since_external_bos": bos_age,
                "external_bos_level": bos_level,
                "distance_to_bos_level_pct": dist_bos_pct,
                "distance_to_bos_level_atr": dist_bos_atr,
                "protected_high": _finite(fr.get("protected_high")),
                "protected_low": _finite(fr.get("protected_low")),
                "distance_to_protected_level_pct": dist_prot_pct,
                "distance_to_protected_level_atr": dist_prot_atr,
                "major_direction": float(maj),
                "micro_direction": float(micro),
                "major_micro_alignment": _finite(fr.get("major_micro_alignment")),
                "last_internal_bos_side": fr.get("last_internal_bos_side"),
                "bars_since_internal_bos": int_age,
                "last_choch_side": fr.get("last_choch_side"),
                "bars_since_choch": choch_age,
                "structure_event_density_10": _finite(fr.get("structure_event_density_10")),
                "structure_event_density_20": _finite(fr.get("structure_event_density_20")),
                "structure_state": fr.get("structure_state"),
                "regime": fr.get("regime"),
                "adx_regime": fr.get("adx_regime"),
                "atr": atr,
                "atr_pct": _finite(fr.get("atr_pct")),
                "atr_pct_rolling_rank": _finite(fr.get("atr_pct_rolling_rank")),
                "vol_range_pct_5": _finite(fr.get("vol_range_pct_5")),
                "vol_range_pct_10": _finite(fr.get("vol_range_pct_10")),
                "vol_range_pct_20": _finite(fr.get("vol_range_pct_20")),
                "ret_3": _finite(fr.get("ret_3")) * (1 if side > 0 else -1),  # signed in trade direction
                "ret_5": _finite(fr.get("ret_5")) * (1 if side > 0 else -1),
                "ret_10": _finite(fr.get("ret_10")) * (1 if side > 0 else -1),
                "trend_aligned": trend_aligned,
                "dir_ema_slope_bucket": _slope_bucket(dir_ema9_s3_atr),
            }
        )
        rows.append(row)
    return pd.DataFrame(rows)


def _adx_bucket(adx: float) -> str:
    if not math.isfinite(adx):
        return "missing"
    if adx < 20:
        return "<20"
    if adx < 25:
        return "20-25"
    if adx < 30:
        return "25-30"
    return ">=30"


def _cross_age_bucket(age: float, direction: Any) -> str:
    if not direction or direction == "":
        return "no_valid_cross"
    if not math.isfinite(age):
        return "no_valid_cross"
    if age <= 2:
        return "0-2"
    if age <= 5:
        return "3-5"
    if age <= 10:
        return "6-10"
    if age <= 20:
        return "11-20"
    return ">20"


def _slope_bucket(slope_atr: float) -> str:
    if not math.isfinite(slope_atr):
        return "missing"
    if slope_atr < -NEAR_ZERO_SLOPE_ATR:
        return "against"  # negative after direction normalize = against trade
    if slope_atr > NEAR_ZERO_SLOPE_ATR:
        return "with"  # positive = in trade direction
    return "near_zero"


# ---------------------------------------------------------------------------
# Event-aligned panel
# ---------------------------------------------------------------------------


def build_event_aligned_panel(
    frame: pd.DataFrame,
    panel: pd.DataFrame,
    *,
    window: int = EVENT_WINDOW,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    closed = panel[panel["closed"] == True] if "closed" in panel.columns else panel  # noqa: E712
    for _, t in closed.iterrows():
        if not bool(t.get("feature_ok", True)):
            continue
        if pd.isna(t.get("trigger_bar")):
            continue
        ti = int(t["trigger_bar"])
        side = _side_sign(t["side"])
        for rel in range(-window, window + 1):
            j = ti + rel
            if j < 0 or j >= len(frame):
                continue
            fr = frame.iloc[j]
            if side < 0:
                di_spread = _finite(fr.get("minus_di")) - _finite(fr.get("plus_di"))
                ema9_s = -_finite(fr.get("ema9_slope_3"))
                ema20_s = -_finite(fr.get("ema20_slope_3"))
            else:
                di_spread = _finite(fr.get("plus_di")) - _finite(fr.get("minus_di"))
                ema9_s = _finite(fr.get("ema9_slope_3"))
                ema20_s = _finite(fr.get("ema20_slope_3"))
            rows.append(
                {
                    "trade_id": t["trade_id"],
                    "relative_bar": rel,
                    "timestamp": fr.get("timestamp"),
                    "side": t["side"],
                    "split": t.get("split"),
                    "winner_net020": bool(t.get("winner_net020")),
                    "net_return_020_pct": t.get("net_return_020_pct"),
                    "pre_entry": rel <= 0,
                    "post_entry": rel > 0,
                    "adx": _finite(fr.get("adx")),
                    "plus_di": _finite(fr.get("plus_di")),
                    "minus_di": _finite(fr.get("minus_di")),
                    "directional_di_spread": di_spread,
                    "ema9_slope": ema9_s,
                    "ema20_slope": ema20_s,
                    "ema_band_width_pct": _finite(fr.get("ema_band_width_pct")),
                    "price_to_ema20_pct": _finite(fr.get("price_to_ema20_pct")),
                    "atr_pct": _finite(fr.get("atr_pct")),
                    "major_direction": fr.get("major_direction"),
                    "micro_direction": fr.get("micro_direction"),
                    "regime": fr.get("regime"),
                }
            )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Summaries / buckets / candidates
# ---------------------------------------------------------------------------


def winner_loser_summary(panel: pd.DataFrame, features: Sequence[str] = NUMERIC_FEATURE_COLS) -> pd.DataFrame:
    closed = panel[panel["closed"] == True].copy()  # noqa: E712
    w = closed[closed["winner_net020"] == True]  # noqa: E712
    l = closed[closed["loser_net020"] == True]  # noqa: E712
    rows = []
    for feat in features:
        if feat not in closed.columns:
            continue
        wv = pd.to_numeric(w[feat], errors="coerce")
        lv = pd.to_numeric(l[feat], errors="coerce")
        smd = standardized_mean_diff(wv.dropna().tolist(), lv.dropna().tolist())
        cd = cliffs_delta(wv.dropna().tolist(), lv.dropna().tolist())
        # positive smd ⇒ winners have higher feature values
        direction = "winners_higher" if (smd or 0) > 0 else ("winners_lower" if (smd or 0) < 0 else "none")
        rows.append(
            {
                "feature": feat,
                "n_winner": int(wv.notna().sum()),
                "n_loser": int(lv.notna().sum()),
                "mean_winner": float(wv.mean()) if wv.notna().any() else None,
                "mean_loser": float(lv.mean()) if lv.notna().any() else None,
                "median_winner": float(wv.median()) if wv.notna().any() else None,
                "median_loser": float(lv.median()) if lv.notna().any() else None,
                "q25_winner": float(wv.quantile(0.25)) if wv.notna().any() else None,
                "q75_winner": float(wv.quantile(0.75)) if wv.notna().any() else None,
                "q25_loser": float(lv.quantile(0.25)) if lv.notna().any() else None,
                "q75_loser": float(lv.quantile(0.75)) if lv.notna().any() else None,
                "standardized_mean_difference": smd,
                "cliffs_delta": cd,
                "mannwhitney_p": mannwhitney_p(wv.dropna().tolist(), lv.dropna().tolist()),
                "missing_count": int(closed[feat].isna().sum()) if feat in closed.columns else None,
                "separation_direction": direction,
            }
        )
    return pd.DataFrame(rows)


def split_feature_direction(panel: pd.DataFrame, features: Sequence[str] = NUMERIC_FEATURE_COLS) -> pd.DataFrame:
    closed = panel[panel["closed"] == True].copy()  # noqa: E712
    rows = []
    for feat in features:
        if feat not in closed.columns:
            continue
        per_split = {}
        for sp in ("development", "validation", "oos"):
            sub = closed[closed["split"] == sp]
            w = pd.to_numeric(sub.loc[sub["winner_net020"] == True, feat], errors="coerce")  # noqa: E712
            l = pd.to_numeric(sub.loc[sub["loser_net020"] == True, feat], errors="coerce")  # noqa: E712
            mw = float(w.median()) if w.notna().any() else np.nan
            ml = float(l.median()) if l.notna().any() else np.nan
            diff = mw - ml if math.isfinite(mw) and math.isfinite(ml) else np.nan
            direction = "winners_higher" if diff > 0 else ("winners_lower" if diff < 0 else "none")
            per_split[sp] = {
                "median_winner": mw,
                "median_loser": ml,
                "difference": diff,
                "effect_direction": direction,
                "n": int(len(sub)),
                "n_winner": int(w.notna().sum()),
                "n_loser": int(l.notna().sum()),
            }
        dirs = [per_split[s]["effect_direction"] for s in ("development", "validation", "oos")]
        same_dv = dirs[0] == dirs[1] and dirs[0] != "none"
        same_all = len(set(d for d in dirs if d != "none")) == 1 and dirs[0] != "none"
        score = sum(1 for d in dirs if d == dirs[0] and d != "none") / 3.0
        rows.append(
            {
                "feature": feat,
                "dev_median_winner": per_split["development"]["median_winner"],
                "dev_median_loser": per_split["development"]["median_loser"],
                "dev_difference": per_split["development"]["difference"],
                "dev_direction": per_split["development"]["effect_direction"],
                "dev_n": per_split["development"]["n"],
                "val_median_winner": per_split["validation"]["median_winner"],
                "val_median_loser": per_split["validation"]["median_loser"],
                "val_difference": per_split["validation"]["difference"],
                "val_direction": per_split["validation"]["effect_direction"],
                "val_n": per_split["validation"]["n"],
                "oos_median_winner": per_split["oos"]["median_winner"],
                "oos_median_loser": per_split["oos"]["median_loser"],
                "oos_difference": per_split["oos"]["difference"],
                "oos_direction": per_split["oos"]["effect_direction"],
                "oos_n": per_split["oos"]["n"],
                "same_direction_dev_val": same_dv,
                "same_direction_all_splits": same_all,
                "sign_stability_score": score,
            }
        )
    return pd.DataFrame(rows)


def pre_signal_adx_path(event_panel: pd.DataFrame) -> pd.DataFrame:
    if event_panel.empty:
        return event_panel
    rows = []
    for rel, g in event_panel.groupby("relative_bar"):
        for win_flag, sub in ((True, g[g["winner_net020"] == True]), (False, g[g["winner_net020"] == False])):  # noqa: E712
            rows.append(
                {
                    "relative_bar": int(rel),
                    "winner_net020": win_flag,
                    "pre_entry": bool(rel <= 0),
                    "post_entry": bool(rel > 0),
                    "n": int(len(sub)),
                    "adx_mean": float(sub["adx"].mean()) if len(sub) else None,
                    "adx_median": float(sub["adx"].median()) if len(sub) else None,
                    "di_spread_mean": float(sub["directional_di_spread"].mean()) if len(sub) else None,
                    "di_spread_median": float(sub["directional_di_spread"].median()) if len(sub) else None,
                }
            )
    return pd.DataFrame(rows).sort_values(["relative_bar", "winner_net020"])


def _bucket_outcome_table(panel: pd.DataFrame, bucket_col: str) -> pd.DataFrame:
    closed = panel[panel["closed"] == True].copy()  # noqa: E712
    rows = []
    for b, g in closed.groupby(bucket_col, dropna=False):
        net = g[NET_COL].astype(float)
        rows.append(
            {
                "bucket": b,
                "n": int(len(g)),
                "winrate": float((net > 0).mean()) if len(g) else None,
                "mean_net_0_20": float(net.mean()) if len(g) else None,
                "median_net_0_20": float(net.median()) if len(g) else None,
                "sum_net_0_20": float(net.sum()) if len(g) else None,
                "profit_factor": _pf(net),
                "mean_mfe": float(g["mfe_pct"].mean()) if "mfe_pct" in g and len(g) else None,
                "mean_mae": float(g["mae_pct"].mean()) if "mae_pct" in g and len(g) else None,
            }
        )
    return pd.DataFrame(rows)


def pullback_context_summary(panel: pd.DataFrame) -> pd.DataFrame:
    """Quantile groups from Development only; applied unchanged to Val/OOS."""
    closed = panel[panel["closed"] == True].copy()  # noqa: E712
    dev = closed[closed["split"] == "development"]
    rows = []
    for feat in ("pullback_depth_atr", "pullback_duration_bars", "chase_distance_atr"):
        if feat not in closed.columns or dev[feat].notna().sum() < 3:
            continue
        q33 = float(dev[feat].quantile(0.33))
        q66 = float(dev[feat].quantile(0.66))

        def _lab(v: float) -> str:
            if not math.isfinite(v):
                return "missing"
            if v <= q33:
                return "low_dev_q33"
            if v <= q66:
                return "mid_dev_q33_q66"
            return "high_dev_q66"

        closed[f"{feat}_group"] = closed[feat].map(_lab)
        for sp, gsp in closed.groupby("split"):
            for b, g in gsp.groupby(f"{feat}_group"):
                net = g[NET_COL].astype(float)
                rows.append(
                    {
                        "feature": feat,
                        "group": b,
                        "split": sp,
                        "dev_q33": q33,
                        "dev_q66": q66,
                        "n": int(len(g)),
                        "winrate": float((net > 0).mean()) if len(g) else None,
                        "mean_net_0_20": float(net.mean()) if len(g) else None,
                        "median_net_0_20": float(net.median()) if len(g) else None,
                        "profit_factor": _pf(net),
                    }
                )
    return pd.DataFrame(rows)


def structure_context_by_outcome(panel: pd.DataFrame) -> pd.DataFrame:
    closed = panel[panel["closed"] == True].copy()  # noqa: E712
    rows = []

    def _add(name: str, series: pd.Series) -> None:
        tmp = closed.copy()
        tmp["_g"] = series
        for b, g in tmp.groupby("_g", dropna=False):
            net = g[NET_COL].astype(float)
            rows.append(
                {
                    "dimension": name,
                    "group": b,
                    "n": int(len(g)),
                    "winrate": float((net > 0).mean()) if len(g) else None,
                    "mean_net_0_20": float(net.mean()) if len(g) else None,
                    "median_net_0_20": float(net.median()) if len(g) else None,
                    "profit_factor": _pf(net),
                    "n_winners": int((net > 0).sum()),
                    "n_losers": int((net <= 0).sum()),
                }
            )

    if "major_micro_alignment" in closed.columns:
        _add("major_micro_alignment", closed["major_micro_alignment"].map(lambda x: "aligned" if x == 1 else ("misaligned" if x == 0 else "na")))
    if "bars_since_external_bos" in closed.columns:
        age = closed["bars_since_external_bos"]
        _add(
            "external_bos_age",
            pd.cut(age, bins=[-0.1, 2, 5, 10, 20, 1e9], labels=["0-2", "3-5", "6-10", "11-20", ">20"]),
        )
    if "distance_to_protected_level_atr" in closed.columns:
        d = closed["distance_to_protected_level_atr"]
        _add(
            "distance_protected_atr",
            pd.cut(d, bins=[-0.1, 0.5, 1.0, 2.0, 1e9], labels=["<=0.5", "0.5-1", "1-2", ">2"]),
        )
    if "bars_since_internal_bos" in closed.columns:
        _add(
            "internal_bos_present_recent",
            closed["bars_since_internal_bos"].map(lambda x: "age<=5" if math.isfinite(_finite(x)) and x <= 5 else "age>5_or_na"),
        )
    if "bars_since_choch" in closed.columns:
        _add(
            "choch_present_recent",
            closed["bars_since_choch"].map(lambda x: "age<=5" if math.isfinite(_finite(x)) and x <= 5 else "age>5_or_na"),
        )
    if "regime" in closed.columns:
        _add("regime", closed["regime"])
    if "trend_aligned" in closed.columns:
        _add("trend_aligned", closed["trend_aligned"].map(lambda x: "yes" if x == 1 else ("no" if x == 0 else "na")))
    return pd.DataFrame(rows)


def side_month_summary(panel: pd.DataFrame) -> pd.DataFrame:
    closed = panel[panel["closed"] == True].copy()  # noqa: E712
    rows = []
    for keys, g in closed.groupby(["side", "month"]):
        net = g[NET_COL].astype(float)
        om = outlier_metrics(net)
        rows.append(
            {
                "side": keys[0],
                "month": keys[1],
                "n": int(len(g)),
                "sum_net_0_20": float(net.sum()),
                "mean_net_0_20": float(net.mean()),
                "median_net_0_20": float(net.median()),
                "winrate": float((net > 0).mean()),
                "profit_factor": _pf(net),
                "best": float(net.max()),
                "worst": float(net.min()),
                "top1_share_of_net": om.get("best_share_of_net_sum"),
                "top3_share_of_net": om.get("top3_share_of_net_sum"),
            }
        )
    # also side totals / month totals
    for side, g in closed.groupby("side"):
        net = g[NET_COL].astype(float)
        om = outlier_metrics(net)
        rows.append(
            {
                "side": side,
                "month": "ALL",
                "n": int(len(g)),
                "sum_net_0_20": float(net.sum()),
                "mean_net_0_20": float(net.mean()),
                "median_net_0_20": float(net.median()),
                "winrate": float((net > 0).mean()),
                "profit_factor": _pf(net),
                "best": float(net.max()),
                "worst": float(net.min()),
                "top1_share_of_net": om.get("best_share_of_net_sum"),
                "top3_share_of_net": om.get("top3_share_of_net_sum"),
            }
        )
    return pd.DataFrame(rows)


def evaluate_diagnostic_candidates(
    panel: pd.DataFrame,
    wl: pd.DataFrame,
    split_dir: pd.DataFrame,
) -> pd.DataFrame:
    closed = panel[panel["closed"] == True].copy()  # noqa: E712
    without_top3 = closed[~closed["top3_trade"]].copy()
    rows = []
    wl_i = wl.set_index("feature") if not wl.empty else pd.DataFrame()
    sd_i = split_dir.set_index("feature") if not split_dir.empty else pd.DataFrame()

    for feat in NUMERIC_FEATURE_COLS:
        if feat not in closed.columns or feat not in sd_i.index:
            continue
        sd = sd_i.loc[feat]
        wrow = wl_i.loc[feat] if feat in wl_i.index else None
        same_dv = bool(sd["same_direction_dev_val"])
        # without top3 direction
        w3 = without_top3[without_top3["winner_net020"] == True]  # noqa: E712
        l3 = without_top3[without_top3["loser_net020"] == True]  # noqa: E712
        mw = float(pd.to_numeric(w3[feat], errors="coerce").median()) if len(w3) else np.nan
        ml = float(pd.to_numeric(l3[feat], errors="coerce").median()) if len(l3) else np.nan
        dir_wo = "winners_higher" if mw > ml else ("winners_lower" if mw < ml else "none")
        survives = same_dv and dir_wo == sd["dev_direction"] and dir_wo != "none"

        # side consistency
        side_dirs = []
        for side, gs in closed.groupby("side"):
            ww = gs[gs["winner_net020"] == True]  # noqa: E712
            ll = gs[gs["loser_net020"] == True]  # noqa: E712
            if len(ww) < 2 or len(ll) < 2:
                continue
            d = float(pd.to_numeric(ww[feat], errors="coerce").median()) - float(
                pd.to_numeric(ll[feat], errors="coerce").median()
            )
            side_dirs.append("winners_higher" if d > 0 else ("winners_lower" if d < 0 else "none"))
        side_consistent = len(set(side_dirs)) == 1 and side_dirs and side_dirs[0] != "none"

        # month consistency (among months with enough n)
        month_dirs = []
        for _, gm in closed.groupby("month"):
            ww = gm[gm["winner_net020"] == True]  # noqa: E712
            ll = gm[gm["loser_net020"] == True]  # noqa: E712
            if len(ww) < 2 or len(ll) < 2:
                continue
            d = float(pd.to_numeric(ww[feat], errors="coerce").median()) - float(
                pd.to_numeric(ll[feat], errors="coerce").median()
            )
            month_dirs.append("winners_higher" if d > 0 else ("winners_lower" if d < 0 else "none"))
        month_consistent = len(set(month_dirs)) == 1 and len(month_dirs) >= 2 and month_dirs[0] != "none"

        min_n = int(min(sd["dev_n"], sd["val_n"], sd["oos_n"]))
        expected = sd["dev_direction"]
        # status
        if min_n < MIN_GROUP_N or (wrow is not None and min(int(wrow["n_winner"]), int(wrow["n_loser"])) < MIN_GROUP_N):
            status = "underpowered"
        elif same_dv and not survives:
            status = "top3_driven"
        elif same_dv and survives and abs(float(sd["dev_difference"] or 0)) > 0:
            status = "interesting_for_followup"
        elif same_dv:
            status = "weak"
        elif sd["dev_direction"] != "none" and sd["val_direction"] != "none" and sd["dev_direction"] != sd["val_direction"]:
            status = "unstable"
        else:
            status = "descriptive_only"

        rows.append(
            {
                "feature_name": feat,
                "expected_direction": expected,
                "dev_effect": float(sd["dev_difference"]) if pd.notna(sd["dev_difference"]) else None,
                "val_effect": float(sd["val_difference"]) if pd.notna(sd["val_difference"]) else None,
                "oos_effect": float(sd["oos_difference"]) if pd.notna(sd["oos_difference"]) else None,
                "same_direction_dev_val": same_dv,
                "survives_without_top3": survives,
                "min_group_n": min_n,
                "side_consistent": side_consistent,
                "month_consistent": month_consistent,
                "status": status,
                "cliffs_delta_full": None if wrow is None else wrow.get("cliffs_delta"),
                "note": "diagnostic only — not an accepted strategy filter",
            }
        )
    return pd.DataFrame(rows).sort_values(["status", "feature_name"])


def robustness_feature_checks(panel: pd.DataFrame, features: Sequence[str]) -> pd.DataFrame:
    """Recompute median winner-loser diff under leave-out / side / split slices."""
    closed = panel[panel["closed"] == True].copy()  # noqa: E712
    slices = {
        "full": closed,
        "without_best": closed[~closed["top1_trade"]],
        "without_top3": closed[~closed["top3_trade"]],
        "long_only": closed[closed["side"] == "long"],
        "short_only": closed[closed["side"] == "short"],
        "development": closed[closed["split"] == "development"],
        "validation": closed[closed["split"] == "validation"],
        "oos": closed[closed["split"] == "oos"],
    }
    rows = []
    for feat in features:
        if feat not in closed.columns:
            continue
        for name, g in slices.items():
            w = pd.to_numeric(g.loc[g["winner_net020"] == True, feat], errors="coerce")  # noqa: E712
            l = pd.to_numeric(g.loc[g["loser_net020"] == True, feat], errors="coerce")  # noqa: E712
            mw = float(w.median()) if w.notna().any() else np.nan
            ml = float(l.median()) if l.notna().any() else np.nan
            rows.append(
                {
                    "feature": feat,
                    "slice": name,
                    "n": int(len(g)),
                    "n_winner": int(w.notna().sum()),
                    "n_loser": int(l.notna().sum()),
                    "median_winner": mw,
                    "median_loser": ml,
                    "difference": mw - ml if math.isfinite(mw) and math.isfinite(ml) else np.nan,
                    "direction": (
                        "winners_higher"
                        if math.isfinite(mw) and math.isfinite(ml) and mw > ml
                        else ("winners_lower" if math.isfinite(mw) and math.isfinite(ml) and mw < ml else "none")
                    ),
                }
            )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Plots (optional)
# ---------------------------------------------------------------------------


def maybe_write_plots(out_dir: Path, event_panel: pd.DataFrame, panel: pd.DataFrame) -> list[str]:
    written: list[str] = []
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return written

    plot_dir = out_dir / "plots"
    plot_dir.mkdir(parents=True, exist_ok=True)

    if not event_panel.empty:
        fig, ax = plt.subplots(figsize=(8, 4))
        for flag, label, color in ((True, "winner", "C0"), (False, "loser", "C1")):
            sub = event_panel[event_panel["winner_net020"] == flag]
            g = sub.groupby("relative_bar")["adx"].mean()
            ax.plot(g.index, g.values, label=label, color=color)
        ax.axvline(0, color="k", ls="--", lw=0.8)
        ax.set_title("ADX path winner vs loser (mean)")
        ax.set_xlabel("relative_bar (0=trigger)")
        ax.legend()
        p = plot_dir / "adx_path_winner_loser.png"
        fig.tight_layout()
        fig.savefig(p, dpi=120)
        plt.close(fig)
        written.append(str(p))

        fig, ax = plt.subplots(figsize=(8, 4))
        for flag, label in ((True, "winner"), (False, "loser")):
            sub = event_panel[event_panel["winner_net020"] == flag]
            g = sub.groupby("relative_bar")["directional_di_spread"].mean()
            ax.plot(g.index, g.values, label=label)
        ax.axvline(0, color="k", ls="--", lw=0.8)
        ax.set_title("Directional DI spread path")
        ax.legend()
        p = plot_dir / "di_spread_path.png"
        fig.tight_layout()
        fig.savefig(p, dpi=120)
        plt.close(fig)
        written.append(str(p))

        fig, ax = plt.subplots(figsize=(8, 4))
        for flag, label in ((True, "winner"), (False, "loser")):
            sub = event_panel[event_panel["winner_net020"] == flag]
            g = sub.groupby("relative_bar")["ema9_slope"].mean()
            ax.plot(g.index, g.values, label=label)
        ax.axvline(0, color="k", ls="--", lw=0.8)
        ax.set_title("EMA9 slope (trade-direction normalized)")
        ax.legend()
        p = plot_dir / "ema9_slope_path.png"
        fig.tight_layout()
        fig.savefig(p, dpi=120)
        plt.close(fig)
        written.append(str(p))

    closed = panel[panel["closed"] == True] if not panel.empty else panel  # noqa: E712
    if not closed.empty and "adx_bucket" in closed.columns:
        fig, ax = plt.subplots(figsize=(7, 4))
        g = closed.groupby("adx_bucket")[NET_COL].mean().reindex(["<20", "20-25", "25-30", ">=30"])
        g.plot(kind="bar", ax=ax)
        ax.set_title("Mean net0.20 by ADX bucket")
        ax.set_ylabel("net_return_0_20_pct")
        p = plot_dir / "net_by_adx_bucket.png"
        fig.tight_layout()
        fig.savefig(p, dpi=120)
        plt.close(fig)
        written.append(str(p))

    if not closed.empty and "cross_age_bucket" in closed.columns:
        fig, ax = plt.subplots(figsize=(7, 4))
        order = ["0-2", "3-5", "6-10", "11-20", ">20", "no_valid_cross"]
        g = closed.groupby("cross_age_bucket")[NET_COL].mean().reindex(order)
        g.plot(kind="bar", ax=ax)
        ax.set_title("Mean net0.20 by EMA cross age")
        p = plot_dir / "net_by_cross_age.png"
        fig.tight_layout()
        fig.savefig(p, dpi=120)
        plt.close(fig)
        written.append(str(p))

    if not closed.empty and "pullback_depth_atr" in closed.columns:
        fig, ax = plt.subplots(figsize=(6, 4))
        ax.scatter(
            closed["pullback_depth_atr"],
            closed[NET_COL],
            c=closed["winner_net020"].map({True: "C0", False: "C1"}),
            alpha=0.7,
        )
        ax.set_xlabel("pullback_depth_atr")
        ax.set_ylabel("net0.20")
        ax.set_title("Pullback depth vs outcome")
        p = plot_dir / "pullback_depth_vs_outcome.png"
        fig.tight_layout()
        fig.savefig(p, dpi=120)
        plt.close(fig)
        written.append(str(p))

    if not closed.empty and "bars_since_external_bos" in closed.columns:
        fig, ax = plt.subplots(figsize=(6, 4))
        ax.scatter(
            closed["bars_since_external_bos"],
            closed[NET_COL],
            c=closed["winner_net020"].map({True: "C0", False: "C1"}),
            alpha=0.7,
        )
        ax.set_xlabel("bars_since_external_bos")
        ax.set_ylabel("net0.20")
        ax.set_title("Structure age vs outcome")
        p = plot_dir / "structure_age_vs_outcome.png"
        fig.tight_layout()
        fig.savefig(p, dpi=120)
        plt.close(fig)
        written.append(str(p))

    return written


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _feat_bullets(
    top_wl: pd.DataFrame,
    *,
    prefix: tuple[str, ...],
    candidates: pd.DataFrame | None = None,
) -> str:
    lines: list[str] = []
    if candidates is not None and not candidates.empty:
        inter = candidates[candidates["status"] == "interesting_for_followup"]
        for _, r in inter.iterrows():
            f = str(r["feature_name"])
            if not any(f.startswith(p) or p in f for p in prefix):
                continue
            lines.append(
                f"- `{f}` [interesting_for_followup]: DevΔ={float(r['dev_effect']):.4g} "
                f"ValΔ={float(r['val_effect']):.4g} wo_top3={r['survives_without_top3']} "
                f"side_ok={r['side_consistent']} _(diagnostisch, kein Filter)_"
            )
            if len(lines) >= 8:
                break
    if len(lines) < 3 and not top_wl.empty:
        for _, r in top_wl.iterrows():
            f = str(r["feature"])
            if not any(f.startswith(p) or p in f for p in prefix):
                continue
            lines.append(
                f"- `{f}`: med_w={r['median_winner']:.4g} med_l={r['median_loser']:.4g} "
                f"δ={r['cliffs_delta']} dir={r['separation_direction']}"
            )
            if len(lines) >= 8:
                break
    return "\n".join(lines) if lines else "- (keine Treffer für diese Familie)"


def write_report(
    out_dir: Path,
    *,
    meta: Mapping[str, Any],
    baseline: Mapping[str, Any],
    candidates: pd.DataFrame,
    wl: pd.DataFrame,
    split_dir: pd.DataFrame,
) -> Path:
    interesting = candidates[candidates["status"] == "interesting_for_followup"] if not candidates.empty else candidates
    unstable = candidates[candidates["status"].isin(["unstable", "top3_driven", "underpowered"])] if not candidates.empty else candidates
    top_wl = wl.sort_values("cliffs_delta", key=lambda s: s.abs(), ascending=False).head(20) if not wl.empty else wl
    same_dv = split_dir[split_dir["same_direction_dev_val"] == True] if not split_dir.empty else split_dir  # noqa: E712
    same_all = split_dir[split_dir["same_direction_all_splits"] == True] if not split_dir.empty else split_dir  # noqa: E712

    lines = [
        "# C3.5c APT Pattern Diagnostic Audit",
        "",
        "Research-only. Descriptive. **No strategy filter promoted.**",
        "",
        "## 1. Datenquelle und Zeitraum",
        "",
        f"- Symbol: `{meta.get('symbol')}` · Variant: `{meta.get('variant')}` · TF: `{meta.get('timeframe')}`",
        f"- Data source: `{meta.get('data_source')}`",
        f"- Data span (5m): `{meta.get('data_start')}` → `{meta.get('data_end')}`",
        f"- Analyze window: `{meta.get('analyze_start')}` → `{meta.get('analyze_end_exclusive')}` (exclusive end)",
        f"- Last analyze bar: `{meta.get('analyze_end_inclusive_last_bar')}`",
        f"- Warmup calendar days: `{meta.get('warmup_calendar_days')}`",
        f"- Config hash A6: `{meta.get('config_hash')}`",
        f"- 5m contrast: `{meta.get('contrast_5m')}`",
        "",
        "## 2. Replay-/Entry-/Exit-Semantik",
        "",
        "- Arming: external_bos (A6)",
        "- TRIGGER on confirmed close → FILL at next open",
        "- Exit A: next opposite filled ENTRY open",
        "- Costs: gross; net0.20 = gross − 0.20% once per roundtrip",
        "- Entry features as-of **trigger close** only (`feature_asof=trigger_close`)",
        "- Post-entry bars in event panel marked `post_entry=True` and **not** used as entry features",
        "",
        "## 3. Tradeanzahl und Splits",
        "",
        f"- Fills: `{baseline.get('n_fills')}` · Closed Exit-A: `{baseline.get('n_closed')}` · Open at end: `{baseline.get('n_open')}`",
        f"- Split method: `{meta.get('splits', {}).get('method')}` (fixed before feature evaluation)",
        f"- Development: `{meta.get('splits', {}).get('splits', {}).get('development')}` · n_closed=`{baseline.get('n_dev')}`",
        f"- Validation: `{meta.get('splits', {}).get('splits', {}).get('validation')}` · n_closed=`{baseline.get('n_val')}`",
        f"- OOS: `{meta.get('splits', {}).get('splits', {}).get('oos')}` · n_closed=`{baseline.get('n_oos')}`",
        "",
        "## 4. Baseline-Ergebnis (closed, net0.20)",
        "",
        f"- mean=`{baseline.get('mean_net_0_20')}` sum=`{baseline.get('sum_net_0_20')}` WR=`{baseline.get('winrate')}` PF=`{baseline.get('profit_factor')}`",
        f"- Long sum=`{baseline.get('long_sum')}` Short sum=`{baseline.get('short_sum')}`",
        "",
        "## 5. Top-1-/Top-3-Konzentration",
        "",
        f"- best_share_of_net=`{baseline.get('best_share')}` top3_share_of_net=`{baseline.get('top3_share')}`",
        f"- sum without best=`{baseline.get('without_best')}` without top3=`{baseline.get('without_top3')}`",
        "",
        "## 6. ADX-/DI-Befunde",
        "",
        _feat_bullets(top_wl, prefix=("adx", "di_"), candidates=candidates),
        "",
        "Hinweis: A6 verlangt bereits ADX rising + DI-Alignment — Level-/Rising-Buckets sind daher oft degeneriert.",
        "",
        "## 7. EMA-/Cross-Befunde",
        "",
        _feat_bullets(top_wl, prefix=("ema", "cross", "price_to_ema", "dir_ema"), candidates=candidates),
        "",
        "Hinweis: A6 verlangt EMA-Slope in Trade-Richtung — `dir_ema_slope_bucket` ist unter A6 oft nur `with`.",
        "",
        "## 8. Pullback-Befunde",
        "",
        _feat_bullets(top_wl, prefix=("pullback", "bars_arm", "bars_pullback", "bars_ready", "chase", "breakout", "rejection", "confirmation", "failed_"), candidates=candidates),
        "",
        "## 9. Market-Structure-Befunde",
        "",
        _feat_bullets(top_wl, prefix=("bars_since", "distance_to", "major", "micro", "structure", "trend_"), candidates=candidates),
        "",
        "## 10. Regime-/Volatilitäts-Befunde",
        "",
        _feat_bullets(top_wl, prefix=("atr", "vol_", "ret_", "regime"), candidates=candidates),
        "",
        "## 11. Long-vs-Short",
        "",
        f"- Long sum_net0.20=`{baseline.get('long_sum')}` · Short sum_net0.20=`{baseline.get('short_sum')}` (Short trägt den Edge in dieser Stichprobe).",
        "- Details: `side_month_summary.csv`.",
        "",
        "## 12. Monatsstabilität",
        "",
        "- Details: `side_month_summary.csv`. Einzelne Monate sind dünn besetzt — keine Monatsoptimierung.",
        "",
        "## 13. Ergebnisse ohne Top-3",
        "",
        f"- Closed without top3: n=`{baseline.get('n_without_top3')}` sum_net0.20=`{baseline.get('without_top3')}`",
        f"- Interesting survivors without top3: `{int(interesting['survives_without_top3'].sum()) if not interesting.empty else 0}`",
        "",
        "## 14. Dev/Val/OOS-Stabilität",
        "",
        f"- Features with same_direction_dev_val: **{len(same_dv)}**",
        f"- Features with same_direction_all_splits: **{len(same_all)}**",
        "",
    ]
    if not same_all.empty:
        lines.append("All-split stable (median direction):")
        for _, r in same_all.iterrows():
            lines.append(
                f"- `{r['feature']}`: {r['dev_direction']} "
                f"(DevΔ={r['dev_difference']:.4g}, ValΔ={r['val_difference']:.4g}, OOSΔ={r['oos_difference']:.4g})"
            )

    lines += [
        "",
        "## 15. Diagnostisch interessante Merkmale",
        "",
        "_Status `interesting_for_followup` = stabile deskriptive Trennung Dev+Val, überlebt ohne Top-3 — **kein** akzeptierter Filter._",
        "_Val n=4 / OOS n=3: alle Follow-up-Treffer bleiben unterpowered für Strategieentscheidungen._",
        "",
    ]
    if interesting.empty:
        lines.append("- Keine Feature erfüllte die Follow-up-Kriterien.")
    else:
        for _, r in interesting.iterrows():
            lines.append(
                f"- `{r['feature_name']}`: DevΔ={r['dev_effect']:.4g} ValΔ={r['val_effect']:.4g} "
                f"OOSΔ={r['oos_effect'] if r['oos_effect'] is None else round(float(r['oos_effect']), 4)} "
                f"wo_top3={r['survives_without_top3']} side_ok={r['side_consistent']} month_ok={r['month_consistent']}"
            )

    lines += [
        "",
        "## 16. Instabile / widerlegte / unterpowerte Hypothesen",
        "",
    ]
    if unstable.empty:
        lines.append("- (siehe `diagnostics_candidates.csv`)")
    else:
        for status, g in unstable.groupby("status"):
            lines.append(f"- **{status}** ({len(g)}): " + ", ".join(f"`{x}`" for x in g["feature_name"].head(15)))

    lines += [
        "",
        "## 17. Was noch nicht bewiesen ist",
        "",
        "- Kein robuster Entry-Filter",
        "- Keine Out-of-Sample-Strategievalidierung jenseits deskriptiver Richtungschecks",
        "- Keine Live-/Forward-Bestätigung",
        "- Val/OOS-Zellen sind mit n=4/3 statistisch dünn",
        "- Edge ohne Top-3 Trades ist historisch negativ (sum_net0.20 without top3 < 0)",
        "",
        "## 18. Empfehlung nächster Schritt",
        "",
        "- Priorisiere all-split-stabile Familien: **flachere Pullbacks** (`pullback_depth_atr`), "
        "**längerer Arm→Trigger**, **ret_10 gegen Chase**, **Structure-Age**, **Rejection-Wick** — nur als Hypothesenliste",
        "- Case-Review der Winner/Loser mit Event-Pfaden (`event_aligned_features.csv`, Plots)",
        "- Keine Threshold-Optimierung / keine SM-Änderung in der nächsten Phase",
        "- Optional: Feature-Stabilität auf erweiterten Coins **nach** APT-Case-Review, nicht davor",
        "",
        "### Klassifikation der Aussagen in diesem Report",
        "",
        "| Typ | Bedeutung |",
        "|---|---|",
        "| deskriptive Beobachtung | Muster in der Stichprobe |",
        "| stabile diagnostische Trennung | gleiche Richtung Dev+Val, überlebt ohne Top-3 |",
        "| unbewiesener Strategie-Filter | noch nicht implementiert / nicht freigegeben |",
        "",
    ]
    path = out_dir / "report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------


def run_pattern_diagnostic_audit(
    *,
    symbol: str = SYMBOL,
    timeframe: str = TIMEFRAME,
    output_dir: Path = DEFAULT_OUT,
    baseline_dir: Path = DEFAULT_BASELINE_DIR,
    analyze_start: str | None = None,
    analyze_end: str | None = None,
    write_plots: bool = True,
) -> dict[str, Any]:
    baseline_info = assert_baseline_readonly(baseline_dir)
    if not baseline_info.get("hash_matches"):
        raise RuntimeError(
            f"C2 baseline hash mismatch: {baseline_info.get('baseline_hash')} != {C2_BASELINE_HASH}"
        )
    assert_safe_output_dir(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    cfg = baseline_a6()
    assert cfg.name == VARIANT

    # Extended APT window (None → data-driven after warmup), same helper as robustness
    frame, frame_meta = build_extended_tf_frame(
        symbol,
        timeframe=timeframe,
        analyze_start=analyze_start,
        analyze_end=analyze_end,
        warmup_calendar_days=WARMUP_CALENDAR_DAYS,
    )
    if frame.empty or not frame_meta.get("frame_ok"):
        raise RuntimeError(f"frame build failed: {frame_meta}")

    a0 = pd.Timestamp(frame_meta["analyze_start"])
    a1 = pd.Timestamp(frame_meta["analyze_end_exclusive"])
    splits = fixed_chrono_splits(a0, a1)

    frame = enrich_diagnostic_frame(frame)
    trades, info, parity_df = generate_exit_a_trades(frame, cfg)
    # Same SM replay for lifecycles + entry snapshots (deterministic with generate)
    _tl, entries, lives = apply_pullback_entry(frame, cfg, return_lifecycles=True)
    filled = enrich_filled_from_entries(_filled_sorted(frame, entries), entries)
    assert len(filled) == int(info["n_fills"])
    # Exit-A identity vs shared helper
    trades_check = trades_exit_a_opposite_entry(frame, filled, timeframe=timeframe, variant=cfg.name)
    assert len(closed_only(trades_check)) == len(closed_only(trades))

    trades = label_trades(trades, splits)
    panel = extract_trade_features(frame, trades, filled, lives)
    closed = closed_only(panel)
    open_rows = panel[panel["closed"] == False]  # noqa: E712

    # Optional light 5m contrast (descriptive only; not full feature panel)
    contrast_5m: dict[str, Any] = {"ran": False}
    try:
        end_inclusive = (a1 - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        f5, m5 = build_extended_tf_frame(
            symbol,
            timeframe="5m",
            analyze_start=analyze_start if analyze_start else a0.strftime("%Y-%m-%d"),
            analyze_end=analyze_end if analyze_end else end_inclusive,
            warmup_calendar_days=WARMUP_CALENDAR_DAYS,
        )
        if not f5.empty:
            _tl5, ent5 = apply_pullback_entry(f5, cfg)
            filled5 = _filled_sorted(f5, ent5)
            t5 = trades_exit_a_opposite_entry(f5, filled5, timeframe="5m", variant=cfg.name)
            sp5 = fixed_chrono_splits(pd.Timestamp(m5["analyze_start"]), pd.Timestamp(m5["analyze_end_exclusive"]))
            t5 = label_trades(t5, sp5)
            c5 = closed_only(t5)
            contrast_5m = {
                "ran": True,
                "n_fills": int(len(filled5)),
                "n_closed": int(len(c5)),
                "mean_net_0_20": float(c5[NET_COL].mean()) if len(c5) else None,
                "sum_net_0_20": float(c5[NET_COL].sum()) if len(c5) else None,
                "winrate": float((c5[NET_COL] > 0).mean()) if len(c5) else None,
                "note": "descriptive contrast only; primary analysis remains 15m",
            }
    except Exception as exc:  # noqa: BLE001
        contrast_5m = {"ran": False, "error": str(exc)}

    event_panel = build_event_aligned_panel(frame, panel)
    wl = winner_loser_summary(panel)
    split_dir = split_feature_direction(panel)
    adx_path = pre_signal_adx_path(event_panel)
    ema_buckets = _bucket_outcome_table(panel, "dir_ema_slope_bucket")
    adx_buckets = _bucket_outcome_table(panel, "adx_bucket")
    # rising flag cross
    adx_rising = _bucket_outcome_table(panel, "adx_rising_flag")
    adx_buckets_all = pd.concat(
        [adx_buckets.assign(bucket_family="adx_level"), adx_rising.assign(bucket_family="adx_rising")],
        ignore_index=True,
    )
    cross_buckets = _bucket_outcome_table(panel, "cross_age_bucket")
    pb_sum = pullback_context_summary(panel)
    struct_sum = structure_context_by_outcome(panel)
    side_month = side_month_summary(panel)
    candidates = evaluate_diagnostic_candidates(panel, wl, split_dir)
    robust = robustness_feature_checks(
        panel,
        list(candidates.loc[candidates["status"].isin(["interesting_for_followup", "weak", "top3_driven"]), "feature_name"])
        if not candidates.empty
        else list(NUMERIC_FEATURE_COLS[:15]),
    )

    om = outlier_metrics(closed[NET_COL]) if len(closed) else {}
    baseline = {
        "n_fills": int(info["n_fills"]),
        "n_closed": int(len(closed)),
        "n_open": int(len(open_rows)),
        "n_dev": int((closed["split"] == "development").sum()),
        "n_val": int((closed["split"] == "validation").sum()),
        "n_oos": int((closed["split"] == "oos").sum()),
        "mean_net_0_20": float(closed[NET_COL].mean()) if len(closed) else None,
        "sum_net_0_20": float(closed[NET_COL].sum()) if len(closed) else None,
        "winrate": float((closed[NET_COL] > 0).mean()) if len(closed) else None,
        "profit_factor": _pf(closed[NET_COL]) if len(closed) else None,
        "long_sum": float(closed.loc[closed["side"] == "long", NET_COL].sum()) if len(closed) else None,
        "short_sum": float(closed.loc[closed["side"] == "short", NET_COL].sum()) if len(closed) else None,
        "best_share": om.get("best_share_of_net_sum"),
        "top3_share": om.get("top3_share_of_net_sum"),
        "without_best": om.get("without_best"),
        "without_top3": om.get("without_top3"),
        "n_without_top3": int((~closed["top3_trade"]).sum()) if len(closed) else 0,
        "parity": info.get("parity"),
    }

    # Persist
    panel.to_csv(output_dir / "trade_feature_panel.csv", index=False)
    open_rows.to_csv(output_dir / "open_trades.csv", index=False)
    event_panel.to_csv(output_dir / "event_aligned_features.csv", index=False)
    wl.to_csv(output_dir / "winner_vs_loser_feature_summary.csv", index=False)
    split_dir.to_csv(output_dir / "split_feature_direction.csv", index=False)
    adx_path.to_csv(output_dir / "pre_signal_adx_path.csv", index=False)
    ema_buckets.to_csv(output_dir / "ema_band_slope_buckets.csv", index=False)
    adx_buckets_all.to_csv(output_dir / "adx_fixed_buckets.csv", index=False)
    cross_buckets.to_csv(output_dir / "cross_age_buckets.csv", index=False)
    pb_sum.to_csv(output_dir / "pullback_context_summary.csv", index=False)
    struct_sum.to_csv(output_dir / "structure_context_by_outcome.csv", index=False)
    side_month.to_csv(output_dir / "side_month_summary.csv", index=False)
    candidates.to_csv(output_dir / "diagnostics_candidates.csv", index=False)
    robust.to_csv(output_dir / "feature_robustness_slices.csv", index=False)
    if not parity_df.empty:
        parity_df.to_csv(output_dir / "parity_events.csv", index=False)

    plots = maybe_write_plots(output_dir, event_panel, panel) if write_plots else []

    meta = {
        "symbol": symbol,
        "variant": VARIANT,
        "timeframe": timeframe,
        "config_hash": config_hash(cfg),
        "baseline_reference_hash": C2_BASELINE_HASH,
        "production_sm_unchanged": True,
        "pine_unchanged": True,
        "no_parameter_tuning": True,
        "no_filter_promotion": True,
        "data_source": frame_meta.get("data_source"),
        "data_start": frame_meta.get("data_start"),
        "data_end": frame_meta.get("data_end"),
        "analyze_start": frame_meta.get("analyze_start"),
        "analyze_end_exclusive": frame_meta.get("analyze_end_exclusive"),
        "analyze_end_inclusive_last_bar": frame_meta.get("analyze_end_inclusive_last_bar"),
        "warmup_calendar_days": frame_meta.get("warmup_calendar_days"),
        "n_analyze_bars": frame_meta.get("n_analyze_bars"),
        "splits": splits,
        "baseline": baseline,
        "contrast_5m": contrast_5m,
        "n_interesting_for_followup": int((candidates["status"] == "interesting_for_followup").sum())
        if not candidates.empty
        else 0,
        "plots": plots,
        "entry_feature_rule": "trigger_close_only",
        "post_entry_in_event_panel_only": True,
    }
    meta["content_hash"] = content_hash_frames(
        {
            "panel": panel[[c for c in panel.columns if c in ("trade_id", NET_COL, "adx", "split")]].head(500)
            if not panel.empty
            else panel,
            "candidates": candidates,
        }
    )
    (output_dir / "metadata.json").write_text(json.dumps(json_safe(meta), indent=2) + "\n", encoding="utf-8")
    write_report(output_dir, meta=meta, baseline=baseline, candidates=candidates, wl=wl, split_dir=split_dir)
    return meta


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="C3.5c APT pattern diagnostic audit")
    p.add_argument("--symbol", default=SYMBOL)
    p.add_argument("--timeframe", default=TIMEFRAME)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--baseline-dir", type=Path, default=DEFAULT_BASELINE_DIR)
    p.add_argument("--analyze-start", default=None, help="optional YYYY-MM-DD; default=data start+warmup")
    p.add_argument("--analyze-end", default=None, help="optional inclusive YYYY-MM-DD")
    p.add_argument("--no-plots", action="store_true")
    args = p.parse_args(list(argv) if argv is not None else None)
    meta = run_pattern_diagnostic_audit(
        symbol=args.symbol,
        timeframe=args.timeframe,
        output_dir=args.output_dir,
        baseline_dir=args.baseline_dir,
        analyze_start=args.analyze_start,
        analyze_end=args.analyze_end,
        write_plots=not args.no_plots,
    )
    print(json.dumps(json_safe({"ok": True, "n_closed": meta["baseline"]["n_closed"], "out": str(args.output_dir)})))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
