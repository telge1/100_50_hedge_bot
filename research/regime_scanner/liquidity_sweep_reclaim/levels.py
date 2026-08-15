"""Level families L1 (C3.1 range) / L2 (C3.4B protected). L3 unavailable."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.liquidity_sweep_reclaim.config import LSRConfig, default_config
from research.regime_scanner.liquidity_sweep_reclaim.models import LevelSnapshot
from research.regime_scanner.trend_audit_shared_replay import PreparedBar
from research.regime_scanner.trend_regime_classifier import (
    config_c3,
    precompute_regime_arrays,
    replay_regime_variant,
)
from research.regime_scanner.trend_structure import MarketStructureState


L3_AVAILABLE = False
L3_UNAVAILABLE_REASON = (
    "No existing causal equal-swing cluster helper with fixed 0.20 ATR tolerance; "
    "improvising clustering would violate reuse rules."
)


def _empty_structure(tf: str = "15m") -> MarketStructureState:
    return MarketStructureState(timeframe=tf)


def _ts(value: object) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def attach_c31_range_columns(
    frame: pd.DataFrame,
    *,
    analyze_start: pd.Timestamp,
    analyze_end: pd.Timestamp,
    c31_variant: str = "balanced",
) -> pd.DataFrame:
    """Attach C3.1 range bounds via causal replay on 15m OHLCV.

    Structure events are empty stubs — range score/bounds still update from price arrays.
    Bounds used for sweeps must be taken from the *prior* bar (see eligible_levels).
    """
    out = frame.copy()
    if "atr" not in out.columns and "atr_14" in out.columns:
        out["atr"] = out["atr_14"]
    if "decision_time" not in out.columns:
        out["decision_time"] = pd.to_datetime(out["timestamp"], utc=True) + pd.Timedelta(minutes=15)

    cfg = config_c3(c31_variant)
    arrays = precompute_regime_arrays(
        out,
        efficiency_window=cfg.efficiency_window,
        net_move_window=cfg.net_move_window,
        overlap_window=cfg.overlap_window,
        range_width_window=cfg.range_width_window,
        range_lookback=cfg.range_lookback,
        failed_breakout_window=cfg.failed_breakout_window,
        alternating_window=cfg.alternating_window,
    )
    prepared: list[PreparedBar] = []
    empty = _empty_structure("15m")
    # Fast path: build PreparedBars without DataFrame.iterrows overhead
    ts_col = pd.to_datetime(out["timestamp"], utc=True)
    dec_col = (
        pd.to_datetime(out["decision_time"], utc=True)
        if "decision_time" in out.columns
        else ts_col + pd.Timedelta(minutes=15)
    )
    atr_vals = (
        out["atr"].to_numpy(dtype=float)
        if "atr" in out.columns
        else out["atr_14"].to_numpy(dtype=float)
    )
    for i in range(len(out)):
        row_dict = {
            "timestamp": ts_col.iloc[i],
            "decision_time": dec_col.iloc[i],
            "open": float(out["open"].iloc[i]),
            "high": float(out["high"].iloc[i]),
            "low": float(out["low"].iloc[i]),
            "close": float(out["close"].iloc[i]),
            "atr": float(atr_vals[i]) if np.isfinite(atr_vals[i]) else np.nan,
            "atr_14": float(atr_vals[i]) if np.isfinite(atr_vals[i]) else np.nan,
        }
        prepared.append(
            PreparedBar(
                bar_index=i,
                decision_time=_ts(dec_col.iloc[i]),
                row=row_dict,
                events_5m=[],
                structure_5m=empty,
                structure_15m=empty,
                structure_30m=empty,
                last_15m_bucket=None,
                last_30m_bucket=None,
                consecutive_bearish_closes=0,
                consecutive_bullish_closes=0,
                bars_since_ll=0,
                bars_since_hh=0,
                scores={},
                structure_skipped=False,
            )
        )
    a0 = _ts(analyze_start)
    a1 = _ts(analyze_end)
    # Replay over full frame so warmup builds range state before analyze_start.
    replay_start = _ts(out["decision_time"].iloc[0]) if len(out) else a0
    replay = replay_regime_variant(
        prepared,
        arrays=arrays,
        cfg=cfg,
        analyze_start=replay_start,
        analyze_end=_ts(out["decision_time"].iloc[-1]) if len(out) else a1,
    )
    timeline = replay.get("timeline") or []
    by_idx = {int(r["bar_index"]): r for r in timeline}

    n = len(out)
    cols = {
        "c31_in_range": np.zeros(n, dtype=bool),
        "c31_range_high": np.full(n, np.nan),
        "c31_range_low": np.full(n, np.nan),
        "c31_range_age": np.zeros(n, dtype=int),
        "c31_range_width_atr": np.full(n, np.nan),
        "c31_range_score": np.full(n, np.nan),
        "c31_box_efficiency": np.full(n, np.nan),
        "c31_bound_drift": np.full(n, np.nan),
        "c31_failed_breakout_event": np.zeros(n, dtype=bool),
        "c31_state": np.array([""] * n, dtype=object),
    }
    for i in range(n):
        r = by_idx.get(i)
        if r is None:
            continue
        cols["c31_in_range"][i] = bool(r.get("in_range"))
        rh, rl = r.get("range_high"), r.get("range_low")
        if rh is not None:
            cols["c31_range_high"][i] = float(rh)
        if rl is not None:
            cols["c31_range_low"][i] = float(rl)
        cols["c31_range_age"][i] = int(r.get("bars_in_range") or 0)
        if r.get("range_width_atr") is not None:
            cols["c31_range_width_atr"][i] = float(r["range_width_atr"])
        if r.get("range_score") is not None:
            cols["c31_range_score"][i] = float(r["range_score"])
        if r.get("box_efficiency") is not None:
            cols["c31_box_efficiency"][i] = float(r["box_efficiency"])
        if r.get("bound_drift_atr") is not None:
            cols["c31_bound_drift"][i] = float(r["bound_drift_atr"])
        cols["c31_failed_breakout_event"][i] = bool(r.get("failed_breakout_event"))
        cols["c31_state"][i] = str(r.get("state") or "")
    for k, v in cols.items():
        out[k] = v
    return out


def _finite(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(v):
        return None
    return v


def _protected_age(frame: pd.DataFrame, i: int, col: str, value: float) -> int:
    """Bars since protected level first appeared at this value (causal walk back)."""
    age = 0
    for j in range(i, -1, -1):
        v = _finite(frame.iloc[j].get(col))
        if v is None or abs(v - value) > 1e-12:
            break
        age += 1
    return max(age - 1, 0)  # age at prior confirmation relative to current prior bar


def eligible_levels_at_prior_bar(
    frame: pd.DataFrame,
    i: int,
    *,
    level_families: tuple[str, ...],
    cfg: LSRConfig | None = None,
) -> list[LevelSnapshot]:
    """Levels known *before* bar i (from closed bar i-1)."""
    c = cfg or default_config()
    if i < 1:
        return []
    prev = frame.iloc[i - 1]
    ts = str(_ts(prev.get("timestamp") or prev.get("decision_time")))
    atr = _finite(prev.get("atr_14") or prev.get("atr")) or 1e-9
    close = _finite(prev.get("close")) or 0.0
    out: list[LevelSnapshot] = []

    if "L1" in level_families:
        in_range = bool(prev.get("c31_in_range"))
        age = int(prev.get("c31_range_age") or 0)
        rh = _finite(prev.get("c31_range_high"))
        rl = _finite(prev.get("c31_range_low"))
        if in_range and age >= c.min_range_age_bars and rh is not None and rl is not None and rh > rl:
            width_pct = (rh - rl) / max(close, 1e-12) * 100.0
            meta = {
                "range_high": rh,
                "range_low": rl,
                "range_start": None,
                "range_age": age,
                "range_width_pct": width_pct,
                "range_width_atr": _finite(prev.get("c31_range_width_atr")),
                "touches_upper": None,
                "touches_lower": None,
                "bound_drift": _finite(prev.get("c31_bound_drift")),
                "box_efficiency": _finite(prev.get("c31_box_efficiency")),
                "range_score": _finite(prev.get("c31_range_score")),
            }
            out.append(
                LevelSnapshot(
                    level_family="L1",
                    level_id=f"L1_low_{i-1}_{rl:.8f}",
                    level_value=rl,
                    side="long",
                    confirmed_timestamp=ts,
                    confirmed_bar=i - 1,
                    age_bars=age,
                    meta=meta,
                )
            )
            out.append(
                LevelSnapshot(
                    level_family="L1",
                    level_id=f"L1_high_{i-1}_{rh:.8f}",
                    level_value=rh,
                    side="short",
                    confirmed_timestamp=ts,
                    confirmed_bar=i - 1,
                    age_bars=age,
                    meta=meta,
                )
            )

    if "L2" in level_families:
        pl = _finite(prev.get("protected_low"))
        ph = _finite(prev.get("protected_high"))
        maj = int(prev.get("major_direction") or 0) if pd.notna(prev.get("major_direction")) else 0
        if pl is not None:
            age = _protected_age(frame, i - 1, "protected_low", pl)
            if age >= c.min_protected_age_bars:
                out.append(
                    LevelSnapshot(
                        level_family="L2",
                        level_id=f"L2_pl_{i-1}_{pl:.8f}",
                        level_value=pl,
                        side="long",
                        confirmed_timestamp=ts,
                        confirmed_bar=i - 1,
                        age_bars=age,
                        meta={
                            "protected_level": pl,
                            "level_confirmed_timestamp": ts,
                            "level_age": age,
                            "major_direction": maj,
                            "last_external_event": None,
                            "distance_to_level_atr": abs(close - pl) / atr,
                        },
                    )
                )
        if ph is not None:
            age = _protected_age(frame, i - 1, "protected_high", ph)
            if age >= c.min_protected_age_bars:
                out.append(
                    LevelSnapshot(
                        level_family="L2",
                        level_id=f"L2_ph_{i-1}_{ph:.8f}",
                        level_value=ph,
                        side="short",
                        confirmed_timestamp=ts,
                        confirmed_bar=i - 1,
                        age_bars=age,
                        meta={
                            "protected_level": ph,
                            "level_confirmed_timestamp": ts,
                            "level_age": age,
                            "major_direction": maj,
                            "last_external_event": None,
                            "distance_to_level_atr": abs(close - ph) / atr,
                        },
                    )
                )

    # L3 intentionally omitted
    return out


def level_still_valid(
    frame: pd.DataFrame,
    i: int,
    snap: LevelSnapshot,
) -> tuple[bool, str | None]:
    """Check whether the setup level remains valid on closed bar i."""
    row = frame.iloc[i]
    if snap.level_family == "L1":
        if not bool(row.get("c31_in_range")):
            return False, "range_no_longer_valid"
        rh = _finite(row.get("c31_range_high"))
        rl = _finite(row.get("c31_range_low"))
        if snap.side == "long":
            if rl is None or abs(rl - snap.level_value) > max(1e-9, abs(snap.level_value) * 1e-6):
                return False, "range_level_replaced"
        else:
            if rh is None or abs(rh - snap.level_value) > max(1e-9, abs(snap.level_value) * 1e-6):
                return False, "range_level_replaced"
        return True, None

    if snap.level_family == "L2":
        col = "protected_low" if snap.side == "long" else "protected_high"
        cur = _finite(row.get(col))
        if cur is None:
            return False, "protected_level_cleared"
        if abs(cur - snap.level_value) > max(1e-9, abs(snap.level_value) * 1e-6):
            return False, "protected_level_replaced"
        # External BOS against the level (true breakout confirmation)
        if snap.side == "long" and bool(row.get("external_bos_down")):
            close = _finite(row.get("close"))
            if close is not None and close < snap.level_value:
                return False, "external_bos_breakout"
        if snap.side == "short" and bool(row.get("external_bos_up")):
            close = _finite(row.get("close"))
            if close is not None and close > snap.level_value:
                return False, "external_bos_breakout"
        choch = str(row.get("choch_side") or "").lower()
        if snap.side == "long" and choch in {"bear", "bearish", "down", "-1"}:
            close = _finite(row.get("close"))
            if close is not None and close < snap.level_value:
                return False, "external_choch_breakout"
        if snap.side == "short" and choch in {"bull", "bullish", "up", "1"}:
            close = _finite(row.get("close"))
            if close is not None and close > snap.level_value:
                return False, "external_choch_breakout"
        return True, None

    return False, "unknown_level_family"
