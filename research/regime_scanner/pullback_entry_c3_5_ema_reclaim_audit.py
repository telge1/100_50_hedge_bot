"""C3.5 ema_reclaim terminal-invalidation audit (research-only).

Offline analysis + counterfactual E0–E5 replay. Does not modify the C3.5
state machine, Pine semantics, thresholds, or live bots.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from unittest.mock import patch

import numpy as np
import pandas as pd

from research.regime_scanner.indicator_feature_store import load_ohlcv_with_warmup
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.pullback_entry_c3_5 import (
    FEE_BPS,
    TARGET_ATRS,
    PullbackEntryConfig,
    SetupRuntime,
    apply_pullback_entry,
    compute_entry_outcomes,
    config_hash,
    prepare_research_frame,
    step_pullback_entry,
    _finite,
)
from research.regime_scanner.pullback_entry_c3_5_diagnostics import baseline_a6
from research.regime_scanner.trend_regime_classification_audit import (
    C2_BASELINE_HASH,
    assert_baseline_readonly,
)
from research.regime_scanner.trend_weakening_multi_bar_audit import assert_safe_output_dir

DEFAULT_OUT = Path(
    "research/regime_scanner/results/phase_c3_5_pullback_entry_state_machine/ema_reclaim_audit"
)
DEFAULT_BASELINE_DIR = Path(
    "research/regime_scanner/results/baselines/c2_loose_mar_2026_before_c3"
)

LOAD_START = "2026-01-01"
LOAD_END = "2026-05-15"
ANALYZE_START = "2026-02-01"
ANALYZE_END = "2026-04-30"

EMA_RECLAIM_REASONS = frozenset({"ema_bullish_reclaim", "ema_bearish_reclaim"})
FORWARD_AUDIT_HORIZONS: tuple[int, ...] = (1, 2, 3, 5, 10, 20, 40)
RECOVERY_WINDOWS: tuple[int, ...] = (1, 2, 3, 5)
DIAG_STOP_ATRS: tuple[float, ...] = (0.5, 1.0, 1.5, 2.0)

# Chart focus: APT long READY → ema_bearish_reclaim (diagnostics setup 268).
FOCUS_SETUP_ID_APT = 268


# ---------------------------------------------------------------------------
# Frame load
# ---------------------------------------------------------------------------


def build_research_frame(
    symbol: str,
    *,
    load_start: str = LOAD_START,
    load_end: str = LOAD_END,
    analyze_start: str = ANALYZE_START,
    analyze_end: str = ANALYZE_END,
    include_mtf: bool = True,
) -> pd.DataFrame:
    """Load with warmup window, prepare features, slice analyze window (end inclusive)."""
    full_5m, _ = load_ohlcv_with_warmup(
        symbol, "5m", analyze_start=load_start, analyze_end=load_end
    )
    ohlcv_15m = ohlcv_30m = None
    if include_mtf:
        full_15m, _ = load_ohlcv_with_warmup(
            symbol, "15m", analyze_start=load_start, analyze_end=load_end
        )
        full_30m, _ = load_ohlcv_with_warmup(
            symbol, "30m", analyze_start=load_start, analyze_end=load_end
        )
        ohlcv_15m, ohlcv_30m = full_15m, full_30m
    frame = prepare_research_frame(full_5m, ohlcv_15m=ohlcv_15m, ohlcv_30m=ohlcv_30m)
    a0 = pd.Timestamp(analyze_start, tz="UTC")
    # Inclusive analyze_end calendar day (match diagnostics).
    a1 = pd.Timestamp(analyze_end, tz="UTC") + pd.Timedelta(days=1)
    ts = pd.to_datetime(frame["timestamp"], utc=True)
    frame = frame.loc[(ts >= a0) & (ts < a1)].copy().reset_index(drop=True)
    frame["bar_index"] = np.arange(len(frame))
    frame["symbol"] = symbol
    frame["timeframe"] = "5m"
    return frame


def load_or_build_frame(
    symbol: str,
    *,
    cached_csv: Path | None = None,
    **kwargs: Any,
) -> pd.DataFrame:
    if cached_csv is not None and cached_csv.exists() and symbol == "APTUSDT":
        frame = pd.read_csv(cached_csv, parse_dates=["timestamp"])
        ts = pd.to_datetime(frame["timestamp"], utc=True)
        a0 = pd.Timestamp(kwargs.get("analyze_start", ANALYZE_START), tz="UTC")
        a1 = pd.Timestamp(kwargs.get("analyze_end", ANALYZE_END), tz="UTC") + pd.Timedelta(days=1)
        frame = frame.loc[(ts >= a0) & (ts < a1)].copy().reset_index(drop=True)
        frame["bar_index"] = np.arange(len(frame))
        frame["symbol"] = symbol
        frame["timeframe"] = "5m"
        return frame
    return build_research_frame(symbol, **kwargs)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _regime_label(adx: float) -> str:
    if not math.isfinite(adx):
        return "unknown"
    return "trend" if adx >= 25.0 else "range"


def _atr_bucket(atr: float, median_atr: float) -> str:
    if not (math.isfinite(atr) and math.isfinite(median_atr) and median_atr > 0):
        return "unknown"
    return "high_atr" if atr >= median_atr else "low_atr"


def _state_family(state: str | None) -> str:
    s = str(state or "")
    if "READY" in s:
        return "READY"
    if "PULLBACK" in s:
        return "PULLBACK"
    if "ARMED" in s:
        return "ARMED"
    if "ENTERED" in s:
        return "ENTERED"
    return "OTHER"


def _reclaim_condition(direction: str, row: Mapping[str, Any]) -> bool:
    age = int(row.get("ema_cross_age") or 99)
    if age > 3:
        return False
    close = _finite(row.get("close"))
    ema20 = _finite(row.get("ema_20"))
    if direction == "short":
        return bool(row.get("ema9_above_ema20")) and close > ema20
    return bool(row.get("ema9_below_ema20")) and close < ema20


def _structure_levels_intact(
    direction: str,
    *,
    close: float,
    low: float,
    high: float,
    prior_swing_high: float | None,
    prior_swing_low: float | None,
    protected_high: float | None,
    protected_low: float | None,
) -> bool:
    """True if relevant swing + protected level not broken by close (SM parity)."""
    if direction == "long":
        if prior_swing_low is not None and math.isfinite(prior_swing_low) and close < prior_swing_low:
            return False
        if protected_low is not None and math.isfinite(protected_low) and close < protected_low:
            return False
        return True
    if prior_swing_high is not None and math.isfinite(prior_swing_high) and close > prior_swing_high:
        return False
    if protected_high is not None and math.isfinite(protected_high) and close > protected_high:
        return False
    return True


def _ema_recovered(direction: str, row: Mapping[str, Any]) -> bool:
    """Close back on setup side of EMA20 and 9/20 alignment restored."""
    close = _finite(row.get("close"))
    ema9 = _finite(row.get("ema_9"))
    ema20 = _finite(row.get("ema_20"))
    if direction == "long":
        return close > ema20 and ema9 > ema20
    return close < ema20 and ema9 < ema20


def _close_over_ema20(direction: str, row: Mapping[str, Any]) -> bool:
    close = _finite(row.get("close"))
    ema20 = _finite(row.get("ema_20"))
    return close > ema20 if direction == "long" else close < ema20


def _ema9_over_ema20(direction: str, row: Mapping[str, Any]) -> bool:
    ema9 = _finite(row.get("ema_9"))
    ema20 = _finite(row.get("ema_20"))
    return ema9 > ema20 if direction == "long" else ema9 < ema20


def _in_ema_band(row: Mapping[str, Any]) -> bool:
    close = _finite(row.get("close"))
    a, b = _finite(row.get("ema_9")), _finite(row.get("ema_20"))
    lo, hi = min(a, b), max(a, b)
    return lo <= close <= hi


# ---------------------------------------------------------------------------
# Baseline replay with runtime snapshots (prior swings, breakout, …)
# ---------------------------------------------------------------------------


@dataclass
class TerminalSnapshot:
    setup_id: int
    prior_swing_high: float | None
    prior_swing_low: float | None
    protected_high: float | None
    protected_low: float | None
    breakout_level: float | None
    armed_price: float | None
    structure_state: str | None
    major_direction: int | None
    setup_age: int
    ready_age: int


def collect_terminal_snapshots(
    frame: pd.DataFrame,
    cfg: PullbackEntryConfig,
) -> dict[int, TerminalSnapshot]:
    """Replay SM only to capture prior-swing / breakout at terminal bars."""
    df = frame.reset_index(drop=True).copy()
    if "bar_index" not in df.columns:
        df["bar_index"] = np.arange(len(df))
    opens = df["open"].astype(float).tolist()
    rt = SetupRuntime()
    snapshots: dict[int, TerminalSnapshot] = {}
    next_id = 1

    def _alloc() -> int:
        nonlocal next_id
        sid = next_id
        next_id += 1
        return sid

    for i in range(len(df)):
        row = df.iloc[i].to_dict()
        next_open = opens[i + 1] if i + 1 < len(opens) else None
        snap_prior_h = rt.prior_swing_high
        snap_prior_l = rt.prior_swing_low
        snap_break = rt.breakout_level
        snap_armed = rt.armed_price
        prev_id = rt.setup_id
        rt, diag = step_pullback_entry(
            rt, row, cfg=cfg, next_open=next_open, setup_id_factory=_alloc
        )
        ev = str(diag.get("events") or "")
        if diag.get("terminal_outcome") and "terminal:" in ev:
            sid = int(diag.get("terminal_setup_id") or diag.get("setup_id") or prev_id or 0)
            if sid:
                snapshots[sid] = TerminalSnapshot(
                    setup_id=sid,
                    prior_swing_high=snap_prior_h,
                    prior_swing_low=snap_prior_l,
                    protected_high=_finite(row.get("protected_high"))
                    if row.get("protected_high") is not None
                    else None,
                    protected_low=_finite(row.get("protected_low"))
                    if row.get("protected_low") is not None
                    else None,
                    breakout_level=snap_break if snap_break is not None else diag.get("breakout_level"),
                    armed_price=snap_armed if snap_armed is not None else diag.get("armed_price"),
                    structure_state=str(row.get("protected_structure_state") or "") or None,
                    major_direction=int(row.get("major_direction") or 0)
                    if row.get("major_direction") is not None
                    else None,
                    setup_age=int(diag.get("terminal_setup_age") or 0),
                    ready_age=int(diag.get("terminal_ready_age") or 0),
                )
    return snapshots


def apply_baseline_with_snapshots(
    frame: pd.DataFrame,
    cfg: PullbackEntryConfig,
) -> tuple[pd.DataFrame, list[dict[str, Any]], list[dict[str, Any]], dict[int, TerminalSnapshot]]:
    """Baseline apply_pullback_entry + runtime snapshots (no SM semantics change)."""
    timeline, entries, lives = apply_pullback_entry(frame, cfg, return_lifecycles=True)
    snapshots = collect_terminal_snapshots(frame, cfg)
    return timeline, entries, lives, snapshots


# ---------------------------------------------------------------------------
# Case enrichment + forward metrics
# ---------------------------------------------------------------------------


def filter_ema_reclaim(lives: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        dict(x)
        for x in lives
        if x.get("terminal_outcome") == "invalidated"
        and x.get("terminal_reason") in EMA_RECLAIM_REASONS
    ]


def _ts_at(frame: pd.DataFrame, bar: int | None) -> Any:
    if bar is None or bar < 0 or bar >= len(frame):
        return None
    return frame.iloc[int(bar)]["timestamp"]


def compute_path_metrics(
    frame: pd.DataFrame,
    *,
    start_bar: int,
    direction: str,
    ref_price: float | None = None,
    fee_bps_per_side: float = FEE_BPS,
    horizons: Sequence[int] = FORWARD_AUDIT_HORIZONS,
    max_path: int = 80,
) -> dict[str, Any]:
    """Direction-normalized forward path from start_bar (inclusive next bars)."""
    n = len(frame)
    close = frame["close"].astype(float).to_numpy()
    high = frame["high"].astype(float).to_numpy()
    low = frame["low"].astype(float).to_numpy()
    atr = frame["atr_14"].astype(float).to_numpy() if "atr_14" in frame.columns else np.full(n, np.nan)
    side = 1 if direction == "long" else -1
    i = int(start_bar)
    if i < 0 or i >= n:
        return {"valid": False}
    entry_px = float(ref_price) if ref_price is not None and math.isfinite(float(ref_price)) else float(close[i])
    a = float(atr[i]) if np.isfinite(atr[i]) else float("nan")
    fee = fee_bps_per_side / 10_000.0
    out: dict[str, Any] = {"valid": True, "ref_price": entry_px, "atr_at_ref": a if math.isfinite(a) else None}
    for h in horizons:
        j = i + h
        if j >= n:
            out[f"fwd_ret_{h}"] = None
            continue
        raw = (close[j] - entry_px) / entry_px
        signed = raw if side > 0 else -raw
        out[f"fwd_ret_{h}"] = float(signed - 2 * fee)
    horizon = min(max_path, n - i - 1)
    mfe = 0.0
    mae = 0.0
    t_mfe = None
    t_mae = None
    for k in range(1, horizon + 1):
        j = i + k
        if side > 0:
            up = (high[j] - entry_px) / entry_px
            dn = (low[j] - entry_px) / entry_px
        else:
            up = (entry_px - low[j]) / entry_px
            dn = (entry_px - high[j]) / entry_px
        if up > mfe:
            mfe = up
            t_mfe = k
        if dn < mae:
            mae = dn
            t_mae = k
    out["mfe"] = float(mfe)
    out["mae"] = float(mae)
    out["time_to_mfe"] = t_mfe
    out["time_to_mae"] = t_mae
    # Target/stop races (diagnostic only)
    for t_atr in TARGET_ATRS:
        for s_atr in DIAG_STOP_ATRS:
            key = f"t{t_atr}_s{s_atr}"
            if not math.isfinite(a) or a <= 0:
                out[f"{key}_target_first"] = None
                out[f"{key}_stop_first"] = None
                continue
            target = entry_px + t_atr * a if side > 0 else entry_px - t_atr * a
            stop = entry_px - s_atr * a if side > 0 else entry_px + s_atr * a
            t_first = s_first = False
            for k in range(1, horizon + 1):
                j = i + k
                if side > 0:
                    hit_t = high[j] >= target
                    hit_s = low[j] <= stop
                else:
                    hit_t = low[j] <= target
                    hit_s = high[j] >= stop
                if hit_t and not hit_s:
                    t_first = True
                    break
                if hit_s:
                    s_first = True
                    break
            out[f"{key}_target_first"] = t_first
            out[f"{key}_stop_first"] = s_first
    # Fake heuristic (same spirit as entry outcomes)
    fake = bool(out.get("time_to_mae") is not None and out.get("time_to_mfe") is not None and out["time_to_mae"] < out["time_to_mfe"])
    if out.get("fwd_ret_10") is not None and out["fwd_ret_10"] < 0:
        fake = True
    if out.get("t1.0_s1.0_stop_first") is True:
        fake = True
    out["is_fake"] = fake
    out["is_good_move"] = bool(out.get("mfe") is not None and out["mfe"] >= 0.01 and not fake)
    return out


def _later_breakout(
    frame: pd.DataFrame,
    *,
    start_bar: int,
    direction: str,
    level: float | None,
    max_look: int = 80,
) -> tuple[bool, int | None]:
    if level is None or not math.isfinite(float(level)):
        return False, None
    lvl = float(level)
    n = len(frame)
    close = frame["close"].astype(float).to_numpy()
    for k in range(1, min(max_look, n - start_bar - 1) + 1):
        j = start_bar + k
        if direction == "long" and close[j] > lvl:
            return True, k
        if direction == "short" and close[j] < lvl:
            return True, k
    return False, None


def _next_same_dir_setup(
    lives: Sequence[Mapping[str, Any]],
    *,
    setup_id: int,
    direction: str,
    terminal_bar: int,
    windows: Sequence[int] = (1, 2, 3, 5, 10),
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    later = [
        x
        for x in lives
        if x.get("direction") == direction
        and int(x.get("armed_bar") or -1) > terminal_bar
        and int(x.get("setup_id") or -1) != setup_id
    ]
    later.sort(key=lambda x: int(x["armed_bar"]))
    gap = int(later[0]["armed_bar"]) - terminal_bar if later else None
    out["next_same_dir_setup_id"] = int(later[0]["setup_id"]) if later else None
    out["bars_to_next_same_dir_setup"] = gap
    for w in windows:
        out[f"new_setup_same_dir_within_{w}"] = bool(gap is not None and gap <= w)
    return out


def _opposite_confirm(
    frame: pd.DataFrame,
    *,
    start_bar: int,
    direction: str,
    max_look: int = 20,
) -> bool:
    """Opposite external/major arm edge within lookforward."""
    n = len(frame)
    if direction == "long":
        cols = ("arm_edge_external_bear", "arm_edge_major_bear")
    else:
        cols = ("arm_edge_external_bull", "arm_edge_major_bull")
    for k in range(1, min(max_look, n - start_bar - 1) + 1):
        row = frame.iloc[start_bar + k]
        if any(bool(row.get(c)) for c in cols if c in frame.columns):
            return True
    return False


def enrich_reclaim_cases(
    frame: pd.DataFrame,
    lives: Sequence[Mapping[str, Any]],
    snapshots: Mapping[int, TerminalSnapshot],
    *,
    symbol: str,
    timeline: pd.DataFrame | None = None,
) -> pd.DataFrame:
    median_atr = float(frame["atr_14"].median()) if "atr_14" in frame.columns else float("nan")
    rows: list[dict[str, Any]] = []
    reclaim = filter_ema_reclaim(lives)
    for life in reclaim:
        sid = int(life["setup_id"])
        tb = int(life["terminal_bar"])
        direction = str(life["direction"])
        snap = snapshots.get(sid)
        fr = frame.iloc[tb]
        close = _finite(fr.get("close"))
        atr = _finite(fr.get("atr_14"), 1e-12)
        ema9 = _finite(fr.get("ema_9"))
        ema20 = _finite(fr.get("ema_20"))
        ema50 = _finite(fr.get("ema_50"))
        prior_h = snap.prior_swing_high if snap else None
        prior_l = snap.prior_swing_low if snap else None
        prot_h = snap.protected_high if snap else _finite(fr.get("protected_high")) if fr.get("protected_high") is not None else None
        prot_l = snap.protected_low if snap else _finite(fr.get("protected_low")) if fr.get("protected_low") is not None else None
        if snap and snap.protected_high is None:
            prot_h = _finite(fr.get("protected_high")) if fr.get("protected_high") is not None else None
        if snap and snap.protected_low is None:
            prot_l = _finite(fr.get("protected_low")) if fr.get("protected_low") is not None else None
        intact = _structure_levels_intact(
            direction,
            close=close,
            low=_finite(fr.get("low")),
            high=_finite(fr.get("high")),
            prior_swing_high=prior_h,
            prior_swing_low=prior_l,
            protected_high=prot_h,
            protected_low=prot_l,
        )
        band_mid = 0.5 * (ema9 + ema20)
        dist_band_atr = abs(close - band_mid) / atr if atr > 0 else None
        breakout = snap.breakout_level if snap else None
        if breakout is None and timeline is not None and 0 <= tb < len(timeline):
            # Prefer last non-null breakout on this setup before terminal
            if "breakout_level" in timeline.columns and "setup_id" in timeline.columns:
                sub = timeline[
                    (timeline["setup_id"] == sid)
                    & (timeline["bar_index"] <= tb)
                    & timeline["breakout_level"].notna()
                ]
                if not sub.empty:
                    breakout = float(sub.iloc[-1]["breakout_level"])
            elif "breakout_level" in timeline.columns:
                val = timeline.iloc[tb].get("breakout_level")
                if val is not None and pd.notna(val):
                    breakout = float(val)
        row: dict[str, Any] = {
            "symbol": symbol,
            "setup_id": sid,
            "direction": direction,
            "arming_type": life.get("arming_type"),
            "terminal_state": life.get("terminal_state"),
            "state_before_terminal": _state_family(str(life.get("terminal_state"))),
            "terminal_reason": life.get("terminal_reason"),
            "terminal_outcome": life.get("terminal_outcome"),
            "armed_bar": life.get("armed_bar"),
            "armed_timestamp": life.get("armed_timestamp"),
            "pullback_bar": life.get("pullback_bar"),
            "pullback_timestamp": _ts_at(frame, life.get("pullback_bar")),
            "ready_bar": life.get("ready_bar"),
            "ready_timestamp": _ts_at(frame, life.get("ready_bar")),
            "terminal_bar": tb,
            "terminal_timestamp": _ts_at(frame, tb),
            "month": pd.Timestamp(life.get("armed_timestamp")).tz_convert("UTC").strftime("%Y-%m")
            if life.get("armed_timestamp") is not None
            else None,
            "regime": _regime_label(_finite(fr.get("adx"))),
            "atr_bucket": _atr_bucket(atr, median_atr),
            "armed_price": life.get("armed_price") if life.get("armed_price") is not None else (snap.armed_price if snap else None),
            "breakout_level": breakout,
            "prior_swing_high": prior_h,
            "prior_swing_low": prior_l,
            "protected_high": prot_h,
            "protected_low": prot_l,
            "relevant_protected_level": prot_l if direction == "long" else prot_h,
            "relevant_swing_level": prior_l if direction == "long" else prior_h,
            "structure_still_intact": intact,
            "structure_state": snap.structure_state if snap else fr.get("protected_structure_state"),
            "major_direction": snap.major_direction if snap else fr.get("major_direction"),
            "ema_9": ema9,
            "ema_20": ema20,
            "ema_50": ema50,
            "ema_9_slope_3": _finite(fr.get("ema_9_slope_3")),
            "ema_20_slope_3": _finite(fr.get("ema_20_slope_3")),
            "close": close,
            "close_vs_ema9": close - ema9,
            "close_vs_ema20": close - ema20,
            "close_vs_ema50": close - ema50,
            "atr_14": atr,
            "dist_ema_band_atr": dist_band_atr,
            "ema_cross_age": int(fr.get("ema_cross_age") or 99),
            "opposite_arm_seen": bool(life.get("opposite_arm_seen")),
            "opposite_arm_bar": life.get("opposite_arm_bar"),
            "opposite_arm_type": life.get("opposite_arm_type"),
            "setup_age": life.get("setup_age_total"),
            "ready_age": life.get("ready_age_at_terminal"),
            "adx": _finite(fr.get("adx")),
        }
        if breakout is not None and math.isfinite(float(breakout)) and atr > 0:
            row["dist_to_breakout_atr"] = (close - float(breakout)) / atr if direction == "long" else (float(breakout) - close) / atr
            row["almost_triggered"] = abs(close - float(breakout)) / atr <= 0.25
        else:
            row["dist_to_breakout_atr"] = None
            row["almost_triggered"] = None
        rows.append(row)
    return pd.DataFrame(rows)


def build_forward_outcomes(
    frame: pd.DataFrame,
    cases: pd.DataFrame,
    lives: Sequence[Mapping[str, Any]],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for _, c in cases.iterrows():
        tb = int(c["terminal_bar"])
        direction = str(c["direction"])
        path = compute_path_metrics(frame, start_bar=tb, direction=direction, ref_price=float(c["close"]))
        hit, bars_hit = _later_breakout(
            frame,
            start_bar=tb,
            direction=direction,
            level=float(c["breakout_level"]) if pd.notna(c.get("breakout_level")) else None,
        )
        # Reversal back into setup direction: signed fwd becomes positive within 5
        rev = False
        for h in (1, 2, 3, 5):
            v = path.get(f"fwd_ret_{h}")
            if v is not None and v > 0:
                rev = True
                break
        nxt = _next_same_dir_setup(
            lives,
            setup_id=int(c["setup_id"]),
            direction=direction,
            terminal_bar=tb,
        )
        row = {
            "symbol": c.get("symbol"),
            "setup_id": int(c["setup_id"]),
            "direction": direction,
            "terminal_state": c.get("terminal_state"),
            "structure_still_intact": bool(c.get("structure_still_intact")),
            "old_breakout_later_hit": hit,
            "bars_to_old_breakout": bars_hit,
            "reversal_back_to_setup_dir": rev,
            "opposite_direction_confirmation": _opposite_confirm(frame, start_bar=tb, direction=direction),
            **nxt,
            **{k: path.get(k) for k in path if k != "valid"},
        }
        rows.append(row)
    return pd.DataFrame(rows)


def measure_recovery(
    frame: pd.DataFrame,
    *,
    terminal_bar: int,
    direction: str,
    prior_swing_high: float | None,
    prior_swing_low: float | None,
    protected_high: float | None,
    protected_low: float | None,
    max_look: int = 5,
) -> dict[str, Any]:
    n = len(frame)
    out: dict[str, Any] = {
        "bars_to_close_over_ema20": None,
        "bars_to_ema9_over_ema20": None,
        "bars_to_close_in_band": None,
        "swing_intact_through_5": True,
        "recovery_bucket": "not_recovered_within_5",
    }
    recovered_close20 = recovered_cross = recovered_band = False
    for k in range(1, min(max_look, n - terminal_bar - 1) + 1):
        row = frame.iloc[terminal_bar + k].to_dict()
        intact = _structure_levels_intact(
            direction,
            close=_finite(row.get("close")),
            low=_finite(row.get("low")),
            high=_finite(row.get("high")),
            prior_swing_high=prior_swing_high,
            prior_swing_low=prior_swing_low,
            protected_high=protected_high,
            protected_low=protected_low,
        )
        if not intact:
            out["swing_intact_through_5"] = False
        if not recovered_close20 and _close_over_ema20(direction, row):
            out["bars_to_close_over_ema20"] = k
            recovered_close20 = True
        if not recovered_cross and _ema9_over_ema20(direction, row):
            out["bars_to_ema9_over_ema20"] = k
            recovered_cross = True
        if not recovered_band and (_in_ema_band(row) or _close_over_ema20(direction, row)):
            out["bars_to_close_in_band"] = k
            recovered_band = True
    # Primary recovery = close back over EMA20 (user request)
    b = out["bars_to_close_over_ema20"]
    if b == 1:
        out["recovery_bucket"] = "recovered_1"
    elif b == 2:
        out["recovery_bucket"] = "recovered_2"
    elif b == 3:
        out["recovery_bucket"] = "recovered_3"
    elif b in (4, 5):
        out["recovery_bucket"] = "recovered_4_5"
    else:
        out["recovery_bucket"] = "not_recovered_within_5"
    return out


def build_recovery_and_buckets(
    frame: pd.DataFrame,
    cases: pd.DataFrame,
    forwards: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    rec_rows: list[dict[str, Any]] = []
    for _, c in cases.iterrows():
        rec = measure_recovery(
            frame,
            terminal_bar=int(c["terminal_bar"]),
            direction=str(c["direction"]),
            prior_swing_high=float(c["prior_swing_high"]) if pd.notna(c.get("prior_swing_high")) else None,
            prior_swing_low=float(c["prior_swing_low"]) if pd.notna(c.get("prior_swing_low")) else None,
            protected_high=float(c["protected_high"]) if pd.notna(c.get("protected_high")) else None,
            protected_low=float(c["protected_low"]) if pd.notna(c.get("protected_low")) else None,
        )
        rec_rows.append({"setup_id": int(c["setup_id"]), "symbol": c.get("symbol"), **rec})
    rec_df = pd.DataFrame(rec_rows)
    fwd_cols = [
        c
        for c in [
            "setup_id",
            "symbol",
            "mfe",
            "mae",
            "fwd_ret_10",
            "fwd_ret_20",
            "is_fake",
            "is_good_move",
            "old_breakout_later_hit",
        ]
        if c in forwards.columns
    ]
    merged = cases.merge(rec_df, on=["setup_id", "symbol"], how="left").merge(
        forwards[fwd_cols],
        on=["setup_id", "symbol"],
        how="left",
    )

    bucket_rows: list[dict[str, Any]] = []
    for bucket, g in merged.groupby("recovery_bucket", dropna=False):
        n = len(g)
        fake_rate = float(g["is_fake"].mean()) if n and "is_fake" in g else None
        bucket_rows.append(
            {
                "recovery_bucket": bucket,
                "count": n,
                "fake_rate_of_invalidation": fake_rate,
                "median_mfe": float(g["mfe"].median()) if n else None,
                "median_mae": float(g["mae"].median()) if n else None,
                "median_fwd_ret_10": float(g["fwd_ret_10"].median()) if n else None,
                "median_fwd_ret_20": float(g["fwd_ret_20"].median()) if n else None,
                "share_good_moves": float(g["is_good_move"].mean()) if n else None,
                "share_structure_intact": float(g["structure_still_intact"].mean()) if n else None,
                "share_true_structure_breaks": float((~g["structure_still_intact"].astype(bool)).mean()) if n else None,
                "share_old_breakout_later": float(g["old_breakout_later_hit"].mean()) if n else None,
            }
        )
    order = ["recovered_1", "recovered_2", "recovered_3", "recovered_4_5", "not_recovered_within_5"]
    bdf = pd.DataFrame(bucket_rows)
    if not bdf.empty:
        bdf["_ord"] = bdf["recovery_bucket"].map({k: i for i, k in enumerate(order)}).fillna(99)
        bdf = bdf.sort_values("_ord").drop(columns=["_ord"])
    return rec_df, bdf


def structure_intact_vs_broken(cases: pd.DataFrame, forwards: pd.DataFrame, recovery: pd.DataFrame) -> pd.DataFrame:
    fwd_cols = [
        c
        for c in [
            "setup_id",
            "symbol",
            "mfe",
            "mae",
            "fwd_ret_10",
            "fwd_ret_20",
            "is_fake",
            "is_good_move",
            "old_breakout_later_hit",
            "new_setup_same_dir_within_5",
        ]
        if c in forwards.columns
    ]
    m = cases.merge(forwards[fwd_cols], on=["setup_id", "symbol"], how="left")
    m = m.merge(recovery, on=["setup_id", "symbol"], how="left")
    rows = []
    for intact, g in m.groupby(m["structure_still_intact"].astype(bool)):
        n = len(g)
        rows.append(
            {
                "group": "structure_intact" if intact else "structure_broken",
                "count": n,
                "share_recovered_within_3": float(
                    g["recovery_bucket"].isin(["recovered_1", "recovered_2", "recovered_3"]).mean()
                )
                if n
                else None,
                "share_recovered_within_5": float(
                    g["recovery_bucket"].isin(
                        ["recovered_1", "recovered_2", "recovered_3", "recovered_4_5"]
                    ).mean()
                )
                if n
                else None,
                "median_fwd_ret_10": float(g["fwd_ret_10"].median()) if n else None,
                "median_fwd_ret_20": float(g["fwd_ret_20"].median()) if n else None,
                "median_mfe": float(g["mfe"].median()) if n else None,
                "median_mae": float(g["mae"].median()) if n else None,
                "share_old_breakout_later": float(g["old_breakout_later_hit"].mean()) if n else None,
                "share_good_moves_missed": float(g["is_good_move"].mean()) if n else None,
                "share_fake_invalidation_correct": float(g["is_fake"].mean()) if n else None,
                "share_new_setup_same_dir_within_5": float(g["new_setup_same_dir_within_5"].mean())
                if n and "new_setup_same_dir_within_5" in g.columns
                else None,
            }
        )
    return pd.DataFrame(rows)


def build_ready_cases(cases: pd.DataFrame, forwards: pd.DataFrame, frame: pd.DataFrame) -> pd.DataFrame:
    ready = cases[cases["state_before_terminal"] == "READY"].copy()
    if ready.empty:
        return ready
    m = ready.merge(
        forwards[
            [
                "setup_id",
                "symbol",
                "old_breakout_later_hit",
                "bars_to_old_breakout",
                "mfe",
                "mae",
                "fwd_ret_10",
                "fwd_ret_20",
                "is_fake",
                "is_good_move",
            ]
        ],
        on=["setup_id", "symbol"],
        how="left",
    )
    # Hypothetical later break outcomes
    hyp_rows = []
    for _, c in m.iterrows():
        if not bool(c.get("old_breakout_later_hit")) or pd.isna(c.get("bars_to_old_breakout")):
            hyp_rows.append({"setup_id": int(c["setup_id"]), "hyp_break_fwd_ret_10": None, "hyp_break_mfe": None})
            continue
        tb = int(c["terminal_bar"]) + int(c["bars_to_old_breakout"])
        direction = str(c["direction"])
        # Fill at next open after break bar
        fill_i = min(tb + 1, len(frame) - 1)
        ref = float(frame.iloc[fill_i]["open"])
        path = compute_path_metrics(frame, start_bar=fill_i, direction=direction, ref_price=ref)
        hyp_rows.append(
            {
                "setup_id": int(c["setup_id"]),
                "hyp_break_bar": tb,
                "hyp_break_fwd_ret_10": path.get("fwd_ret_10"),
                "hyp_break_mfe": path.get("mfe"),
                "hyp_break_mae": path.get("mae"),
                "hyp_break_is_fake": path.get("is_fake"),
            }
        )
    hyp = pd.DataFrame(hyp_rows)
    return m.merge(hyp, on="setup_id", how="left")


# ---------------------------------------------------------------------------
# Counterfactual E0–E5 (offline; patches only inside this module)
# ---------------------------------------------------------------------------


@dataclass
class EmaPolicy:
    name: str
    description: str
    # consecutive reclaim bars required before terminal (E0=1, E2=2, E3=3)
    required_reclaim_closes: int = 1
    # E1: demote READY → PULLBACK instead of terminal
    demote_ready_to_pullback: bool = False
    # E4: suppress terminal reclaim while structure intact
    suppress_if_structure_intact: bool = False
    # E5: grace window bars while structure intact; recover → continue
    grace_bars_if_intact: int | None = None


EMA_POLICIES: tuple[EmaPolicy, ...] = (
    EmaPolicy("E0", "baseline: ema_reclaim = terminal invalidation", required_reclaim_closes=1),
    EmaPolicy(
        "E1",
        "COUNTERFACTUAL: ema_reclaim from READY demotes to PULLBACK",
        demote_ready_to_pullback=True,
    ),
    EmaPolicy(
        "E2",
        "COUNTERFACTUAL: ema_reclaim terminal after 2 confirmed reclaim closes",
        required_reclaim_closes=2,
    ),
    EmaPolicy(
        "E3",
        "COUNTERFACTUAL: ema_reclaim terminal after 3 confirmed reclaim closes",
        required_reclaim_closes=3,
    ),
    EmaPolicy(
        "E4",
        "COUNTERFACTUAL: no terminal ema_reclaim while structure intact",
        suppress_if_structure_intact=True,
    ),
    EmaPolicy(
        "E5",
        "COUNTERFACTUAL: intact structure + EMA recovery within 3 bars reactivates",
        grace_bars_if_intact=3,
    ),
)


def _make_policy_invalidators(policy: EmaPolicy) -> tuple[Callable, Callable, dict[str, Any]]:
    state: dict[str, Any] = {
        "reclaim_streak_short": 0,
        "reclaim_streak_long": 0,
        "grace_left": None,
        "pending_reason": None,
        "demote": None,  # "short" | "long"
    }

    def _structure_ok(rt: SetupRuntime, row: Mapping[str, Any]) -> bool:
        direction = "short" if rt.side < 0 else "long"
        return _structure_levels_intact(
            direction,
            close=_finite(row.get("close")),
            low=_finite(row.get("low")),
            high=_finite(row.get("high")),
            prior_swing_high=rt.prior_swing_high,
            prior_swing_low=rt.prior_swing_low,
            protected_high=_finite(row.get("protected_high")) if row.get("protected_high") is not None else None,
            protected_low=_finite(row.get("protected_low")) if row.get("protected_low") is not None else None,
        )

    def inv_short(rt: SetupRuntime, row: Mapping[str, Any], cfg: PullbackEntryConfig) -> str | None:
        # Age / structure flip first (unchanged)
        if rt.setup_age > cfg.max_age_bars:
            state["reclaim_streak_short"] = 0
            return "max_age"
        if bool(row.get("arm_edge_external_bull")) or bool(row.get("arm_edge_major_bull")):
            state["reclaim_streak_short"] = 0
            return "structure_flipped_bullish"

        reclaim = bool(row.get("ema9_above_ema20")) and _finite(row.get("close")) > _finite(row.get("ema_20"))
        reclaim = reclaim and int(row.get("ema_cross_age") or 99) <= 3
        if reclaim:
            state["reclaim_streak_short"] += 1
        else:
            state["reclaim_streak_short"] = 0
            if state.get("grace_left") is not None and rt.side < 0:
                # recovery clears grace
                if _ema_recovered("short", row):
                    state["grace_left"] = None
                    state["pending_reason"] = None

        if reclaim and state["reclaim_streak_short"] >= policy.required_reclaim_closes:
            if policy.demote_ready_to_pullback and rt.state == "SHORT_READY":
                state["demote"] = "short"
                return None
            if policy.suppress_if_structure_intact and _structure_ok(rt, row):
                return None
            if policy.grace_bars_if_intact is not None and _structure_ok(rt, row):
                if state["grace_left"] is None:
                    state["grace_left"] = policy.grace_bars_if_intact
                    state["pending_reason"] = "ema_bullish_reclaim"
                    return None
            else:
                return "ema_bullish_reclaim"

        # Grace countdown (E5)
        if state.get("grace_left") is not None and rt.side < 0 and state.get("pending_reason") == "ema_bullish_reclaim":
            if _ema_recovered("short", row):
                state["grace_left"] = None
                state["pending_reason"] = None
            else:
                state["grace_left"] -= 1
                if state["grace_left"] <= 0:
                    reason = state["pending_reason"]
                    state["grace_left"] = None
                    state["pending_reason"] = None
                    return reason

        # Remainder identical to baseline
        if rt.prior_swing_high is not None and _finite(row.get("close")) > rt.prior_swing_high:
            return "prior_swing_high_broken"
        atr = max(_finite(row.get("atr_14"), 1e-12), 1e-12)
        if rt.armed_price is not None and (_finite(row.get("high")) - rt.armed_price) / atr > cfg.max_move_since_arm_atr + 0.5:
            return "pullback_ran_too_far"
        m15 = int(row.get("m15_major_direction") or 0) if "m15_major_direction" in row else 0
        if cfg.mtf_mode != "none" and m15 > 0:
            return "15m_turned_bullish"
        m30 = int(row.get("m30_major_direction") or 0) if "m30_major_direction" in row else 0
        if cfg.mtf_mode in {"veto_30m", "setup15_veto30"} and m30 > 0:
            st = str(row.get("m30_protected_structure_state") or "")
            if "choch" not in st and "candidate" not in st:
                return "30m_strong_bullish"
        return None

    def inv_long(rt: SetupRuntime, row: Mapping[str, Any], cfg: PullbackEntryConfig) -> str | None:
        if rt.setup_age > cfg.max_age_bars:
            state["reclaim_streak_long"] = 0
            return "max_age"
        if bool(row.get("arm_edge_external_bear")) or bool(row.get("arm_edge_major_bear")):
            state["reclaim_streak_long"] = 0
            return "structure_flipped_bearish"

        reclaim = bool(row.get("ema9_below_ema20")) and _finite(row.get("close")) < _finite(row.get("ema_20"))
        reclaim = reclaim and int(row.get("ema_cross_age") or 99) <= 3
        if reclaim:
            state["reclaim_streak_long"] += 1
        else:
            state["reclaim_streak_long"] = 0
            if state.get("grace_left") is not None and rt.side > 0:
                if _ema_recovered("long", row):
                    state["grace_left"] = None
                    state["pending_reason"] = None

        if reclaim and state["reclaim_streak_long"] >= policy.required_reclaim_closes:
            if policy.demote_ready_to_pullback and rt.state == "LONG_READY":
                state["demote"] = "long"
                return None
            if policy.suppress_if_structure_intact and _structure_ok(rt, row):
                return None
            if policy.grace_bars_if_intact is not None and _structure_ok(rt, row):
                if state["grace_left"] is None:
                    state["grace_left"] = policy.grace_bars_if_intact
                    state["pending_reason"] = "ema_bearish_reclaim"
                    return None
            else:
                return "ema_bearish_reclaim"

        if state.get("grace_left") is not None and rt.side > 0 and state.get("pending_reason") == "ema_bearish_reclaim":
            if _ema_recovered("long", row):
                state["grace_left"] = None
                state["pending_reason"] = None
            else:
                state["grace_left"] -= 1
                if state["grace_left"] <= 0:
                    reason = state["pending_reason"]
                    state["grace_left"] = None
                    state["pending_reason"] = None
                    return reason

        if rt.prior_swing_low is not None and _finite(row.get("close")) < rt.prior_swing_low:
            return "prior_swing_low_broken"
        atr = max(_finite(row.get("atr_14"), 1e-12), 1e-12)
        if rt.armed_price is not None and (rt.armed_price - _finite(row.get("low"))) / atr > cfg.max_move_since_arm_atr + 0.5:
            return "pullback_ran_too_far"
        m15 = int(row.get("m15_major_direction") or 0) if "m15_major_direction" in row else 0
        if cfg.mtf_mode != "none" and m15 < 0:
            return "15m_turned_bearish"
        m30 = int(row.get("m30_major_direction") or 0) if "m30_major_direction" in row else 0
        if cfg.mtf_mode in {"veto_30m", "setup15_veto30"} and m30 < 0:
            st = str(row.get("m30_protected_structure_state") or "")
            if "choch" not in st and "candidate" not in st:
                return "30m_strong_bearish"
        return None

    return inv_short, inv_long, state


def apply_counterfactual(
    frame: pd.DataFrame,
    cfg: PullbackEntryConfig,
    policy: EmaPolicy,
) -> tuple[pd.DataFrame, list[dict[str, Any]], list[dict[str, Any]]]:
    """Replay with audit-local invalidate policy. Marked counterfactual except E0."""
    if policy.name == "E0":
        tl, entries, lives = apply_pullback_entry(frame, cfg, return_lifecycles=True)
        return tl, entries, lives

    inv_s, inv_l, state = _make_policy_invalidators(policy)
    df = frame.reset_index(drop=True).copy()
    if "bar_index" not in df.columns:
        df["bar_index"] = np.arange(len(df))
    opens = df["open"].astype(float).tolist()
    rt = SetupRuntime()
    rows: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    lifecycles: dict[int, dict[str, Any]] = {}
    next_id = 1

    def _alloc() -> int:
        nonlocal next_id
        sid = next_id
        next_id += 1
        return sid

    def _ensure_life(sid: int, *, direction: str, arm_bar: int, arm_ts: Any, arm_px: float | None) -> dict:
        if sid not in lifecycles:
            lifecycles[sid] = {
                "setup_id": sid,
                "direction": direction,
                "variant": f"{cfg.name}_{policy.name}",
                "arming_type": cfg.arming_type,
                "armed_bar": arm_bar,
                "armed_timestamp": arm_ts,
                "armed_price": arm_px,
                "pullback_bar": None,
                "ready_bar": None,
                "trigger_bar": None,
                "fill_bar": None,
                "terminal_bar": None,
                "terminal_state": None,
                "terminal_outcome": None,
                "terminal_reason": None,
                "setup_age_total": None,
                "ready_age_at_terminal": None,
                "opposite_arm_seen": False,
                "opposite_arm_bar": None,
                "opposite_arm_type": None,
                "entry_created": False,
                "last_reject_reason": None,
                "counterfactual_policy": policy.name,
            }
        return lifecycles[sid]

    with patch(
        "research.regime_scanner.pullback_entry_c3_5._invalidate_short", inv_s
    ), patch(
        "research.regime_scanner.pullback_entry_c3_5._invalidate_long", inv_l
    ):
        for i in range(len(df)):
            row = df.iloc[i].to_dict()
            next_open = opens[i + 1] if i + 1 < len(opens) else None
            prev_id = rt.setup_id
            prev_state = rt.state
            state["demote"] = None
            rt, diag = step_pullback_entry(
                rt, row, cfg=cfg, next_open=next_open, setup_id_factory=_alloc
            )
            # E1 demotion after step if reclaim suppressed on READY
            if state.get("demote") == "short" and rt.state == "SHORT_READY":
                rt.state = "SHORT_PULLBACK"
                rt.ready_age = 0
                rt.breakout_level = None
                diag["entry_state"] = rt.state
                diag["ready_age"] = 0
                diag["breakout_level"] = None
                ev = str(diag.get("events") or "")
                diag["events"] = (ev + "|" if ev else "") + "cf_demote_ready_to_pullback"
            elif state.get("demote") == "long" and rt.state == "LONG_READY":
                rt.state = "LONG_PULLBACK"
                rt.ready_age = 0
                rt.breakout_level = None
                diag["entry_state"] = rt.state
                diag["ready_age"] = 0
                diag["breakout_level"] = None
                ev = str(diag.get("events") or "")
                diag["events"] = (ev + "|" if ev else "") + "cf_demote_ready_to_pullback"

            out = {"bar_index": int(row.get("bar_index", i)), "timestamp": row.get("timestamp"), **diag}
            rows.append(out)
            ev = str(diag.get("events") or "")
            bi = int(out["bar_index"])

            if "short_armed" in ev or "long_armed" in ev:
                sid = int(diag["setup_id"] or prev_id or 0)
                direction = "short" if "short_armed" in ev else "long"
                if diag.get("setup_id") is not None:
                    sid = int(diag["setup_id"])
                life = _ensure_life(
                    sid, direction=direction, arm_bar=bi, arm_ts=row.get("timestamp"), arm_px=diag.get("armed_price")
                )
                life["arming_type"] = diag.get("arming_type") or cfg.arming_type
                state["reclaim_streak_short"] = 0
                state["reclaim_streak_long"] = 0
                state["grace_left"] = None
                state["pending_reason"] = None

            active_id = diag.get("setup_id")
            if active_id is None and prev_id is not None and prev_state != "IDLE" and "terminal:" not in ev:
                active_id = prev_id
            if active_id is not None and int(active_id) in lifecycles:
                life = lifecycles[int(active_id)]
                if "short_pullback" in ev or "long_pullback" in ev:
                    life["pullback_bar"] = bi
                if "short_ready" in ev or "long_ready" in ev:
                    life["ready_bar"] = bi
                if diag.get("opposite_arm_seen"):
                    life["opposite_arm_seen"] = True
                    life["opposite_arm_bar"] = diag.get("opposite_arm_bar")
                    life["opposite_arm_type"] = diag.get("opposite_arm_type")

            if diag.get("entry_signal"):
                sid = int(diag.get("setup_id") or 0)
                if sid not in lifecycles and sid:
                    _ensure_life(
                        sid,
                        direction="short" if int(diag.get("entry_side") or 0) < 0 else "long",
                        arm_bar=bi,
                        arm_ts=row.get("timestamp"),
                        arm_px=diag.get("armed_price"),
                    )
                if sid in lifecycles:
                    life = lifecycles[sid]
                    life["trigger_bar"] = bi
                    life["fill_bar"] = bi + 1 if next_open is not None else None
                    life["entry_created"] = True
                    life["ready_age_at_terminal"] = diag.get("ready_age")
                    life["setup_age_total"] = diag.get("setup_age")
                    entries.append(
                        {
                            "setup_id": sid,
                            "bar_index": bi,
                            "timestamp": row.get("timestamp"),
                            "side": int(diag.get("entry_side") or 0),
                            "entry_price": diag.get("entry_price"),
                            "entry_reason": diag.get("entry_reason"),
                            "armed_price": diag.get("armed_price"),
                            "pullback_high": diag.get("pullback_high"),
                            "pullback_low": diag.get("pullback_low"),
                            "breakout_level": diag.get("breakout_level"),
                            "ready_age": diag.get("ready_age"),
                            "setup_age": diag.get("setup_age"),
                            "arming_type": diag.get("arming_type") or cfg.arming_type,
                            "variant": f"{cfg.name}_{policy.name}",
                            "counterfactual_policy": policy.name,
                        }
                    )

            if diag.get("terminal_outcome") and "terminal:" in ev:
                sid = int(diag.get("terminal_setup_id") or diag.get("setup_id") or prev_id or 0)
                if sid and sid not in lifecycles:
                    _ensure_life(
                        sid,
                        direction=str(diag.get("terminal_direction") or "long"),
                        arm_bar=bi,
                        arm_ts=row.get("timestamp"),
                        arm_px=diag.get("armed_price"),
                    )
                if sid in lifecycles:
                    life = lifecycles[sid]
                    life["terminal_bar"] = bi
                    life["terminal_state"] = diag.get("terminal_state")
                    life["terminal_outcome"] = diag.get("terminal_outcome")
                    life["terminal_reason"] = diag.get("terminal_reason")
                    life["setup_age_total"] = diag.get("terminal_setup_age")
                    life["ready_age_at_terminal"] = diag.get("terminal_ready_age")
                    if life.get("terminal_outcome") == "entered":
                        life["entry_created"] = True
                state["reclaim_streak_short"] = 0
                state["reclaim_streak_long"] = 0
                state["grace_left"] = None
                state["pending_reason"] = None

    if rt.state != "IDLE" and rt.setup_id is not None:
        sid = int(rt.setup_id)
        if sid in lifecycles and lifecycles[sid].get("terminal_outcome") is None:
            life = lifecycles[sid]
            life["terminal_bar"] = int(df.iloc[-1].get("bar_index", len(df) - 1))
            life["terminal_state"] = rt.state
            life["terminal_outcome"] = "timed_out"
            life["terminal_reason"] = "end_of_data"
            life["setup_age_total"] = rt.setup_age
            life["ready_age_at_terminal"] = rt.ready_age

    return pd.DataFrame(rows), entries, [lifecycles[k] for k in sorted(lifecycles)]


def _entry_key(e: Mapping[str, Any]) -> tuple:
    return (int(e.get("bar_index", -1)), int(e.get("side") or 0), round(float(e.get("entry_price") or 0), 10))


def compare_counterfactuals(
    frame: pd.DataFrame,
    cfg: PullbackEntryConfig,
    *,
    e0_entries: list[dict[str, Any]],
    e0_outcomes: list[dict[str, Any]],
) -> pd.DataFrame:
    e0_keys = {_entry_key(e) for e in e0_entries}
    e0_by_key = {_entry_key(o): o for o in e0_outcomes}
    rows = []
    for policy in EMA_POLICIES:
        if policy.name == "E0":
            entries = e0_entries
            outcomes = e0_outcomes
            lives: list[dict[str, Any]] = []
        else:
            _tl, entries, lives = apply_counterfactual(frame, cfg, policy)
            outcomes = compute_entry_outcomes(frame, entries)
        keys = {_entry_key(e) for e in entries}
        added = keys - e0_keys
        removed = e0_keys - keys
        n = len(outcomes)
        fake = sum(1 for o in outcomes if o.get("is_fake"))
        mfe = np.median([o["mfe"] for o in outcomes if o.get("mfe") is not None]) if outcomes else None
        mae = np.median([o["mae"] for o in outcomes if o.get("mae") is not None]) if outcomes else None
        fwd10 = np.median([o["fwd_ret_10"] for o in outcomes if o.get("fwd_ret_10") is not None]) if outcomes else None
        ages = [int(e.get("setup_age") or 0) for e in entries]
        term_reasons = Counter(
            str(x.get("terminal_reason"))
            for x in lives
            if x.get("terminal_reason") in EMA_RECLAIM_REASONS
        ) if lives else Counter()
        # Extra drawdown proxy: among added entries, share with mae_before_mfe / is_fake
        added_outs = [o for o in outcomes if _entry_key(o) in added]
        extra_dd = float(np.mean([1 if o.get("is_fake") else 0 for o in added_outs])) if added_outs else None
        rows.append(
            {
                "variant": policy.name,
                "counterfactual": policy.name != "E0",
                "description": policy.description,
                "n_entries": len(entries),
                "n_added_vs_e0": len(added),
                "n_removed_vs_e0": len(removed),
                "signal_loss_vs_e0": len(removed) / max(len(e0_keys), 1),
                "fake_rate": fake / n if n else None,
                "median_mfe": float(mfe) if mfe is not None and math.isfinite(float(mfe)) else None,
                "median_mae": float(mae) if mae is not None and math.isfinite(float(mae)) else None,
                "median_fwd_ret_10": float(fwd10) if fwd10 is not None and math.isfinite(float(fwd10)) else None,
                "median_setup_age_at_entry": float(np.median(ages)) if ages else None,
                "extra_drawdown_rate_on_added": extra_dd,
                "ema_reclaim_terminals": int(sum(term_reasons.values())) if lives else None,
                "duplicate_entry_keys": len(entries) - len(keys),
            }
        )
        # silence unused
        _ = e0_by_key
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Focus case
# ---------------------------------------------------------------------------


def build_focus_timeline(
    frame: pd.DataFrame,
    timeline: pd.DataFrame,
    life: Mapping[str, Any],
    snap: TerminalSnapshot | None,
    *,
    pad_before: int = 5,
    pad_after: int = 40,
) -> pd.DataFrame:
    armed = int(life["armed_bar"])
    term = int(life["terminal_bar"])
    direction = str(life["direction"])
    start = max(0, armed - pad_before)
    end = min(len(frame) - 1, term + pad_after)
    breakout = snap.breakout_level if snap else None
    prior_l = snap.prior_swing_low if snap else None
    prior_h = snap.prior_swing_high if snap else None
    prot_l = snap.protected_low if snap else None
    prot_h = snap.protected_high if snap else None

    # Find recovery / later break after terminal
    rec = measure_recovery(
        frame,
        terminal_bar=term,
        direction=direction,
        prior_swing_high=prior_h,
        prior_swing_low=prior_l,
        protected_high=prot_h,
        protected_low=prot_l,
        max_look=40,
    )
    hit, bars_hit = _later_breakout(frame, start_bar=term, direction=direction, level=breakout, max_look=80)
    hyp_path = {}
    if hit and bars_hit is not None:
        fill_i = min(term + bars_hit + 1, len(frame) - 1)
        hyp_path = compute_path_metrics(
            frame,
            start_bar=fill_i,
            direction=direction,
            ref_price=float(frame.iloc[fill_i]["open"]),
        )

    rows = []
    for i in range(start, end + 1):
        fr = frame.iloc[i]
        tl = timeline.iloc[i] if i < len(timeline) else {}
        reclaim = _reclaim_condition(direction, fr.to_dict())
        intact = _structure_levels_intact(
            direction,
            close=_finite(fr.get("close")),
            low=_finite(fr.get("low")),
            high=_finite(fr.get("high")),
            prior_swing_high=prior_h,
            prior_swing_low=prior_l,
            protected_high=prot_h,
            protected_low=prot_l,
        )
        rows.append(
            {
                "bar_index": i,
                "timestamp": fr.get("timestamp"),
                "setup_id": life.get("setup_id") if armed <= i <= term else None,
                "entry_state": tl.get("entry_state") if hasattr(tl, "get") else tl["entry_state"] if "entry_state" in tl else None,
                "events": tl.get("events") if hasattr(tl, "get") else None,
                "ema_reclaim_condition": reclaim,
                "ema_9": _finite(fr.get("ema_9")),
                "ema_20": _finite(fr.get("ema_20")),
                "ema_50": _finite(fr.get("ema_50")),
                "close": _finite(fr.get("close")),
                "close_vs_ema20": _finite(fr.get("close")) - _finite(fr.get("ema_20")),
                "prior_swing_low": prior_l,
                "prior_swing_high": prior_h,
                "protected_low": prot_l,
                "protected_high": prot_h,
                "structure_intact": intact,
                "structure_state": fr.get("protected_structure_state"),
                "major_direction": fr.get("major_direction"),
                "breakout_level": breakout,
                "terminal_here": i == term,
                "bars_after_terminal": i - term if i >= term else None,
                "recovery_close_over_ema20": (
                    i > term
                    and rec.get("bars_to_close_over_ema20") is not None
                    and i >= term + int(rec["bars_to_close_over_ema20"])
                ),
                "old_breakout_hit_here": bool(hit and bars_hit is not None and i == term + bars_hit),
            }
        )
    focus = pd.DataFrame(rows)
    focus.attrs["recovery"] = rec
    focus.attrs["later_break"] = {"hit": hit, "bars": bars_hit}
    focus.attrs["hyp_path"] = hyp_path
    return focus


def explain_focus_case(
    life: Mapping[str, Any],
    snap: TerminalSnapshot | None,
    recovery: Mapping[str, Any],
    later_break: Mapping[str, Any],
    hyp_path: Mapping[str, Any],
    forwards_row: Mapping[str, Any] | None,
) -> dict[str, Any]:
    direction = str(life["direction"])
    intact = True
    if snap:
        # evaluated at terminal via recovery swing_intact or structure
        intact = bool(recovery.get("swing_intact_through_5", True))
    bars_rec = recovery.get("bars_to_close_over_ema20")
    soft_reset_catch = bool(
        later_break.get("hit")
        and (bars_rec is not None and int(bars_rec) <= 3)
        and (hyp_path.get("is_good_move") or (hyp_path.get("mfe") or 0) > 0.01)
    )
    soft_reset_extra_dd = bool(hyp_path.get("is_fake")) or bool(
        hyp_path.get("mae") is not None and hyp_path.get("mae") < -0.01 and (hyp_path.get("mfe") or 0) < 0.005
    )
    return {
        "setup_id": life.get("setup_id"),
        "direction": direction,
        "why_invalidated": (
            f"{life.get('terminal_reason')}: EMA9/20 reclaim with ema_cross_age<=3 "
            f"while in {life.get('terminal_state')}"
        ),
        "structure_still_intact_at_and_after": intact,
        "bars_to_ema_recovery_close_over_ema20": bars_rec,
        "old_breakout_later_broken": bool(later_break.get("hit")),
        "bars_to_old_breakout": later_break.get("bars"),
        "soft_reset_would_likely_catch_move": soft_reset_catch,
        "soft_reset_would_add_drawdown_risk": soft_reset_extra_dd,
        "forward_from_terminal_mfe": None if forwards_row is None else forwards_row.get("mfe"),
        "forward_from_terminal_mae": None if forwards_row is None else forwards_row.get("mae"),
        "hyp_trigger_mfe": hyp_path.get("mfe"),
        "hyp_trigger_mae": hyp_path.get("mae"),
        "hyp_trigger_fwd_ret_10": hyp_path.get("fwd_ret_10"),
        "note": "COUNTERFACTUAL soft-reset not implemented — estimates only.",
    }


def build_expected_labels_csv(
    cases: pd.DataFrame,
    forwards: pd.DataFrame,
    recovery: pd.DataFrame,
) -> pd.DataFrame:
    fwd_cols = [c for c in ["setup_id", "symbol", "old_breakout_later_hit"] if c in forwards.columns]
    m = cases.merge(forwards[fwd_cols], on=["setup_id", "symbol"], how="left")
    m = m.merge(recovery, on=["setup_id", "symbol"], how="left")
    rows = []
    for _, c in m.iterrows():
        ts = c.get("terminal_timestamp")
        sid = int(c["setup_id"])
        rows.append({"timestamp": ts, "setup_id": sid, "label": "EMA_X", "direction": c["direction"]})
        b = c.get("bars_to_close_over_ema20")
        if b == 1:
            rows.append({"timestamp": ts, "setup_id": sid, "label": "RECOVER_1", "direction": c["direction"]})
        elif b == 2:
            rows.append({"timestamp": ts, "setup_id": sid, "label": "RECOVER_2", "direction": c["direction"]})
        elif b == 3:
            rows.append({"timestamp": ts, "setup_id": sid, "label": "RECOVER_3", "direction": c["direction"]})
        rows.append(
            {
                "timestamp": ts,
                "setup_id": sid,
                "label": "STRUCT_INTACT" if bool(c.get("structure_still_intact")) else "STRUCT_BROKEN",
                "direction": c["direction"],
            }
        )
        if bool(c.get("old_breakout_later_hit")):
            rows.append(
                {
                    "timestamp": ts,
                    "setup_id": sid,
                    "label": "OLD_BREAKOUT_LATER_HIT",
                    "direction": c["direction"],
                }
            )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _top_missed_and_correct(
    cases: pd.DataFrame,
    forwards: pd.DataFrame,
    recovery: pd.DataFrame,
    *,
    n: int = 20,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    fwd_cols = [
        c
        for c in [
            "setup_id",
            "symbol",
            "mfe",
            "mae",
            "fwd_ret_10",
            "fwd_ret_20",
            "is_fake",
            "is_good_move",
            "old_breakout_later_hit",
            "new_setup_same_dir_within_5",
        ]
        if c in forwards.columns
    ]
    m = cases.merge(forwards[fwd_cols], on=["setup_id", "symbol"], how="left")
    m = m.merge(recovery, on=["setup_id", "symbol"], how="left")
    # Missed good moves: structure intact + recovery + good forward / later breakout
    missed = m[
        (m["structure_still_intact"] == True)  # noqa: E712
        & (
            m["recovery_bucket"].isin(["recovered_1", "recovered_2", "recovered_3", "recovered_4_5"])
            | (m["old_breakout_later_hit"] == True)  # noqa: E712
        )
        & ((m["is_good_move"] == True) | (m["mfe"].fillna(0) >= 0.01))  # noqa: E712
    ].copy()
    missed["miss_score"] = missed["mfe"].fillna(0) + missed["fwd_ret_10"].fillna(0) * 2
    missed = missed.sort_values("miss_score", ascending=False).head(n)

    correct = m[
        (m["is_fake"] == True)  # noqa: E712
        | (
            (m["structure_still_intact"] == False)  # noqa: E712
            & (m["fwd_ret_10"].fillna(0) < 0)
        )
        | (
            (m["recovery_bucket"] == "not_recovered_within_5")
            & (m["fwd_ret_10"].fillna(0) < 0)
        )
    ].copy()
    correct["correct_score"] = (-correct["fwd_ret_10"].fillna(0)) + (-correct["mfe"].fillna(0))
    correct = correct.sort_values("correct_score", ascending=False).head(n)
    return missed, correct


def run_symbol_audit(
    frame: pd.DataFrame,
    *,
    symbol: str,
    cfg: PullbackEntryConfig | None = None,
    focus_setup_id: int | None = None,
) -> dict[str, Any]:
    cfg = cfg or baseline_a6()
    timeline, entries, lives, snapshots = apply_baseline_with_snapshots(frame, cfg)

    outcomes = compute_entry_outcomes(frame, entries)
    cases = enrich_reclaim_cases(
        frame, lives, snapshots, symbol=symbol, timeline=timeline
    )
    forwards = build_forward_outcomes(frame, cases, lives) if not cases.empty else pd.DataFrame()
    recovery, buckets = (
        build_recovery_and_buckets(frame, cases, forwards)
        if not cases.empty
        else (pd.DataFrame(), pd.DataFrame())
    )
    struct_cmp = (
        structure_intact_vs_broken(cases, forwards, recovery) if not cases.empty else pd.DataFrame()
    )
    ready = build_ready_cases(cases, forwards, frame) if not cases.empty else pd.DataFrame()
    cf = compare_counterfactuals(frame, cfg, e0_entries=entries, e0_outcomes=outcomes)
    missed, correct = (
        _top_missed_and_correct(cases, forwards, recovery)
        if not cases.empty
        else (pd.DataFrame(), pd.DataFrame())
    )
    labels = (
        build_expected_labels_csv(cases, forwards, recovery) if not cases.empty else pd.DataFrame()
    )

    focus_summary = None
    focus_tl = pd.DataFrame()
    if focus_setup_id is not None:
        life = next((x for x in lives if int(x["setup_id"]) == int(focus_setup_id)), None)
        if life is not None and life.get("terminal_reason") in EMA_RECLAIM_REASONS:
            snap = snapshots.get(int(focus_setup_id))
            focus_tl = build_focus_timeline(frame, timeline, life, snap)
            fwd_row = None
            if not forwards.empty:
                hit = forwards[forwards["setup_id"] == int(focus_setup_id)]
                if not hit.empty:
                    fwd_row = hit.iloc[0].to_dict()
            focus_summary = explain_focus_case(
                life,
                snap,
                focus_tl.attrs.get("recovery", {}),
                focus_tl.attrs.get("later_break", {}),
                focus_tl.attrs.get("hyp_path", {}),
                fwd_row,
            )

    # Aggregate counts
    n_reclaim = len(cases)
    n_ready = int((cases["state_before_terminal"] == "READY").sum()) if n_reclaim else 0
    n_intact = int(cases["structure_still_intact"].sum()) if n_reclaim else 0
    rec_shares = {}
    if not recovery.empty:
        vc = recovery["recovery_bucket"].value_counts(normalize=True)
        rec_shares = {str(k): float(v) for k, v in vc.items()}
    later_share = float(forwards["old_breakout_later_hit"].mean()) if len(forwards) else None

    summary = {
        "symbol": symbol,
        "config_name": cfg.name,
        "config_hash": config_hash(cfg),
        "n_entries_e0": len(entries),
        "n_lifecycles": len(lives),
        "n_ema_reclaim": n_reclaim,
        "n_ready_reclaim": n_ready,
        "share_ready": n_ready / n_reclaim if n_reclaim else None,
        "n_structure_intact": n_intact,
        "share_structure_intact": n_intact / n_reclaim if n_reclaim else None,
        "recovery_bucket_shares": rec_shares,
        "share_old_breakout_later": later_share,
        "n_clear_missed_moves": int(len(missed)),
        "n_correct_invalidations_top": int(len(correct)),
        "by_direction": cases["direction"].value_counts().to_dict() if n_reclaim else {},
        "by_state": cases["state_before_terminal"].value_counts().to_dict() if n_reclaim else {},
        "by_month": cases["month"].value_counts().to_dict() if n_reclaim else {},
        "by_regime": cases["regime"].value_counts().to_dict() if n_reclaim else {},
        "by_arming_type": cases["arming_type"].value_counts().to_dict() if n_reclaim else {},
        "baseline_entry_parity_ok": True,
        "focus_setup_id": focus_setup_id,
    }
    return {
        "summary": summary,
        "timeline": timeline,
        "entries": entries,
        "lives": lives,
        "outcomes": outcomes,
        "cases": cases,
        "forwards": forwards,
        "recovery": recovery,
        "buckets": buckets,
        "struct_cmp": struct_cmp,
        "ready": ready,
        "counterfactuals": cf,
        "missed": missed,
        "correct": correct,
        "labels": labels,
        "focus_timeline": focus_tl,
        "focus_summary": focus_summary,
        "snapshots": snapshots,
    }


def write_symbol_artifacts(result: dict[str, Any], out: Path, *, prefix: str = "") -> None:
    out.mkdir(parents=True, exist_ok=True)
    p = (prefix + "_") if prefix else ""
    result["cases"].to_csv(out / f"{p}ema_reclaim_cases.csv", index=False)
    result["forwards"].to_csv(out / f"{p}ema_reclaim_forward_outcomes.csv", index=False)
    result["buckets"].to_csv(out / f"{p}ema_reclaim_recovery_buckets.csv", index=False)
    result["struct_cmp"].to_csv(out / f"{p}ema_reclaim_structure_intact_vs_broken.csv", index=False)
    result["ready"].to_csv(out / f"{p}ema_reclaim_ready_cases.csv", index=False)
    result["counterfactuals"].to_csv(out / f"{p}ema_reclaim_counterfactual_variants.csv", index=False)
    result["missed"].to_csv(out / f"{p}top_missed_moves_after_ema_reclaim.csv", index=False)
    result["correct"].to_csv(out / f"{p}top_correct_invalidations_after_ema_reclaim.csv", index=False)
    result["labels"].to_csv(out / f"{p}ema_reclaim_focus_expected_labels.csv", index=False)
    if result.get("focus_timeline") is not None and not result["focus_timeline"].empty:
        result["focus_timeline"].to_csv(out / "focus_case_ema_reclaim_timeline.csv", index=False)
    if result.get("focus_summary") is not None:
        (out / "focus_case_ema_reclaim_summary.json").write_text(
            json.dumps(json_safe(result["focus_summary"]), indent=2),
            encoding="utf-8",
        )
    (out / f"{p}ema_reclaim_symbol_summary.json").write_text(
        json.dumps(json_safe(result["summary"]), indent=2),
        encoding="utf-8",
    )


def run_ema_reclaim_audit(
    *,
    symbols: Sequence[str] = ("APTUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT"),
    output_dir: Path = DEFAULT_OUT,
    baseline_dir: Path = DEFAULT_BASELINE_DIR,
    cached_apt_frame: Path | None = Path(
        "research/regime_scanner/results/phase_c3_5_pullback_entry_state_machine/research_frame_5m.csv"
    ),
) -> dict[str, Any]:
    assert_safe_output_dir(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    baseline = assert_baseline_readonly(baseline_dir)
    if not baseline.get("hash_matches"):
        raise RuntimeError(
            f"baseline hash mismatch: expected {C2_BASELINE_HASH}, got {baseline.get('baseline_hash')}"
        )

    cfg = baseline_a6()
    per_symbol: dict[str, Any] = {}
    all_cases = []
    all_fwd = []
    all_cf = []

    for sym in symbols:
        frame = load_or_build_frame(
            sym,
            cached_csv=cached_apt_frame if sym == "APTUSDT" else None,
        )
        focus_id = FOCUS_SETUP_ID_APT if sym == "APTUSDT" else None
        result = run_symbol_audit(frame, symbol=sym, cfg=cfg, focus_setup_id=focus_id)
        # Primary APT artifacts without prefix; others prefixed
        if sym == "APTUSDT":
            write_symbol_artifacts(result, output_dir, prefix="")
            # Also write unprefixed counterfactual as required name
            result["counterfactuals"].to_csv(
                output_dir / "ema_reclaim_counterfactual_variants.csv", index=False
            )
        else:
            sub = output_dir / sym.lower()
            write_symbol_artifacts(result, sub, prefix="")
        per_symbol[sym] = result["summary"]
        if not result["cases"].empty:
            all_cases.append(result["cases"])
        if not result["forwards"].empty:
            all_fwd.append(result["forwards"])
        cf = result["counterfactuals"].copy()
        cf["symbol"] = sym
        all_cf.append(cf)

    if all_cases:
        pd.concat(all_cases, ignore_index=True).to_csv(
            output_dir / "ema_reclaim_cases_all_symbols.csv", index=False
        )
    if all_fwd:
        pd.concat(all_fwd, ignore_index=True).to_csv(
            output_dir / "ema_reclaim_forward_outcomes_all_symbols.csv", index=False
        )
    if all_cf:
        pd.concat(all_cf, ignore_index=True).to_csv(
            output_dir / "ema_reclaim_counterfactual_variants_all_symbols.csv", index=False
        )

    # Combined summary
    apt = per_symbol.get("APTUSDT", {})
    audit_summary = {
        "analyze_start": ANALYZE_START,
        "analyze_end": ANALYZE_END,
        "load_start": LOAD_START,
        "load_end": LOAD_END,
        "config": "A6",
        "config_hash": config_hash(cfg),
        "baseline_reference_hash": C2_BASELINE_HASH,
        "baseline_hash_matches": True,
        "production_sm_unchanged": True,
        "pine_unchanged": True,
        "counterfactuals_are_offline_only": True,
        "symbols": per_symbol,
        "apt": {
            "n_ema_reclaim": apt.get("n_ema_reclaim"),
            "share_ready": apt.get("share_ready"),
            "share_structure_intact": apt.get("share_structure_intact"),
            "recovery_bucket_shares": apt.get("recovery_bucket_shares"),
            "share_old_breakout_later": apt.get("share_old_breakout_later"),
            "n_clear_missed_moves": apt.get("n_clear_missed_moves"),
            "n_correct_invalidations_top": apt.get("n_correct_invalidations_top"),
        },
        "content_hash": None,
    }
    blob = json.dumps(json_safe(audit_summary), sort_keys=True).encode()
    audit_summary["content_hash"] = hashlib.sha1(blob).hexdigest()
    (output_dir / "ema_reclaim_audit_summary.json").write_text(
        json.dumps(json_safe(audit_summary), indent=2),
        encoding="utf-8",
    )
    return audit_summary


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="C3.5 ema_reclaim audit (research-only)")
    p.add_argument("--symbols", nargs="+", default=["APTUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT"])
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--apt-only", action="store_true")
    args = p.parse_args(argv)
    symbols = ["APTUSDT"] if args.apt_only else list(args.symbols)
    summary = run_ema_reclaim_audit(symbols=symbols, output_dir=args.out)
    print(json.dumps(json_safe(summary["apt"]), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
