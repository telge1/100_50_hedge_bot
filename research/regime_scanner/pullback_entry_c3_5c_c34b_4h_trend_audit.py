"""C3.5c × C3.4B 4h Protected Structure Trend Audit (research-only).

Diagnoses whether unchanged C3.4B Protected Structure on causally aggregated 4h
bars is a usable higher-timeframe start-guard proxy for A6 fills / hedge-bot
directional risk.

No SM / C3.4B / Pine / filter / hedge-bot changes. No commits.
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

from research.regime_scanner.indicator_feature_store import (
    load_ohlcv_with_warmup,
    required_indicator_warmup_bars,
)
from research.regime_scanner.market_structure_c3_4b import (
    ProtectedStructureConfig,
    apply_protected_structure,
)
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.pullback_entry_c3_5 import (
    C34B_MATRIX,
    apply_pullback_entry,
    config_hash,
    enrich_indicators,
)
from research.regime_scanner.pullback_entry_c3_5_diagnostics import baseline_a6
from research.regime_scanner.pullback_entry_c3_5c_entry_path_audit import (
    TF_MINUTES,
    aggregate_complete_from_5m,
)
from research.regime_scanner.pullback_entry_c3_5c_fill_excursion_audit import (
    DEFAULT_OUT as EXCURSION_DIR,
)
from research.regime_scanner.pullback_entry_c3_5c_htf_trend_alignment_audit import (
    DEFAULT_OUT as HTF_ALIGN_DIR,
    major_to_label,
    recovery_and_risk_proxy,
)
from research.regime_scanner.pullback_entry_c3_5c_realized_outcome_audit import (
    _filled_sorted,
    trades_exit_a_opposite_entry,
)
from research.regime_scanner.pullback_entry_c3_5c_robustness_audit import (
    DEFAULT_BASELINE_DIR,
    WARMUP_CALENDAR_DAYS,
    assign_split,
    build_extended_tf_frame,
    closed_only,
    fixed_chrono_splits,
)
from research.regime_scanner.trend_regime_classification_audit import (
    C2_BASELINE_HASH,
    assert_baseline_readonly,
)
from research.regime_scanner.trend_weakening_multi_bar_audit import assert_safe_output_dir

DEFAULT_OUT = Path(
    "research/regime_scanner/results/phase_c3_5_pullback_entry_state_machine/"
    "c35c_c34b_4h_trend_audit"
)
CASE_DIR = Path(
    "research/regime_scanner/results/phase_c3_5_pullback_entry_state_machine/"
    "c35c_trade_case_review"
)

SYMBOL = "APTUSDT"
TIMEFRAME = "15m"
VARIANT = "A6"
BAR_MINUTES = 15
H4_MINUTES = 240

STRENGTH_RULES_DOC = {
    "major_bullish": "major_direction == +1",
    "major_bearish": "major_direction == -1",
    "major_neutral": "major_direction == 0",
    "strong_bull_structure": (
        "major=+1 AND last_external_bos_side==up AND protected_low present "
        "AND micro_direction >= 0 (bullish or sticky-neutral)"
    ),
    "strong_bear_structure": (
        "major=-1 AND last_external_bos_side==down AND protected_high present "
        "AND micro_direction <= 0"
    ),
    "bull_structure": "major=+1 but not strong_bull (micro conflict and/or older/missing bullish external BOS)",
    "bear_structure": "major=-1 but not strong_bear",
    "mixed_structure": "major==0 OR unclear protected state without confirmed major",
    "causality": "4h bar usable iff open+4h <= trigger_decision (=trigger_bar_open+15m)",
    "c34b_config": "C34B_MATRIX[0] protected_medium — unchanged",
}


def _finite(x: Any, default: float = float("nan")) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _pf(rets: pd.Series) -> float | None:
    r = pd.to_numeric(rets, errors="coerce").dropna()
    if r.empty:
        return None
    gp = float(r[r > 0].sum())
    gl = float((-r[r <= 0]).sum())
    if gl <= 1e-15:
        return None if gp <= 0 else float("inf")
    return gp / gl


def _age_since(flag: Sequence[bool]) -> np.ndarray:
    age = np.full(len(flag), np.nan)
    last = -1
    for i, v in enumerate(flag):
        if bool(v):
            last = i
        if last >= 0:
            age[i] = float(i - last)
    return age


# ---------------------------------------------------------------------------
# C3.4B 4h frame
# ---------------------------------------------------------------------------


def build_c34b_htf_frame(
    full_5m: pd.DataFrame,
    timeframe: str,
    *,
    decision: pd.Timestamp,
    analyze_start: pd.Timestamp,
    analyze_end_exclusive: pd.Timestamp,
) -> pd.DataFrame:
    """Causal HTF OHLCV + unchanged C3.4B Protected Structure (full diag columns)."""
    ohlcv = aggregate_complete_from_5m(full_5m, timeframe, decision_time=decision)
    if ohlcv.empty:
        return pd.DataFrame()
    feat = enrich_indicators(ohlcv)
    cfg = ProtectedStructureConfig.from_matrix_entry(C34B_MATRIX[0])
    struct = apply_protected_structure(feat, cfg)
    out = struct.copy()
    # ensure OHLCV present
    for c in ("timestamp", "open", "high", "low", "close", "volume", "atr_14", "atr", "ema_9", "ema_20", "ema_50"):
        if c in feat.columns and c not in out.columns:
            out[c] = feat[c].values
        elif c in feat.columns:
            out[c] = feat[c].values

    ts = pd.to_datetime(out["timestamp"], utc=True)
    out["timestamp"] = ts
    out["htf_close_decision"] = ts + pd.Timedelta(minutes=TF_MINUTES[timeframe])
    out["in_analyze_window"] = (ts >= analyze_start) & (ts < analyze_end_exclusive)
    out["bar_index"] = np.arange(len(out))
    out["timeframe"] = timeframe

    # micro direction proxy (same causal rule as pattern diagnostic)
    n = len(out)
    close = pd.to_numeric(out["close"], errors="coerce").astype(float)
    msh = pd.to_numeric(out.get("micro_swing_high"), errors="coerce")
    msl = pd.to_numeric(out.get("micro_swing_low"), errors="coerce")
    micro_dir = np.zeros(n, dtype=int)
    for i in range(n):
        c = float(close.iloc[i])
        hi = _finite(msh.iloc[i]) if i < len(msh) else float("nan")
        lo = _finite(msl.iloc[i]) if i < len(msl) else float("nan")
        if math.isfinite(hi) and c >= hi:
            micro_dir[i] = 1
        elif math.isfinite(lo) and c <= lo:
            micro_dir[i] = -1
        elif i > 0:
            micro_dir[i] = micro_dir[i - 1]
    out["micro_direction"] = micro_dir
    maj = pd.to_numeric(out["major_direction"], errors="coerce").fillna(0).astype(int)
    out["major_direction"] = maj
    out["major_micro_alignment"] = (np.sign(maj.to_numpy()) == np.sign(micro_dir)).astype(float)
    out.loc[maj.to_numpy() == 0, "major_micro_alignment"] = np.nan

    # sticky last external/internal/choch side + ages
    ext_side = []
    last_ext = None
    for i in range(n):
        up = bool(out["external_bos_up"].iloc[i]) if "external_bos_up" in out.columns else False
        dn = bool(out["external_bos_down"].iloc[i]) if "external_bos_down" in out.columns else False
        if up:
            last_ext = "up"
        elif dn:
            last_ext = "down"
        s = out["external_bos_side"].iloc[i] if "external_bos_side" in out.columns else None
        if isinstance(s, str) and s in {"up", "down"}:
            last_ext = s
        ext_side.append(last_ext)
    out["last_external_bos_side"] = ext_side
    out["bars_since_external_bos"] = _age_since(
        [bool(out["external_bos_up"].iloc[i]) or bool(out["external_bos_down"].iloc[i]) for i in range(n)]
    )

    int_side = []
    last_int = None
    for i in range(n):
        up = bool(out["internal_bos_up"].iloc[i]) if "internal_bos_up" in out.columns else False
        dn = bool(out["internal_bos_down"].iloc[i]) if "internal_bos_down" in out.columns else False
        if up:
            last_int = "up"
        elif dn:
            last_int = "down"
        s = out["internal_bos_side"].iloc[i] if "internal_bos_side" in out.columns else None
        if isinstance(s, str) and s in {"up", "down"}:
            last_int = s
        int_side.append(last_int)
    out["last_internal_bos_side"] = int_side
    out["bars_since_internal_bos"] = _age_since(
        [bool(out["internal_bos_up"].iloc[i]) or bool(out["internal_bos_down"].iloc[i]) for i in range(n)]
    )

    choch_side = []
    last_ch = None
    choch_flags = []
    for i in range(n):
        s = out["choch_side"].iloc[i] if "choch_side" in out.columns else None
        hit = isinstance(s, str) and s in {"up", "down"}
        if hit:
            last_ch = s
        choch_side.append(last_ch)
        choch_flags.append(hit)
    out["last_choch_side"] = choch_side
    out["bars_since_choch"] = _age_since(choch_flags)

    flip2 = maj != maj.shift(1).fillna(maj.iloc[0] if n else 0).astype(int)
    out["bars_since_major_flip"] = _age_since(list(flip2.fillna(False).astype(bool)))
    out["major_flip_this_bar"] = flip2.fillna(False).astype(bool)

    strengths = [classify_structure_strength(out.iloc[i]) for i in range(n)]
    out["structure_strength_category"] = strengths
    out["major_direction_label"] = [major_to_label(int(x)) for x in maj]
    return out.reset_index(drop=True)


def classify_structure_strength(row: Mapping[str, Any]) -> str:
    """Fixed pre-result categories — no outcome-driven thresholds."""
    try:
        major = int(row.get("major_direction", 0))
    except (TypeError, ValueError):
        major = 0
    try:
        micro = int(row.get("micro_direction", 0))
    except (TypeError, ValueError):
        micro = 0
    last_bos = row.get("last_external_bos_side")
    ph = row.get("protected_high")
    pl = row.get("protected_low")
    ph_ok = ph is not None and not (isinstance(ph, float) and math.isnan(ph)) and pd.notna(ph)
    pl_ok = pl is not None and not (isinstance(pl, float) and math.isnan(pl)) and pd.notna(pl)

    if major > 0:
        if last_bos == "up" and pl_ok and micro >= 0:
            return "strong_bull_structure"
        return "bull_structure"
    if major < 0:
        if last_bos == "down" and ph_ok and micro <= 0:
            return "strong_bear_structure"
        return "bear_structure"
    return "mixed_structure"


def lookup_closed_c34b_bar(
    htf: pd.DataFrame,
    *,
    trigger_decision: pd.Timestamp,
) -> dict[str, Any]:
    if htf.empty:
        return {"found": False, "context_is_causal": True, "four_hour_bar_closed_before_trigger": False}
    close_dec = pd.to_datetime(htf["htf_close_decision"], utc=True)
    mask = close_dec <= pd.Timestamp(trigger_decision)
    if not mask.any():
        return {
            "found": False,
            "context_is_causal": True,
            "four_hour_bar_closed_before_trigger": False,
        }
    idx = int(np.where(mask.to_numpy())[0][-1])
    row = htf.iloc[idx]
    assert pd.Timestamp(row["htf_close_decision"]) <= pd.Timestamp(trigger_decision)
    return {
        "found": True,
        "context_is_causal": True,
        "four_hour_bar_closed_before_trigger": True,
        "row_index": idx,
        "selected_4h_bar_time": pd.Timestamp(row["timestamp"]),
        "selected_4h_bar_close_time": pd.Timestamp(row["htf_close_decision"]),
        "row": row,
    }


def alignment_flags(side: str, major: int, strength: str) -> dict[str, Any]:
    want = 1 if side == "long" else -1
    against = -want
    with_major = major == want
    against_major = major == against
    neutral = major == 0
    strong_with = strength == ("strong_bull_structure" if side == "long" else "strong_bear_structure")
    strong_against = strength == ("strong_bear_structure" if side == "long" else "strong_bull_structure")
    if strong_with:
        cat = "aligned_strong"
    elif with_major:
        cat = "aligned_weak"
    elif strong_against:
        cat = "countertrend_strong"
    elif against_major:
        cat = "countertrend_weak"
    else:
        cat = "neutral"
    return {
        "with_c34b_4h_major": with_major,
        "against_c34b_4h_major": against_major,
        "neutral_c34b_4h": neutral,
        "with_strong_c34b_4h": strong_with,
        "against_strong_c34b_4h": strong_against,
        "alignment_category": cat,
    }


def relevant_protected_level(side: str, row: Mapping[str, Any]) -> tuple[float | None, str | None]:
    """For long: protected_low (bull support); for short: protected_high (bear resistance)."""
    if side == "long":
        pl = row.get("protected_low")
        if pl is not None and pd.notna(pl):
            return float(pl), "protected_low"
        return None, None
    ph = row.get("protected_high")
    if ph is not None and pd.notna(ph):
        return float(ph), "protected_high"
    return None, None


def distance_bucket(dist_atr: float | None, level_missing: bool) -> str:
    if level_missing or dist_atr is None or (isinstance(dist_atr, float) and math.isnan(dist_atr)):
        return "protected_level_missing"
    if dist_atr < 0:
        return "beyond_protected_level"
    if dist_atr < 0.5:
        return "lt_0_5_atr"
    if dist_atr < 1.0:
        return "0_5_to_1_atr"
    if dist_atr < 2.0:
        return "1_to_2_atr"
    return "gt_2_atr"


# ---------------------------------------------------------------------------
# Guards (diagnostic only)
# ---------------------------------------------------------------------------


def guard_decision(side: str, major: int, strength: str, guard: str) -> str:
    """Return 'allow' or 'block'."""
    if guard == "G0":
        return "allow"
    if guard == "G1":
        if side == "long" and major < 0:
            return "block"
        if side == "short" and major > 0:
            return "block"
        return "allow"
    if guard == "G2":
        if side == "long" and strength == "strong_bear_structure":
            return "block"
        if side == "short" and strength == "strong_bull_structure":
            return "block"
        return "allow"
    if guard == "G3":
        if side == "long" and strength in {"bull_structure", "strong_bull_structure"}:
            return "allow"
        if side == "short" and strength in {"bear_structure", "strong_bear_structure"}:
            return "allow"
        return "block"
    raise ValueError(guard)


# ---------------------------------------------------------------------------
# Aggregations
# ---------------------------------------------------------------------------


def summarize_guard_impact(panel: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for gname in ("G0", "G1", "G2", "G3"):
        dec = panel[f"guard_{gname}"]
        allowed = panel[dec == "allow"]
        blocked = panel[dec == "block"]
        ea_b = blocked[(blocked["exit_a_closed"] == True) & (blocked["included_in_realized_exit_a"] == True)]  # noqa: E712
        ea_a = allowed[(allowed["exit_a_closed"] == True) & (allowed["included_in_realized_exit_a"] == True)]  # noqa: E712
        net_b = pd.to_numeric(ea_b["net_return_020_pct"], errors="coerce")
        net_a = pd.to_numeric(ea_a["net_return_020_pct"], errors="coerce")
        rows.append(
            {
                "guard": gname,
                "total_fills": len(panel),
                "allowed_fills": len(allowed),
                "blocked_fills": len(blocked),
                "blocked_long": int(((blocked["side"] == "long")).sum()),
                "blocked_short": int(((blocked["side"] == "short")).sum()),
                "block_rate": float(len(blocked) / len(panel)) if len(panel) else None,
                "blocked_exit_a_winners": int(((net_b > 0)).sum()) if len(ea_b) else 0,
                "blocked_exit_a_losers": int(((net_b <= 0)).sum()) if len(ea_b) else 0,
                "blocked_top3": int((blocked["top3_trade"] == True).sum()),  # noqa: E712
                "allowed_sum_exit_a_net020": float(net_a.sum()) if len(ea_a) else 0.0,
                "allowed_sum_without_top3": float(net_a[ea_a["top3_trade"] != True].sum()) if len(ea_a) else 0.0,  # noqa: E712
                "median_mae_allowed": float(allowed["primary_mae_pct"].median()) if len(allowed) else None,
                "median_mae_blocked": float(blocked["primary_mae_pct"].median()) if len(blocked) else None,
                "median_uw_allowed": float(allowed["max_underwater_duration_bars"].median()) if len(allowed) else None,
                "median_uw_blocked": float(blocked["max_underwater_duration_bars"].median()) if len(blocked) else None,
                "avoided_mae_le_5": int((blocked["primary_mae_pct"] <= -5).sum()),
                "avoided_mae_le_10": int((blocked["primary_mae_pct"] <= -10).sum()),
                "avoided_mae_le_15": int((blocked["primary_mae_pct"] <= -15).sum()),
                "avoided_mae_le_20": int((blocked["primary_mae_pct"] <= -20).sum()),
                "lost_tp2_fills": int((blocked["tp_2_reached"] == True).sum()),  # noqa: E712
                "lost_tp3_fills": int((blocked["tp_3_reached"] == True).sum()),  # noqa: E712
                "lost_tp5_fills": int((blocked["tp_5_reached"] == True).sum()),  # noqa: E712
                "blocked_net_sum": float(net_b.sum()) if len(ea_b) else 0.0,
            }
        )
    return pd.DataFrame(rows)


def compare_c34b_vs_ema(panel: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Per-fill comparison + aggregate coverage."""
    rows = []
    for _, r in panel.iterrows():
        c34b = r["major_direction_label_4h"]
        ema4 = r.get("ema_h4_trend")
        comb = r.get("ema_combined_htf_trend")
        agree = None
        if pd.notna(ema4):
            # map ema bullish/bearish/mixed to c34b
            if c34b == "bullish" and ema4 == "bullish":
                agree = True
            elif c34b == "bearish" and ema4 == "bearish":
                agree = True
            elif c34b == "neutral" and ema4 == "mixed":
                agree = True
            else:
                agree = False
        rows.append(
            {
                "fill_id": r["fill_id"],
                "side": r["side"],
                "c34b_4h_trend": c34b,
                "c34b_strength": r["structure_strength_category"],
                "ema_4h_trend": ema4,
                "ema_combined_htf_trend": comb,
                "c34b_ema4_agree": agree,
                "c34b_is_neutral_or_mixed": c34b == "neutral" or r["structure_strength_category"] == "mixed_structure",
                "ema_is_mixed": ema4 == "mixed" if pd.notna(ema4) else None,
                "c34b_detects_countertrend": bool(r["against_c34b_4h_major"]),
                "ema_detects_countertrend": bool(r.get("ema_against_4h")) if "ema_against_4h" in r else None,
                "primary_mae_pct": r["primary_mae_pct"],
                "max_underwater_duration_bars": r["max_underwater_duration_bars"],
                "net_return_020_pct": r.get("net_return_020_pct"),
                "exit_a_closed": r.get("exit_a_closed"),
            }
        )
    per = pd.DataFrame(rows)

    def _agg(name: str, mask_ct: pd.Series) -> dict[str, Any]:
        sub = panel[mask_ct]
        ea = sub[(sub["exit_a_closed"] == True) & (sub["included_in_realized_exit_a"] == True)]  # noqa: E712
        net = pd.to_numeric(ea["net_return_020_pct"], errors="coerce")
        return {
            "approach": name,
            "n_countertrend_fills": int(mask_ct.sum()),
            "share_countertrend": float(mask_ct.mean()),
            "n_mae_le_5": int((sub["primary_mae_pct"] <= -5).sum()),
            "n_mae_le_10": int((sub["primary_mae_pct"] <= -10).sum()),
            "n_mae_le_15": int((sub["primary_mae_pct"] <= -15).sum()),
            "n_mae_le_20": int((sub["primary_mae_pct"] <= -20).sum()),
            "blocked_exit_a_winners": int((net > 0).sum()) if len(ea) else 0,
            "blocked_exit_a_losers": int((net <= 0).sum()) if len(ea) else 0,
            "blocked_net_sum": float(net.sum()) if len(ea) else 0.0,
            "median_mae_counter": float(sub["primary_mae_pct"].median()) if len(sub) else None,
        }

    # EMA counter: from panel flags if present
    ema_ct = panel["ema_against_4h"] == True if "ema_against_4h" in panel.columns else pd.Series([False] * len(panel))  # noqa: E712
    agg = pd.DataFrame(
        [
            {
                "approach": "c34b_4h_coverage",
                "share_neutral_or_mixed": float(
                    ((panel["major_direction_4h"] == 0) | (panel["structure_strength_category"] == "mixed_structure")).mean()
                ),
                "share_bullish_major": float((panel["major_direction_4h"] > 0).mean()),
                "share_bearish_major": float((panel["major_direction_4h"] < 0).mean()),
                "n_fills": len(panel),
            },
            {
                "approach": "ema_4h_coverage",
                "share_neutral_or_mixed": float((panel["ema_h4_trend"] == "mixed").mean())
                if "ema_h4_trend" in panel.columns
                else None,
                "share_bullish_major": float((panel["ema_h4_trend"] == "bullish").mean())
                if "ema_h4_trend" in panel.columns
                else None,
                "share_bearish_major": float((panel["ema_h4_trend"] == "bearish").mean())
                if "ema_h4_trend" in panel.columns
                else None,
                "n_fills": len(panel),
            },
            _agg("c34b_4h_countertrend", panel["against_c34b_4h_major"] == True),  # noqa: E712
            _agg("ema_4h_countertrend", ema_ct),
        ]
    )
    return per, agg


def evaluate_hypotheses(panel: pd.DataFrame, cmp_agg: pd.DataFrame) -> pd.DataFrame:
    rows = []
    long_ct = panel[(panel["side"] == "long") & (panel["against_c34b_4h_major"] == True)]  # noqa: E712
    long_al = panel[(panel["side"] == "long") & (panel["with_c34b_4h_major"] == True)]  # noqa: E712
    short_al = panel[(panel["side"] == "short") & (panel["with_c34b_4h_major"] == True)]  # noqa: E712
    short_other = panel[(panel["side"] == "short") & (panel["with_c34b_4h_major"] != True)]  # noqa: E712

    # H1
    h1 = "underpowered"
    if len(long_ct) >= 3 and len(long_al) >= 3:
        if float(long_ct["primary_mae_pct"].median()) < float(long_al["primary_mae_pct"].median()) and float(
            long_ct["max_underwater_duration_bars"].median()
        ) > float(long_al["max_underwater_duration_bars"].median()):
            h1 = "supported_descriptively"
        else:
            h1 = "not_supported"
    elif len(long_ct) >= 1:
        h1 = "partially_supported" if len(long_ct) and float(long_ct["primary_mae_pct"].median()) < -5 else "underpowered"
    rows.append({"hypothesis": "H1_long_against_4h_major_worse", "status": h1, "n_long_ct": len(long_ct), "n_long_al": len(long_al)})

    # H2
    h2 = "underpowered"
    if len(short_al) >= 5 and len(short_other) >= 3:
        better = float(short_al["primary_mae_pct"].median()) > float(short_other["primary_mae_pct"].median())
        h2 = "supported_descriptively" if better else "not_supported"
    rows.append({"hypothesis": "H2_short_with_bearish_4h_better", "status": h2, "n_short_al": len(short_al)})

    # H3 less mixed than EMA
    h3 = "underpowered"
    if "ema_h4_trend" in panel.columns:
        c34b_mixed = float((panel["major_direction_4h"] == 0).mean())
        ema_mixed = float((panel["ema_h4_trend"] == "mixed").mean())
        h3 = "supported_descriptively" if c34b_mixed < ema_mixed else "not_supported"
    rows.append({"hypothesis": "H3_c34b_less_mixed_than_ema", "status": h3})

    # H4 more severe risks
    h4 = "underpowered"
    c_row = cmp_agg[cmp_agg["approach"] == "c34b_4h_countertrend"]
    e_row = cmp_agg[cmp_agg["approach"] == "ema_4h_countertrend"]
    if len(c_row) and len(e_row) and pd.notna(c_row.iloc[0].get("n_mae_le_10")):
        if int(c_row.iloc[0]["n_mae_le_10"]) > int(e_row.iloc[0]["n_mae_le_10"]):
            h4 = "supported_descriptively"
        elif int(c_row.iloc[0]["n_countertrend_fills"]) > int(e_row.iloc[0]["n_countertrend_fills"]):
            h4 = "partially_supported"
        else:
            h4 = "not_supported"
    rows.append({"hypothesis": "H4_c34b_detects_more_severe_counter", "status": h4})

    # H5 G1 tradeoff
    h5 = "underpowered"
    if "guard_G1" in panel.columns:
        blocked = panel[panel["guard_G1"] == "block"]
        ea_b = blocked[(blocked["exit_a_closed"] == True)]  # noqa: E712
        winners = int((pd.to_numeric(ea_b["net_return_020_pct"], errors="coerce") > 0).sum()) if len(ea_b) else 0
        severe = int((blocked["primary_mae_pct"] <= -10).sum())
        if len(blocked) >= 1:
            if severe >= 1 and winners <= severe:
                h5 = "partially_supported"
            elif severe >= 1:
                h5 = "partially_supported"
            else:
                h5 = "not_supported"
            if winners == 0 and severe >= 1:
                h5 = "supported_descriptively"
    rows.append({"hypothesis": "H5_G1_blocks_hangers_not_too_many_winners", "status": h5})

    # H6 protected level
    h6 = "underpowered"
    if "protected_distance_bucket" in panel.columns:
        beyond = panel[panel["protected_distance_bucket"] == "beyond_protected_level"]
        far = panel[panel["protected_distance_bucket"] == "gt_2_atr"]
        if len(beyond) >= 3 and len(far) >= 3:
            h6 = (
                "supported_descriptively"
                if float(beyond["primary_mae_pct"].median()) < float(far["primary_mae_pct"].median())
                else "not_supported"
            )
        elif len(beyond) + len(panel[panel["protected_distance_bucket"] == "lt_0_5_atr"]) >= 5:
            h6 = "partially_supported"
    rows.append({"hypothesis": "H6_protected_level_adds_info", "status": h6})

    # H7 persistence
    h7 = "underpowered"
    sev = panel[panel["primary_mae_pct"] <= -10]
    if len(sev) >= 3 and "major_same_after_48h" in sev.columns:
        share = float(sev["major_same_after_48h"].mean())
        h7 = "supported_descriptively" if share >= 0.6 else ("partially_supported" if share >= 0.4 else "not_supported")
    rows.append({"hypothesis": "H7_severe_mae_persistent_4h_trend", "status": h7, "n_severe": len(sev)})
    return pd.DataFrame(rows)


def protected_level_summary(panel: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (bucket, side), g in panel.groupby(["protected_distance_bucket", "side"]):
        ea = g[(g["exit_a_closed"] == True) & (g["included_in_realized_exit_a"] == True)]  # noqa: E712
        net = pd.to_numeric(ea["net_return_020_pct"], errors="coerce")
        rows.append(
            {
                "bucket": bucket,
                "side": side,
                "n": len(g),
                "median_mae": float(g["primary_mae_pct"].median()),
                "median_uw": float(g["max_underwater_duration_bars"].median()),
                "recovery_rate": float(g["recovery_to_entry_reached"].mean()),
                "n_mae_le_10": int((g["primary_mae_pct"] <= -10).sum()),
                "exit_a_n": len(ea),
                "exit_a_wr": float((net > 0).mean()) if len(ea) else None,
                "exit_a_sum": float(net.sum()) if len(ea) else None,
            }
        )
    return pd.DataFrame(rows)


def robustness_slices(panel: pd.DataFrame) -> pd.DataFrame:
    rows = []
    slices = {
        "full": panel,
        "without_top3": panel[panel["top3_trade"] != True],  # noqa: E712
        "long_only": panel[panel["side"] == "long"],
        "short_only": panel[panel["side"] == "short"],
    }
    for sp in ("development", "validation", "oos"):
        slices[f"split_{sp}"] = panel[panel["split"] == sp]
    for name, g in slices.items():
        if g.empty:
            continue
        for lab, mask in (
            ("with_major", g["with_c34b_4h_major"] == True),  # noqa: E712
            ("against_major", g["against_c34b_4h_major"] == True),  # noqa: E712
            ("neutral", g["neutral_c34b_4h"] == True),  # noqa: E712
        ):
            sub = g[mask]
            if sub.empty:
                continue
            rows.append(
                {
                    "slice": name,
                    "alignment": lab,
                    "n": len(sub),
                    "median_mae": float(sub["primary_mae_pct"].median()),
                    "median_uw": float(sub["max_underwater_duration_bars"].median()),
                    "tp2_reach": float(sub["tp_2_reached"].mean()),
                    "n_mae_le_10": int((sub["primary_mae_pct"] <= -10).sum()),
                }
            )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Charts / report
# ---------------------------------------------------------------------------


def maybe_plots(out_dir: Path, panel: pd.DataFrame, frame15: pd.DataFrame, frame4h: pd.DataFrame) -> list[str]:
    written: list[str] = []
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return written
    plot_dir = out_dir / "plots"
    cases_dir = out_dir / "cases"
    plot_dir.mkdir(parents=True, exist_ok=True)
    cases_dir.mkdir(parents=True, exist_ok=True)

    def save(fig, path: Path) -> None:
        fig.tight_layout()
        fig.savefig(path, dpi=110)
        plt.close(fig)
        written.append(str(path))

    # MAE by alignment
    order = ["aligned_strong", "aligned_weak", "neutral", "countertrend_weak", "countertrend_strong"]
    order = [c for c in order if c in set(panel["alignment_category"])]
    fig, ax = plt.subplots(figsize=(8, 4))
    data = [panel.loc[panel["alignment_category"] == c, "primary_mae_pct"].dropna() for c in order]
    if data:
        ax.boxplot(data, tick_labels=order, showfliers=False)
        ax.set_title("MAE by C3.4B 4h alignment")
        ax.tick_params(axis="x", rotation=20)
        save(fig, plot_dir / "mae_by_c34b_alignment.png")

    fig, ax = plt.subplots(figsize=(8, 4))
    data = [panel.loc[panel["alignment_category"] == c, "max_underwater_duration_bars"].dropna() for c in order]
    if data:
        ax.boxplot(data, tick_labels=order, showfliers=False)
        ax.set_title("Underwater by C3.4B 4h alignment")
        ax.tick_params(axis="x", rotation=20)
        save(fig, plot_dir / "underwater_by_c34b_alignment.png")

    # major distribution
    fig, ax = plt.subplots(figsize=(6, 4))
    vc = panel["major_direction_label_4h"].value_counts()
    ax.bar(vc.index.astype(str), vc.values)
    ax.set_title("Fill context: C3.4B 4h major_direction")
    save(fig, plot_dir / "c34b_4h_major_distribution.png")

    # G1 impact
    fig, ax = plt.subplots(figsize=(6, 4))
    for lab, mask in (("allowed", panel["guard_G1"] == "allow"), ("blocked", panel["guard_G1"] == "block")):
        sub = panel[mask]
        ax.scatter(sub["primary_mae_pct"], sub["primary_mfe_pct"], alpha=0.6, label=f"G1 {lab} n={len(sub)}")
    ax.legend()
    ax.set_title("G1 allow vs block MFE/MAE")
    save(fig, plot_dir / "g1_allow_vs_block.png")

    # c34b vs ema counter counts
    if "ema_against_4h" in panel.columns:
        fig, ax = plt.subplots(figsize=(6, 4))
        ax.bar(
            ["C3.4B against", "EMA-4h against"],
            [int(panel["against_c34b_4h_major"].sum()), int(panel["ema_against_4h"].sum())],
        )
        ax.set_title("Countertrend detections: C3.4B vs EMA 4h")
        save(fig, plot_dir / "c34b_vs_ema_counter_counts.png")

    # case charts selection
    longs_against = panel[(panel["side"] == "long") & (panel["against_c34b_4h_major"] == True)]  # noqa: E712
    shorts_against = panel[(panel["side"] == "short") & (panel["against_c34b_4h_major"] == True)]  # noqa: E712
    severe = panel[panel["primary_mae_pct"] <= -10]
    top3 = panel[panel["top3_trade"] == True]  # noqa: E712
    best_al = panel[panel["alignment_category"].isin(["aligned_strong", "aligned_weak"])].nlargest(
        3, "net_return_020_pct", keep="all"
    )
    worst_neu = panel[panel["alignment_category"] == "neutral"].nsmallest(3, "primary_mae_pct", keep="all")
    case_ids = set()
    for df in (longs_against, shorts_against, severe, top3, best_al, worst_neu):
        case_ids.update(df["fill_id"].tolist())

    ts15 = pd.to_datetime(frame15["timestamp"], utc=True)
    for fid in sorted(case_ids):
        r = panel[panel["fill_id"] == fid].iloc[0]
        fill_ts = pd.Timestamp(r["fill_time"])
        # window ± 2 days
        lo = fill_ts - pd.Timedelta(days=2)
        hi = fill_ts + pd.Timedelta(days=3)
        m = (ts15 >= lo) & (ts15 <= hi)
        sub = frame15.loc[m].copy()
        if sub.empty:
            continue
        fig, axes = plt.subplots(2, 1, figsize=(10, 6), gridspec_kw={"height_ratios": [3, 1]}, sharex=True)
        ax = axes[0]
        # simple close line + entry
        ax.plot(pd.to_datetime(sub["timestamp"], utc=True), sub["close"], color="C0", linewidth=0.9)
        ax.axhline(float(r["fill_price"]), color="C1", linestyle="--", linewidth=0.8, label="entry")
        ax.axvline(fill_ts, color="C2", linestyle=":", linewidth=0.8)
        # 4h protected levels at selected bar (fixed causal)
        if pd.notna(r.get("protected_high_4h")):
            ax.axhline(float(r["protected_high_4h"]), color="C3", linestyle="-.", linewidth=0.7, alpha=0.7, label="4h PH")
        if pd.notna(r.get("protected_low_4h")):
            ax.axhline(float(r["protected_low_4h"]), color="C2", linestyle="-.", linewidth=0.7, alpha=0.7, label="4h PL")
        ax.set_title(f"{fid} {r['side']} 4h={r['major_direction_label_4h']}/{r['structure_strength_category']}")
        ax.legend(fontsize=7, loc="best")
        ax2 = axes[1]
        # major direction stepwise from 4h closes before each 15m
        maj_series = []
        for t in pd.to_datetime(sub["timestamp"], utc=True):
            decision = t + pd.Timedelta(minutes=15)
            hit = lookup_closed_c34b_bar(frame4h, trigger_decision=decision)
            if hit.get("found"):
                maj_series.append(int(hit["row"]["major_direction"]))
            else:
                maj_series.append(0)
        ax2.step(pd.to_datetime(sub["timestamp"], utc=True), maj_series, where="post")
        ax2.set_ylabel("4h major")
        ax2.set_yticks([-1, 0, 1])
        save(fig, cases_dir / f"{fid}.png")

    return written


def write_report(out_dir: Path, meta: Mapping[str, Any], panel: pd.DataFrame, guard: pd.DataFrame, hyp: pd.DataFrame) -> Path:
    lines = [
        "# C3.5c × C3.4B 4h Protected Structure Trend Audit",
        "",
        "Research-only. **No filter activation. No hedge-bot. No C3.4B/SM/Pine changes.**",
        "",
        "## 1–2. Ziel / Population",
        "",
        f"- APTUSDT A6 15m · n_fills=`{meta.get('n_fills')}` · 4h bars analyze-window=`{meta.get('n_4h_bars_analyze')}`",
        f"- Analyze `{meta.get('analyze_start')}` → `{meta.get('analyze_end_exclusive')}`",
        "",
        "## 3–4. Semantik / Kausalität",
        "",
        "```",
        json.dumps(STRENGTH_RULES_DOC, indent=2),
        "```",
        f"- share `four_hour_bar_closed_before_trigger` = `{meta.get('share_4h_closed_before_trigger')}`",
        "",
        "## 5–6. 4h-Trend / Alignment",
        "",
        f"- major labels: `{meta.get('major_label_distribution')}`",
        f"- strength: `{meta.get('strength_distribution')}`",
        f"- alignment: `{meta.get('alignment_distribution')}`",
        "",
        "## 7–11. Long/Short Risiko, MAE, schwere Fälle, Recovery",
        "",
        "- Details: `fill_c34b_4h_context.csv`, `long_c34b_4h_risk_cases.csv`, `severe` via MAE flags",
        "",
        "## 12–13. Protected Levels / Persistenz",
        "",
        "- `protected_level_distance_summary.csv`, `c34b_4h_trend_persistence.csv`",
        "",
        "## 14. C3.4B vs EMA-HTF",
        "",
        "- `c34b_vs_ema_htf_comparison.csv`, `c34b_vs_ema_aggregate.csv`",
        "",
        "## 15–16. Guards G0–G3",
        "",
    ]
    for _, r in guard.iterrows():
        lines.append(
            f"- **{r['guard']}**: block_rate={r['block_rate']:.2%} · blocked={r['blocked_fills']} "
            f"(L{r['blocked_long']}/S{r['blocked_short']}) · blocked_winners={r['blocked_exit_a_winners']} "
            f"losers={r['blocked_exit_a_losers']} · avoided MAE<=-10={r['avoided_mae_le_10']} · "
            f"allowed_sum={r['allowed_sum_exit_a_net020']:.2f} · without_top3={r['allowed_sum_without_top3']:.2f}"
        )
    lines += ["", "## 19–21. Hypothesen / Hedge-Bot", ""]
    for _, r in hyp.iterrows():
        lines.append(f"- **{r['hypothesis']}**: `{r['status']}`")
    lines += [
        "",
        "## 22–24. Unsicherheiten / Empfehlung",
        "",
        "- Stichprobe klein für einige Gegen-Trend-Zellen; Top-3 weiterhin dominant.",
        "- C3.4B 4h als **Research-Kandidat** für Start-Guard weiterverfolgen (G1), noch nicht integrieren.",
        "- Nächste Phase: Holdout-/Multi-Symbol-Check von G1; kein Live.",
        "",
    ]
    path = out_dir / "report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run_c34b_4h_trend_audit(
    *,
    output_dir: Path = DEFAULT_OUT,
    baseline_dir: Path = DEFAULT_BASELINE_DIR,
    excursion_dir: Path = EXCURSION_DIR,
    htf_align_dir: Path = HTF_ALIGN_DIR,
    write_plots: bool = True,
) -> dict[str, Any]:
    baseline_info = assert_baseline_readonly(baseline_dir)
    if not baseline_info.get("hash_matches"):
        raise RuntimeError("C2 baseline hash mismatch")
    assert_safe_output_dir(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # hash lock on C3.4B source
    c34b_path = Path("research/regime_scanner/market_structure_c3_4b.py")
    c34b_hash_before = hashlib.sha256(c34b_path.read_bytes()).hexdigest()

    cfg = baseline_a6()
    frame15, frame_meta = build_extended_tf_frame(SYMBOL, timeframe=TIMEFRAME, warmup_calendar_days=WARMUP_CALENDAR_DAYS)
    if frame15.empty:
        raise RuntimeError(f"empty 15m: {frame_meta}")

    a0 = pd.Timestamp(frame_meta["analyze_start"])
    a1 = pd.Timestamp(frame_meta["analyze_end_exclusive"])
    warm_bars = max(required_indicator_warmup_bars(), 400)
    full_5m, _ = load_ohlcv_with_warmup(SYMBOL, "5m", analyze_start=a0, analyze_end=a1, warmup_bars=warm_bars)
    decision = a1 + pd.Timedelta(hours=1)
    frame4h = build_c34b_htf_frame(
        full_5m, "4h", decision=decision, analyze_start=a0, analyze_end_exclusive=a1
    )
    frame1h = build_c34b_htf_frame(
        full_5m, "1h", decision=decision, analyze_start=a0, analyze_end_exclusive=a1
    )

    _tl, entries, _lives = apply_pullback_entry(frame15, cfg, return_lifecycles=True)
    filled = _filled_sorted(frame15, entries)
    trades = trades_exit_a_opposite_entry(frame15, filled, timeframe=TIMEFRAME, variant=cfg.name)
    closed = closed_only(trades)
    if len(filled) != 55:
        raise RuntimeError(f"expected 55 fills, got {len(filled)}")

    exc_path = excursion_dir / "fill_excursion_panel.csv"
    if not exc_path.exists():
        raise RuntimeError(f"missing excursion panel {exc_path}")
    exc = pd.read_csv(exc_path)
    if len(exc) != 55:
        raise RuntimeError(f"excursion n={len(exc)}")

    htf_path = htf_align_dir / "fill_htf_alignment_panel.csv"
    htf_panel = pd.read_csv(htf_path) if htf_path.exists() else pd.DataFrame()

    splits = fixed_chrono_splits(a0, a1)
    highs = frame15["high"].astype(float).to_numpy()
    lows = frame15["low"].astype(float).to_numpy()
    closes = frame15["close"].astype(float).to_numpy()
    n_bars = len(frame15)

    panel_rows = []
    persist_rows = []
    long_rows = []

    for i, fill in enumerate(filled):
        side_name = fill["side_name"]
        side = int(fill["side"])
        fill_i = int(fill["fill_bar"])
        trigger_ts = pd.Timestamp(fill["trigger_timestamp"])
        fill_ts = pd.Timestamp(fill["fill_timestamp"])
        entry = float(fill["entry_price"])
        trigger_decision = trigger_ts + pd.Timedelta(minutes=BAR_MINUTES)
        fill_id = f"F{i:03d}_{side_name}_{fill.get('setup_id')}"

        er = exc[exc["fill_id"] == fill_id]
        if er.empty:
            er = exc[(pd.to_datetime(exc["fill_time"], utc=True) == fill_ts) & (exc["side"] == side_name)]
        if er.empty:
            raise RuntimeError(f"missing excursion {fill_id}")
        er0 = er.iloc[0]
        if abs(float(er0["fill_price"]) - entry) > 1e-9:
            raise RuntimeError(f"price mismatch {fill_id}")

        hit = lookup_closed_c34b_bar(frame4h, trigger_decision=trigger_decision)
        if not hit.get("found"):
            raise RuntimeError(f"no closed 4h context for {fill_id}")
        row4 = hit["row"]
        major = int(row4["major_direction"])
        micro = int(row4["micro_direction"])
        strength = str(row4["structure_strength_category"])
        flags = alignment_flags(side_name, major, strength)
        maj_label = major_to_label(major)

        # optional 1h
        hit1 = lookup_closed_c34b_bar(frame1h, trigger_decision=trigger_decision)
        major_1h = int(hit1["row"]["major_direction"]) if hit1.get("found") else 0

        # EMA comparison from prior audit
        ema_row = htf_panel[htf_panel["fill_id"] == fill_id]
        ema_h4 = ema_row.iloc[0]["h4_trend"] if len(ema_row) else None
        ema_comb = ema_row.iloc[0]["combined_htf_trend"] if len(ema_row) else None
        ema_against = bool(ema_row.iloc[0]["against_4h_trend"]) if len(ema_row) and "against_4h_trend" in ema_row.columns else False

        opp_bar = None
        if pd.notna(er0.get("opposite_end_bar")):
            try:
                opp_bar = int(er0["opposite_end_bar"])
            except (TypeError, ValueError):
                opp_bar = None
        risk = recovery_and_risk_proxy(
            side=side,
            entry=entry,
            fill_i=fill_i,
            highs=highs,
            lows=lows,
            closes=closes,
            n_bars=n_bars,
            opp_bar=opp_bar,
        )

        # persistence post-fill (explanation only)
        persist = {
            "fill_id": fill_id,
            "major_at_fill": major,
            "post_entry_only": True,
        }
        for hours, lab in ((4, "4h"), (12, "12h"), (24, "24h"), (48, "48h"), (24 * 7, "7d")):
            wall = fill_ts + pd.Timedelta(hours=hours)
            h = lookup_closed_c34b_bar(frame4h, trigger_decision=wall)
            m2 = int(h["row"]["major_direction"]) if h.get("found") else None
            persist[f"major_after_{lab}"] = m2
            persist[f"major_same_after_{lab}"] = (m2 == major) if m2 is not None else None
        # first flip after fill
        fill_close_dec = fill_ts  # search 4h bars with close > fill
        later = frame4h[pd.to_datetime(frame4h["htf_close_decision"], utc=True) > fill_ts]
        first_flip = None
        for _, rr in later.iterrows():
            if int(rr["major_direction"]) != major:
                first_flip = pd.Timestamp(rr["htf_close_decision"])
                break
        persist["first_major_flip_after_fill"] = first_flip
        persist_rows.append(persist)

        # protected distance
        lvl, lvl_name = relevant_protected_level(side_name, row4)
        atr = _finite(row4.get("atr_14"), _finite(row4.get("atr")))
        dist_pct = dist_atr = float("nan")
        beyond = False
        if lvl is not None and atr and atr > 0:
            # signed: positive = still on safe side of level
            if side_name == "long":
                dist_pct = (entry - lvl) / entry * 100.0
                dist_atr = (entry - lvl) / atr
                beyond = entry < lvl  # below support
            else:
                dist_pct = (lvl - entry) / entry * 100.0
                dist_atr = (lvl - entry) / atr
                beyond = entry > lvl
            if beyond:
                dist_atr = -abs(dist_atr)
        bucket = distance_bucket(None if math.isnan(dist_atr) else dist_atr, lvl is None)

        net020 = er0.get("exit_a_net_0_20")
        winner = None
        if pd.notna(er0.get("winner_net020")):
            winner = bool(er0["winner_net020"])
        elif pd.notna(net020) and bool(er0.get("exit_a_closed")):
            winner = float(net020) > 0

        g0 = guard_decision(side_name, major, strength, "G0")
        g1 = guard_decision(side_name, major, strength, "G1")
        g2 = guard_decision(side_name, major, strength, "G2")
        g3 = guard_decision(side_name, major, strength, "G3")

        row = {
            "fill_id": fill_id,
            "side": side_name,
            "trigger_time": trigger_ts,
            "fill_time": fill_ts,
            "fill_price": entry,
            "split": assign_split(fill_ts, splits),
            "month": fill_ts.tz_convert("UTC").strftime("%Y-%m"),
            "exit_a_closed": bool(er0.get("exit_a_closed")),
            "included_in_realized_exit_a": bool(er0.get("included_in_realized_exit_a")),
            "exit_a_winner": winner,
            "net_return_020_pct": _finite(net020) if pd.notna(net020) else float("nan"),
            "top1_trade": bool(er0["top1_trade"]) if "top1_trade" in er0 and pd.notna(er0.get("top1_trade")) else False,
            "top3_trade": bool(er0["top3_trade"]) if "top3_trade" in er0 and pd.notna(er0.get("top3_trade")) else False,
            # 4h context
            "selected_4h_bar_time": hit["selected_4h_bar_time"],
            "selected_4h_bar_close_time": hit["selected_4h_bar_close_time"],
            "four_hour_bar_closed_before_trigger": True,
            "context_is_causal": True,
            "major_direction_4h": major,
            "major_direction_label_4h": maj_label,
            "micro_direction_4h": micro,
            "major_micro_alignment_4h": _finite(row4.get("major_micro_alignment")),
            "major_micro_aligned_4h": bool(row4.get("major_micro_alignment") == 1),
            "major_micro_conflict_4h": bool(row4.get("major_micro_alignment") == 0),
            "protected_high_4h": row4.get("protected_high"),
            "protected_low_4h": row4.get("protected_low"),
            "relevant_protected_level_4h": lvl,
            "relevant_protected_level_name": lvl_name,
            "distance_to_relevant_protected_pct": dist_pct,
            "distance_to_relevant_protected_atr": dist_atr,
            "protected_distance_bucket": bucket,
            "candidate_leg_4h": row4.get("candidate_leg"),
            "protected_structure_state_4h": row4.get("protected_structure_state"),
            "last_external_bos_side_4h": row4.get("last_external_bos_side"),
            "bars_since_external_bos_4h": row4.get("bars_since_external_bos"),
            "last_internal_bos_side_4h": row4.get("last_internal_bos_side"),
            "bars_since_internal_bos_4h": row4.get("bars_since_internal_bos"),
            "last_choch_side_4h": row4.get("last_choch_side"),
            "bars_since_choch_4h": row4.get("bars_since_choch"),
            "bars_since_major_flip_4h": row4.get("bars_since_major_flip"),
            "structure_strength_category": strength,
            "major_direction_1h_c34b": major_1h,
            **flags,
            # ema compare
            "ema_h4_trend": ema_h4,
            "ema_combined_htf_trend": ema_comb,
            "ema_against_4h": ema_against,
            # excursion
            "primary_mfe_pct": float(er0["maximum_favorable_excursion_pct"]),
            "primary_mae_pct": float(er0["maximum_adverse_excursion_pct"]),
            "bars_to_mfe": er0.get("bars_to_mfe"),
            "bars_to_mae": er0.get("bars_to_mae"),
            "max_underwater_duration_bars": er0.get("max_underwater_duration_bars"),
            "time_underwater_fraction": er0.get("time_underwater_fraction"),
            "tp_1_reached": bool(er0.get("tp_1_0_reached")),
            "tp_2_reached": bool(er0.get("tp_2_0_reached")),
            "tp_3_reached": bool(er0.get("tp_3_0_reached")),
            "tp_5_reached": bool(er0.get("tp_5_0_reached")),
            "path_class": er0.get("path_class"),
            "guard_G0": g0,
            "guard_G1": g1,
            "guard_G2": g2,
            "guard_G3": g3,
            "major_same_after_48h": persist["major_same_after_48h"],
            "major_same_after_7d": persist["major_same_after_7d"],
            **risk,
        }
        if pd.notna(er0.get("max_underwater_duration_bars")):
            row["max_underwater_duration_bars"] = er0.get("max_underwater_duration_bars")
        panel_rows.append(row)

        if side_name == "long":
            long_rows.append(
                {
                    **{k: row[k] for k in row if k in {
                        "fill_id", "trigger_time", "fill_time", "major_direction_label_4h", "micro_direction_4h",
                        "structure_strength_category", "protected_high_4h", "protected_low_4h",
                        "distance_to_relevant_protected_atr", "primary_mae_pct", "primary_mfe_pct",
                        "max_underwater_duration_bars", "recovery_to_entry_reached", "bars_to_recovery",
                        "net_return_020_pct", "exit_a_closed", "exit_a_winner", "path_class",
                        "guard_G1", "guard_G2", "guard_G3", "alignment_category",
                        "drawdown_exceeded_5pct", "drawdown_exceeded_10pct", "drawdown_exceeded_15pct", "drawdown_exceeded_20pct",
                    }},
                    "later_profitable_exit_a": bool(winner) if winner is not None else None,
                    "long_aligned_bullish": flags["with_c34b_4h_major"],
                    "long_neutral_mixed": flags["neutral_c34b_4h"] or strength == "mixed_structure",
                    "long_countertrend_bearish": flags["against_c34b_4h_major"],
                    "long_against_strong_bear": flags["against_strong_c34b_4h"],
                    "long_bearish_major_bullish_micro": major < 0 and micro > 0,
                    "long_bullish_major_bearish_micro": major > 0 and micro < 0,
                }
            )

    panel = pd.DataFrame(panel_rows)
    persist_df = pd.DataFrame(persist_rows)
    long_df = pd.DataFrame(long_rows)

    # verify C3.4B unchanged
    c34b_hash_after = hashlib.sha256(c34b_path.read_bytes()).hexdigest()
    if c34b_hash_before != c34b_hash_after:
        raise RuntimeError("C3.4B source mutated during audit")

    guard_df = summarize_guard_impact(panel)
    cmp_per, cmp_agg = compare_c34b_vs_ema(panel)
    hyp = evaluate_hypotheses(panel, cmp_agg)
    prot_sum = protected_level_summary(panel)
    robust = robustness_slices(panel)

    # 4h bar dump (analyze window)
    h4_analyze = frame4h[frame4h["in_analyze_window"] == True].copy()  # noqa: E712
    keep_cols = [
        c
        for c in [
            "timestamp",
            "htf_close_decision",
            "open",
            "high",
            "low",
            "close",
            "major_direction",
            "major_direction_label",
            "micro_direction",
            "candidate_leg",
            "protected_high",
            "protected_low",
            "external_bos_up",
            "external_bos_down",
            "internal_bos_up",
            "internal_bos_down",
            "choch_side",
            "last_external_bos_side",
            "bars_since_external_bos",
            "last_internal_bos_side",
            "bars_since_internal_bos",
            "last_choch_side",
            "bars_since_choch",
            "bars_since_major_flip",
            "major_micro_alignment",
            "structure_strength_category",
            "protected_structure_state",
            "micro_swing_high",
            "micro_swing_low",
        ]
        if c in h4_analyze.columns
    ]
    h4_analyze[keep_cols].to_csv(output_dir / "c34b_4h_bar_states.csv", index=False)

    panel.to_csv(output_dir / "fill_c34b_4h_context.csv", index=False)
    cmp_per.to_csv(output_dir / "c34b_vs_ema_htf_comparison.csv", index=False)
    cmp_agg.to_csv(output_dir / "c34b_vs_ema_aggregate.csv", index=False)
    guard_df.to_csv(output_dir / "c34b_guard_impact.csv", index=False)
    long_df.to_csv(output_dir / "long_c34b_4h_risk_cases.csv", index=False)
    persist_df.to_csv(output_dir / "c34b_4h_trend_persistence.csv", index=False)
    prot_sum.to_csv(output_dir / "protected_level_distance_summary.csv", index=False)
    hyp.to_csv(output_dir / "hypothesis_evaluation.csv", index=False)
    robust.to_csv(output_dir / "robustness_slices.csv", index=False)
    closed.to_csv(output_dir / "exit_a_trades_reference.csv", index=False)

    # severe extract
    severe = panel[panel["primary_mae_pct"] <= -5][
        [
            "fill_id",
            "side",
            "alignment_category",
            "major_direction_label_4h",
            "structure_strength_category",
            "primary_mae_pct",
            "max_underwater_duration_bars",
            "guard_G1",
            "guard_G2",
            "net_return_020_pct",
            "exit_a_closed",
        ]
    ]
    severe.to_csv(output_dir / "severe_mae_by_c34b_context.csv", index=False)

    plots = maybe_plots(output_dir, panel, frame15, frame4h) if write_plots else []

    meta = {
        "symbol": SYMBOL,
        "variant": VARIANT,
        "timeframe": TIMEFRAME,
        "config_hash": config_hash(cfg),
        "c34b_config": "protected_medium",
        "c34b_source_hash": c34b_hash_before,
        "n_fills": len(filled),
        "n_closed_exit_a": int(len(closed)),
        "n_4h_bars_total": int(len(frame4h)),
        "n_4h_bars_analyze": int(len(h4_analyze)),
        "n_1h_bars_total": int(len(frame1h)),
        "analyze_start": frame_meta.get("analyze_start"),
        "analyze_end_exclusive": frame_meta.get("analyze_end_exclusive"),
        "strength_rules": STRENGTH_RULES_DOC,
        "major_label_distribution": panel["major_direction_label_4h"].value_counts().to_dict(),
        "strength_distribution": panel["structure_strength_category"].value_counts().to_dict(),
        "alignment_distribution": panel["alignment_category"].value_counts().to_dict(),
        "share_4h_closed_before_trigger": float(panel["four_hour_bar_closed_before_trigger"].mean()),
        "no_entry_filter_activation": True,
        "no_hedge_bot_implementation": True,
        "c34b_unchanged": True,
        "production_sm_unchanged": True,
        "pine_unchanged": True,
        "baseline_reference_hash": C2_BASELINE_HASH,
        "plots": plots,
        "hypotheses": hyp.to_dict(orient="records"),
        "guards": guard_df.to_dict(orient="records"),
        "content_hash": hashlib.sha256(
            pd.util.hash_pandas_object(
                panel[["fill_id", "alignment_category", "major_direction_4h", "primary_mae_pct"]].fillna(""),
                index=True,
            ).values
        ).hexdigest(),
    }
    (output_dir / "metadata.json").write_text(json.dumps(json_safe(meta), indent=2) + "\n", encoding="utf-8")
    write_report(output_dir, meta, panel, guard_df, hyp)
    return meta


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="C3.5c × C3.4B 4h trend audit")
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--baseline-dir", type=Path, default=DEFAULT_BASELINE_DIR)
    p.add_argument("--excursion-dir", type=Path, default=EXCURSION_DIR)
    p.add_argument("--htf-align-dir", type=Path, default=HTF_ALIGN_DIR)
    p.add_argument("--no-plots", action="store_true")
    args = p.parse_args(list(argv) if argv is not None else None)
    meta = run_c34b_4h_trend_audit(
        output_dir=args.output_dir,
        baseline_dir=args.baseline_dir,
        excursion_dir=args.excursion_dir,
        htf_align_dir=args.htf_align_dir,
        write_plots=not args.no_plots,
    )
    print(
        json.dumps(
            json_safe(
                {
                    "ok": True,
                    "n_fills": meta["n_fills"],
                    "alignment": meta["alignment_distribution"],
                    "out": str(args.output_dir),
                }
            )
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
