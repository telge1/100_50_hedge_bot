"""C3.5c Higher-Timeframe Trend Alignment Audit (research-only, descriptive).

Diagnoses whether C3.5c A6 fills (all 55) differ in excursion / recovery / Exit-A
when opened with vs against the higher-timeframe trend.

No SM / Pine / entry-filter / hedge-bot / stop-TP changes. No commits.
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
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.pullback_entry_c3_5 import (
    apply_pullback_entry,
    config_hash,
    prepare_research_frame,
)
from research.regime_scanner.pullback_entry_c3_5_diagnostics import baseline_a6
from research.regime_scanner.pullback_entry_c3_5c_entry_path_audit import (
    TF_MINUTES,
    aggregate_complete_from_5m,
)
from research.regime_scanner.pullback_entry_c3_5c_fill_excursion_audit import (
    DEFAULT_OUT as EXCURSION_DIR,
    MAX_BARS_7D,
    fav_adv_from_bar,
    path_arrays,
    signed_return_pct,
)
from research.regime_scanner.pullback_entry_c3_5c_pattern_diagnostic_audit import (
    enrich_diagnostic_frame,
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
    "c35c_htf_trend_alignment_audit"
)
CASE_DIR = Path(
    "research/regime_scanner/results/phase_c3_5_pullback_entry_state_machine/"
    "c35c_trade_case_review"
)

SYMBOL = "APTUSDT"
TIMEFRAME = "15m"
VARIANT = "A6"
BAR_MINUTES = 15
COST_ROUNDTRIP_PCT = 0.20

COMBINED_LABELS = ("strong_bull", "bull", "mixed", "bear", "strong_bear")
ALIGNMENT_LABELS = (
    "aligned_strong",
    "aligned_weak",
    "neutral_context",
    "countertrend_weak",
    "countertrend_strong",
    "conflicting_timeframes",
)

TREND_RULES_DOC = {
    "ema_bullish": "EMA9>EMA20>EMA50 AND ema20_slope_1>0 AND ema50_slope_1>=0",
    "ema_bearish": "EMA9<EMA20<EMA50 AND ema20_slope_1<0 AND ema50_slope_1<=0",
    "ema_else": "mixed",
    "strong_bear": "1h bearish AND 4h bearish AND major_direction==-1",
    "strong_bull": "1h bullish AND 4h bullish AND major_direction==+1",
    "bear": ">=2 bearish among {1h,4h,major} AND zero bullish among them",
    "bull": ">=2 bullish among {1h,4h,major} AND zero bearish among them",
    "mixed": "otherwise (incl. 1h vs 4h conflict or single-leg trend)",
    "causality": "HTF bar usable only if open+tf <= trigger_decision (=trigger_bar_open+15m)",
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


# ---------------------------------------------------------------------------
# Trend classification (fixed rules, no optimization)
# ---------------------------------------------------------------------------


def classify_ema_trend(row: Mapping[str, Any]) -> str:
    """Return bullish / bearish / mixed from fixed EMA stack + slopes."""
    e9 = _finite(row.get("ema_9"))
    e20 = _finite(row.get("ema_20"))
    e50 = _finite(row.get("ema_50"))
    s20 = _finite(row.get("ema_20_slope_1"))
    s50 = _finite(row.get("ema_50_slope_1"))
    if any(math.isnan(v) for v in (e9, e20, e50, s20, s50)):
        return "mixed"
    if e9 > e20 > e50 and s20 > 0 and s50 >= 0:
        return "bullish"
    if e9 < e20 < e50 and s20 < 0 and s50 <= 0:
        return "bearish"
    return "mixed"


def major_to_label(major: Any) -> str:
    try:
        m = int(major)
    except (TypeError, ValueError):
        return "neutral"
    if m > 0:
        return "bullish"
    if m < 0:
        return "bearish"
    return "neutral"


def _leg_sign(label: str) -> int:
    if label == "bullish":
        return 1
    if label == "bearish":
        return -1
    return 0


def classify_combined_htf(major_label: str, h1: str, h4: str) -> str:
    """Fixed combined HTF trend. Documented in TREND_RULES_DOC."""
    m, a, b = _leg_sign(major_label), _leg_sign(h1), _leg_sign(h4)
    if a == -1 and b == -1 and m == -1:
        return "strong_bear"
    if a == 1 and b == 1 and m == 1:
        return "strong_bull"
    bears = sum(1 for x in (m, a, b) if x == -1)
    bulls = sum(1 for x in (m, a, b) if x == 1)
    if bears >= 2 and bulls == 0:
        return "bear"
    if bulls >= 2 and bears == 0:
        return "bull"
    return "mixed"


def htf_timeframes_conflict(h1: str, h4: str) -> bool:
    return h1 in {"bullish", "bearish"} and h4 in {"bullish", "bearish"} and h1 != h4


def classify_alignment_category(
    side: str,
    combined: str,
    h1: str,
    h4: str,
) -> str:
    conflict = htf_timeframes_conflict(h1, h4)
    long = side == "long"
    if long:
        if combined == "strong_bull":
            return "aligned_strong"
        if combined == "bull":
            return "aligned_weak"
        if combined == "strong_bear":
            return "countertrend_strong"
        if combined == "bear":
            return "countertrend_weak"
        if conflict:
            return "conflicting_timeframes"
        return "neutral_context"
    # short
    if combined == "strong_bear":
        return "aligned_strong"
    if combined == "bear":
        return "aligned_weak"
    if combined == "strong_bull":
        return "countertrend_strong"
    if combined == "bull":
        return "countertrend_weak"
    if conflict:
        return "conflicting_timeframes"
    return "neutral_context"


def with_trend_flags(side: str, major_label: str, h1: str, h4: str, combined: str) -> dict[str, bool]:
    want = "bullish" if side == "long" else "bearish"
    against = "bearish" if side == "long" else "bullish"
    return {
        "with_major_trend": major_label == want,
        "against_major_trend": major_label == against,
        "with_1h_trend": h1 == want,
        "against_1h_trend": h1 == against,
        "with_4h_trend": h4 == want,
        "against_4h_trend": h4 == against,
        "all_timeframes_aligned": major_label == want and h1 == want and h4 == want,
        "higher_timeframes_conflict": htf_timeframes_conflict(h1, h4),
        "conflicting_timeframes": htf_timeframes_conflict(h1, h4),
        "structure_ema_conflict": (
            (major_label == want and (h1 == against or h4 == against))
            or (major_label == against and (h1 == want or h4 == want))
        ),
        "combined_is_aligned": combined
        in ({"strong_bull", "bull"} if side == "long" else {"strong_bear", "bear"}),
        "combined_is_counter": combined
        in ({"strong_bear", "bear"} if side == "long" else {"strong_bull", "bull"}),
    }


# ---------------------------------------------------------------------------
# HTF frame builders / causal lookup
# ---------------------------------------------------------------------------


def build_tf_research_slice(
    full_5m: pd.DataFrame,
    timeframe: str,
    *,
    decision: pd.Timestamp,
    analyze_start: pd.Timestamp,
    analyze_end_exclusive: pd.Timestamp,
) -> pd.DataFrame:
    ohlcv = aggregate_complete_from_5m(full_5m, timeframe, decision_time=decision)
    if ohlcv.empty:
        return pd.DataFrame()
    frame = prepare_research_frame(ohlcv, ohlcv_15m=None, ohlcv_30m=None)
    ts = pd.to_datetime(frame["timestamp"], utc=True)
    # keep warmup bars for EMA maturity; slice only for display meta — lookup uses full causal frame
    frame = frame.copy()
    frame["timestamp"] = ts
    frame["bar_index"] = np.arange(len(frame))
    frame["timeframe"] = timeframe
    frame["htf_close_decision"] = ts + pd.Timedelta(minutes=TF_MINUTES[timeframe])
    # analyze flag
    frame["in_analyze_window"] = (ts >= analyze_start) & (ts < analyze_end_exclusive)
    return frame.reset_index(drop=True)


def lookup_last_closed_htf(
    htf: pd.DataFrame,
    *,
    trigger_decision: pd.Timestamp,
    tf_minutes: int,
) -> dict[str, Any]:
    """Last fully closed HTF bar with close_decision <= trigger_decision."""
    if htf.empty:
        return {"found": False, "context_is_causal": True, "htf_bar_closed_before_trigger": False}
    close_dec = pd.to_datetime(htf["htf_close_decision"], utc=True)
    mask = close_dec <= pd.Timestamp(trigger_decision)
    if not mask.any():
        return {
            "found": False,
            "context_is_causal": True,
            "htf_bar_closed_before_trigger": False,
            "tf_minutes": tf_minutes,
        }
    idx = int(np.where(mask.to_numpy())[0][-1])
    row = htf.iloc[idx]
    # sanity: no open bar (close after trigger)
    assert pd.Timestamp(row["htf_close_decision"]) <= pd.Timestamp(trigger_decision)
    trend = classify_ema_trend(row)
    return {
        "found": True,
        "context_is_causal": True,
        "htf_bar_closed_before_trigger": True,
        "tf_minutes": tf_minutes,
        "context_bar_time": pd.Timestamp(row["timestamp"]),
        "context_close_decision": pd.Timestamp(row["htf_close_decision"]),
        "ema_9": _finite(row.get("ema_9")),
        "ema_20": _finite(row.get("ema_20")),
        "ema_50": _finite(row.get("ema_50")),
        "ema_20_slope": _finite(row.get("ema_20_slope_1")),
        "ema_50_slope": _finite(row.get("ema_50_slope_1")),
        "ema9_minus_ema20_pct": (
            (_finite(row.get("ema_9")) - _finite(row.get("ema_20"))) / _finite(row.get("close")) * 100.0
            if _finite(row.get("close"))
            else float("nan")
        ),
        "ema20_minus_ema50_pct": (
            (_finite(row.get("ema_20")) - _finite(row.get("ema_50"))) / _finite(row.get("close")) * 100.0
            if _finite(row.get("close"))
            else float("nan")
        ),
        "major_direction_htf": int(row["major_direction"]) if pd.notna(row.get("major_direction")) else 0,
        "trend": trend,
        "row_index": idx,
    }


def htf_trend_at_wall(
    htf: pd.DataFrame,
    *,
    wall_time: pd.Timestamp,
) -> str | None:
    """Post-fill explanation only: trend of last closed HTF bar at wall_time."""
    if htf.empty:
        return None
    close_dec = pd.to_datetime(htf["htf_close_decision"], utc=True)
    mask = close_dec <= pd.Timestamp(wall_time)
    if not mask.any():
        return None
    idx = int(np.where(mask.to_numpy())[0][-1])
    return classify_ema_trend(htf.iloc[idx])


# ---------------------------------------------------------------------------
# Recovery / hedge-bot risk proxy
# ---------------------------------------------------------------------------


def recovery_and_risk_proxy(
    *,
    side: int,
    entry: float,
    fill_i: int,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    n_bars: int,
    opp_bar: int | None,
) -> dict[str, Any]:
    """Direction-normalized recovery + MAE/MFE horizons. Not hedge-bot PnL."""
    end_data = n_bars - 1
    end_opp = opp_bar if opp_bar is not None else end_data
    end_7d = min(end_data, fill_i + MAX_BARS_7D)
    end_primary = min(end_opp, end_7d)

    def _horizon_end(hours: float) -> int:
        bars = max(1, int(round(hours * 60 / BAR_MINUTES)))
        return min(end_data, fill_i + bars - 1)

    out: dict[str, Any] = {}
    for label, hours in (("24h", 24.0), ("48h", 48.0), ("7d", 24.0 * 7)):
        end_h = _horizon_end(hours)
        p = path_arrays(side, entry, highs, lows, closes, fill_i, end_h)
        out[f"mfe_to_{label}"] = p.get("maximum_favorable_excursion_pct")
        out[f"mae_to_{label}"] = p.get("maximum_adverse_excursion_pct")

    p_prim = path_arrays(side, entry, highs, lows, closes, fill_i, end_primary)
    out["max_underwater_duration_bars"] = p_prim.get("max_underwater_duration_bars")
    mae = float(p_prim.get("maximum_adverse_excursion_pct") or 0.0)
    for thr in (5, 10, 15, 20, 30, 40):
        out[f"drawdown_exceeded_{thr}pct"] = bool(mae <= -float(thr))
        # continued against = MAE reached threshold
        if thr in (10, 20, 30, 40):
            out[f"continued_against_{thr}pct"] = bool(mae <= -float(thr))

    # Recovery: after first underwater close, first bar whose OHLC touches entry again
    recovered = False
    bars_to_rec: int | None = None
    ever_underwater = False
    max_adverse_before_rec = 0.0
    run_adv = 0.0
    for off in range(0, end_primary - fill_i + 1):
        i = fill_i + off
        fav, adv = fav_adv_from_bar(side, entry, float(highs[i]), float(lows[i]))
        close_s = signed_return_pct(side, entry, float(closes[i]))
        run_adv = min(run_adv, adv)
        if close_s < -1e-12:
            ever_underwater = True
        if ever_underwater and not recovered:
            if side > 0:
                touched = float(highs[i]) >= entry - 1e-12
            else:
                touched = float(lows[i]) <= entry + 1e-12
            if touched:
                recovered = True
                bars_to_rec = off
                max_adverse_before_rec = run_adv
    if not ever_underwater:
        recovered = True
        bars_to_rec = 0
        max_adverse_before_rec = 0.0

    out["recovery_to_entry_reached"] = bool(recovered)
    out["bars_to_recovery"] = bars_to_rec
    out["minutes_to_recovery"] = None if bars_to_rec is None else bars_to_rec * BAR_MINUTES
    out["max_adverse_before_recovery"] = max_adverse_before_rec if recovered else mae
    out["never_recovered_before_opposite_or_data_end"] = bool(ever_underwater and not recovered)
    out["ever_underwater"] = ever_underwater
    for hours, key in ((6, "6h"), (12, "12h"), (24, "24h"), (48, "48h"), (24 * 7, "7d")):
        lim = int(round(hours * 60 / BAR_MINUTES))
        out[f"recovered_within_{key}"] = bool(
            recovered and bars_to_rec is not None and bars_to_rec <= lim
        )
    return out


# ---------------------------------------------------------------------------
# Aggregations / hypotheses / plots / report
# ---------------------------------------------------------------------------


def summarize_alignment_excursion(panel: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for cat, g in panel.groupby("alignment_category"):
        rows.append(_excursion_group_row(g, group=str(cat)))
    return pd.DataFrame(rows)


def _excursion_group_row(g: pd.DataFrame, *, group: str) -> dict[str, Any]:
    return {
        "group": group,
        "n": len(g),
        "n_long": int((g["side"] == "long").sum()),
        "n_short": int((g["side"] == "short").sum()),
        "share_long": float((g["side"] == "long").mean()) if len(g) else None,
        "median_mfe": float(g["primary_mfe_pct"].median()),
        "mean_mfe": float(g["primary_mfe_pct"].mean()),
        "median_mae": float(g["primary_mae_pct"].median()),
        "mean_mae": float(g["primary_mae_pct"].mean()),
        "p75_mae": float(g["primary_mae_pct"].quantile(0.75)),
        "p90_mae": float(g["primary_mae_pct"].quantile(0.90)),
        "median_underwater_bars": float(g["max_underwater_duration_bars"].median()),
        "tp1_reach": float(g["tp_1_reached"].mean()) if "tp_1_reached" in g else None,
        "tp2_reach": float(g["tp_2_reached"].mean()) if "tp_2_reached" in g else None,
        "tp3_reach": float(g["tp_3_reached"].mean()) if "tp_3_reached" in g else None,
        "tp5_reach": float(g["tp_5_reached"].mean()) if "tp_5_reached" in g else None,
        "sl1_reach": float(g["sl_1_reached"].mean()) if "sl_1_reached" in g else None,
        "sl2_reach": float(g["sl_2_reached"].mean()) if "sl_2_reached" in g else None,
        "sl3_reach": float(g["sl_3_reached"].mean()) if "sl_3_reached" in g else None,
        "sl5_reach": float(g["sl_5_reached"].mean()) if "sl_5_reached" in g else None,
        "median_adverse_before_tp2": float(g["adverse_before_tp_2"].median()) if "adverse_before_tp_2" in g else None,
        "median_adverse_before_tp3": float(g["adverse_before_tp_3"].median()) if "adverse_before_tp_3" in g else None,
        "median_adverse_before_tp5": float(g["adverse_before_tp_5"].median()) if "adverse_before_tp_5" in g else None,
        "share_persistent_adverse": float((g["path_class"] == "persistent_adverse").mean()) if "path_class" in g else None,
        "share_deep_adverse_then_recovery": float((g["path_class"] == "deep_adverse_then_recovery").mean())
        if "path_class" in g
        else None,
        "share_clean_immediate_favorable": float((g["path_class"] == "clean_immediate_favorable").mean())
        if "path_class" in g
        else None,
    }


def summarize_exit_a(panel: pd.DataFrame) -> pd.DataFrame:
    ea = panel[(panel["exit_a_closed"] == True) & (panel["included_in_realized_exit_a"] == True)].copy()  # noqa: E712
    rows = []
    if ea.empty:
        return pd.DataFrame()
    for cat, g in ea.groupby("alignment_category"):
        rows.append(_exit_a_row(g, group=str(cat)))
        for side, gs in g.groupby("side"):
            rows.append(_exit_a_row(gs, group=f"{cat}|{side}"))
    rows.append(_exit_a_row(ea, group="all_closed"))
    return pd.DataFrame(rows)


def _exit_a_row(g: pd.DataFrame, *, group: str) -> dict[str, Any]:
    net = pd.to_numeric(g.get("net_return_020_pct", g.get("exit_a_net_0_20")), errors="coerce")
    wins = net > 0
    best = float(net.max()) if len(net) else None
    sum_net = float(net.sum()) if len(net) else None
    without_best = float(net.sum() - net.max()) if len(net) > 1 else (0.0 if len(net) == 1 else None)
    top3 = g["top3_trade"] == True if "top3_trade" in g.columns else pd.Series([False] * len(g))  # noqa: E712
    without_top3 = net[~top3.fillna(False)]
    return {
        "group": group,
        "n": len(g),
        "winrate": float(wins.mean()) if len(g) else None,
        "mean_net_0_20": float(net.mean()) if len(g) else None,
        "median_net_0_20": float(net.median()) if len(g) else None,
        "sum_net_0_20": sum_net,
        "profit_factor": _pf(net),
        "best": best,
        "worst": float(net.min()) if len(net) else None,
        "top1_share": float((g["top1_trade"] == True).mean()) if "top1_trade" in g.columns else None,  # noqa: E712
        "top3_share": float(top3.fillna(False).mean()) if len(g) else None,
        "sum_without_best": without_best,
        "sum_without_top3": float(without_top3.sum()) if len(without_top3) else None,
        "n_without_top3": int((~top3.fillna(False)).sum()),
    }


def long_short_trend_matrix(panel: pd.DataFrame) -> pd.DataFrame:
    def bucket(side: str, combined: str) -> str | None:
        if side == "long":
            if combined in {"strong_bull", "bull"}:
                return "long_in_bull"
            if combined == "mixed":
                return "long_in_mixed"
            if combined in {"strong_bear", "bear"}:
                return "long_in_bear"
        else:
            if combined in {"strong_bear", "bear"}:
                return "short_in_bear"
            if combined == "mixed":
                return "short_in_mixed"
            if combined in {"strong_bull", "bull"}:
                return "short_in_bull"
        return None

    tmp = panel.copy()
    tmp["matrix_cell"] = [bucket(s, c) for s, c in zip(tmp["side"], tmp["combined_htf_trend"])]
    rows = []
    for cell, g in tmp.dropna(subset=["matrix_cell"]).groupby("matrix_cell"):
        row = _excursion_group_row(g, group=str(cell))
        ea = g[(g["exit_a_closed"] == True) & (g["included_in_realized_exit_a"] == True)]  # noqa: E712
        if len(ea):
            net = pd.to_numeric(ea["net_return_020_pct"], errors="coerce")
            row["exit_a_n"] = len(ea)
            row["exit_a_winrate"] = float((net > 0).mean())
            row["exit_a_mean_net"] = float(net.mean())
            row["exit_a_sum_net"] = float(net.sum())
        else:
            row["exit_a_n"] = 0
            row["exit_a_winrate"] = None
            row["exit_a_mean_net"] = None
            row["exit_a_sum_net"] = None
        row["recovery_rate"] = float(g["recovery_to_entry_reached"].mean()) if "recovery_to_entry_reached" in g else None
        rows.append(row)
    return pd.DataFrame(rows)


def timeframe_agreement_summary(panel: pd.DataFrame) -> pd.DataFrame:
    rows = []
    masks = {
        "all_15m_1h_4h_aligned": panel["all_timeframes_aligned"] == True,  # noqa: E712
        "h1_h4_aligned_micro_against": (panel["with_1h_trend"] == True)  # noqa: E712
        & (panel["with_4h_trend"] == True)
        & (panel["major_micro_alignment"] == 0),
        "h1_h4_conflict": panel["higher_timeframes_conflict"] == True,  # noqa: E712
        "structure_vs_ema_conflict": panel["structure_ema_conflict"] == True,  # noqa: E712
    }
    for name, m in masks.items():
        g = panel[m]
        rows.append(_excursion_group_row(g, group=name) if len(g) else {"group": name, "n": 0})
    return pd.DataFrame(rows)


def recovery_by_alignment(panel: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for keys, g in panel.groupby(["alignment_category", "side"]):
        cat, side = keys
        rec = g[g["ever_underwater"] == True]  # noqa: E712
        bars = pd.to_numeric(rec["bars_to_recovery"], errors="coerce")
        rows.append(
            {
                "alignment_category": cat,
                "side": side,
                "n": len(g),
                "n_ever_underwater": len(rec),
                "recovery_rate_among_underwater": float(rec["recovery_to_entry_reached"].mean()) if len(rec) else None,
                "recovery_rate_all": float(g["recovery_to_entry_reached"].mean()),
                "never_recovered_rate": float(g["never_recovered_before_opposite_or_data_end"].mean()),
                "median_bars_to_recovery": float(bars.median()) if bars.notna().any() else None,
                "p75_bars_to_recovery": float(bars.quantile(0.75)) if bars.notna().any() else None,
                "p90_bars_to_recovery": float(bars.quantile(0.90)) if bars.notna().any() else None,
                "recovered_within_6h": float(g["recovered_within_6h"].mean()),
                "recovered_within_12h": float(g["recovered_within_12h"].mean()),
                "recovered_within_24h": float(g["recovered_within_24h"].mean()),
                "recovered_within_48h": float(g["recovered_within_48h"].mean()),
                "recovered_within_7d": float(g["recovered_within_7d"].mean()),
                "median_adverse_before_recovery": float(g["max_adverse_before_recovery"].median()),
            }
        )
    # also by alignment only
    for cat, g in panel.groupby("alignment_category"):
        bars = pd.to_numeric(g.loc[g["ever_underwater"] == True, "bars_to_recovery"], errors="coerce")  # noqa: E712
        rows.append(
            {
                "alignment_category": cat,
                "side": "both",
                "n": len(g),
                "n_ever_underwater": int((g["ever_underwater"] == True).sum()),  # noqa: E712
                "recovery_rate_among_underwater": float(
                    g.loc[g["ever_underwater"] == True, "recovery_to_entry_reached"].mean()  # noqa: E712
                )
                if (g["ever_underwater"] == True).any()  # noqa: E712
                else None,
                "recovery_rate_all": float(g["recovery_to_entry_reached"].mean()),
                "never_recovered_rate": float(g["never_recovered_before_opposite_or_data_end"].mean()),
                "median_bars_to_recovery": float(bars.median()) if bars.notna().any() else None,
                "p75_bars_to_recovery": float(bars.quantile(0.75)) if bars.notna().any() else None,
                "p90_bars_to_recovery": float(bars.quantile(0.90)) if bars.notna().any() else None,
                "recovered_within_6h": float(g["recovered_within_6h"].mean()),
                "recovered_within_12h": float(g["recovered_within_12h"].mean()),
                "recovered_within_24h": float(g["recovered_within_24h"].mean()),
                "recovered_within_48h": float(g["recovered_within_48h"].mean()),
                "recovered_within_7d": float(g["recovered_within_7d"].mean()),
                "median_adverse_before_recovery": float(g["max_adverse_before_recovery"].median()),
            }
        )
    return pd.DataFrame(rows)


def severe_countertrend_cases(panel: pd.DataFrame, case_dir: Path) -> pd.DataFrame:
    rows = []
    for thr in (5, 10, 15, 20):
        g = panel[panel["primary_mae_pct"] <= -float(thr)]
        for _, r in g.iterrows():
            chart = None
            if case_dir.exists() and bool(r.get("exit_a_closed")):
                # best-effort link
                slug_side = str(r["side"])
                chart = str(case_dir / "cases")
            rows.append(
                {
                    "severity_mae_threshold": -float(thr),
                    "fill_id": r["fill_id"],
                    "side": r["side"],
                    "alignment_category": r["alignment_category"],
                    "combined_htf_trend": r["combined_htf_trend"],
                    "trigger_time": r["trigger_time"],
                    "fill_time": r["fill_time"],
                    "major_direction": r.get("major_direction"),
                    "h1_trend": r.get("h1_trend"),
                    "h4_trend": r.get("h4_trend"),
                    "primary_mae_pct": r["primary_mae_pct"],
                    "bars_to_mae": r.get("bars_to_mae"),
                    "recovery_to_entry_reached": r.get("recovery_to_entry_reached"),
                    "primary_mfe_pct": r["primary_mfe_pct"],
                    "exit_a_closed": r.get("exit_a_closed"),
                    "net_return_020_pct": r.get("net_return_020_pct"),
                    "path_class": r.get("path_class"),
                    "countertrend_long_in_bear": bool(
                        r["side"] == "long" and r["combined_htf_trend"] in {"bear", "strong_bear"}
                    ),
                    "countertrend_short_in_bull": bool(
                        r["side"] == "short" and r["combined_htf_trend"] in {"bull", "strong_bull"}
                    ),
                    "case_chart_path": chart,
                }
            )
    return pd.DataFrame(rows)


def evaluate_hypotheses(panel: pd.DataFrame) -> pd.DataFrame:
    rows = []
    ct_long = panel[(panel["side"] == "long") & (panel["combined_htf_trend"].isin(["bear", "strong_bear"]))]
    al_short = panel[(panel["side"] == "short") & (panel["combined_htf_trend"].isin(["bear", "strong_bear"]))]
    al_long = panel[(panel["side"] == "long") & (panel["combined_htf_trend"].isin(["bull", "strong_bull"]))]
    all_aligned = panel[panel["all_timeframes_aligned"] == True]  # noqa: E712
    mixed = panel[panel["combined_htf_trend"] == "mixed"]
    strong_ct = panel[panel["alignment_category"] == "countertrend_strong"]
    short_all = panel[panel["side"] == "short"]
    long_all = panel[panel["side"] == "long"]

    # H1
    h1_status = "underpowered"
    if len(ct_long) >= 3 and len(al_long) >= 1:
        if float(ct_long["primary_mae_pct"].median()) < float(al_long["primary_mae_pct"].median()) and float(
            ct_long["max_underwater_duration_bars"].median()
        ) > float(al_long["max_underwater_duration_bars"].median()):
            h1_status = "supported_descriptively"
        else:
            h1_status = "not_supported"
    elif len(ct_long) >= 3:
        # compare to all longs not counter
        other = long_all[~long_all.index.isin(ct_long.index)]
        if len(other) and float(ct_long["primary_mae_pct"].median()) < float(other["primary_mae_pct"].median()):
            h1_status = "partially_supported"
        else:
            h1_status = "underpowered"
    rows.append(
        {
            "hypothesis": "H1_countertrend_long_worse_mae_underwater",
            "status": h1_status,
            "n_counter_long": len(ct_long),
            "n_aligned_long": len(al_long),
            "med_mae_ct_long": float(ct_long["primary_mae_pct"].median()) if len(ct_long) else None,
            "med_uw_ct_long": float(ct_long["max_underwater_duration_bars"].median()) if len(ct_long) else None,
        }
    )

    # H2
    h2 = "underpowered"
    if len(al_short) >= 5:
        other_s = short_all[~short_all.index.isin(al_short.index)]
        if len(other_s) >= 3:
            better_mae = float(al_short["primary_mae_pct"].median()) > float(other_s["primary_mae_pct"].median())
            better_tp = float(al_short["tp_2_reached"].mean()) >= float(other_s["tp_2_reached"].mean())
            h2 = "supported_descriptively" if better_mae and better_tp else ("partially_supported" if better_mae or better_tp else "not_supported")
        else:
            h2 = "partially_supported" if float(al_short["primary_mae_pct"].median()) > float(short_all["primary_mae_pct"].median()) else "underpowered"
    rows.append({"hypothesis": "H2_aligned_short_better", "status": h2, "n_aligned_short": len(al_short)})

    # H3
    h3 = "underpowered"
    if len(all_aligned) >= 5:
        rest = panel[~panel.index.isin(all_aligned.index)]
        severe_al = float((all_aligned["primary_mae_pct"] <= -10).mean())
        severe_rest = float((rest["primary_mae_pct"] <= -10).mean()) if len(rest) else None
        h3 = "supported_descriptively" if severe_rest is not None and severe_al < severe_rest else "not_supported"
    rows.append({"hypothesis": "H3_all_tf_alignment_reduces_severe_mae", "status": h3, "n_all_aligned": len(all_aligned)})

    # H4
    h4 = "underpowered"
    if len(mixed) >= 5 and len(strong_ct) >= 3:
        if float(mixed["primary_mae_pct"].median()) > float(strong_ct["primary_mae_pct"].median()):
            # mixed less bad than strong counter; compare to aligned
            aligned = panel[panel["alignment_category"].isin(["aligned_strong", "aligned_weak"])]
            if len(aligned) and float(mixed["primary_mae_pct"].median()) < float(aligned["primary_mae_pct"].median()):
                h4 = "supported_descriptively"
            else:
                h4 = "partially_supported"
        else:
            h4 = "not_supported"
    rows.append({"hypothesis": "H4_mixed_between_aligned_and_strong_counter", "status": h4})

    # H5 short advantage mostly HTF
    h5 = "confounded_by_side"
    # within short: aligned vs counter
    s_al = short_all[short_all["alignment_category"].isin(["aligned_strong", "aligned_weak"])]
    s_ct = short_all[short_all["alignment_category"].isin(["countertrend_strong", "countertrend_weak"])]
    if len(s_al) >= 5 and len(s_ct) >= 3:
        if abs(float(s_al["primary_mae_pct"].median()) - float(s_ct["primary_mae_pct"].median())) < 0.5:
            h5 = "confounded_by_side"  # alignment adds little within short
        else:
            h5 = "partially_supported"
    # share of shorts that are aligned in bear
    share_short_in_bear = float(short_all["combined_htf_trend"].isin(["bear", "strong_bear"]).mean()) if len(short_all) else None
    rows.append(
        {
            "hypothesis": "H5_short_edge_is_htf_bear_context",
            "status": h5,
            "share_shorts_in_bear_htf": share_short_in_bear,
            "n_short_aligned": len(s_al),
            "n_short_counter": len(s_ct),
        }
    )

    # H6 top3 aligned
    top3 = panel[panel["top3_trade"] == True]  # noqa: E712
    if len(top3) == 0:
        h6 = "underpowered"
    else:
        aligned_ok = bool(
            (
                ((top3["side"] == "short") & (top3["combined_htf_trend"].isin(["bear", "strong_bear"])))
                | ((top3["side"] == "long") & (top3["combined_htf_trend"].isin(["bull", "strong_bull"])))
            ).all()
        )
        h6 = "supported_descriptively" if aligned_ok else "not_supported"
        if aligned_ok and len(top3) <= 3:
            h6 = "top3_driven"
    rows.append({"hypothesis": "H6_top3_in_trend_direction", "status": h6, "n_top3": len(top3)})
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
        for side_name, gs in [("all", g)] + [(s, g[g["side"] == s]) for s in ("long", "short")]:
            if gs.empty:
                continue
            for cat in ("aligned_strong", "aligned_weak", "countertrend_weak", "countertrend_strong", "neutral_context", "conflicting_timeframes"):
                sub = gs[gs["alignment_category"] == cat]
                if len(sub) < 1:
                    continue
                rows.append(
                    {
                        "slice": name,
                        "side_filter": side_name,
                        "alignment_category": cat,
                        "n": len(sub),
                        "median_mae": float(sub["primary_mae_pct"].median()),
                        "median_mfe": float(sub["primary_mfe_pct"].median()),
                        "median_underwater": float(sub["max_underwater_duration_bars"].median()),
                        "tp2_reach": float(sub["tp_2_reached"].mean()),
                        "never_recovered_rate": float(sub["never_recovered_before_opposite_or_data_end"].mean()),
                    }
                )
    # same-side aligned vs counter
    for side in ("long", "short"):
        al = panel[(panel["side"] == side) & (panel["alignment_category"].isin(["aligned_strong", "aligned_weak"]))]
        ct = panel[(panel["side"] == side) & (panel["alignment_category"].isin(["countertrend_strong", "countertrend_weak"]))]
        rows.append(
            {
                "slice": f"within_{side}_aligned_vs_counter",
                "side_filter": side,
                "alignment_category": "aligned_minus_counter_mae",
                "n": len(al),
                "n_counter": len(ct),
                "median_mae": float(al["primary_mae_pct"].median()) if len(al) else None,
                "median_mae_counter": float(ct["primary_mae_pct"].median()) if len(ct) else None,
                "mae_delta_aligned_minus_counter": (
                    float(al["primary_mae_pct"].median()) - float(ct["primary_mae_pct"].median())
                    if len(al) and len(ct)
                    else None
                ),
                "median_mfe": float(al["primary_mfe_pct"].median()) if len(al) else None,
                "median_underwater": float(al["max_underwater_duration_bars"].median()) if len(al) else None,
                "tp2_reach": float(al["tp_2_reached"].mean()) if len(al) else None,
                "never_recovered_rate": float(al["never_recovered_before_opposite_or_data_end"].mean()) if len(al) else None,
            }
        )
    return pd.DataFrame(rows)


def maybe_plots(out_dir: Path, panel: pd.DataFrame, matrix: pd.DataFrame, recovery: pd.DataFrame) -> list[str]:
    written = []
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return written
    plot_dir = out_dir / "plots"
    plot_dir.mkdir(parents=True, exist_ok=True)

    def save(fig, name: str) -> None:
        p = plot_dir / name
        fig.tight_layout()
        fig.savefig(p, dpi=110)
        plt.close(fig)
        written.append(str(p))

    # MAE by alignment
    order = [c for c in ALIGNMENT_LABELS if c in set(panel["alignment_category"])]
    fig, ax = plt.subplots(figsize=(9, 4))
    data = [panel.loc[panel["alignment_category"] == c, "primary_mae_pct"].dropna() for c in order]
    if data:
        ax.boxplot(data, labels=order, showfliers=False)
        ax.set_title("MAE by alignment")
        ax.tick_params(axis="x", rotation=30)
        save(fig, "mae_by_alignment.png")

    fig, ax = plt.subplots(figsize=(9, 4))
    data = [panel.loc[panel["alignment_category"] == c, "primary_mfe_pct"].dropna() for c in order]
    if data:
        ax.boxplot(data, labels=order, showfliers=False)
        ax.set_title("MFE by alignment")
        ax.tick_params(axis="x", rotation=30)
        save(fig, "mfe_by_alignment.png")

    fig, ax = plt.subplots(figsize=(9, 4))
    data = [panel.loc[panel["alignment_category"] == c, "max_underwater_duration_bars"].dropna() for c in order]
    if data:
        ax.boxplot(data, labels=order, showfliers=False)
        ax.set_title("Underwater bars by alignment")
        ax.tick_params(axis="x", rotation=30)
        save(fig, "underwater_by_alignment.png")

    # matrix heatmap n / median mae
    if not matrix.empty and "group" in matrix.columns:
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.bar(matrix["group"], matrix["n"])
        ax.set_title("Long/Short × HTF trend cell counts")
        ax.tick_params(axis="x", rotation=30)
        save(fig, "long_short_htf_matrix_n.png")
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.bar(matrix["group"], matrix["median_mae"])
        ax.set_title("Median MAE by matrix cell")
        ax.tick_params(axis="x", rotation=30)
        save(fig, "long_short_htf_matrix_mae.png")

    # recovery rates
    both = recovery[recovery["side"] == "both"] if "side" in recovery.columns else recovery
    if not both.empty:
        fig, ax = plt.subplots(figsize=(9, 4))
        ax.bar(both["alignment_category"], both["recovery_rate_all"])
        ax.set_title("Recovery rate by alignment")
        ax.tick_params(axis="x", rotation=30)
        save(fig, "recovery_rate_by_alignment.png")
        fig, ax = plt.subplots(figsize=(9, 4))
        ax.bar(both["alignment_category"], both["median_bars_to_recovery"])
        ax.set_title("Median bars to recovery")
        ax.tick_params(axis="x", rotation=30)
        save(fig, "recovery_time_by_alignment.png")

    # TP/SL reach
    fig, ax = plt.subplots(figsize=(9, 4))
    ax.bar(order, [panel.loc[panel.alignment_category == c, "tp_2_reached"].mean() for c in order], label="TP2")
    ax.bar(order, [panel.loc[panel.alignment_category == c, "tp_5_reached"].mean() for c in order], alpha=0.5, label="TP5")
    ax.legend()
    ax.set_title("TP2/TP5 reach by alignment")
    ax.tick_params(axis="x", rotation=30)
    save(fig, "tp_reach_by_alignment.png")

    fig, ax = plt.subplots(figsize=(9, 4))
    ax.bar(order, [panel.loc[panel.alignment_category == c, "sl_2_reached"].mean() for c in order], label="SL2", color="C3")
    ax.bar(order, [panel.loc[panel.alignment_category == c, "sl_5_reached"].mean() for c in order], alpha=0.5, label="SL5", color="C5")
    ax.legend()
    ax.set_title("SL2/SL5 reach by alignment")
    ax.tick_params(axis="x", rotation=30)
    save(fig, "sl_reach_by_alignment.png")

    # Exit-A returns
    ea = panel[(panel["exit_a_closed"] == True) & (panel["included_in_realized_exit_a"] == True)]  # noqa: E712
    if len(ea):
        fig, ax = plt.subplots(figsize=(9, 4))
        cats = [c for c in order if c in set(ea["alignment_category"])]
        ax.bar(cats, [ea.loc[ea.alignment_category == c, "net_return_020_pct"].mean() for c in cats])
        ax.set_title("Exit-A mean net0.20 by alignment")
        ax.tick_params(axis="x", rotation=30)
        save(fig, "exit_a_return_by_alignment.png")

    # 1h/4h combo counts
    fig, ax = plt.subplots(figsize=(7, 5))
    piv = panel.pivot_table(index="h1_trend", columns="h4_trend", values="fill_id", aggfunc="count").fillna(0)
    im = ax.imshow(piv.values, cmap="Blues")
    ax.set_xticks(range(len(piv.columns)))
    ax.set_xticklabels(list(piv.columns))
    ax.set_yticks(range(len(piv.index)))
    ax.set_yticklabels(list(piv.index))
    ax.set_xlabel("4h trend")
    ax.set_ylabel("1h trend")
    ax.set_title("1h × 4h trend counts")
    fig.colorbar(im, ax=ax, fraction=0.046)
    save(fig, "h1_h4_trend_combo.png")

    # aligned short vs counter long
    fig, ax = plt.subplots(figsize=(6, 5))
    a_s = panel[(panel["side"] == "short") & (panel["alignment_category"].isin(["aligned_strong", "aligned_weak"]))]
    c_l = panel[(panel["side"] == "long") & (panel["alignment_category"].isin(["countertrend_strong", "countertrend_weak"]))]
    ax.scatter(a_s["primary_mae_pct"], a_s["primary_mfe_pct"], alpha=0.7, label="aligned short")
    ax.scatter(c_l["primary_mae_pct"], c_l["primary_mfe_pct"], alpha=0.7, label="countertrend long")
    ax.legend()
    ax.set_title("Aligned Shorts vs Countertrend Longs")
    ax.set_xlabel("MAE %")
    ax.set_ylabel("MFE %")
    save(fig, "aligned_short_vs_counter_long.png")

    # severe timeline
    sev = panel[panel["primary_mae_pct"] <= -10].sort_values("fill_time")
    if len(sev):
        fig, ax = plt.subplots(figsize=(10, 4))
        colors = ["C3" if s == "long" else "C0" for s in sev["side"]]
        ax.scatter(pd.to_datetime(sev["fill_time"]), sev["primary_mae_pct"], c=colors)
        ax.set_title("Severe MAE (<= -10%) timeline")
        ax.tick_params(axis="x", rotation=30)
        save(fig, "severe_countertrend_timeline.png")

    # trend persistence share
    if "h1_same_after_24h" in panel.columns:
        fig, ax = plt.subplots(figsize=(7, 4))
        ax.bar(["1h same 24h", "4h same 24h", "1h same 48h", "4h same 48h"], [
            float(panel["h1_same_after_24h"].mean()),
            float(panel["h4_same_after_24h"].mean()),
            float(panel["h1_same_after_48h"].mean()),
            float(panel["h4_same_after_48h"].mean()),
        ])
        ax.set_title("HTF trend persistence after fill")
        save(fig, "trend_persistence_after_fill.png")

    return written


def write_report(out_dir: Path, meta: Mapping[str, Any], panel: pd.DataFrame, hyp: pd.DataFrame, matrix: pd.DataFrame) -> Path:
    dist = panel["alignment_category"].value_counts().to_dict()
    comb = panel["combined_htf_trend"].value_counts().to_dict()
    lines = [
        "# C3.5c Higher-Timeframe Trend Alignment Audit",
        "",
        "Research-only. **No entry filter. No hedge-bot implementation. No stop/TP optimization.**",
        "",
        "## 1. Ziel und Abgrenzung",
        "",
        "Prüfen, wie sich Preisweg/Drawdown/Recovery unterscheiden, wenn ein A6-Fill mit oder gegen den übergeordneten Trend erfolgt.",
        "",
        "## 2. Population und Daten",
        "",
        f"- Symbol `{meta.get('symbol')}` · A6 · 15m · n_fills=`{meta.get('n_fills')}`",
        f"- Analyze: `{meta.get('analyze_start')}` → `{meta.get('analyze_end_exclusive')}`",
        f"- Excursion panel reused: `{meta.get('excursion_panel_path')}`",
        "",
        "## 3. Trenddefinitionen",
        "",
        "```",
        json.dumps(TREND_RULES_DOC, indent=2),
        "```",
        "",
        "## 4. Kausalitätsprüfung",
        "",
        f"- Alle HTF-Kontexte: `context_is_causal` share = `{meta.get('share_context_causal')}`",
        f"- `htf_bar_closed_before_trigger` share = `{meta.get('share_htf_closed_before_trigger')}`",
        "",
        "## 5–6. Alignment- / Long-Short-Verteilung",
        "",
        f"- combined_htf: `{comb}`",
        f"- alignment: `{dist}`",
        f"- side: long=`{int((panel.side=='long').sum())}` short=`{int((panel.side=='short').sum())}`",
        "",
        "## 7–10. Excursion / Recovery / TP-SL / Exit-A",
        "",
        "- Siehe `alignment_excursion_summary.csv`, `recovery_by_alignment.csv`, `alignment_exit_a_summary.csv`",
        "",
    ]
    # key matrix lines
    if not matrix.empty:
        lines.append("## 11–12. Long/Short × Bull/Bear Matrix")
        lines.append("")
        for _, r in matrix.iterrows():
            lines.append(
                f"- `{r['group']}`: n={r['n']} medMAE={r['median_mae']:.3f} medMFE={r['median_mfe']:.3f} "
                f"uw={r['median_underwater_bars']:.1f} exitA_WR={r.get('exit_a_winrate')}"
            )
        lines.append("")

    lines += [
        "## 13–14. Konflikte",
        "",
        "- `timeframe_agreement_summary.csv`",
        "",
        "## 15. Schwere Gegenläufe",
        "",
        f"- MAE<=-5%: `{int((panel.primary_mae_pct<=-5).sum())}` · <=-10%: `{int((panel.primary_mae_pct<=-10).sum())}` · "
        f"<=-15%: `{int((panel.primary_mae_pct<=-15).sum())}` · <=-20%: `{int((panel.primary_mae_pct<=-20).sum())}`",
        "",
        "## 16–18. Top-3 / ohne Top-3 / Splits",
        "",
        "- `robustness_slices.csv`",
        "",
        "## 19. Hypothesen",
        "",
    ]
    for _, r in hyp.iterrows():
        lines.append(f"- **{r['hypothesis']}**: `{r['status']}`")

    # guard hypothetical
    ct_long = panel[(panel.side == "long") & (panel.combined_htf_trend.isin(["bear", "strong_bear"]))]
    guard_block = panel[panel.alignment_category.isin(["countertrend_strong"])]
    would_block = guard_block
    blocked_winners = would_block[(would_block.exit_a_closed == True) & (would_block.net_return_020_pct > 0)]  # noqa: E712
    lines += [
        "",
        "## 20. Bedeutung für den Hedge-Bot",
        "",
        "Nur Proxy: Gegenlauf-/Recovery-Statistik ist **kein** Hedge-Bot-PnL.",
        f"- Countertrend Longs in bear/strong_bear: n=`{len(ct_long)}`",
        f"- Starker Gegen-Trend-Guard (countertrend_strong) würde n=`{len(would_block)}` Fills blockieren; "
        f"darunter Exit-A-Winner: n=`{len(blocked_winners)}`",
        "",
        "## 21. Was noch nicht bewiesen ist",
        "",
        "- Kausaler Nutzen eines Guards out-of-sample",
        "- Interaktion mit Cycle-/Hedge-Orders",
        "- Intrabar-Unsicherheit und dünne Val/OOS-Zellen",
        "",
        "## 22. Empfehlung nächste Phase",
        "",
        "- Deskriptiven Guard-Kandidaten als **Holdout-Hypothese** formulieren (nicht aktivieren)",
        "- Speziell countertrend Longs case-weise vs aligned Shorts vertiefen",
        "- Optional: multi-symbol Check, ob Short-Edge ohne Bear-HTF verschwindet",
        "- Keine SM-/Pine-/Bot-Änderung",
        "",
    ]
    path = out_dir / "report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run_htf_trend_alignment_audit(
    *,
    output_dir: Path = DEFAULT_OUT,
    baseline_dir: Path = DEFAULT_BASELINE_DIR,
    excursion_dir: Path = EXCURSION_DIR,
    case_dir: Path = CASE_DIR,
    write_plots: bool = True,
) -> dict[str, Any]:
    baseline_info = assert_baseline_readonly(baseline_dir)
    if not baseline_info.get("hash_matches"):
        raise RuntimeError("C2 baseline hash mismatch")
    assert_safe_output_dir(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    cfg = baseline_a6()
    frame15, frame_meta = build_extended_tf_frame(SYMBOL, timeframe=TIMEFRAME, warmup_calendar_days=WARMUP_CALENDAR_DAYS)
    if frame15.empty:
        raise RuntimeError(f"empty 15m frame: {frame_meta}")
    frame15 = enrich_diagnostic_frame(frame15)

    # load same 5m for HTF
    a0 = pd.Timestamp(frame_meta["analyze_start"])
    a1 = pd.Timestamp(frame_meta["analyze_end_exclusive"])
    warm_bars = max(required_indicator_warmup_bars(), 400)
    full_5m, _ = load_ohlcv_with_warmup(SYMBOL, "5m", analyze_start=a0, analyze_end=a1, warmup_bars=warm_bars)
    decision = a1 + pd.Timedelta(hours=1)
    frame_1h = build_tf_research_slice(full_5m, "1h", decision=decision, analyze_start=a0, analyze_end_exclusive=a1)
    frame_4h = build_tf_research_slice(full_5m, "4h", decision=decision, analyze_start=a0, analyze_end_exclusive=a1)

    _tl, entries, _lives = apply_pullback_entry(frame15, cfg, return_lifecycles=True)
    filled = _filled_sorted(frame15, entries)
    trades = trades_exit_a_opposite_entry(frame15, filled, timeframe=TIMEFRAME, variant=cfg.name)
    closed = closed_only(trades)
    if len(filled) != 55:
        raise RuntimeError(f"expected 55 fills, got {len(filled)}")

    # load excursion panel
    exc_path = excursion_dir / "fill_excursion_panel.csv"
    if not exc_path.exists():
        raise RuntimeError(f"missing excursion panel: {exc_path} — run fill excursion audit first")
    exc = pd.read_csv(exc_path)
    if len(exc) != 55:
        raise RuntimeError(f"excursion panel n={len(exc)} != 55")

    splits = fixed_chrono_splits(a0, a1)
    timestamps = list(frame15["timestamp"])
    highs = frame15["high"].astype(float).to_numpy()
    lows = frame15["low"].astype(float).to_numpy()
    closes = frame15["close"].astype(float).to_numpy()
    n_bars = len(frame15)

    panel_rows = []
    hedge_rows = []
    persist_rows = []

    for i, fill in enumerate(filled):
        side_name = fill["side_name"]
        side = int(fill["side"])
        fill_i = int(fill["fill_bar"])
        trig_i = int(fill["trigger_bar"])
        trigger_ts = pd.Timestamp(fill["trigger_timestamp"])
        fill_ts = pd.Timestamp(fill["fill_timestamp"])
        entry = float(fill["entry_price"])
        trigger_decision = trigger_ts + pd.Timedelta(minutes=BAR_MINUTES)

        # structure at trigger bar (causal closed 15m)
        fr = frame15.iloc[trig_i]
        major = int(fr["major_direction"]) if pd.notna(fr.get("major_direction")) else 0
        major_label = major_to_label(major)
        micro = int(fr["micro_direction"]) if "micro_direction" in fr and pd.notna(fr.get("micro_direction")) else 0

        h1 = lookup_last_closed_htf(frame_1h, trigger_decision=trigger_decision, tf_minutes=60)
        h4 = lookup_last_closed_htf(frame_4h, trigger_decision=trigger_decision, tf_minutes=240)
        h1_trend = h1.get("trend", "mixed") if h1.get("found") else "mixed"
        h4_trend = h4.get("trend", "mixed") if h4.get("found") else "mixed"
        combined = classify_combined_htf(major_label, h1_trend, h4_trend)
        align_cat = classify_alignment_category(side_name, combined, h1_trend, h4_trend)
        flags = with_trend_flags(side_name, major_label, h1_trend, h4_trend, combined)

        # match excursion row by fill_id pattern or fill_time+side
        fill_id = f"F{i:03d}_{side_name}_{fill.get('setup_id')}"
        er = exc[exc["fill_id"] == fill_id]
        if er.empty:
            er = exc[
                (pd.to_datetime(exc["fill_time"], utc=True) == fill_ts)
                & (exc["side"] == side_name)
            ]
        if er.empty:
            raise RuntimeError(f"excursion row missing for {fill_id}")
        er0 = er.iloc[0]

        # verify excursion identity
        if abs(float(er0["fill_price"]) - entry) > 1e-9:
            raise RuntimeError(f"fill price mismatch {fill_id}")

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

        # trend persistence (explanation only)
        persist = {
            "fill_id": fill_id,
            "h1_trend_at_fill": h1_trend,
            "h4_trend_at_fill": h4_trend,
            "combined_at_fill": combined,
        }
        for hours, lab in ((4, "4h"), (12, "12h"), (24, "24h"), (48, "48h")):
            wall = fill_ts + pd.Timedelta(hours=hours)
            t1 = htf_trend_at_wall(frame_1h, wall_time=wall)
            t4 = htf_trend_at_wall(frame_4h, wall_time=wall)
            persist[f"h1_trend_after_{lab}"] = t1
            persist[f"h4_trend_after_{lab}"] = t4
            persist[f"h1_same_after_{lab}"] = t1 == h1_trend
            persist[f"h4_same_after_{lab}"] = t4 == h4_trend
        persist_rows.append(persist)

        net020 = er0.get("exit_a_net_0_20")
        if pd.isna(net020) and "net_return_0_20_pct" in er0:
            net020 = er0.get("net_return_0_20_pct")
        winner = None
        if pd.notna(er0.get("winner_net020")):
            winner = bool(er0["winner_net020"])
        elif pd.notna(net020) and bool(er0.get("exit_a_closed")):
            winner = float(net020) > 0

        # external bos side
        bos_side = None
        if bool(fr.get("external_bos_down")):
            bos_side = "down"
        elif bool(fr.get("external_bos_up")):
            bos_side = "up"

        dist_atr = _finite(fr.get("distance_to_protected_level_atr")) if "distance_to_protected_level_atr" in fr.index else float("nan")
        if math.isnan(dist_atr):
            # compute simple distance to protected level
            atr = _finite(fr.get("atr_14"), 0.0)
            prot = fr.get("protected_high") if side < 0 else fr.get("protected_low")
            if atr and pd.notna(prot):
                dist_atr = abs(float(fr["close"]) - float(prot)) / atr

        row = {
            "fill_id": fill_id,
            "side": side_name,
            "side_sign": side,
            "trigger_time": trigger_ts,
            "fill_time": fill_ts,
            "fill_price": entry,
            "trigger_decision_time": trigger_decision,
            "split": assign_split(fill_ts, splits),
            "month": fill_ts.tz_convert("UTC").strftime("%Y-%m"),
            "exit_a_closed": bool(er0.get("exit_a_closed")),
            "included_in_realized_exit_a": bool(er0.get("included_in_realized_exit_a")),
            "exit_a_winner": winner,
            "net_return_020_pct": _finite(net020) if pd.notna(net020) else float("nan"),
            "top1_trade": bool(er0["top1_trade"]) if "top1_trade" in er0 and pd.notna(er0.get("top1_trade")) else False,
            "top3_trade": bool(er0["top3_trade"]) if "top3_trade" in er0 and pd.notna(er0.get("top3_trade")) else False,
            # trend
            "major_direction": major,
            "major_direction_label": major_label,
            "micro_direction": micro,
            "major_micro_alignment": _finite(fr.get("major_micro_alignment")),
            "h1_trend": h1_trend,
            "h4_trend": h4_trend,
            "combined_htf_trend": combined,
            "alignment_category": align_cat,
            **flags,
            "1h_context_bar_time": h1.get("context_bar_time"),
            "4h_context_bar_time": h4.get("context_bar_time"),
            "major_structure_timestamp": trigger_ts,  # major from trigger bar open ts
            "context_is_causal": bool(h1.get("context_is_causal") and h4.get("context_is_causal")),
            "htf_bar_closed_before_trigger": bool(h1.get("htf_bar_closed_before_trigger") and h4.get("htf_bar_closed_before_trigger")),
            # ema
            "1h_ema9": h1.get("ema_9"),
            "1h_ema20": h1.get("ema_20"),
            "1h_ema50": h1.get("ema_50"),
            "1h_ema20_slope": h1.get("ema_20_slope"),
            "1h_ema50_slope": h1.get("ema_50_slope"),
            "1h_ema9_minus_ema20_pct": h1.get("ema9_minus_ema20_pct"),
            "1h_ema20_minus_ema50_pct": h1.get("ema20_minus_ema50_pct"),
            "4h_ema9": h4.get("ema_9"),
            "4h_ema20": h4.get("ema_20"),
            "4h_ema50": h4.get("ema_50"),
            "4h_ema20_slope": h4.get("ema_20_slope"),
            "4h_ema50_slope": h4.get("ema_50_slope"),
            "4h_ema9_minus_ema20_pct": h4.get("ema9_minus_ema20_pct"),
            "4h_ema20_minus_ema50_pct": h4.get("ema20_minus_ema50_pct"),
            "bars_since_external_bos": _finite(fr.get("bars_since_external_bos_any", fr.get("bars_since_external_bos"))),
            "distance_to_protected_level_atr": dist_atr,
            "external_bos_side": bos_side,
            "regime_state": fr.get("regime") if "regime" in fr.index else fr.get("protected_structure_state"),
            # excursion
            "primary_mfe_pct": float(er0["maximum_favorable_excursion_pct"]),
            "primary_mae_pct": float(er0["maximum_adverse_excursion_pct"]),
            "bars_to_mfe": er0.get("bars_to_mfe"),
            "bars_to_mae": er0.get("bars_to_mae"),
            "max_underwater_duration_bars": er0.get("max_underwater_duration_bars"),
            "time_underwater_fraction": er0.get("time_underwater_fraction"),
            "tp_0_5_reached": bool(er0.get("tp_0_5_reached")),
            "tp_1_reached": bool(er0.get("tp_1_0_reached")),
            "tp_2_reached": bool(er0.get("tp_2_0_reached")),
            "tp_3_reached": bool(er0.get("tp_3_0_reached")),
            "tp_5_reached": bool(er0.get("tp_5_0_reached")),
            "sl_0_5_reached": bool(er0.get("sl_0_5_reached")),
            "sl_1_reached": bool(er0.get("sl_1_0_reached")),
            "sl_2_reached": bool(er0.get("sl_2_0_reached")),
            "sl_3_reached": bool(er0.get("sl_3_0_reached")),
            "sl_5_reached": bool(er0.get("sl_5_0_reached")),
            "adverse_before_tp_1": er0.get("adverse_before_tp_1"),
            "adverse_before_tp_2": er0.get("adverse_before_tp_2"),
            "adverse_before_tp_3": er0.get("adverse_before_tp_3"),
            "adverse_before_tp_5": er0.get("adverse_before_tp_5"),
            "path_class": er0.get("path_class"),
            # persistence quick cols for plots
            "h1_same_after_24h": persist["h1_same_after_24h"],
            "h4_same_after_24h": persist["h4_same_after_24h"],
            "h1_same_after_48h": persist["h1_same_after_48h"],
            "h4_same_after_48h": persist["h4_same_after_48h"],
            **{k: risk[k] for k in risk},
        }
        # prefer excursion underwater if present
        if pd.notna(er0.get("max_underwater_duration_bars")):
            row["max_underwater_duration_bars"] = er0.get("max_underwater_duration_bars")
        panel_rows.append(row)
        hedge_rows.append({"fill_id": fill_id, "side": side_name, "alignment_category": align_cat, **risk})

    panel = pd.DataFrame(panel_rows)
    hedge = pd.DataFrame(hedge_rows)
    persist_df = pd.DataFrame(persist_rows)

    align_sum = summarize_alignment_excursion(panel)
    exit_sum = summarize_exit_a(panel)
    matrix = long_short_trend_matrix(panel)
    tf_agree = timeframe_agreement_summary(panel)
    rec_sum = recovery_by_alignment(panel)
    severe = severe_countertrend_cases(panel, case_dir)
    hyp = evaluate_hypotheses(panel)
    robust = robustness_slices(panel)

    # trend distribution over analyze window (1h bars in window)
    h1_win = frame_1h[frame_1h["in_analyze_window"] == True].copy()  # noqa: E712
    if len(h1_win):
        h1_win["ema_trend"] = h1_win.apply(classify_ema_trend, axis=1)
        trend_dist = h1_win["ema_trend"].value_counts().to_dict()
    else:
        trend_dist = {}

    panel.to_csv(output_dir / "fill_htf_alignment_panel.csv", index=False)
    align_sum.to_csv(output_dir / "alignment_excursion_summary.csv", index=False)
    exit_sum.to_csv(output_dir / "alignment_exit_a_summary.csv", index=False)
    matrix.to_csv(output_dir / "long_short_trend_matrix.csv", index=False)
    tf_agree.to_csv(output_dir / "timeframe_agreement_summary.csv", index=False)
    hedge.to_csv(output_dir / "hedgebot_directional_risk_proxy.csv", index=False)
    rec_sum.to_csv(output_dir / "recovery_by_alignment.csv", index=False)
    severe.to_csv(output_dir / "severe_countertrend_cases.csv", index=False)
    persist_df.to_csv(output_dir / "trend_persistence_after_fill.csv", index=False)
    hyp.to_csv(output_dir / "hypothesis_evaluation.csv", index=False)
    robust.to_csv(output_dir / "robustness_slices.csv", index=False)
    closed.to_csv(output_dir / "exit_a_trades_reference.csv", index=False)

    plots = maybe_plots(output_dir, panel, matrix, rec_sum) if write_plots else []

    meta = {
        "symbol": SYMBOL,
        "variant": VARIANT,
        "timeframe": TIMEFRAME,
        "config_hash": config_hash(cfg),
        "n_fills": len(filled),
        "n_closed_exit_a": int(len(closed)),
        "analyze_start": frame_meta.get("analyze_start"),
        "analyze_end_exclusive": frame_meta.get("analyze_end_exclusive"),
        "excursion_panel_path": str(exc_path),
        "trend_rules": TREND_RULES_DOC,
        "alignment_distribution": panel["alignment_category"].value_counts().to_dict(),
        "combined_distribution": panel["combined_htf_trend"].value_counts().to_dict(),
        "side_distribution": panel["side"].value_counts().to_dict(),
        "h1_trend_distribution_in_window": trend_dist,
        "share_context_causal": float(panel["context_is_causal"].mean()),
        "share_htf_closed_before_trigger": float(panel["htf_bar_closed_before_trigger"].mean()),
        "no_entry_filter_activation": True,
        "no_hedge_bot_implementation": True,
        "no_stop_tp_optimization": True,
        "production_sm_unchanged": True,
        "pine_unchanged": True,
        "baseline_reference_hash": C2_BASELINE_HASH,
        "plots": plots,
        "hypotheses": hyp.to_dict(orient="records"),
        "content_hash": hashlib.sha256(
            pd.util.hash_pandas_object(
                panel[["fill_id", "alignment_category", "combined_htf_trend", "primary_mae_pct"]].fillna(""),
                index=True,
            ).values
        ).hexdigest(),
    }
    (output_dir / "metadata.json").write_text(json.dumps(json_safe(meta), indent=2) + "\n", encoding="utf-8")
    write_report(out_dir=output_dir, meta=meta, panel=panel, hyp=hyp, matrix=matrix)
    return meta


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="C3.5c HTF trend alignment audit")
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--baseline-dir", type=Path, default=DEFAULT_BASELINE_DIR)
    p.add_argument("--excursion-dir", type=Path, default=EXCURSION_DIR)
    p.add_argument("--no-plots", action="store_true")
    args = p.parse_args(list(argv) if argv is not None else None)
    meta = run_htf_trend_alignment_audit(
        output_dir=args.output_dir,
        baseline_dir=args.baseline_dir,
        excursion_dir=args.excursion_dir,
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
