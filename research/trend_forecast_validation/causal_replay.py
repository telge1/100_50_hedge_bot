"""Causal scanner replay via existing C3.4B / C3.5 research APIs (thin adapter)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.market_structure_c3_4b import (
    RESEARCH_MATRIX,
    ProtectedStructureConfig,
    apply_protected_structure,
)
from research.regime_scanner.pullback_entry_c3_5 import (
    asof_htf_context,
    attach_structure_edges,
    enrich_indicators,
    prepare_research_frame,
)
from research.regime_scanner.pullback_entry_c3_5c_entry_path_audit import aggregate_complete_from_5m
from research.regime_scanner.timeframes import aggregate_candles
from research.trend_forecast_validation.config import ForecastValidationConfig, parse_utc
from research.trend_forecast_validation.data_loader import slice_period_masks


PULLBACK_STATES_BEARISH_CONTEXT = frozenset(
    {"bearish_pullback", "bearish_break_attempt", "bearish_choch"}
)
PULLBACK_STATES_BULLISH_CONTEXT = frozenset(
    {"bullish_pullback", "bullish_break_attempt", "bullish_choch"}
)


def _decision_end(frame_5m: pd.DataFrame) -> pd.Timestamp:
    last = pd.to_datetime(frame_5m["timestamp"], utc=True).iloc[-1]
    return last + pd.Timedelta(minutes=5)


def build_htf_ohlcv(frame_5m: pd.DataFrame, timeframe: str, decision: pd.Timestamp) -> pd.DataFrame:
    key = str(timeframe).strip().lower()
    if key in {"15m", "30m"}:
        return aggregate_candles(frame_5m, key, decision)
    if key in {"1h", "4h"}:
        return aggregate_complete_from_5m(frame_5m, key, decision_time=decision)
    raise ValueError(f"unsupported HTF timeframe: {timeframe}")


def run_causal_scanner_replay(
    frame_5m: pd.DataFrame,
    cfg: ForecastValidationConfig,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Run existing C3 structure stack once over the full history (causal bar-by-bar).

    Returns
    -------
    trace : DataFrame
        Per-5m-candle scanner snapshot with HTF attach columns.
    warmup_state : dict
        State summary at end of warmup.
    """
    base = frame_5m.copy().reset_index(drop=True)
    base["timestamp"] = pd.to_datetime(base["timestamp"], utc=True)
    if "decision_time" not in base.columns:
        base["decision_time"] = base["timestamp"] + pd.Timedelta(minutes=cfg.candle_interval_minutes)
    base["symbol"] = cfg.symbol
    base["timeframe"] = cfg.timeframe

    decision = _decision_end(base)
    htf_30 = build_htf_ohlcv(base, "30m", decision) if "30m" in cfg.htf_timeframes else None
    htf_4h = build_htf_ohlcv(base, "4h", decision) if "4h" in cfg.htf_timeframes else None

    # Primary LTF+30m path reuses prepare_research_frame (enrich + C3.4B edges + asof).
    trace = prepare_research_frame(base, ohlcv_15m=None, ohlcv_30m=htf_30)

    # Extra C3.4B diagnostic columns not forwarded by attach_structure_edges.
    feat = enrich_indicators(base)
    cfg_struct = ProtectedStructureConfig.from_matrix_entry(RESEARCH_MATRIX[0])
    struct_full = apply_protected_structure(feat, cfg_struct)
    for col in (
        "close_break_protected_up",
        "close_break_protected_down",
        "wick_break_protected_up",
        "wick_break_protected_down",
        "active_external_break_level",
        "micro_swing_high",
        "micro_swing_low",
        "transition_reason",
    ):
        if col in struct_full.columns and col not in trace.columns:
            trace[col] = struct_full[col].to_numpy()
        elif col in struct_full.columns:
            # Prefer full struct values for consistency
            trace[col] = struct_full[col].to_numpy()

    # Attach 4h structure causally (same enrich+structure then asof).
    if htf_4h is not None and not htf_4h.empty:
        f4 = enrich_indicators(htf_4h)
        f4 = attach_structure_edges(f4)
        before_cols = set(trace.columns)
        trace = asof_htf_context(trace, f4, tf_minutes=240, prefix="h4")
        # Rename merge key for clarity
        if "htf_close_decision" in trace.columns and "h4_htf_close_decision" not in trace.columns:
            # asof keeps right key as htf_close_decision from last merge; capture explicitly
            pass

    # Explicit HTF visibility columns for every 5m bar (merge_asof, no Python row loop).
    dec = pd.to_datetime(trace["decision_time"], utc=True)
    left = pd.DataFrame({"decision_time": dec, "_i": np.arange(len(trace))}).sort_values("decision_time")

    if htf_30 is not None and not htf_30.empty:
        right30 = pd.DataFrame(
            {
                "decision_time": pd.to_datetime(htf_30["timestamp"], utc=True)
                + pd.Timedelta(minutes=30),
                "last_visible_30m_timestamp": pd.to_datetime(htf_30["timestamp"], utc=True).astype(str),
            }
        ).sort_values("decision_time")
        m30 = pd.merge_asof(left, right30, on="decision_time", direction="backward")
        m30 = m30.sort_values("_i")
        trace["last_visible_30m_timestamp"] = m30["last_visible_30m_timestamp"].to_numpy()
        trace["htf_30m_fully_closed"] = pd.notna(m30["last_visible_30m_timestamp"]).to_numpy()
    else:
        trace["last_visible_30m_timestamp"] = None
        trace["htf_30m_fully_closed"] = False

    if htf_4h is not None and not htf_4h.empty:
        right4 = pd.DataFrame(
            {
                "decision_time": pd.to_datetime(htf_4h["timestamp"], utc=True)
                + pd.Timedelta(minutes=240),
                "last_visible_4h_timestamp": pd.to_datetime(htf_4h["timestamp"], utc=True).astype(str),
            }
        ).sort_values("decision_time")
        m4 = pd.merge_asof(left, right4, on="decision_time", direction="backward")
        m4 = m4.sort_values("_i")
        trace["last_visible_4h_timestamp"] = m4["last_visible_4h_timestamp"].to_numpy()
        trace["htf_4h_fully_closed"] = pd.notna(m4["last_visible_4h_timestamp"]).to_numpy()
    else:
        trace["last_visible_4h_timestamp"] = None
        trace["htf_4h_fully_closed"] = False

    trace["htf_both_closed"] = (
        trace["htf_30m_fully_closed"].astype(bool) & trace["htf_4h_fully_closed"].astype(bool)
    )

    # Period labels
    masks = slice_period_masks(trace["timestamp"], cfg)
    trace["in_warmup"] = masks["warmup"].to_numpy()
    trace["in_development"] = masks["development"].to_numpy()
    trace["in_out_of_sample"] = masks["out_of_sample"].to_numpy()
    trace["period"] = np.where(
        trace["in_out_of_sample"],
        "out_of_sample",
        np.where(trace["in_development"], "development", np.where(trace["in_warmup"], "warmup", "other")),
    )

    warmup_state = summarize_warmup_state(trace, cfg, masks["effective_warmup_start"])
    meta = {
        "structure_config": ProtectedStructureConfig.from_matrix_entry(RESEARCH_MATRIX[0]).variant_name,
        "components": [
            "research.regime_scanner.pullback_entry_c3_5.enrich_indicators",
            "research.regime_scanner.pullback_entry_c3_5.attach_structure_edges",
            "research.regime_scanner.market_structure_c3_4b.apply_protected_structure",
            "research.regime_scanner.timeframes.aggregate_candles (30m)",
            "research.regime_scanner.pullback_entry_c3_5c_entry_path_audit.aggregate_complete_from_5m (4h)",
            "research.regime_scanner.pullback_entry_c3_5.asof_htf_context",
        ],
        "n_trace_rows": int(len(trace)),
    }
    warmup_state["replay_meta"] = meta
    return trace, warmup_state


def summarize_warmup_state(
    trace: pd.DataFrame,
    cfg: ForecastValidationConfig,
    effective_warmup_start: pd.Timestamp,
) -> dict[str, Any]:
    warm_end = pd.Timestamp(parse_utc(cfg.warmup_end))
    warm = trace.loc[
        (pd.to_datetime(trace["timestamp"], utc=True) >= effective_warmup_start)
        & (pd.to_datetime(trace["timestamp"], utc=True) <= warm_end)
    ]
    if warm.empty:
        return {
            "scanner_state_ready": False,
            "reason": "empty_warmup",
            "effective_warmup_start": str(effective_warmup_start),
            "warmup_end": str(warm_end),
        }
    last = warm.iloc[-1]
    # Confirmed externals: count rising edges of external BOS as proxy for confirmed swings,
    # plus non-null protected levels.
    bos_up = warm["external_bos_up"].fillna(False).astype(bool)
    bos_dn = warm["external_bos_down"].fillna(False).astype(bool)
    edge_up = bos_up & ~bos_up.shift(1).fillna(False)
    edge_dn = bos_dn & ~bos_dn.shift(1).fillna(False)

    ema_ready = all(
        pd.notna(last.get(c)) for c in ("ema_9", "ema_20", "ema_59", "ema_200") if c in warm.columns
    )
    has_ext_high = bool(pd.notna(last.get("protected_high"))) or int(edge_up.sum()) > 0
    has_ext_low = bool(pd.notna(last.get("protected_low"))) or int(edge_dn.sum()) > 0
    major = last.get("major_direction")
    major_ok = major is not None and not (isinstance(major, float) and np.isnan(major))
    days = (
        pd.to_datetime(warm["timestamp"], utc=True).iloc[-1]
        - pd.to_datetime(warm["timestamp"], utc=True).iloc[0]
    ).total_seconds() / 86400.0
    ready = bool(
        ema_ready
        and has_ext_high
        and has_ext_low
        and major_ok
        and days >= cfg.min_warmup_days * 0.9  # allow slight calendar shortfall if data starts late
    )
    return {
        "effective_warmup_start": str(effective_warmup_start),
        "warmup_end": str(warm_end),
        "warmup_candles": int(len(warm)),
        "warmup_days": float(days),
        "confirmed_external_high_count": int(edge_up.sum()),
        "confirmed_external_low_count": int(edge_dn.sum()),
        "current_external_swing_high": _finite_or_none(last.get("micro_swing_high")),
        "current_external_swing_low": _finite_or_none(last.get("micro_swing_low")),
        "protected_high": _finite_or_none(last.get("protected_high")),
        "protected_low": _finite_or_none(last.get("protected_low")),
        "major_trend": int(major) if major_ok else None,
        "major_trend_label": (
            "bullish" if major_ok and int(major) > 0 else "bearish" if major_ok and int(major) < 0 else "flat"
        ),
        "ema_warmup_complete": bool(ema_ready),
        "htf_30m_context_available": bool(last.get("htf_30m_fully_closed")),
        "htf_4h_context_available": bool(last.get("htf_4h_fully_closed")),
        "scanner_state_ready": ready,
        "protected_structure_state": last.get("protected_structure_state"),
    }


def _finite_or_none(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(x):
        return None
    return x


def prefix_invariance_check(frame_5m: pd.DataFrame, n: int, cfg: ForecastValidationConfig) -> dict[str, Any]:
    """Replay full vs prefix[:n]; compare scanner fields on the shared prefix."""
    full, _ = run_causal_scanner_replay(frame_5m, cfg)
    pref, _ = run_causal_scanner_replay(frame_5m.iloc[:n].copy(), cfg)
    cols = [
        c
        for c in [
            "protected_structure_state",
            "major_direction",
            "protected_high",
            "protected_low",
            "external_bos_up",
            "external_bos_down",
            "choch_side",
            "ema_20",
            "adx",
        ]
        if c in full.columns and c in pref.columns
    ]
    a = full.iloc[:n][cols].reset_index(drop=True)
    b = pref[cols].reset_index(drop=True)
    # Float-tolerant compare
    equal = True
    mismatches: list[str] = []
    for c in cols:
        if pd.api.types.is_float_dtype(a[c]) or pd.api.types.is_float_dtype(b[c]):
            if not np.allclose(
                pd.to_numeric(a[c], errors="coerce").fillna(0).to_numpy(),
                pd.to_numeric(b[c], errors="coerce").fillna(0).to_numpy(),
                equal_nan=True,
                rtol=0,
                atol=1e-9,
            ):
                equal = False
                mismatches.append(c)
        else:
            if not a[c].astype(str).equals(b[c].astype(str)):
                equal = False
                mismatches.append(c)
    return {"n": n, "equal": equal, "compared_columns": cols, "mismatches": mismatches}
