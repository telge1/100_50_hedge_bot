"""C3.5c APT 15m fill excursion audit (research-only, descriptive).

Analyzes ALL A6 fills (not only Exit-A closed trades): MFE/MAE, TP/SL reach,
first-touch matrix, path class, 55-vs-29 reconciliation, pre-TP adverse,
entry reclaim after adverse, and descriptive blocker classes by horizon.

No SM / Pine / filter / stop-TP optimization changes. No commits.
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
    "c35c_fill_excursion_audit"
)
PATTERN_DIR = Path(
    "research/regime_scanner/results/phase_c3_5_pullback_entry_state_machine/"
    "c35c_pattern_diagnostic_audit"
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

HORIZON_BARS: tuple[int, ...] = (1, 2, 3, 4, 6, 8, 12, 16, 24, 32, 48, 64, 96, 192, 672)
MAX_CALENDAR_DAYS = 7
MAX_BARS_7D = MAX_CALENDAR_DAYS * 24 * 60 // BAR_MINUTES  # 672

TP_LEVELS_PCT: tuple[float, ...] = (0.25, 0.50, 0.75, 1.00, 1.50, 2.00, 3.00, 4.00, 5.00, 7.50, 10.00)
SL_LEVELS_PCT: tuple[float, ...] = tuple(-x for x in TP_LEVELS_PCT)

# Blocker / reclaim diagnostics (descriptive only; not a strategy filter)
TP_BLOCKER_LEVEL_PCT = 0.25
FAST_WINNER_MAX_BARS = 12  # bar_offset < 12 ≡ touch within first 12 bars (offsets 0..11)
SEVERE_MAE_THRESHOLD_PCT = -3.0
CORE_TP_ADVERSE_LEVELS_PCT: tuple[float, ...] = (0.25, 0.50, 0.75, 1.00, 1.50, 2.00, 3.00)

# Formulas (direction-normalized):
# Long:  signed_ret(px) = (px/entry - 1)*100
#        fav from highs, adv from lows
# Short: signed_ret(px) = (entry/px - 1)*100
#        fav from lows,  adv from highs


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


def signed_return_pct(side: int, entry: float, px: float) -> float:
    if side > 0:
        return (px / entry - 1.0) * 100.0
    return (entry / px - 1.0) * 100.0


def fav_adv_from_bar(side: int, entry: float, high: float, low: float) -> tuple[float, float]:
    """Favorable (positive) and adverse (negative) excursion % on one bar."""
    if side > 0:
        fav = (high / entry - 1.0) * 100.0
        adv = (low / entry - 1.0) * 100.0
    else:
        fav = (entry / low - 1.0) * 100.0 if low > 0 else float("nan")
        adv = (entry / high - 1.0) * 100.0 if high > 0 else float("nan")
    return float(fav), float(adv)


# ---------------------------------------------------------------------------
# Population reconciliation
# ---------------------------------------------------------------------------


def reconcile_fill_population(
    filled: Sequence[Mapping[str, Any]],
    trades: pd.DataFrame,
    *,
    n_bars: int,
    data_end_ts: Any,
) -> pd.DataFrame:
    """Tag every fill: Exit-A entry, same-dir skip, terminal open, pairing."""
    fills = sorted(filled, key=lambda x: (int(x["fill_bar"]), int(x["side"])))
    # Map Exit-A trade entries by (fill_bar, side_name)
    exit_a_keys: dict[tuple[int, str], dict[str, Any]] = {}
    for _, t in trades.iterrows():
        # match fill timestamp / setup
        exit_a_keys[(pd.Timestamp(t["entry_timestamp"]), str(t["side"]))] = t.to_dict()

    # Walk Exit-A sequential pairing (same algorithm as trades_exit_a_opposite_entry)
    used_as_entry: set[int] = set()
    used_as_exit_only: set[int] = set()  # opposite that also becomes next entry
    skip_same_dir: set[int] = set()
    pairing: dict[int, dict[str, Any]] = {}

    i = 0
    while i < len(fills):
        e = fills[i]
        fi = int(e["fill_bar"])
        side = int(e["side"])
        used_as_entry.add(i)
        j = i + 1
        exit_idx = None
        while j < len(fills):
            if int(fills[j]["side"]) == -side:
                exit_idx = j
                break
            skip_same_dir.add(j)
            j += 1
        if exit_idx is None:
            pairing[i] = {
                "next_opposite_fill_idx": None,
                "exit_a_closed": False,
                "is_terminal_open_fill": True,
                "exclusion_reason": None,
            }
            break
        pairing[i] = {
            "next_opposite_fill_idx": exit_idx,
            "exit_a_closed": True,
            "is_terminal_open_fill": False,
            "exclusion_reason": None,
        }
        # opposite becomes next entry
        i = exit_idx

    for k in skip_same_dir:
        pairing[k] = {
            "next_opposite_fill_idx": None,
            "exit_a_closed": False,
            "is_terminal_open_fill": False,
            "exclusion_reason": "same_direction_while_exit_a_position_open",
        }

    rows = []
    for idx, e in enumerate(fills):
        info = pairing.get(
            idx,
            {
                "next_opposite_fill_idx": None,
                "exit_a_closed": False,
                "is_terminal_open_fill": False,
                "exclusion_reason": "unclassified_pairing_gap",
            },
        )
        opp_idx = info.get("next_opposite_fill_idx")
        opp = fills[opp_idx] if opp_idx is not None else None
        included = idx in used_as_entry
        fill_id = f"F{idx:03d}_{e['side_name']}_{e.get('setup_id')}"
        bars_after = max(0, n_bars - 1 - int(e["fill_bar"]))
        # match trade
        et = pd.Timestamp(e["fill_timestamp"])
        trade = exit_a_keys.get((et, e["side_name"]))
        excl = info.get("exclusion_reason")
        if included and info.get("is_terminal_open_fill"):
            excl = None
        elif included and info.get("exit_a_closed"):
            excl = None
        elif not included:
            excl = excl or "same_direction_while_exit_a_position_open"

        rows.append(
            {
                "fill_id": fill_id,
                "fill_index": idx,
                "setup_id": e.get("setup_id"),
                "side": e["side_name"],
                "side_sign": int(e["side"]),
                "trigger_bar": int(e["trigger_bar"]),
                "fill_bar": int(e["fill_bar"]),
                "trigger_time": e.get("trigger_timestamp"),
                "fill_time": e["fill_timestamp"],
                "fill_price": float(e["entry_price"]),
                "next_opposite_fill_id": (
                    f"F{opp_idx:03d}_{opp['side_name']}_{opp.get('setup_id')}" if opp is not None else None
                ),
                "next_opposite_fill_index": opp_idx,
                "next_opposite_fill_time": opp["fill_timestamp"] if opp is not None else None,
                "exit_a_price": float(opp["entry_price"]) if opp is not None else None,
                "exit_a_closed": bool(info.get("exit_a_closed")),
                "included_in_realized_exit_a": bool(included),
                "exclusion_reason": excl,
                "is_terminal_open_fill": bool(info.get("is_terminal_open_fill")),
                "duplicate_or_pairing_issue": False,
                "data_end_timestamp": data_end_ts,
                "bars_available_after_fill": bars_after,
                "exit_a_trade_closed_flag": bool(trade["closed"]) if trade else None,
                "exit_a_net_0_20": trade.get("net_return_0_20_pct") if trade else None,
            }
        )

    # sanity: every index classified
    for idx in range(len(fills)):
        if idx not in pairing and idx not in used_as_entry:
            rows[idx]["duplicate_or_pairing_issue"] = True
            rows[idx]["exclusion_reason"] = rows[idx].get("exclusion_reason") or "pairing_gap"

    return pd.DataFrame(rows)


def reconciliation_summary(recon: pd.DataFrame, *, n_arms: int, n_triggers: int, n_lives: int) -> pd.DataFrame:
    n_fills = len(recon)
    n_long = int((recon["side"] == "long").sum())
    n_short = int((recon["side"] == "short").sum())
    n_included = int(recon["included_in_realized_exit_a"].sum())
    n_closed = int(((recon["included_in_realized_exit_a"]) & (recon["exit_a_closed"])).sum())
    n_open = int(recon["is_terminal_open_fill"].sum())
    n_skip = int((recon["exclusion_reason"] == "same_direction_while_exit_a_position_open").sum())
    n_gap = int(recon["duplicate_or_pairing_issue"].sum())
    n_unassigned = int(
        (
            (~recon["included_in_realized_exit_a"])
            & (recon["exclusion_reason"].isna() | (recon["exclusion_reason"] == "unclassified_pairing_gap"))
        ).sum()
    )
    rows = [
        {"metric": "arms_lifecycles", "value": n_arms},
        {"metric": "triggers_entry_created", "value": n_triggers},
        {"metric": "fills", "value": n_fills},
        {"metric": "long_fills", "value": n_long},
        {"metric": "short_fills", "value": n_short},
        {"metric": "exit_a_entries_included", "value": n_included},
        {"metric": "closed_exit_a_trades", "value": n_closed},
        {"metric": "terminal_open_fills", "value": n_open},
        {"metric": "same_direction_skipped_fills", "value": n_skip},
        {"metric": "unassigned_fills", "value": n_unassigned},
        {"metric": "pairing_gap_flags", "value": n_gap},
        {"metric": "fills_minus_exit_a_entries", "value": n_fills - n_included},
        {"metric": "identity_check_55_eq_30_plus_25", "value": int(n_fills == n_included + n_skip)},
        {"metric": "n_lives_total", "value": n_lives},
    ]
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Path / excursion mathematics
# ---------------------------------------------------------------------------


def path_arrays(
    side: int,
    entry: float,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    start: int,
    end_inclusive: int,
) -> dict[str, Any]:
    """Compute path stats from fill bar start..end_inclusive (inclusive)."""
    if end_inclusive < start:
        return {"empty": True}
    h = highs[start : end_inclusive + 1]
    l = lows[start : end_inclusive + 1]
    c = closes[start : end_inclusive + 1]
    n = len(c)
    fav = np.empty(n)
    adv = np.empty(n)
    close_s = np.empty(n)
    for i in range(n):
        fav[i], adv[i] = fav_adv_from_bar(side, entry, float(h[i]), float(l[i]))
        close_s[i] = signed_return_pct(side, entry, float(c[i]))
    mfe = float(np.max(fav))
    mae = float(np.min(adv))
    bars_to_mfe = int(np.where(fav >= mfe - 1e-15)[0][0])
    bars_to_mae = int(np.where(adv <= mae + 1e-15)[0][0])
    mfe_before = bars_to_mfe < bars_to_mae
    mae_before = bars_to_mae < bars_to_mfe
    same_bar = bars_to_mfe == bars_to_mae
    # running extrema kept for potential diagnostics
    run_mfe = np.maximum.accumulate(fav)
    run_mae = np.minimum.accumulate(adv)
    # underwater: close_s < 0
    underwater = close_s < 0
    # max consecutive underwater
    max_uw = 0
    cur = 0
    for v in underwater:
        if v:
            cur += 1
            max_uw = max(max_uw, cur)
        else:
            cur = 0
    first_pos = next((i for i, v in enumerate(close_s) if v > 0), None)
    first_neg = next((i for i, v in enumerate(close_s) if v < 0), None)
    # first excursion direction on bar 0
    f0, a0 = float(fav[0]), float(adv[0])
    intrabar_unknown = False
    if f0 > 1e-12 and a0 < -1e-12:
        first_dir = "intrabar_unknown"
        intrabar_unknown = True
    elif f0 > 1e-12:
        first_dir = "favorable"
    elif a0 < -1e-12:
        first_dir = "adverse"
    else:
        first_dir = "flat"
    return {
        "empty": False,
        "n_bars": n,
        "close_return_pct": float(close_s[-1]),
        "maximum_favorable_excursion_pct": mfe,
        "maximum_adverse_excursion_pct": mae,
        "high_favorable_pct": float(np.max(fav)),
        "low_adverse_pct": float(np.min(adv)),
        "close_favorable_pct": float(close_s[-1]),
        "excursion_range_pct": mfe - mae,
        "mfe_minus_abs_mae": mfe - abs(mae),
        "mfe_to_mae_ratio": (mfe / abs(mae)) if abs(mae) > 1e-15 else None,
        "bars_to_mfe": bars_to_mfe,
        "bars_to_mae": bars_to_mae,
        "mfe_before_mae": mfe_before,
        "mae_before_mfe": mae_before,
        "same_bar_mfe_mae": same_bar,
        "first_positive_close_bar": first_pos,
        "first_negative_close_bar": first_neg,
        "max_underwater_duration_bars": int(max_uw),
        "time_in_profit_fraction": float(np.mean(close_s > 0)) if n else None,
        "time_underwater_fraction": float(np.mean(close_s < 0)) if n else None,
        "first_excursion_direction": first_dir,
        "intrabar_order_unknown": intrabar_unknown,
        "fav": fav,
        "adv": adv,
        "close_s": close_s,
        "run_mfe": run_mfe,
        "run_mae": run_mae,
    }


def first_touch_level(
    side: int,
    entry: float,
    highs: np.ndarray,
    lows: np.ndarray,
    start: int,
    end_inclusive: int,
    level_pct: float,
) -> dict[str, Any]:
    """First bar where OHLC touches signed level_pct (positive=TP, negative=SL)."""
    for i in range(start, end_inclusive + 1):
        fav, adv = fav_adv_from_bar(side, entry, float(highs[i]), float(lows[i]))
        rel = i - start
        if level_pct >= 0:
            if fav >= level_pct - 1e-15:
                return {"reached": True, "bar_offset": rel, "bar_index": i, "touch_side": "favorable"}
        else:
            if adv <= level_pct + 1e-15:
                return {"reached": True, "bar_offset": rel, "bar_index": i, "touch_side": "adverse"}
    return {"reached": False, "bar_offset": None, "bar_index": None, "touch_side": None}


def adverse_before_tp(
    path: Mapping[str, Any],
    tp_pct: float,
) -> float | None:
    """Max adverse (most negative) through first TP bar (inclusive). Legacy wrapper."""
    detail = adverse_before_tp_detail(path, tp_pct)
    return detail.get("adverse_incl_tp_bar_pct")


def adverse_before_tp_detail(
    path: Mapping[str, Any],
    tp_pct: float,
) -> dict[str, Any]:
    """Adverse excursion before first TP touch.

    - adverse_incl_tp_bar_pct: min(adv) through TP bar inclusive (existing semantics)
    - adverse_excl_tp_bar_pct: min(adv) strictly before TP bar (None if TP on bar 0)
    - never_hit: True if TP not reached on this path window
    When never_hit, both adverse values are the full-window MAE (excl is identical).
    """
    empty = {
        "never_hit": True,
        "tp_bar_offset": None,
        "adverse_incl_tp_bar_pct": None,
        "adverse_excl_tp_bar_pct": None,
    }
    if path.get("empty"):
        return empty
    fav = path["fav"]
    adv = path["adv"]
    if len(adv) == 0:
        return empty
    hit = np.where(fav >= tp_pct - 1e-15)[0]
    if len(hit) == 0:
        mae = float(np.min(adv))
        return {
            "never_hit": True,
            "tp_bar_offset": None,
            "adverse_incl_tp_bar_pct": mae,
            "adverse_excl_tp_bar_pct": mae,
        }
    k = int(hit[0])
    incl = float(np.min(adv[: k + 1]))
    excl = float(np.min(adv[:k])) if k > 0 else None
    return {
        "never_hit": False,
        "tp_bar_offset": k,
        "adverse_incl_tp_bar_pct": incl,
        "adverse_excl_tp_bar_pct": excl,
    }


def entry_reclaim_after_adverse(
    path: Mapping[str, Any],
    *,
    timestamps: Sequence[Any] | None = None,
    fill_bar: int = 0,
    eps: float = 1e-12,
) -> dict[str, Any]:
    """Entry reclaim after a genuine adverse excursion (High/Low + Close touch).

    Long: high>=entry (fav>=0) or close>=entry.
    Short: low<=entry (fav>=0) or close<=entry.
    Fills that never went adverse are never classified as reclaimed_after_adverse.
    """
    out: dict[str, Any] = {
        "had_adverse_excursion": False,
        "reclaimed_after_adverse": False,
        "reclaim_bar_offset": None,
        "reclaim_bar_index": None,
        "reclaim_timestamp": None,
        "bars_to_reclaim": None,
        "worst_adverse_before_reclaim_pct": None,
        "never_reclaim_within_window": False,
        "never_adverse": True,
    }
    if path.get("empty"):
        out["never_reclaim_within_window"] = True
        return out
    fav = path["fav"]
    adv = path["adv"]
    close_s = path["close_s"]
    first_adv = next((i for i, a in enumerate(adv) if float(a) < -eps), None)
    if first_adv is None:
        return out
    out["had_adverse_excursion"] = True
    out["never_adverse"] = False
    reclaim_i = None
    for i in range(first_adv, len(fav)):
        if float(fav[i]) >= -1e-15 or float(close_s[i]) >= -1e-15:
            reclaim_i = i
            break
    if reclaim_i is None:
        out["never_reclaim_within_window"] = True
        out["worst_adverse_before_reclaim_pct"] = float(np.min(adv))
        return out
    out["reclaimed_after_adverse"] = True
    out["reclaim_bar_offset"] = int(reclaim_i)
    out["bars_to_reclaim"] = int(reclaim_i)
    out["reclaim_bar_index"] = int(fill_bar + reclaim_i)
    if timestamps is not None and 0 <= fill_bar + reclaim_i < len(timestamps):
        out["reclaim_timestamp"] = timestamps[fill_bar + reclaim_i]
    out["worst_adverse_before_reclaim_pct"] = float(np.min(adv[: reclaim_i + 1]))
    return out


def classify_fill_blocker(
    path: Mapping[str, Any],
    *,
    tp_0_25: Mapping[str, Any],
    reclaim: Mapping[str, Any],
    truncated: bool,
    severe_mae_threshold_pct: float = SEVERE_MAE_THRESHOLD_PCT,
    fast_winner_max_bars: int = FAST_WINNER_MAX_BARS,
) -> dict[str, Any]:
    """Deterministic blocker labels: one exclusive primary class + boolean flags.

    Priority for blocker_class (first match wins):
      1. fast_winner — TP 0.25% with bar_offset < fast_winner_max_bars
      2. delayed_winner — TP 0.25% later but within window
      3. reclaimed_entry_only — entry reclaimed after adverse, TP 0.25% not hit
      4. never_profitable_within_horizon — MFE <= 0
      5. open_blocker_at_horizon — close < 0 at window end and entry not reclaimed
      6. severe_adverse_excursion — MAE <= threshold (fallback if nothing else)
      7. other_path — residual

    flag_severe_adverse_excursion is always set independently when MAE <= threshold.
    No 'total loss' / liquidation class.
    """
    empty = path.get("empty", True)
    mfe = float("nan") if empty else float(path["maximum_favorable_excursion_pct"])
    mae = float("nan") if empty else float(path["maximum_adverse_excursion_pct"])
    close_r = float("nan") if empty else float(path["close_return_pct"])
    tp_hit = bool(tp_0_25.get("reached"))
    tp_off = tp_0_25.get("bar_offset")
    reclaimed = bool(reclaim.get("reclaimed_after_adverse"))

    flag_fast = bool(tp_hit and tp_off is not None and int(tp_off) < int(fast_winner_max_bars))
    flag_delayed = bool(tp_hit and tp_off is not None and int(tp_off) >= int(fast_winner_max_bars))
    flag_reclaim_only = bool(reclaimed and not tp_hit)
    flag_never_prof = bool(not empty and mfe <= 0.0)
    flag_open_blocker = bool(
        not empty and close_r < 0.0 and not reclaimed and not tp_hit
    )
    flag_severe = bool(not empty and mae <= severe_mae_threshold_pct + 1e-15)

    if empty:
        primary = "unresolved_at_data_end" if truncated else "other_path"
    elif flag_fast:
        primary = "fast_winner"
    elif flag_delayed:
        primary = "delayed_winner"
    elif flag_reclaim_only:
        primary = "reclaimed_entry_only"
    elif flag_never_prof:
        primary = "never_profitable_within_horizon"
    elif flag_open_blocker:
        primary = "open_blocker_at_horizon"
    elif flag_severe:
        primary = "severe_adverse_excursion"
    else:
        primary = "other_path"

    return {
        "blocker_class": primary,
        "flag_fast_winner": flag_fast,
        "flag_delayed_winner": flag_delayed,
        "flag_reclaimed_entry_only": flag_reclaim_only,
        "flag_open_blocker_at_horizon": flag_open_blocker,
        "flag_never_profitable_within_horizon": flag_never_prof,
        "flag_severe_adverse_excursion": flag_severe,
        "truncated_horizon": bool(truncated),
    }


def favorable_before_sl(path: Mapping[str, Any], sl_pct: float) -> float | None:
    if path.get("empty"):
        return None
    fav = path["fav"]
    adv = path["adv"]
    hit = np.where(adv <= sl_pct + 1e-15)[0]
    if len(hit) == 0:
        return float(np.max(fav)) if len(fav) else None
    k = int(hit[0])
    return float(np.max(fav[: k + 1]))


def classify_path(path: Mapping[str, Any], *, truncated: bool) -> str:
    """Fixed descriptive rules — not a strategy."""
    if path.get("empty"):
        return "unresolved_at_data_end" if truncated else "range_chop"
    mfe = float(path["maximum_favorable_excursion_pct"])
    mae = float(path["maximum_adverse_excursion_pct"])
    abs_mae = abs(mae)
    first = path["first_excursion_direction"]
    close_r = float(path["close_return_pct"])
    if truncated and path["n_bars"] < 8 and mfe < 0.5 and abs_mae < 0.5:
        return "unresolved_at_data_end"
    if first == "favorable" and abs_mae < 0.25 and mfe >= 0.25:
        return "clean_immediate_favorable"
    if mae < -2.0 and mfe >= 1.0 and path["mae_before_mfe"]:
        return "deep_adverse_then_recovery"
    if path["mae_before_mfe"] and abs_mae < 1.0 and mfe >= 0.5:
        return "shallow_adverse_then_favorable"
    if mfe >= 1.0 and close_r < 0 and abs_mae >= 0.5:
        return "favorable_then_full_reversal"
    if mfe < 0.5 and abs_mae >= 1.0 and close_r <= 0:
        return "persistent_adverse"
    if mfe < 1.0 and abs_mae < 1.0:
        return "range_chop"
    if truncated:
        return "unresolved_at_data_end"
    return "range_chop"


# ---------------------------------------------------------------------------
# Per-fill analysis
# ---------------------------------------------------------------------------


def _opp_fill_bar(recon_row: Mapping[str, Any], fills: Sequence[Mapping[str, Any]]) -> int | None:
    idx = recon_row.get("next_opposite_fill_index")
    if idx is None or (isinstance(idx, float) and math.isnan(idx)):
        return None
    return int(fills[int(idx)]["fill_bar"])


def analyze_fill_core(
    *,
    fill: Mapping[str, Any],
    recon_row: Mapping[str, Any],
    fills: Sequence[Mapping[str, Any]],
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    timestamps: Sequence[Any],
    n_bars: int,
) -> tuple[
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    side = int(fill["side"])
    entry = float(fill["entry_price"])
    fill_i = int(fill["fill_bar"])
    opp_bar = _opp_fill_bar(recon_row, fills)
    end_data = n_bars - 1
    end_7d = min(end_data, fill_i + MAX_BARS_7D)
    end_opp = opp_bar if opp_bar is not None else end_data
    # primary diagnostic window: min(opposite, 7d, data end)
    end_primary = min(end_opp, end_7d, end_data)
    intended_primary = min(end_opp, fill_i + MAX_BARS_7D)
    truncated_primary = end_primary < intended_primary

    path_primary = path_arrays(side, entry, highs, lows, closes, fill_i, end_primary)
    path_to_opp = path_arrays(side, entry, highs, lows, closes, fill_i, end_opp if opp_bar is not None else end_data)

    reclaim_primary = entry_reclaim_after_adverse(
        path_primary, timestamps=timestamps, fill_bar=fill_i
    )
    tp025_primary = first_touch_level(
        side, entry, highs, lows, fill_i, end_primary, TP_BLOCKER_LEVEL_PCT
    )
    blocker_primary = classify_fill_blocker(
        path_primary, tp_0_25=tp025_primary, reclaim=reclaim_primary, truncated=truncated_primary
    )

    # horizon rows + long-form TP adverse by horizon
    horizon_rows = []
    tp_horizon_rows: list[dict[str, Any]] = []
    for hb in HORIZON_BARS:
        # hb bars including fill bar: 1 = fill only, 2 = fill+next, …
        end_h = min(end_data, fill_i + hb - 1)
        trunc = end_h < fill_i + hb - 1
        avail = end_h - fill_i + 1
        p = path_arrays(side, entry, highs, lows, closes, fill_i, end_h)
        if p.get("empty"):
            continue
        reclaim_h = entry_reclaim_after_adverse(p, timestamps=timestamps, fill_bar=fill_i)
        tp025_h = first_touch_level(side, entry, highs, lows, fill_i, end_h, TP_BLOCKER_LEVEL_PCT)
        blocker_h = classify_fill_blocker(
            p, tp_0_25=tp025_h, reclaim=reclaim_h, truncated=trunc
        )
        horizon_rows.append(
            {
                "horizon_bars": hb,
                "horizon_minutes": hb * BAR_MINUTES,
                "bars_available": avail,
                "truncated": trunc,
                "close_return_pct": p["close_return_pct"],
                "maximum_favorable_excursion_pct": p["maximum_favorable_excursion_pct"],
                "maximum_adverse_excursion_pct": p["maximum_adverse_excursion_pct"],
                "bars_to_mfe": p["bars_to_mfe"],
                "bars_to_mae": p["bars_to_mae"],
                "minutes_to_mfe": p["bars_to_mfe"] * BAR_MINUTES,
                "minutes_to_mae": p["bars_to_mae"] * BAR_MINUTES,
                "time_in_profit_fraction": p["time_in_profit_fraction"],
                "time_underwater_fraction": p["time_underwater_fraction"],
                "mfe_before_mae": p["mfe_before_mae"],
                "first_excursion_direction": p["first_excursion_direction"],
                "intrabar_order_unknown": p["intrabar_order_unknown"],
                "tp_0_25_reached": bool(tp025_h["reached"]),
                "tp_0_25_bar_offset": tp025_h["bar_offset"],
                "tp_0_25_timestamp": None
                if tp025_h["bar_index"] is None
                else timestamps[int(tp025_h["bar_index"])],
                "had_adverse_excursion": reclaim_h["had_adverse_excursion"],
                "reclaimed_after_adverse": reclaim_h["reclaimed_after_adverse"],
                "reclaim_bar_offset": reclaim_h["reclaim_bar_offset"],
                "reclaim_timestamp": reclaim_h["reclaim_timestamp"],
                "bars_to_reclaim": reclaim_h["bars_to_reclaim"],
                "worst_adverse_before_reclaim_pct": reclaim_h["worst_adverse_before_reclaim_pct"],
                "never_reclaim_within_horizon": reclaim_h["never_reclaim_within_window"],
                "never_adverse": reclaim_h["never_adverse"],
                "never_reclaim_to_data_end": bool(
                    reclaim_h["never_reclaim_within_window"] and end_h >= end_data
                ),
                **blocker_h,
            }
        )
        for lvl in TP_LEVELS_PCT:
            touch = first_touch_level(side, entry, highs, lows, fill_i, end_h, lvl)
            detail = adverse_before_tp_detail(p, lvl)
            tp_horizon_rows.append(
                {
                    "horizon_bars": hb,
                    "horizon_minutes": hb * BAR_MINUTES,
                    "bars_available": avail,
                    "truncated": trunc,
                    "level_type": "TP",
                    "level_pct": lvl,
                    "level_reached": bool(touch["reached"]),
                    "never_hit": bool(detail["never_hit"]),
                    "first_touch_bar_offset": touch["bar_offset"],
                    "first_touch_bar_index": touch["bar_index"],
                    "bars_to_touch": touch["bar_offset"],
                    "minutes_to_touch": None
                    if touch["bar_offset"] is None
                    else touch["bar_offset"] * BAR_MINUTES,
                    "first_touch_time": None
                    if touch["bar_index"] is None
                    else timestamps[int(touch["bar_index"])],
                    "adverse_excursion_before_tp": detail["adverse_incl_tp_bar_pct"],
                    "adverse_excursion_before_tp_excl": detail["adverse_excl_tp_bar_pct"],
                    "adverse_incl_tp_bar_pct": detail["adverse_incl_tp_bar_pct"],
                    "adverse_excl_tp_bar_pct": detail["adverse_excl_tp_bar_pct"],
                }
            )

    # level touches on primary path
    level_rows = []
    path_seq = []
    for lvl in TP_LEVELS_PCT:
        touch = first_touch_level(side, entry, highs, lows, fill_i, end_primary, lvl)
        detail = adverse_before_tp_detail(path_primary, lvl) if path_primary.get("fav") is not None else {
            "never_hit": True,
            "adverse_incl_tp_bar_pct": None,
            "adverse_excl_tp_bar_pct": None,
        }
        adv_b = detail.get("adverse_incl_tp_bar_pct")
        close_at = None
        if touch["reached"] and touch["bar_index"] is not None:
            close_at = signed_return_pct(side, entry, float(closes[int(touch["bar_index"])]))
        level_rows.append(
            {
                "level_type": "TP",
                "level_pct": lvl,
                "level_reached": bool(touch["reached"]),
                "never_hit": bool(detail.get("never_hit", not touch["reached"])),
                "first_touch_bar_offset": touch["bar_offset"],
                "first_touch_bar_index": touch["bar_index"],
                "bars_to_touch": touch["bar_offset"],
                "minutes_to_touch": None if touch["bar_offset"] is None else touch["bar_offset"] * BAR_MINUTES,
                "first_touch_time": None
                if touch["bar_index"] is None
                else timestamps[int(touch["bar_index"])],
                "adverse_excursion_before_tp": adv_b,
                "adverse_excursion_before_tp_excl": detail.get("adverse_excl_tp_bar_pct"),
                "favorable_excursion_before_sl": None,
                "close_return_at_touch": close_at,
                "reached_before_opposite_fill": bool(
                    touch["reached"] and (opp_bar is None or (touch["bar_index"] is not None and touch["bar_index"] <= opp_bar))
                ),
                "reached_before_data_end": bool(touch["reached"]),
            }
        )
        if touch["reached"]:
            path_seq.append(
                {
                    "event": "TP",
                    "level_pct": lvl,
                    "bar_offset": touch["bar_offset"],
                    "time": timestamps[int(touch["bar_index"])],
                }
            )
    for lvl in SL_LEVELS_PCT:
        touch = first_touch_level(side, entry, highs, lows, fill_i, end_primary, lvl)
        fav_b = favorable_before_sl(path_primary, lvl) if path_primary.get("fav") is not None else None
        close_at = None
        if touch["reached"] and touch["bar_index"] is not None:
            close_at = signed_return_pct(side, entry, float(closes[int(touch["bar_index"])]))
        level_rows.append(
            {
                "level_type": "SL",
                "level_pct": lvl,
                "level_reached": bool(touch["reached"]),
                "never_hit": bool(not touch["reached"]),
                "first_touch_bar_offset": touch["bar_offset"],
                "first_touch_bar_index": touch["bar_index"],
                "bars_to_touch": touch["bar_offset"],
                "minutes_to_touch": None if touch["bar_offset"] is None else touch["bar_offset"] * BAR_MINUTES,
                "first_touch_time": None
                if touch["bar_index"] is None
                else timestamps[int(touch["bar_index"])],
                "adverse_excursion_before_tp": None,
                "adverse_excursion_before_tp_excl": None,
                "favorable_excursion_before_sl": fav_b,
                "close_return_at_touch": close_at,
                "reached_before_opposite_fill": bool(
                    touch["reached"] and (opp_bar is None or (touch["bar_index"] is not None and touch["bar_index"] <= opp_bar))
                ),
                "reached_before_data_end": bool(touch["reached"]),
            }
        )
        if touch["reached"]:
            path_seq.append(
                {
                    "event": "SL",
                    "level_pct": lvl,
                    "bar_offset": touch["bar_offset"],
                    "time": timestamps[int(touch["bar_index"])],
                }
            )
    path_seq.sort(key=lambda x: (x["bar_offset"] is None, x["bar_offset"], x["level_pct"]))

    # first-touch matrix
    ft_rows = []
    for tp in TP_LEVELS_PCT:
        for sl in SL_LEVELS_PCT:
            tp_t = first_touch_level(side, entry, highs, lows, fill_i, end_primary, tp)
            sl_t = first_touch_level(side, entry, highs, lows, fill_i, end_primary, sl)
            tp_b = tp_t["bar_offset"]
            sl_b = sl_t["bar_offset"]
            tp_first = sl_first = both_same = neither = False
            ambiguous = False
            if tp_t["reached"] and sl_t["reached"]:
                if tp_b < sl_b:
                    tp_first = True
                elif sl_b < tp_b:
                    sl_first = True
                else:
                    both_same = True
                    ambiguous = True
            elif tp_t["reached"]:
                tp_first = True
            elif sl_t["reached"]:
                sl_first = True
            else:
                neither = True
            # conservative: same bar → SL first; optimistic → TP first
            if both_same:
                cons_result = "SL"
                opt_result = "TP"
                bars_res = tp_b
            elif tp_first:
                cons_result = opt_result = "TP"
                bars_res = tp_b
            elif sl_first:
                cons_result = opt_result = "SL"
                bars_res = sl_b
            else:
                cons_result = opt_result = "neither"
                bars_res = None
            # expectancy placeholder in summary; per fill store signed outcome at touch
            if cons_result == "TP":
                cons_ret = tp
            elif cons_result == "SL":
                cons_ret = sl
            else:
                cons_ret = None
            if opt_result == "TP":
                opt_ret = tp
            elif opt_result == "SL":
                opt_ret = sl
            else:
                opt_ret = None
            ft_rows.append(
                {
                    "tp_level_pct": tp,
                    "sl_level_pct": sl,
                    "tp_first": tp_first,
                    "sl_first": sl_first,
                    "both_same_bar": both_same,
                    "neither": neither,
                    "intrabar_ambiguous": ambiguous,
                    "same_bar_ambiguous": ambiguous,
                    "bars_to_resolution": bars_res,
                    "minutes_to_resolution": None if bars_res is None else bars_res * BAR_MINUTES,
                    "first_touch_time": None
                    if bars_res is None
                    else timestamps[min(n_bars - 1, fill_i + int(bars_res))],
                    "result_if_conservative": cons_result,
                    "result_if_optimistic": opt_result,
                    "conservative_return_pct": cons_ret,
                    "optimistic_return_pct": opt_ret,
                    "conservative_net_0_20": None if cons_ret is None else cons_ret - COST_ROUNDTRIP_PCT,
                    "optimistic_net_0_20": None if opt_ret is None else opt_ret - COST_ROUNDTRIP_PCT,
                }
            )

    # first fixed thresholds sequence
    first_threshold = None
    for thr in (0.25, 0.50, 1.00, 2.00, 3.00):
        tp_t = first_touch_level(side, entry, highs, lows, fill_i, end_primary, thr)
        sl_t = first_touch_level(side, entry, highs, lows, fill_i, end_primary, -thr)
        candidates = []
        if tp_t["reached"]:
            candidates.append(("favorable", thr, tp_t["bar_offset"]))
        if sl_t["reached"]:
            candidates.append(("adverse", -thr, sl_t["bar_offset"]))
        if candidates:
            candidates.sort(key=lambda x: (x[2], 0 if x[0] == "adverse" else 1))
            first_threshold = {"direction": candidates[0][0], "level_pct": candidates[0][1], "bar_offset": candidates[0][2]}
            break

    path_class = classify_path(path_primary, truncated=truncated_primary)

    tp_flags = {}
    for lvl in TP_LEVELS_PCT:
        touch = first_touch_level(side, entry, highs, lows, fill_i, end_primary, lvl)
        key = f"tp_{str(lvl).replace('.', '_')}_reached"
        tp_flags[key] = bool(touch["reached"])
    sl_flags = {}
    for lvl in SL_LEVELS_PCT:
        touch = first_touch_level(side, entry, highs, lows, fill_i, end_primary, lvl)
        key = f"sl_{str(abs(lvl)).replace('.', '_')}_reached"
        sl_flags[key] = bool(touch["reached"])

    close_path = path_primary.get("close_s")
    close_path_list = close_path.tolist() if close_path is not None else None

    # core TP adverse wide columns (incl + excl + never_hit) for requested levels
    tp_adverse_wide: dict[str, Any] = {}
    for lvl in CORE_TP_ADVERSE_LEVELS_PCT:
        detail = adverse_before_tp_detail(path_primary, lvl)
        tag = str(lvl).replace(".", "_")
        tp_adverse_wide[f"adverse_before_tp_{tag}"] = detail["adverse_incl_tp_bar_pct"]
        tp_adverse_wide[f"adverse_before_tp_{tag}_excl"] = detail["adverse_excl_tp_bar_pct"]
        tp_adverse_wide[f"tp_{tag}_never_hit"] = detail["never_hit"]
        touch = first_touch_level(side, entry, highs, lows, fill_i, end_primary, lvl)
        tp_adverse_wide[f"tp_{tag}_bars_to_touch"] = touch["bar_offset"]
        tp_adverse_wide[f"tp_{tag}_first_touch_time"] = (
            None if touch["bar_index"] is None else timestamps[int(touch["bar_index"])]
        )

    panel = {
        "maximum_favorable_excursion_pct": path_primary.get("maximum_favorable_excursion_pct"),
        "maximum_adverse_excursion_pct": path_primary.get("maximum_adverse_excursion_pct"),
        "close_return_pct_primary": path_primary.get("close_return_pct"),
        "bars_to_mfe": path_primary.get("bars_to_mfe"),
        "bars_to_mae": path_primary.get("bars_to_mae"),
        "minutes_to_mfe": None
        if path_primary.get("bars_to_mfe") is None
        else path_primary["bars_to_mfe"] * BAR_MINUTES,
        "minutes_to_mae": None
        if path_primary.get("bars_to_mae") is None
        else path_primary["bars_to_mae"] * BAR_MINUTES,
        "timestamp_of_mfe": None
        if path_primary.get("bars_to_mfe") is None
        else timestamps[fill_i + int(path_primary["bars_to_mfe"])],
        "timestamp_of_mae": None
        if path_primary.get("bars_to_mae") is None
        else timestamps[fill_i + int(path_primary["bars_to_mae"])],
        "mfe_before_mae": path_primary.get("mfe_before_mae"),
        "mae_before_mfe": path_primary.get("mae_before_mfe"),
        "same_bar_mfe_mae": path_primary.get("same_bar_mfe_mae"),
        "first_excursion_direction": path_primary.get("first_excursion_direction"),
        "intrabar_order_unknown": path_primary.get("intrabar_order_unknown"),
        "first_positive_close_bar": path_primary.get("first_positive_close_bar"),
        "first_negative_close_bar": path_primary.get("first_negative_close_bar"),
        "max_underwater_duration_bars": path_primary.get("max_underwater_duration_bars"),
        "max_underwater_duration_minutes": None
        if path_primary.get("max_underwater_duration_bars") is None
        else path_primary["max_underwater_duration_bars"] * BAR_MINUTES,
        "time_in_profit_fraction": path_primary.get("time_in_profit_fraction"),
        "time_underwater_fraction": path_primary.get("time_underwater_fraction"),
        "mfe_minus_abs_mae": path_primary.get("mfe_minus_abs_mae"),
        "mfe_to_mae_ratio": path_primary.get("mfe_to_mae_ratio"),
        "path_class": path_class,
        "first_threshold": json.dumps(first_threshold) if first_threshold else None,
        "primary_end_bar": end_primary,
        "opposite_end_bar": opp_bar,
        "truncated_primary": truncated_primary,
        "path_to_opp_mfe": path_to_opp.get("maximum_favorable_excursion_pct"),
        "path_to_opp_mae": path_to_opp.get("maximum_adverse_excursion_pct"),
        # legacy wide adverse-before-TP aliases (incl TP bar)
        "adverse_before_tp_0_5": adverse_before_tp(path_primary, 0.5),
        "adverse_before_tp_1": adverse_before_tp(path_primary, 1.0),
        "adverse_before_tp_2": adverse_before_tp(path_primary, 2.0),
        "adverse_before_tp_3": adverse_before_tp(path_primary, 3.0),
        "adverse_before_tp_5": adverse_before_tp(path_primary, 5.0),
        "favorable_before_sl_0_5": favorable_before_sl(path_primary, -0.5),
        "favorable_before_sl_1": favorable_before_sl(path_primary, -1.0),
        "favorable_before_sl_2": favorable_before_sl(path_primary, -2.0),
        "favorable_before_sl_3": favorable_before_sl(path_primary, -3.0),
        "favorable_before_sl_5": favorable_before_sl(path_primary, -5.0),
        **tp_adverse_wide,
        "had_adverse_excursion": reclaim_primary["had_adverse_excursion"],
        "reclaimed_after_adverse": reclaim_primary["reclaimed_after_adverse"],
        "reclaim_bar_offset": reclaim_primary["reclaim_bar_offset"],
        "reclaim_timestamp": reclaim_primary["reclaim_timestamp"],
        "bars_to_reclaim": reclaim_primary["bars_to_reclaim"],
        "worst_adverse_before_reclaim_pct": reclaim_primary["worst_adverse_before_reclaim_pct"],
        "never_reclaim_within_primary": reclaim_primary["never_reclaim_within_window"],
        "never_adverse": reclaim_primary["never_adverse"],
        "never_reclaim_to_data_end": bool(
            reclaim_primary["never_reclaim_within_window"] and end_primary >= end_data
        ),
        **blocker_primary,
        **tp_flags,
        **sl_flags,
        "close_s_path": close_path_list,
    }
    return panel, horizon_rows, level_rows, ft_rows, path_seq, tp_horizon_rows


# ---------------------------------------------------------------------------
# Aggregations / charts / report
# ---------------------------------------------------------------------------


def attach_context_features(
    panel: pd.DataFrame,
    pattern_dir: Path,
    case_dir: Path,
) -> pd.DataFrame:
    out = panel.copy()
    feat_cols = [
        "pullback_depth_atr",
        "bars_arm_to_trigger",
        "chase_distance_atr",
        "adx",
        "adx_slope_5",
        "ema9_minus_ema20_pct",
        "ema20_minus_ema50_pct",
        "bars_since_external_bos",
        "major_direction",
        "micro_direction",
        "regime",
        "top1_trade",
        "top3_trade",
        "winner_net020",
        "split",
        "month",
        "net_return_0_20_pct",
        "automatic_archetypes",
    ]
    pat = pattern_dir / "trade_feature_panel.csv"
    if pat.exists():
        pdf = pd.read_csv(pat)
        pdf["entry_timestamp"] = pd.to_datetime(pdf["entry_timestamp"], utc=True)
        out["fill_time"] = pd.to_datetime(out["fill_time"], utc=True)
        keep = ["entry_timestamp", "side"] + [c for c in feat_cols if c in pdf.columns and c not in ("split", "month")]
        # avoid duplicate
        merge_cols = [c for c in keep if c not in ("entry_timestamp", "side") or c in ("entry_timestamp", "side")]
        sub = pdf[["entry_timestamp", "side"] + [c for c in feat_cols if c in pdf.columns]].copy()
        sub = sub.rename(columns={"entry_timestamp": "fill_time"})
        out = out.merge(sub, on=["fill_time", "side"], how="left", suffixes=("", "_pat"))
    cpath = case_dir / "trades_flagged.csv"
    if cpath.exists():
        cdf = pd.read_csv(cpath)
        if "automatic_archetypes" in cdf.columns:
            cdf["entry_timestamp"] = pd.to_datetime(cdf["entry_timestamp"], utc=True)
            out["fill_time"] = pd.to_datetime(out["fill_time"], utc=True)
            out = out.merge(
                cdf[["entry_timestamp", "side", "automatic_archetypes"]].rename(columns={"entry_timestamp": "fill_time"}),
                on=["fill_time", "side"],
                how="left",
                suffixes=("", "_case"),
            )
            if "automatic_archetypes_case" in out.columns:
                out["automatic_archetypes"] = out["automatic_archetypes"].fillna(out["automatic_archetypes_case"])
    return out


def summarize_horizons(by_h: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (hb, side), g in by_h.groupby(["horizon_bars", "side"]):
        mfe = g["maximum_favorable_excursion_pct"]
        mae = g["maximum_adverse_excursion_pct"]
        rows.append(
            {
                "horizon_bars": hb,
                "side": side,
                "n": len(g),
                "mean_mfe": float(mfe.mean()),
                "median_mfe": float(mfe.median()),
                "q25_mfe": float(mfe.quantile(0.25)),
                "q75_mfe": float(mfe.quantile(0.75)),
                "q90_mfe": float(mfe.quantile(0.90)),
                "mean_mae": float(mae.mean()),
                "median_mae": float(mae.median()),
                "q25_abs_mae": float((-mae).quantile(0.25)),
                "q75_abs_mae": float((-mae).quantile(0.75)),
                "q90_abs_mae": float((-mae).quantile(0.90)),
                "share_mfe_gt_abs_mae": float((mfe > (-mae)).mean()),
                "share_first_favorable": float((g["first_excursion_direction"] == "favorable").mean()),
                "share_first_adverse": float((g["first_excursion_direction"] == "adverse").mean()),
                "share_in_profit_at_horizon": float((g["close_return_pct"] > 0).mean()),
            }
        )
    # both sides
    for hb, g in by_h.groupby("horizon_bars"):
        mfe = g["maximum_favorable_excursion_pct"]
        mae = g["maximum_adverse_excursion_pct"]
        rows.append(
            {
                "horizon_bars": hb,
                "side": "both",
                "n": len(g),
                "mean_mfe": float(mfe.mean()),
                "median_mfe": float(mfe.median()),
                "q25_mfe": float(mfe.quantile(0.25)),
                "q75_mfe": float(mfe.quantile(0.75)),
                "q90_mfe": float(mfe.quantile(0.90)),
                "mean_mae": float(mae.mean()),
                "median_mae": float(mae.median()),
                "q25_abs_mae": float((-mae).quantile(0.25)),
                "q75_abs_mae": float((-mae).quantile(0.75)),
                "q90_abs_mae": float((-mae).quantile(0.90)),
                "share_mfe_gt_abs_mae": float((mfe > (-mae)).mean()),
                "share_first_favorable": float((g["first_excursion_direction"] == "favorable").mean()),
                "share_first_adverse": float((g["first_excursion_direction"] == "adverse").mean()),
                "share_in_profit_at_horizon": float((g["close_return_pct"] > 0).mean()),
            }
        )
    return pd.DataFrame(rows)


def summarize_level_touches(levels: pd.DataFrame, panel: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    meta_cols = ["fill_id", "split", "included_in_realized_exit_a", "exit_a_closed"]
    if "side" not in levels.columns and "side" in panel.columns:
        meta_cols = ["fill_id", "side", "split", "included_in_realized_exit_a", "exit_a_closed"]
    merged = levels.merge(panel[meta_cols], on="fill_id", how="left")
    if "side" not in merged.columns and "side" in panel.columns:
        merged = merged.merge(panel[["fill_id", "side"]], on="fill_id", how="left")
    tp_rows, sl_rows = [], []

    def _tp_row(g: pd.DataFrame, *, level: float, side: str, slice_name: str) -> dict[str, Any]:
        reached = g[g["level_reached"] == True]  # noqa: E712
        return {
            "level_pct": level,
            "side": side,
            "slice": slice_name,
            "n": len(g),
            "n_reached": len(reached),
            "reach_rate": float(g["level_reached"].mean()) if len(g) else None,
            "median_bars_to_touch": float(reached["bars_to_touch"].median()) if len(reached) else None,
            "median_adverse_before_touch": float(reached["adverse_excursion_before_tp"].median()) if len(reached) else None,
            "p75_adverse_before_touch": float(reached["adverse_excursion_before_tp"].quantile(0.75)) if len(reached) else None,
            "p90_adverse_before_touch": float(reached["adverse_excursion_before_tp"].quantile(0.90)) if len(reached) else None,
        }

    def _sl_row(g: pd.DataFrame, *, level: float, side: str, slice_name: str) -> dict[str, Any]:
        reached = g[g["level_reached"] == True]  # noqa: E712
        return {
            "level_pct": level,
            "side": side,
            "slice": slice_name,
            "n": len(g),
            "n_reached": len(reached),
            "reach_rate": float(g["level_reached"].mean()) if len(g) else None,
            "median_bars_to_touch": float(reached["bars_to_touch"].median()) if len(reached) else None,
            "median_favorable_before_touch": float(reached["favorable_excursion_before_sl"].median()) if len(reached) else None,
        }

    for lvl, g in merged[merged["level_type"] == "TP"].groupby("level_pct"):
        for side, gs in list(g.groupby("side")) + [("both", g)]:
            tp_rows.append(_tp_row(gs, level=float(lvl), side=str(side), slice_name="all_fills"))
            closed = gs[(gs["included_in_realized_exit_a"] == True) & (gs["exit_a_closed"] == True)]  # noqa: E712
            if len(closed):
                tp_rows.append(_tp_row(closed, level=float(lvl), side=str(side), slice_name="closed_exit_a"))
            for sp, gsp in gs.groupby("split"):
                tp_rows.append(_tp_row(gsp, level=float(lvl), side=str(side), slice_name=f"split_{sp}"))
    for lvl, g in merged[merged["level_type"] == "SL"].groupby("level_pct"):
        for side, gs in list(g.groupby("side")) + [("both", g)]:
            sl_rows.append(_sl_row(gs, level=float(lvl), side=str(side), slice_name="all_fills"))
            closed = gs[(gs["included_in_realized_exit_a"] == True) & (gs["exit_a_closed"] == True)]  # noqa: E712
            if len(closed):
                sl_rows.append(_sl_row(closed, level=float(lvl), side=str(side), slice_name="closed_exit_a"))
            for sp, gsp in gs.groupby("split"):
                sl_rows.append(_sl_row(gsp, level=float(lvl), side=str(side), slice_name=f"split_{sp}"))
    return pd.DataFrame(tp_rows), pd.DataFrame(sl_rows)


def summarize_first_touch_grid(ft: pd.DataFrame) -> pd.DataFrame:
    rows = []
    group_keys = ["tp_level_pct", "sl_level_pct"]
    for side_label, g0 in [("both", ft)] + ([(s, ft[ft["side"] == s]) for s in sorted(ft["side"].dropna().unique())] if "side" in ft.columns else []):
        if g0.empty:
            continue
        for (tp, sl), g in g0.groupby(group_keys):
            n = len(g)
            resolved = g[~g["neither"]]
            rows.append(
                {
                    "side": side_label,
                    "tp_level_pct": tp,
                    "sl_level_pct": sl,
                    "n": n,
                    "n_resolved": int(len(resolved)),
                    "tp_first_rate": float(g["tp_first"].mean()),
                    "sl_first_rate": float(g["sl_first"].mean()),
                    "ambiguous_rate": float(g["intrabar_ambiguous"].mean()),
                    "neither_rate": float(g["neither"].mean()),
                    "median_resolution_bars": float(resolved["bars_to_resolution"].median()) if len(resolved) else None,
                    "conservative_expectancy_gross": float(g["conservative_return_pct"].dropna().mean())
                    if g["conservative_return_pct"].notna().any()
                    else None,
                    "optimistic_expectancy_gross": float(g["optimistic_return_pct"].dropna().mean())
                    if g["optimistic_return_pct"].notna().any()
                    else None,
                    "conservative_expectancy_net_0_20": float(g["conservative_net_0_20"].dropna().mean())
                    if g["conservative_net_0_20"].notna().any()
                    else None,
                    "optimistic_expectancy_net_0_20": float(g["optimistic_net_0_20"].dropna().mean())
                    if g["optimistic_net_0_20"].notna().any()
                    else None,
                }
            )
    return pd.DataFrame(rows)


def summarize_blocker_by_horizon(by_h: pd.DataFrame) -> pd.DataFrame:
    rows = []
    if by_h.empty or "blocker_class" not in by_h.columns:
        return pd.DataFrame()
    for (hb, side), g in list(by_h.groupby(["horizon_bars", "side"])) + [
        ((hb, "both"), g) for hb, g in by_h.groupby("horizon_bars")
    ]:
        row: dict[str, Any] = {
            "horizon_bars": hb,
            "side": side,
            "n": len(g),
            "share_reclaimed_after_adverse": float(g["reclaimed_after_adverse"].mean()),
            "share_never_adverse": float(g["never_adverse"].mean()),
            "share_tp_0_25_reached": float(g["tp_0_25_reached"].mean()),
            "share_truncated": float(g["truncated"].mean()),
        }
        for cls in (
            "fast_winner",
            "delayed_winner",
            "reclaimed_entry_only",
            "open_blocker_at_horizon",
            "never_profitable_within_horizon",
            "severe_adverse_excursion",
            "other_path",
            "unresolved_at_data_end",
        ):
            row[f"share_{cls}"] = float((g["blocker_class"] == cls).mean())
        row["share_flag_severe_adverse"] = float(g["flag_severe_adverse_excursion"].mean())
        rows.append(row)
    return pd.DataFrame(rows)


def summarize_tp_adverse_by_horizon(tp_by_h: pd.DataFrame) -> pd.DataFrame:
    rows = []
    if tp_by_h.empty:
        return pd.DataFrame()
    for (hb, lvl, side), g in list(tp_by_h.groupby(["horizon_bars", "level_pct", "side"])) + [
        ((hb, lvl, "both"), g)
        for (hb, lvl), g in tp_by_h.groupby(["horizon_bars", "level_pct"])
    ]:
        reached = g[g["level_reached"] == True]  # noqa: E712
        rows.append(
            {
                "horizon_bars": hb,
                "level_pct": lvl,
                "side": side,
                "n": len(g),
                "reach_rate": float(g["level_reached"].mean()),
                "never_hit_rate": float(g["never_hit"].mean()),
                "median_bars_to_touch": float(reached["bars_to_touch"].median()) if len(reached) else None,
                "median_adverse_incl_tp": float(reached["adverse_incl_tp_bar_pct"].median()) if len(reached) else None,
                "median_adverse_excl_tp": float(reached["adverse_excl_tp_bar_pct"].dropna().median())
                if len(reached) and reached["adverse_excl_tp_bar_pct"].notna().any()
                else None,
                "p90_adverse_incl_tp": float(reached["adverse_incl_tp_bar_pct"].quantile(0.90)) if len(reached) else None,
            }
        )
    return pd.DataFrame(rows)


def maybe_plots(
    out_dir: Path,
    panel: pd.DataFrame,
    hor_sum: pd.DataFrame,
    tp_sum: pd.DataFrame,
    sl_sum: pd.DataFrame,
    ft_grid: pd.DataFrame,
) -> list[str]:
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

    fig, ax = plt.subplots(figsize=(7, 4))
    ax.hist(panel["maximum_favorable_excursion_pct"].dropna(), bins=20, color="C2", alpha=0.8)
    ax.set_title("MFE distribution (55 fills)")
    ax.set_xlabel("MFE %")
    save(fig, "mfe_distribution.png")

    fig, ax = plt.subplots(figsize=(7, 4))
    ax.hist(panel["maximum_adverse_excursion_pct"].dropna(), bins=20, color="C3", alpha=0.8)
    ax.set_title("MAE distribution (55 fills)")
    ax.set_xlabel("MAE %")
    save(fig, "mae_distribution.png")

    fig, ax = plt.subplots(figsize=(6, 5))
    ax.scatter(panel["maximum_adverse_excursion_pct"], panel["maximum_favorable_excursion_pct"], alpha=0.7)
    ax.set_xlabel("MAE %")
    ax.set_ylabel("MFE %")
    ax.set_title("MFE vs MAE")
    save(fig, "mfe_vs_mae_scatter.png")

    both = hor_sum[hor_sum["side"] == "both"] if "side" in hor_sum.columns else hor_sum
    if not both.empty:
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.plot(both["horizon_bars"], both["median_mfe"], label="median MFE")
        ax.plot(both["horizon_bars"], -both["median_mae"].abs(), label="median MAE (signed)")
        ax.set_xlabel("horizon bars")
        ax.legend()
        ax.set_title("Median MFE/MAE by horizon")
        save(fig, "mfe_mae_by_horizon.png")

    if "slice" in tp_sum.columns:
        tp_b = tp_sum[(tp_sum["side"] == "both") & (tp_sum["slice"] == "all_fills")]
    elif not tp_sum.empty:
        tp_b = tp_sum[tp_sum["side"] == "both"]
    else:
        tp_b = tp_sum
    if not tp_b.empty:
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.bar([str(x) for x in tp_b["level_pct"]], tp_b["reach_rate"])
        ax.set_title("TP reach rate")
        ax.set_ylabel("reach_rate")
        save(fig, "tp_reach_rate.png")
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.plot(tp_b["level_pct"], tp_b["median_adverse_before_touch"], marker="o")
        ax.set_title("Median adverse before TP")
        save(fig, "median_adverse_before_tp.png")

    if "slice" in sl_sum.columns:
        sl_b = sl_sum[(sl_sum["side"] == "both") & (sl_sum["slice"] == "all_fills")]
    else:
        sl_b = sl_sum[sl_sum["side"] == "both"] if not sl_sum.empty else sl_sum
    if not sl_b.empty:
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.bar([str(x) for x in sl_b["level_pct"]], sl_b["reach_rate"], color="C3")
        ax.set_title("SL reach rate")
        save(fig, "sl_reach_rate.png")

    # heatmaps for selected grid (both sides)
    ft_both = ft_grid[ft_grid["side"] == "both"] if "side" in ft_grid.columns else ft_grid
    for mode, col in (("conservative", "conservative_expectancy_net_0_20"), ("optimistic", "optimistic_expectancy_net_0_20")):
        if ft_both.empty or col not in ft_both.columns:
            continue
        piv = ft_both.pivot(index="sl_level_pct", columns="tp_level_pct", values=col)
        fig, ax = plt.subplots(figsize=(9, 7))
        im = ax.imshow(piv.values, aspect="auto", cmap="RdYlGn")
        ax.set_xticks(range(len(piv.columns)))
        ax.set_xticklabels([str(c) for c in piv.columns], rotation=45, fontsize=7)
        ax.set_yticks(range(len(piv.index)))
        ax.set_yticklabels([str(i) for i in piv.index], fontsize=7)
        ax.set_xlabel("TP %")
        ax.set_ylabel("SL %")
        ax.set_title(f"First-touch expectancy net0.20 ({mode})")
        fig.colorbar(im, ax=ax, fraction=0.046)
        save(fig, f"first_touch_heatmap_{mode}.png")

    # long vs short
    fig, ax = plt.subplots(figsize=(7, 4))
    for side, color in (("long", "C0"), ("short", "C1")):
        sub = panel[panel["side"] == side]
        ax.scatter(sub["maximum_adverse_excursion_pct"], sub["maximum_favorable_excursion_pct"], alpha=0.6, label=side, c=color)
    ax.legend()
    ax.set_title("Long vs Short MFE/MAE")
    save(fig, "long_short_mfe_mae.png")

    # winner vs loser among exit-a
    ea = panel[panel["included_in_realized_exit_a"] == True]  # noqa: E712
    if "winner_net020" in ea.columns and ea["winner_net020"].notna().any():
        fig, ax = plt.subplots(figsize=(7, 4))
        for flag, lab in ((True, "winner"), (False, "loser")):
            sub = ea[ea["winner_net020"] == flag]
            ax.scatter(sub["maximum_adverse_excursion_pct"], sub["maximum_favorable_excursion_pct"], alpha=0.7, label=lab)
        ax.legend()
        ax.set_title("Exit-A winner vs loser MFE/MAE")
        save(fig, "winner_loser_mfe_mae.png")

    if "top3_trade" in panel.columns:
        fig, ax = plt.subplots(figsize=(7, 4))
        for flag, lab in ((True, "top3"), (False, "rest")):
            sub = panel[panel["top3_trade"] == flag]
            if len(sub):
                ax.scatter(sub["maximum_adverse_excursion_pct"], sub["maximum_favorable_excursion_pct"], alpha=0.7, label=lab)
        ax.legend()
        ax.set_title("Top-3 vs Rest")
        save(fig, "top3_vs_rest_mfe_mae.png")

    fig, ax = plt.subplots(figsize=(7, 4))
    ax.hist(panel["bars_to_mfe"].dropna(), bins=20, alpha=0.7, label="bars_to_mfe")
    ax.hist(panel["bars_to_mae"].dropna(), bins=20, alpha=0.7, label="bars_to_mae")
    ax.legend()
    ax.set_title("Time to MFE / MAE (bars)")
    save(fig, "time_to_mfe_mae.png")

    fig, ax = plt.subplots(figsize=(7, 4))
    ax.hist(panel["max_underwater_duration_bars"].dropna(), bins=20, color="C3")
    ax.set_title("Max underwater duration (bars)")
    save(fig, "underwater_duration.png")

    # path curves of fills (close return, first 96 bars)
    if "close_s_path" in panel.columns:
        fig, ax = plt.subplots(figsize=(10, 5))
        for _, r in panel.iterrows():
            path = r["close_s_path"]
            if path is None or (isinstance(path, float) and math.isnan(path)):
                continue
            if isinstance(path, str):
                try:
                    path = json.loads(path)
                except Exception:
                    continue
            ys = list(path)[:96]
            ax.plot(range(len(ys)), ys, alpha=0.25, color="C0" if r["side"] == "long" else "C1", linewidth=0.8)
        ax.axhline(0, color="k", linewidth=0.6)
        ax.set_title("Direction-normalized close paths (first 96 bars)")
        ax.set_xlabel("bars from fill")
        ax.set_ylabel("signed close return %")
        save(fig, "fill_path_curves.png")

    if not both.empty:
        fig, ax = plt.subplots(figsize=(9, 5))
        ax.plot(both["horizon_bars"], both["mean_mfe"], label="mean MFE")
        ax.plot(both["horizon_bars"], both["mean_mae"], label="mean MAE")
        ax.legend()
        ax.set_title("Mean excursion path by horizon")
        save(fig, "mean_path_by_horizon.png")

    return written


def write_report(out_dir: Path, meta: Mapping[str, Any], panel: pd.DataFrame, recon_sum: pd.DataFrame, tp_sum: pd.DataFrame, ft_grid: pd.DataFrame) -> Path:
    rs = {r["metric"]: r["value"] for _, r in recon_sum.iterrows()}
    both24 = panel  # primary window stats
    lines = [
        "# C3.5c APT 15m Fill Excursion Audit",
        "",
        "Research-only. **No stop/TP optimization. No strategy filter.**",
        "",
        "## 1. Ziel und Population",
        "",
        f"- Symbol `{meta.get('symbol')}` · A6 · 15m · Entry = next open after trigger close",
        f"- Analyze: `{meta.get('analyze_start')}` → `{meta.get('analyze_end_exclusive')}`",
        f"- Fills analyzed: **{meta.get('n_fills')}**",
        "",
        "## 2–3. Rekonstruktion 55 Fills vs 29 Closed",
        "",
        f"- Arms/lifecycles: `{rs.get('arms_lifecycles')}` · Triggers(entry_created): `{rs.get('triggers_entry_created')}`",
        f"- Fills: `{rs.get('fills')}` (long `{rs.get('long_fills')}` / short `{rs.get('short_fills')}`)",
        f"- Exit-A entries included: `{rs.get('exit_a_entries_included')}` (= closed `{rs.get('closed_exit_a_trades')}` + open `{rs.get('terminal_open_fills')}`)",
        f"- Same-direction skips while Exit-A position open: `{rs.get('same_direction_skipped_fills')}`",
        f"- Identity: 55 = 30 Exit-A entries + 25 skips → `{rs.get('identity_check_55_eq_30_plus_25')}`",
        "",
        "Exit-A ist sequentiell non-overlapping: Opposite Fill schließt und eröffnet gleichzeitig; "
        "gleiche Richtung während offener Position wird **nicht** als neuer Exit-A-Trade gezählt.",
        "",
        "## 4. Datenzeitraum und Trunkierung",
        "",
        f"- Primary path window: min(opposite fill, 7d={MAX_BARS_7D} bars, data end)",
        f"- Horizons: {list(HORIZON_BARS)} bars",
        "",
        "## 5. MFE/MAE-Gesamtbild (primary window)",
        "",
        f"- median MFE=`{float(panel['maximum_favorable_excursion_pct'].median()):.3f}%` · "
        f"median MAE=`{float(panel['maximum_adverse_excursion_pct'].median()):.3f}%`",
        f"- mean MFE=`{float(panel['maximum_favorable_excursion_pct'].mean()):.3f}%` · "
        f"mean MAE=`{float(panel['maximum_adverse_excursion_pct'].mean()):.3f}%`",
        f"- share first favorable=`{float((panel['first_excursion_direction']=='favorable').mean()):.3f}` · "
        f"first adverse=`{float((panel['first_excursion_direction']=='adverse').mean()):.3f}` · "
        f"intrabar_unknown=`{float((panel['first_excursion_direction']=='intrabar_unknown').mean()):.3f}`",
        "",
        "## 6–8. Horizonte / TP / SL",
        "",
        "- Details: `excursion_summary_by_horizon.csv`, `tp_reach_summary.csv`, `sl_reach_summary.csv`",
        "",
    ]
    tp_b = tp_sum.copy()
    if "slice" in tp_b.columns:
        tp_b = tp_b[(tp_b["side"] == "both") & (tp_b["slice"] == "all_fills")].sort_values("level_pct")
    elif not tp_b.empty:
        tp_b = tp_b[tp_b["side"] == "both"].sort_values("level_pct")
    if not tp_b.empty:
        lines.append("TP reach (both, all fills):")
        for _, r in tp_b.iterrows():
            lines.append(
                f"- TP {r['level_pct']}%: reach={r['reach_rate']:.2%} · "
                f"med bars={r['median_bars_to_touch']} · med adv_before={r['median_adverse_before_touch']}"
            )

    lines += [
        "",
        "## 9. First-Touch-Matrix",
        "",
        "- Vollständiges Gitter in `first_touch_grid_summary.csv` / Heatmaps.",
        "- Same-bar TP+SL: **ambiguous**; konservativ=SL zuerst, optimistisch=TP zuerst — beide nur diagnostisch.",
        "",
        "## 10–11. Gegenlauf vor TP / Entry-Reclaim / Blocker",
        "",
        f"- med adverse before TP1%=`{float(panel['adverse_before_tp_1'].median()):.3f}%`",
        f"- med adverse before TP2%=`{float(panel['adverse_before_tp_2'].median()):.3f}%`",
        f"- med adverse before TP3%=`{float(panel['adverse_before_tp_3'].median()):.3f}%`",
        f"- med adverse before TP5%=`{float(panel['adverse_before_tp_5'].median()):.3f}%`",
        f"- med favorable before SL1%=`{float(panel['favorable_before_sl_1'].median()):.3f}%`",
        f"- share reclaimed_after_adverse (primary)=`{float(panel['reclaimed_after_adverse'].mean()):.3f}`",
        f"- blocker_class distribution (primary): see panel / `blocker_summary_by_horizon.csv`",
        f"- long-form pre-TP adverse by horizon: `fill_tp_adverse_by_horizon.csv`",
        "",
        "## 12. Long vs Short",
        "",
    ]
    for side in ("long", "short"):
        sub = panel[panel["side"] == side]
        lines.append(
            f"- {side}: n={len(sub)} medMFE={float(sub['maximum_favorable_excursion_pct'].median()):.3f} "
            f"medMAE={float(sub['maximum_adverse_excursion_pct'].median()):.3f}"
        )

    ea = panel[panel["included_in_realized_exit_a"] == True]  # noqa: E712
    lines += ["", "## 13–14. Exit-A Winner/Loser & Top-3", ""]
    if "winner_net020" in ea.columns and ea["winner_net020"].notna().any():
        for flag, lab in ((True, "winner"), (False, "loser")):
            sub = ea[ea["winner_net020"] == flag]
            lines.append(
                f"- Exit-A {lab}: n={len(sub)} medMFE={float(sub['maximum_favorable_excursion_pct'].median()):.3f} "
                f"medMAE={float(sub['maximum_adverse_excursion_pct'].median()):.3f}"
            )
    if "top3_trade" in panel.columns:
        for flag, lab in ((True, "top3"), (False, "rest_exit_a")):
            sub = ea[ea["top3_trade"] == flag] if flag else ea[ea["top3_trade"] != True]  # noqa: E712
            if len(sub):
                lines.append(
                    f"- {lab}: n={len(sub)} medMFE={float(sub['maximum_favorable_excursion_pct'].median()):.3f} "
                    f"medMAE={float(sub['maximum_adverse_excursion_pct'].median()):.3f}"
                )

    lines += [
        "",
        "## 15. Dev/Val/OOS",
        "",
        "- Split am Fill-Zeitpunkt (kalendarisch 60/20/20 wie Pattern-Audit). Siehe Side/Split-Tabellen.",
        "",
        "## 16. Archetypen",
        "",
        "- `excursion_by_archetype.csv` verbindet vorhandene Case-Review-Tags nur für Exit-A-Entries.",
        "",
        "## 17–18. Stop-/TP-Sensitivität (nur Diagnose)",
        "",
        "- Aussagen der Form: „bei Stop X wären Y Fills vor späterem TP ausgestoppt“ stehen in First-Touch-Grid / adverse_before_tp.",
        "- **Kein** optimaler Stop/TP wird empfohlen.",
        "",
        "## 19–20. Frühpfad vs lange Bewegung",
        "",
        f"- median bars_to_mfe=`{float(panel['bars_to_mfe'].median()):.1f}` · bars_to_mae=`{float(panel['bars_to_mae'].median()):.1f}`",
        f"- median max underwater bars=`{float(panel['max_underwater_duration_bars'].median()):.1f}`",
        "",
        "## 21. Offene Unsicherheiten",
        "",
        "- Intrabar High/Low-Reihenfolge unbekannt",
        "- Val/OOS dünn",
        "- Top-3-Dominanz bleibt",
        "",
        "## 22. Empfehlung nächste Phase",
        "",
        "- Deskriptive Stop-Toleranz-Bänder aus `adverse_before_tp_*` und First-Touch-Grid ableiten (Holdout-Hypothesen, keine Optimierung)",
        "- Besonders Short-Fills und Exit-A-Winner-Frühpfade weiter case-weise prüfen",
        "- Keine SM-/Pine-Änderung",
        "",
    ]
    # answers to core questions briefly
    lines += [
        "## Kernfragen (Kurzantworten)",
        "",
        f"1. Typischer Gegenlauf (med |MAE|): `{float((-panel['maximum_adverse_excursion_pct']).median()):.3f}%`",
        f"2. Gegenlauf vor TP: siehe Spalten adverse_before_tp_* / tp_reach_summary",
        f"3. Zuerst Gewinn/Verlust: favorable `{float((panel['first_excursion_direction']=='favorable').mean()):.1%}` / "
        f"adverse `{float((panel['first_excursion_direction']=='adverse').mean()):.1%}` / "
        f"unknown `{float((panel['first_excursion_direction']=='intrabar_unknown').mean()):.1%}`",
        "4–5. Winner underwater / Loser prior MFE: siehe excursion_by_outcome.csv",
        "6–8. TP/SL reach & timing: tp_reach_summary / sl_reach_summary",
        "9. Long/Short: Abschnitt 12",
        "10. Top-3 Frühpfad: Abschnitt 14 / Plots",
        "11–14. Lange Läufe / Stops / Reversals: path_class + underwater + first_touch_grid",
        f"15. 55 vs 29: `{rs.get('same_direction_skipped_fills')}` Skips + `{rs.get('closed_exit_a_trades')}` closed + `{rs.get('terminal_open_fills')}` open = `{rs.get('fills')}`",
        "",
    ]
    path = out_dir / "report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run_fill_excursion_audit(
    *,
    output_dir: Path = DEFAULT_OUT,
    baseline_dir: Path = DEFAULT_BASELINE_DIR,
    pattern_dir: Path = PATTERN_DIR,
    case_dir: Path = CASE_DIR,
    write_plots: bool = True,
) -> dict[str, Any]:
    baseline_info = assert_baseline_readonly(baseline_dir)
    if not baseline_info.get("hash_matches"):
        raise RuntimeError("C2 baseline hash mismatch")
    assert_safe_output_dir(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    cfg = baseline_a6()
    frame, frame_meta = build_extended_tf_frame(SYMBOL, timeframe=TIMEFRAME, warmup_calendar_days=WARMUP_CALENDAR_DAYS)
    if frame.empty:
        raise RuntimeError(f"empty frame: {frame_meta}")

    _tl, entries, lives = apply_pullback_entry(frame, cfg, return_lifecycles=True)
    filled = _filled_sorted(frame, entries)
    trades = trades_exit_a_opposite_entry(frame, filled, timeframe=TIMEFRAME, variant=cfg.name)
    closed = closed_only(trades)

    n_bars = len(frame)
    timestamps = list(frame["timestamp"])
    highs = frame["high"].astype(float).to_numpy()
    lows = frame["low"].astype(float).to_numpy()
    closes = frame["close"].astype(float).to_numpy()
    data_end = timestamps[-1]

    recon = reconcile_fill_population(filled, trades, n_bars=n_bars, data_end_ts=data_end)
    n_arms = sum(1 for L in lives if L.get("armed_bar") is not None)
    n_trig = sum(1 for L in lives if L.get("entry_created"))
    recon_sum = reconciliation_summary(recon, n_arms=n_arms, n_triggers=n_trig, n_lives=len(lives))

    # splits
    a0 = pd.Timestamp(frame_meta["analyze_start"])
    a1 = pd.Timestamp(frame_meta["analyze_end_exclusive"])
    splits = fixed_chrono_splits(a0, a1)

    panel_rows = []
    horizon_rows = []
    level_rows = []
    ft_rows = []
    seq_rows = []
    tp_horizon_rows = []

    for _, rr in recon.iterrows():
        idx = int(rr["fill_index"])
        fill = filled[idx]
        core, horizons, levels, fts, seq, tp_h = analyze_fill_core(
            fill=fill,
            recon_row=rr.to_dict(),
            fills=filled,
            highs=highs,
            lows=lows,
            closes=closes,
            timestamps=timestamps,
            n_bars=n_bars,
        )
        fill_id = rr["fill_id"]
        ft = pd.Timestamp(fill["fill_timestamp"])
        split = assign_split(ft, splits)
        month = ft.tz_convert("UTC").strftime("%Y-%m")
        row = {
            **rr.to_dict(),
            **{k: v for k, v in core.items()},
            "split": split,
            "month": month,
        }
        # drop numpy path from panel
        panel_rows.append(row)
        for h in horizons:
            horizon_rows.append({"fill_id": fill_id, "side": rr["side"], "split": split, **h})
        for lv in levels:
            level_rows.append({"fill_id": fill_id, "side": rr["side"], **lv})
        for ft_r in fts:
            ft_rows.append({"fill_id": fill_id, "side": rr["side"], **ft_r})
        for s in seq:
            seq_rows.append({"fill_id": fill_id, "side": rr["side"], **s})
        for th in tp_h:
            tp_horizon_rows.append({"fill_id": fill_id, "side": rr["side"], "split": split, **th})

    panel = pd.DataFrame(panel_rows)
    panel = attach_context_features(panel, pattern_dir, case_dir)
    by_h = pd.DataFrame(horizon_rows)
    levels = pd.DataFrame(level_rows)
    ft = pd.DataFrame(ft_rows)
    seq = pd.DataFrame(seq_rows)
    tp_by_h = pd.DataFrame(tp_horizon_rows)

    # verify closed identity vs shared Exit-A helper
    n_closed_check = int(((recon["included_in_realized_exit_a"]) & (recon["exit_a_closed"])).sum())
    if n_closed_check != len(closed):
        raise RuntimeError(f"closed Exit-A mismatch: recon={n_closed_check} trades={len(closed)}")
    if len(filled) != 55:
        raise RuntimeError(f"expected 55 A6 fills, got {len(filled)}")

    hor_sum = summarize_horizons(by_h)
    tp_sum, sl_sum = summarize_level_touches(levels, panel)
    ft_grid = summarize_first_touch_grid(ft)
    blocker_sum = summarize_blocker_by_horizon(by_h)
    tp_adv_sum = summarize_tp_adverse_by_horizon(tp_by_h)

    # by side / outcome / archetype
    side_rows = []
    for side, g in panel.groupby("side"):
        side_rows.append(
            {
                "side": side,
                "n": len(g),
                "median_mfe": float(g["maximum_favorable_excursion_pct"].median()),
                "median_mae": float(g["maximum_adverse_excursion_pct"].median()),
                "mean_mfe": float(g["maximum_favorable_excursion_pct"].mean()),
                "mean_mae": float(g["maximum_adverse_excursion_pct"].mean()),
                "share_first_favorable": float((g["first_excursion_direction"] == "favorable").mean()),
                "share_first_adverse": float((g["first_excursion_direction"] == "adverse").mean()),
                "median_bars_to_mfe": float(g["bars_to_mfe"].median()),
                "median_bars_to_mae": float(g["bars_to_mae"].median()),
                "median_underwater_bars": float(g["max_underwater_duration_bars"].median()),
            }
        )
    side_df = pd.DataFrame(side_rows)

    outcome_rows = []
    ea = panel[panel["included_in_realized_exit_a"] == True].copy()  # noqa: E712
    if "winner_net020" in ea.columns:
        for flag, lab in ((True, "exit_a_winner"), (False, "exit_a_loser")):
            g = ea[ea["winner_net020"] == flag]
            if len(g):
                outcome_rows.append(
                    {
                        "group": lab,
                        "n": len(g),
                        "median_mfe": float(g["maximum_favorable_excursion_pct"].median()),
                        "median_mae": float(g["maximum_adverse_excursion_pct"].median()),
                        "median_adverse_before_tp_1": float(g["adverse_before_tp_1"].median()),
                        "share_mae_before_mfe": float(g["mae_before_mfe"].mean()),
                    }
                )
    if "top3_trade" in ea.columns:
        for flag, lab in ((True, "top3"), (False, "without_top3")):
            g = ea[ea["top3_trade"] == flag] if flag else ea[ea["top3_trade"] != True]  # noqa: E712
            if len(g):
                outcome_rows.append(
                    {
                        "group": lab,
                        "n": len(g),
                        "median_mfe": float(g["maximum_favorable_excursion_pct"].median()),
                        "median_mae": float(g["maximum_adverse_excursion_pct"].median()),
                        "median_adverse_before_tp_1": float(g["adverse_before_tp_1"].median()),
                        "share_mae_before_mfe": float(g["mae_before_mfe"].mean()),
                    }
                )
    for sp, g in panel.groupby("split"):
        outcome_rows.append(
            {
                "group": f"split_{sp}",
                "n": len(g),
                "median_mfe": float(g["maximum_favorable_excursion_pct"].median()),
                "median_mae": float(g["maximum_adverse_excursion_pct"].median()),
                "median_adverse_before_tp_1": float(g["adverse_before_tp_1"].median()),
                "share_mae_before_mfe": float(g["mae_before_mfe"].mean()),
            }
        )
    outcome_df = pd.DataFrame(outcome_rows)

    arch_rows = []
    if "automatic_archetypes" in panel.columns:
        for _, r in panel.dropna(subset=["automatic_archetypes"]).iterrows():
            for tag in str(r["automatic_archetypes"]).split("|"):
                if not tag:
                    continue
                arch_rows.append(
                    {
                        "archetype": tag,
                        "fill_id": r["fill_id"],
                        "mfe": r["maximum_favorable_excursion_pct"],
                        "mae": r["maximum_adverse_excursion_pct"],
                        "path_class": r["path_class"],
                        "side": r["side"],
                    }
                )
    arch_long = pd.DataFrame(arch_rows)
    if not arch_long.empty:
        arch_sum = (
            arch_long.groupby("archetype")
            .agg(n=("fill_id", "count"), median_mfe=("mfe", "median"), median_mae=("mae", "median"))
            .reset_index()
        )
    else:
        arch_sum = pd.DataFrame()

    # write panel without list path column (keep for plots via copy)
    panel_for_csv = panel.copy()
    if "close_s_path" in panel_for_csv.columns:
        panel_for_csv["close_s_path"] = panel_for_csv["close_s_path"].apply(
            lambda x: json.dumps(x) if isinstance(x, list) else x
        )
    recon.to_csv(output_dir / "fill_population_reconciliation.csv", index=False)
    recon_sum.to_csv(output_dir / "fill_reconciliation_summary.csv", index=False)
    panel_for_csv.to_csv(output_dir / "fill_excursion_panel.csv", index=False)
    by_h.to_csv(output_dir / "fill_excursion_by_horizon.csv", index=False)
    levels.to_csv(output_dir / "level_touch_events.csv", index=False)
    ft.to_csv(output_dir / "first_touch_matrix.csv", index=False)
    seq.to_csv(output_dir / "fill_path_sequence.csv", index=False)
    hor_sum.to_csv(output_dir / "excursion_summary_by_horizon.csv", index=False)
    tp_sum.to_csv(output_dir / "tp_reach_summary.csv", index=False)
    sl_sum.to_csv(output_dir / "sl_reach_summary.csv", index=False)
    ft_grid.to_csv(output_dir / "first_touch_grid_summary.csv", index=False)
    side_df.to_csv(output_dir / "excursion_by_side.csv", index=False)
    outcome_df.to_csv(output_dir / "excursion_by_outcome.csv", index=False)
    arch_sum.to_csv(output_dir / "excursion_by_archetype.csv", index=False)
    closed.to_csv(output_dir / "exit_a_trades_reference.csv", index=False)
    tp_by_h.to_csv(output_dir / "fill_tp_adverse_by_horizon.csv", index=False)
    blocker_sum.to_csv(output_dir / "blocker_summary_by_horizon.csv", index=False)
    tp_adv_sum.to_csv(output_dir / "tp_adverse_summary_by_horizon.csv", index=False)

    plots = maybe_plots(output_dir, panel, hor_sum, tp_sum, sl_sum, ft_grid) if write_plots else []

    meta = {
        "symbol": SYMBOL,
        "variant": VARIANT,
        "timeframe": TIMEFRAME,
        "config_hash": config_hash(cfg),
        "n_fills": len(filled),
        "n_exit_a_trades": len(trades),
        "n_closed_exit_a": int(len(closed)),
        "n_open_exit_a": int((~trades["closed"]).sum()) if len(trades) else 0,
        "n_same_direction_skips": int((recon["exclusion_reason"] == "same_direction_while_exit_a_position_open").sum()),
        "analyze_start": frame_meta.get("analyze_start"),
        "analyze_end_exclusive": frame_meta.get("analyze_end_exclusive"),
        "data_end": str(data_end),
        "formulas": {
            "long_signed_ret": "(px/entry - 1)*100",
            "short_signed_ret": "(entry/px - 1)*100",
            "long_fav_adv": "fav from high, adv from low",
            "short_fav_adv": "fav from low, adv from high",
            "same_bar_tp_sl": "ambiguous; conservative=SL first; optimistic=TP first",
        },
        "horizons_bars": list(HORIZON_BARS),
        "tp_levels": list(TP_LEVELS_PCT),
        "sl_levels": list(SL_LEVELS_PCT),
        "max_calendar_days": MAX_CALENDAR_DAYS,
        "blocker_definitions": {
            "fast_winner": "TP 0.25% with bar_offset < 12 (within first 12 bars)",
            "delayed_winner": "TP 0.25% with bar_offset >= 12 within window",
            "reclaimed_entry_only": "entry reclaimed after adverse; TP 0.25% not reached",
            "open_blocker_at_horizon": "close < 0 at window end; entry not reclaimed; TP 0.25% not hit",
            "never_profitable_within_horizon": "MFE <= 0 within window",
            "severe_adverse_excursion": f"MAE <= {SEVERE_MAE_THRESHOLD_PCT}% (also always as flag)",
            "priority": [
                "fast_winner",
                "delayed_winner",
                "reclaimed_entry_only",
                "never_profitable_within_horizon",
                "open_blocker_at_horizon",
                "severe_adverse_excursion",
                "other_path",
            ],
            "reclaim_touch": "long: high|close >= entry; short: low|close <= entry; requires prior adverse",
            "adverse_before_tp_incl": "min(adv) through first TP bar inclusive",
            "adverse_before_tp_excl": "min(adv) strictly before first TP bar",
        },
        "no_stop_tp_optimization": True,
        "no_filter_promotion": True,
        "production_sm_unchanged": True,
        "pine_unchanged": True,
        "baseline_reference_hash": C2_BASELINE_HASH,
        "plots": plots,
        "reconciliation": {r["metric"]: r["value"] for _, r in recon_sum.iterrows()},
        "content_hash": hashlib.sha256(
            pd.util.hash_pandas_object(recon[["fill_id", "exclusion_reason", "included_in_realized_exit_a"]].fillna(""), index=True).values
        ).hexdigest(),
    }
    (output_dir / "metadata.json").write_text(json.dumps(json_safe(meta), indent=2) + "\n", encoding="utf-8")
    write_report(output_dir, meta, panel, recon_sum, tp_sum, ft_grid)
    return meta


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="C3.5c APT 15m fill excursion audit")
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--baseline-dir", type=Path, default=DEFAULT_BASELINE_DIR)
    p.add_argument("--no-plots", action="store_true")
    args = p.parse_args(list(argv) if argv is not None else None)
    meta = run_fill_excursion_audit(output_dir=args.output_dir, baseline_dir=args.baseline_dir, write_plots=not args.no_plots)
    print(json.dumps(json_safe({"ok": True, "n_fills": meta["n_fills"], "n_closed": meta["n_closed_exit_a"], "out": str(args.output_dir)})))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
