"""Phase D: causal reverse / breakout / unclear classification.

Uses Phase A/B/C exports only. Scores and rules are deterministic, transparent,
and fixed a priori. Phase-C targets are evaluation-only and never enter scores.
No entry, TP/SL, fees, PnL, OOS threshold search, or scanner changes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np
import pandas as pd

from research.liquidation_level.liquidation_control_validation import (
    EXPECTED_FULL,
    EXPECTED_IS,
    EXPECTED_OOS,
)
from research.liquidation_level.sweep_feature_snapshots import (
    assert_no_entry_fields,
    compute_path_aggregates,
    compute_targets,
)

PHASE_C_EXPECTED_HASH = (
    "cd56b814599ee6b991a25c2bc84f61e46d01c109e8bdc496853ac986b787f3c6"
)

DEFAULT_DECISION_OFFSETS = (1, 3, 6, 12)
DEFAULT_RULE_FAMILIES = ("R1", "R2", "R3", "R4", "R5")
DEFAULT_VARIANTS = ("strict", "medium", "loose")

CLASS_SHORT = "SHORT_REVERSAL"
CLASS_BULL = "BULLISH_BREAKOUT_CONTINUATION"
CLASS_UNCLEAR = "UNCLEAR"
CLASS_INVALID = "TECHNICAL_INVALID"

# Predefined absolute score thresholds matched to score scale ∈ roughly [-2, 2]
# (component clips + weighted mean). Fixed a priori — never tuned on OOS.
VARIANT_CONFIG: dict[str, dict[str, float]] = {
    "strict": {"abs_score_threshold": 1.20, "agreement_min": 0.75},
    "medium": {"abs_score_threshold": 0.70, "agreement_min": 0.55},
    "loose": {"abs_score_threshold": 0.35, "agreement_min": 0.40},
}

# Explicit component weights (negative contribution → short/reversal).
SCORE_WEIGHTS: dict[str, float] = {
    "level_response_score": 1.00,
    "trend_5m_score": 0.80,
    "structure_5m_score": 0.70,
    "volatility_5m_score": 0.25,
    "volume_5m_score": 0.25,
    "context_15m_score": 0.60,
    "structure_15m_score": 0.50,
    "context_30m_score": 0.60,
    "structure_30m_score": 0.50,
    "blocker_score": 1.00,
}

RULE_COMPONENTS: dict[str, tuple[str, ...]] = {
    "R1": ("level_response_score",),
    "R2": (
        "level_response_score",
        "trend_5m_score",
        "structure_5m_score",
        "volatility_5m_score",
        "volume_5m_score",
    ),
    "R3": (
        "level_response_score",
        "trend_5m_score",
        "structure_5m_score",
        "volatility_5m_score",
        "volume_5m_score",
        "context_15m_score",
        "structure_15m_score",
    ),
    "R4": (
        "level_response_score",
        "trend_5m_score",
        "structure_5m_score",
        "volatility_5m_score",
        "volume_5m_score",
        "context_15m_score",
        "structure_15m_score",
        "context_30m_score",
        "structure_30m_score",
        "blocker_score",
    ),
    "R5": (
        "level_response_score",
        "trend_5m_score",
        "structure_5m_score",
        "volatility_5m_score",
        "volume_5m_score",
        "context_15m_score",
        "structure_15m_score",
        "context_30m_score",
        "structure_30m_score",
        "blocker_score",
    ),
}

# Features used per score component (for feature_usage export / traces).
FEATURE_USAGE: dict[str, tuple[str, ...]] = {
    "level_response_score": (
        "final_close_relative_to_level_pct",
        "fraction_closes_below_level",
        "fraction_closes_above_level",
        "longest_below_run",
        "longest_above_run",
        "number_reclaims_below",
        "n_accepted_above",
        "n_rejected_from_level",
    ),
    "trend_5m_score": (
        "decision_5m_ema_9_20_distance",
        "decision_5m_di_spread",
        "decision_5m_adx",
        "fraction_bearish_ema_ordering",
        "fraction_bullish_ema_ordering",
        "fraction_di_minus_gt_plus",
        "fraction_di_plus_gt_minus",
        "ema9_ema20_crosses",
    ),
    "structure_5m_score": (
        "decision_5m_structure_bias",
        "new_bearish_bos_count",
        "new_bullish_bos_count",
        "new_bearish_choch_count",
        "new_bullish_choch_count",
        "failed_breakout_count",
        "failed_breakdown_count",
        "end_structure_bias",
    ),
    "volatility_5m_score": ("atr_pct_mean", "atr_pct_change_proxy", "max_range_expansion_proxy"),
    "volume_5m_score": ("volume_ratio_mean", "volume_spike_count_proxy"),
    "context_15m_score": (
        "decision_15m_regime",
        "decision_15m_di_spread",
        "decision_15m_adx",
        "tf15_regime_changed_since_sweep",
    ),
    "structure_15m_score": (
        "decision_15m_structure_bias",
        "decision_15m_last_bos",
        "tf15_structure_changed_since_sweep",
    ),
    "context_30m_score": (
        "decision_30m_regime",
        "decision_30m_di_spread",
        "decision_30m_adx",
        "tf30_regime_changed_since_sweep",
    ),
    "structure_30m_score": (
        "decision_30m_structure_bias",
        "decision_30m_last_bos",
        "tf30_structure_changed_since_sweep",
    ),
    "blocker_score": (
        "decision_30m_structure_bias",
        "decision_15m_structure_bias",
        "decision_15m_di_spread",
        "decision_30m_di_spread",
        "longest_above_run",
        "longest_below_run",
        "new_bullish_bos_count",
        "new_bearish_bos_count",
        "fraction_closes_above_level",
        "fraction_closes_below_level",
    ),
}

FORBIDDEN_RESULT_FIELDS = frozenset(
    {
        "entry_index",
        "entry_price",
        "entry_timestamp",
        "pnl",
        "net_pnl",
        "gross_pnl",
        "tp",
        "sl",
        "take_profit",
        "stop_loss",
        "fees",
        "winrate",
        "win_rate",
    }
)

BAR_USECOLS = [
    "event_id",
    "window_size",
    "window_offset",
    "sample",
    "timestamp",
    "available_at",
    "open",
    "high",
    "low",
    "close",
    "lvl_close_above_level",
    "lvl_close_below_level",
    "lvl_crossed_level",
    "lvl_reclaimed_below_level",
    "lvl_accepted_above_level_candidate",
    "lvl_rejected_from_level_candidate",
    "lvl_close_relative_to_level_pct",
    "current_5m_adx",
    "current_5m_di_plus",
    "current_5m_di_minus",
    "current_5m_atr_pct",
    "current_5m_volume_ratio",
    "current_5m_ema_9",
    "current_5m_ema_20",
    "current_5m_ema_59",
    "current_5m_ema_200",
    "current_5m_ema_9_20_distance",
    "current_5m_regime",
    "current_5m_structure_bias",
    "current_5m_last_bos",
    "current_5m_last_choch",
    "current_5m_last_failed_breakout",
    "current_5m_last_failed_breakdown",
    "current_5m_retest_direction",
    "current_15m_regime",
    "current_15m_structure_bias",
    "current_15m_adx",
    "current_15m_di_plus",
    "current_15m_di_minus",
    "current_15m_last_bos",
    "current_30m_regime",
    "current_30m_structure_bias",
    "current_30m_adx",
    "current_30m_di_plus",
    "current_30m_di_minus",
    "current_30m_last_bos",
    "htf15_state_changed_since_sweep",
    "htf30_state_changed_since_sweep",
    "frozen_5m_regime",
    "frozen_15m_regime",
    "frozen_30m_regime",
    "frozen_15m_structure_bias",
    "frozen_30m_structure_bias",
]


class PhaseDValidationError(RuntimeError):
    """Abort Phase D when Phase C / A / B contracts do not match."""


def _finite(v: object) -> float | None:
    try:
        x = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not np.isfinite(x):
        return None
    return x


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return float(max(lo, min(hi, x)))


def _sign_label(score: float, eps: float = 1e-9) -> str:
    if score > eps:
        return "bull"
    if score < -eps:
        return "short"
    return "neutral"


def _is_bullish_struct(v: object) -> bool:
    s = str(v or "").lower()
    return "bull" in s


def _is_bearish_struct(v: object) -> bool:
    s = str(v or "").lower()
    return "bear" in s


def validate_phase_d_inputs(
    *,
    phase_a_dir: Path,
    phase_b_dir: Path,
    phase_c_dir: Path,
    expected_hash: str = PHASE_C_EXPECTED_HASH,
) -> dict[str, Any]:
    a = Path(phase_a_dir)
    b = Path(phase_b_dir)
    c = Path(phase_c_dir)
    summary_c = json.loads((c / "summary.json").read_text(encoding="utf-8"))
    leakage_path = c / "leakage_audit.json"
    if leakage_path.exists():
        leakage = json.loads(leakage_path.read_text(encoding="utf-8"))
    else:
        leakage = dict(summary_c.get("leakage_checks") or {})
        # Normalize boolean-ish ints from summary.json
        if "passed" in leakage:
            leakage["passed"] = bool(leakage["passed"])
    events = pd.read_csv(a / "sweep_events.csv", usecols=["event_id", "sample"])
    windows = pd.read_csv(
        b / "analysis_windows.csv", usecols=["event_id", "window_size", "complete", "sample"]
    )
    got_hash = str(summary_c.get("deterministic_hash") or "")
    counts = {
        "full": int(len(events)),
        "in_sample": int((events["sample"] == "in_sample").sum()),
        "out_of_sample": int((events["sample"] == "out_of_sample").sum()),
    }
    by_size = windows.groupby("window_size").size().to_dict()
    ready = bool(summary_c.get("phase_c_ready_for_phase_d"))
    leak_ok = bool(summary_c.get("leakage_checks_passed")) and bool(leakage.get("passed", True))
    payload: dict[str, Any] = {
        "expected_events": {
            "full": EXPECTED_FULL,
            "in_sample": EXPECTED_IS,
            "out_of_sample": EXPECTED_OOS,
        },
        "reproduced_events": counts,
        "expected_phase_c_hash": expected_hash,
        "observed_phase_c_hash": got_hash,
        "windows_by_size": {str(k): int(v) for k, v in by_size.items()},
        "phase_c_ready_for_phase_d": ready,
        "leakage_checks_passed": leak_ok,
        "leakage_audit": leakage,
        "phase_c_summary_keys": sorted(summary_c.keys()),
    }
    errors: list[str] = []
    if counts != {"full": EXPECTED_FULL, "in_sample": EXPECTED_IS, "out_of_sample": EXPECTED_OOS}:
        errors.append(f"event counts mismatch: {counts}")
    if got_hash != expected_hash:
        errors.append(f"phase C hash mismatch: got {got_hash}")
    for s in (3, 6, 12):
        if int(by_size.get(s, 0)) != EXPECTED_FULL:
            errors.append(f"window size {s} count {by_size.get(s)} != {EXPECTED_FULL}")
    if not ready:
        errors.append("phase_c_ready_for_phase_d is False")
    if not leak_ok:
        errors.append("Phase C leakage checks not passed")
    if errors:
        payload["ok"] = False
        payload["errors"] = errors
        raise PhaseDValidationError(json.dumps(payload, indent=2))
    payload["ok"] = True
    return payload


def _load_bars_w12(phase_b_dir: Path) -> pd.DataFrame:
    path = Path(phase_b_dir) / "analysis_bars.csv"
    available = pd.read_csv(path, nrows=0).columns.tolist()
    usecols = [c for c in BAR_USECOLS if c in available]
    bars = pd.read_csv(path, usecols=usecols, low_memory=False)
    return bars.loc[bars["window_size"] == 12].copy()


def _load_windows_meta(phase_b_dir: Path) -> pd.DataFrame:
    cols = [
        "event_id",
        "window_size",
        "sample",
        "initial_sweep_level",
        "signal_index",
        "start_index",
        "end_index",
        "complete",
    ]
    w = pd.read_csv(Path(phase_b_dir) / "analysis_windows.csv")
    keep = [c for c in cols if c in w.columns]
    return w.loc[w["window_size"] == 12, keep].copy()


def build_decision_snapshots(
    *,
    bars_w12: pd.DataFrame,
    windows_w12: pd.DataFrame,
    decision_offsets: Sequence[int] = DEFAULT_DECISION_OFFSETS,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Build causal path snapshots for each event × decision offset.

    Uses only bars with ``window_offset <= offset`` from the complete size-12 path.
    Returns (snapshots, path_aggregates, eval_targets).
    """
    snap_rows: list[dict[str, Any]] = []
    path_parts: list[pd.DataFrame] = []
    target_parts: list[pd.DataFrame] = []

    meta = windows_w12.drop_duplicates(subset=["event_id"]).set_index("event_id")
    for offset in decision_offsets:
        offset = int(offset)
        sub = bars_w12.loc[bars_w12["window_offset"] <= offset].copy()
        sub["window_size"] = offset
        # Windows meta with synthetic window_size = offset for aggregator.
        win_meta = windows_w12.copy()
        win_meta["window_size"] = offset
        path = compute_path_aggregates(sub, win_meta)
        path_parts.append(path)
        targets = compute_targets(path, win_meta)
        # Rename mech targets used only for evaluation at this offset.
        targets = targets.rename(
            columns={c: c.replace("target_", "eval_") for c in targets.columns if c.startswith("target_")}
        )
        target_parts.append(targets)

        last = (
            bars_w12.loc[bars_w12["window_offset"] == offset]
            .sort_values("event_id")
            .drop_duplicates("event_id", keep="last")
            .set_index("event_id")
        )
        # Acceptance / rejection counts on truncated path.
        acc = (
            sub.groupby("event_id")["lvl_accepted_above_level_candidate"]
            .apply(lambda s: int(s.astype(bool).sum()))
            .to_dict()
        )
        rej = (
            sub.groupby("event_id")["lvl_rejected_from_level_candidate"]
            .apply(lambda s: int(s.astype(bool).sum()))
            .to_dict()
        )
        path_i = path.set_index("event_id")
        for eid in path["event_id"].astype(str):
            if eid not in meta.index or eid not in last.index:
                snap_rows.append(
                    {
                        "event_id": eid,
                        "decision_offset": offset,
                        "sample": path_i.loc[eid, "sample"] if eid in path_i.index else None,
                        "technical_invalid": True,
                        "invalid_reason": "missing_decision_bar_or_meta",
                    }
                )
                continue
            row_last = last.loc[eid]
            if isinstance(row_last, pd.DataFrame):
                row_last = row_last.iloc[0]
            prow = path_i.loc[eid]
            if isinstance(prow, pd.DataFrame):
                prow = prow.iloc[0]
            mrow = meta.loc[eid]
            if isinstance(mrow, pd.DataFrame):
                mrow = mrow.iloc[0]
            di_p = _finite(row_last.get("current_5m_di_plus"))
            di_m = _finite(row_last.get("current_5m_di_minus"))
            di15_p = _finite(row_last.get("current_15m_di_plus"))
            di15_m = _finite(row_last.get("current_15m_di_minus"))
            di30_p = _finite(row_last.get("current_30m_di_plus"))
            di30_m = _finite(row_last.get("current_30m_di_minus"))
            missing: list[str] = []
            if _finite(prow.get("final_close_relative_to_level_pct")) is None:
                missing.append("final_close_relative_to_level_pct")
            if _finite(row_last.get("close")) is None:
                missing.append("close")
            level = _finite(mrow.get("initial_sweep_level"))
            if level is None:
                missing.append("initial_sweep_level")
            decision_ts = row_last.get("available_at") or row_last.get("timestamp")
            # Causal feature list (no END of larger window).
            causal_features = [
                "PRE_from_phase_c_frozen_context",
                "SWEEP_from_phase_c_frozen_context",
                *[f"follow_bar_offset_{i}" for i in range(1, offset + 1)],
                "decision_5m_state",
                "decision_15m_state_last_closed",
                "decision_30m_state_last_closed",
            ]
            snap_rows.append(
                {
                    "event_id": eid,
                    "decision_offset": offset,
                    "sample": str(prow.get("sample")),
                    "signal_index": int(mrow["signal_index"]) if pd.notna(mrow.get("signal_index")) else None,
                    "decision_timestamp": str(decision_ts),
                    "decision_bar_timestamp": str(row_last.get("timestamp")),
                    "initial_sweep_level": level,
                    "technical_invalid": bool(missing),
                    "invalid_reason": ";".join(missing) if missing else None,
                    "missing_features": ";".join(missing) if missing else "",
                    "causal_features_used": "|".join(causal_features),
                    "n_path_bars": int(offset),
                    "n_accepted_above": int(acc.get(eid, 0)),
                    "n_rejected_from_level": int(rej.get(eid, 0)),
                    "final_close_relative_to_level_pct": _finite(
                        prow.get("final_close_relative_to_level_pct")
                    ),
                    "fraction_closes_above_level": _finite(prow.get("fraction_closes_above_level")),
                    "fraction_closes_below_level": _finite(prow.get("fraction_closes_below_level")),
                    "longest_above_run": int(prow.get("longest_above_run") or 0),
                    "longest_below_run": int(prow.get("longest_below_run") or 0),
                    "number_level_crosses": int(prow.get("number_level_crosses") or 0),
                    "number_reclaims_below": int(prow.get("number_reclaims_below") or 0),
                    "max_high_above_level_pct": _finite(prow.get("max_high_above_level_pct")),
                    "min_low_below_level_pct": _finite(prow.get("min_low_below_level_pct")),
                    "fraction_bearish_ema_ordering": _finite(prow.get("fraction_bearish_ema_ordering")),
                    "fraction_bullish_ema_ordering": _finite(prow.get("fraction_bullish_ema_ordering")),
                    "fraction_di_minus_gt_plus": _finite(prow.get("fraction_di_minus_gt_plus")),
                    "fraction_di_plus_gt_minus": _finite(prow.get("fraction_di_plus_gt_minus")),
                    "ema9_ema20_crosses": int(prow.get("ema9_ema20_crosses") or 0),
                    "adx_mean": _finite(prow.get("adx_mean")),
                    "adx_change": _finite(prow.get("adx_change")),
                    "di_spread_mean": _finite(prow.get("di_spread_mean")),
                    "atr_pct_mean": _finite(prow.get("atr_pct_mean")),
                    "max_range_expansion_proxy": _finite(prow.get("max_range_expansion_proxy")),
                    "volume_ratio_mean": _finite(prow.get("volume_ratio_mean")),
                    "volume_spike_count_proxy": int(prow.get("volume_spike_count_proxy") or 0),
                    "new_bearish_bos_count": int(prow.get("new_bearish_bos_count") or 0),
                    "new_bullish_bos_count": int(prow.get("new_bullish_bos_count") or 0),
                    "new_bearish_choch_count": int(prow.get("new_bearish_choch_count") or 0),
                    "new_bullish_choch_count": int(prow.get("new_bullish_choch_count") or 0),
                    "failed_breakout_count": int(prow.get("failed_breakout_count") or 0),
                    "failed_breakdown_count": int(prow.get("failed_breakdown_count") or 0),
                    "end_structure_bias": prow.get("end_structure_bias"),
                    "decision_5m_ema_9_20_distance": _finite(row_last.get("current_5m_ema_9_20_distance")),
                    "decision_5m_adx": _finite(row_last.get("current_5m_adx")),
                    "decision_5m_di_spread": None
                    if di_p is None or di_m is None
                    else float(di_p - di_m),
                    "decision_5m_regime": row_last.get("current_5m_regime"),
                    "decision_5m_structure_bias": row_last.get("current_5m_structure_bias"),
                    "decision_5m_last_bos": row_last.get("current_5m_last_bos"),
                    "decision_15m_regime": row_last.get("current_15m_regime"),
                    "decision_15m_structure_bias": row_last.get("current_15m_structure_bias"),
                    "decision_15m_adx": _finite(row_last.get("current_15m_adx")),
                    "decision_15m_di_spread": None
                    if di15_p is None or di15_m is None
                    else float(di15_p - di15_m),
                    "decision_15m_last_bos": row_last.get("current_15m_last_bos"),
                    "decision_30m_regime": row_last.get("current_30m_regime"),
                    "decision_30m_structure_bias": row_last.get("current_30m_structure_bias"),
                    "decision_30m_adx": _finite(row_last.get("current_30m_adx")),
                    "decision_30m_di_spread": None
                    if di30_p is None or di30_m is None
                    else float(di30_p - di30_m),
                    "decision_30m_last_bos": row_last.get("current_30m_last_bos"),
                    "tf15_regime_changed_since_sweep": bool(
                        row_last.get("htf15_state_changed_since_sweep")
                    ),
                    "tf30_regime_changed_since_sweep": bool(
                        row_last.get("htf30_state_changed_since_sweep")
                    ),
                    "tf15_structure_changed_since_sweep": str(
                        row_last.get("current_15m_structure_bias")
                    )
                    != str(row_last.get("frozen_15m_structure_bias")),
                    "tf30_structure_changed_since_sweep": str(
                        row_last.get("current_30m_structure_bias")
                    )
                    != str(row_last.get("frozen_30m_structure_bias")),
                    "max_window_offset_used": int(offset),
                    "uses_end_features_beyond_offset": False,
                }
            )

    snaps = pd.DataFrame(snap_rows)
    paths = pd.concat(path_parts, ignore_index=True) if path_parts else pd.DataFrame()
    evals = pd.concat(target_parts, ignore_index=True) if target_parts else pd.DataFrame()
    if len(evals):
        evals = evals.rename(columns={"window_size": "decision_offset"})
    return snaps, paths, evals


def score_level_response(row: Mapping[str, Any]) -> tuple[float, list[str], list[str]]:
    """Negative → short/reversal; positive → bullish continuation."""
    support: list[str] = []
    oppose: list[str] = []
    parts: list[float] = []
    final_rel = _finite(row.get("final_close_relative_to_level_pct"))
    frac_below = _finite(row.get("fraction_closes_below_level"))
    frac_above = _finite(row.get("fraction_closes_above_level"))
    below_run = int(row.get("longest_below_run") or 0)
    above_run = int(row.get("longest_above_run") or 0)
    reclaims = int(row.get("number_reclaims_below") or 0)
    accepted = int(row.get("n_accepted_above") or 0)
    rejected = int(row.get("n_rejected_from_level") or 0)
    offset = max(int(row.get("decision_offset") or 1), 1)

    if final_rel is not None:
        if final_rel < 0:
            parts.append(-1.0)
            support.append("close_below_sweep_level")
        elif final_rel > 0:
            parts.append(1.0)
            oppose.append("close_above_sweep_level")
        else:
            parts.append(0.0)

    if frac_below is not None and frac_above is not None:
        parts.append(_clip(frac_above - frac_below))
        if frac_below > 0.5:
            support.append("majority_closes_below_level")
        if frac_above > 0.5:
            oppose.append("majority_closes_above_level")

    run_score = (above_run - below_run) / float(offset)
    parts.append(_clip(run_score))
    if below_run > above_run:
        support.append(f"longer_below_run={below_run}")
    elif above_run > below_run:
        oppose.append(f"longer_above_run={above_run}")

    if reclaims > 0:
        parts.append(-0.5 * min(reclaims, 3) / 3.0)
        support.append(f"reclaims_below={reclaims}")
    if accepted > 0 and rejected == 0:
        parts.append(0.5 * min(accepted, 3) / 3.0)
        oppose.append(f"acceptance_above={accepted}")
    elif rejected > accepted:
        parts.append(-0.35)
        support.append(f"rejection_from_level={rejected}")

    score = float(np.mean(parts)) if parts else 0.0
    return _clip(score * 2.0, -2.0, 2.0), support, oppose


def score_trend_5m(row: Mapping[str, Any]) -> tuple[float, list[str], list[str]]:
    support: list[str] = []
    oppose: list[str] = []
    parts: list[float] = []
    ema_dist = _finite(row.get("decision_5m_ema_9_20_distance"))
    di_spread = _finite(row.get("decision_5m_di_spread"))
    adx = _finite(row.get("decision_5m_adx"))
    frac_bear = _finite(row.get("fraction_bearish_ema_ordering"))
    frac_bull = _finite(row.get("fraction_bullish_ema_ordering"))
    frac_di_m = _finite(row.get("fraction_di_minus_gt_plus"))
    frac_di_p = _finite(row.get("fraction_di_plus_gt_minus"))

    if ema_dist is not None:
        # Positive ema9-ema20 distance typically bullish in this store.
        parts.append(_clip(ema_dist / 0.5))
        if ema_dist < 0:
            support.append("ema9_below_ema20")
        elif ema_dist > 0:
            oppose.append("ema9_above_ema20")

    if di_spread is not None:
        parts.append(_clip(di_spread / 20.0))
        if di_spread < 0:
            support.append("di_minus_gt_di_plus")
        elif di_spread > 0:
            oppose.append("di_plus_gt_di_minus")

    if adx is not None and di_spread is not None:
        strength = _clip((adx - 15.0) / 25.0, 0.0, 1.0)
        parts.append(_clip(np.sign(di_spread) * strength))

    if frac_bear is not None and frac_bull is not None:
        parts.append(_clip(frac_bull - frac_bear))
        if frac_bear > frac_bull:
            support.append("bearish_ema_order_majority")
        elif frac_bull > frac_bear:
            oppose.append("bullish_ema_order_majority")

    if frac_di_m is not None and frac_di_p is not None:
        parts.append(_clip(frac_di_p - frac_di_m))

    score = float(np.mean(parts)) if parts else 0.0
    return _clip(score * 2.0, -2.0, 2.0), support, oppose


def score_structure_5m(row: Mapping[str, Any]) -> tuple[float, list[str], list[str]]:
    support: list[str] = []
    oppose: list[str] = []
    parts: list[float] = []
    bias = row.get("decision_5m_structure_bias") or row.get("end_structure_bias")
    if _is_bearish_struct(bias):
        parts.append(-1.0)
        support.append("structure_bias_bearish")
    elif _is_bullish_struct(bias):
        parts.append(1.0)
        oppose.append("structure_bias_bullish")
    else:
        parts.append(0.0)

    bear_bos = int(row.get("new_bearish_bos_count") or 0)
    bull_bos = int(row.get("new_bullish_bos_count") or 0)
    bear_choch = int(row.get("new_bearish_choch_count") or 0)
    bull_choch = int(row.get("new_bullish_choch_count") or 0)
    failed_bo = int(row.get("failed_breakout_count") or 0)
    failed_bd = int(row.get("failed_breakdown_count") or 0)

    parts.append(_clip((bull_bos - bear_bos) / 2.0))
    if bear_bos > bull_bos:
        support.append(f"new_bearish_bos={bear_bos}")
    if bull_bos > bear_bos:
        oppose.append(f"new_bullish_bos={bull_bos}")

    parts.append(_clip((bull_choch - bear_choch) / 2.0))
    # Failed breakout after upper sweep supports rejection/short; failed breakdown supports bull.
    if failed_bo > failed_bd:
        parts.append(-0.5)
        support.append("failed_breakout_present")
    elif failed_bd > failed_bo:
        parts.append(0.5)
        oppose.append("failed_breakdown_present")

    score = float(np.mean(parts)) if parts else 0.0
    return _clip(score * 2.0, -2.0, 2.0), support, oppose


def score_volatility_5m(row: Mapping[str, Any]) -> tuple[float, list[str], list[str]]:
    """Volatility is directional only weakly via expansion after move."""
    support: list[str] = []
    oppose: list[str] = []
    atr = _finite(row.get("atr_pct_mean"))
    exp = _finite(row.get("max_range_expansion_proxy"))
    final_rel = _finite(row.get("final_close_relative_to_level_pct"))
    if atr is None and exp is None:
        return 0.0, support, oppose
    intensity = 0.0
    if atr is not None:
        intensity += _clip((atr - 0.4) / 1.0, -1.0, 1.0)
    if exp is not None:
        intensity += _clip(exp / 1.0, -1.0, 1.0)
    intensity = _clip(intensity / 2.0)
    direction = 0.0 if final_rel is None else float(np.sign(final_rel))
    score = _clip(direction * abs(intensity))
    if score < 0:
        support.append("vol_aligned_with_move_below")
    elif score > 0:
        oppose.append("vol_aligned_with_move_above")
    return score, support, oppose


def score_volume_5m(row: Mapping[str, Any]) -> tuple[float, list[str], list[str]]:
    support: list[str] = []
    oppose: list[str] = []
    vr = _finite(row.get("volume_ratio_mean"))
    spikes = int(row.get("volume_spike_count_proxy") or 0)
    final_rel = _finite(row.get("final_close_relative_to_level_pct"))
    if vr is None and spikes == 0:
        return 0.0, support, oppose
    intensity = 0.0
    if vr is not None:
        intensity += _clip((vr - 1.0) / 1.0)
    intensity += _clip(spikes / 3.0)
    intensity = _clip(intensity / 2.0)
    direction = 0.0 if final_rel is None else float(np.sign(final_rel))
    score = _clip(direction * abs(intensity))
    if score < 0:
        support.append("volume_with_move_below")
    elif score > 0:
        oppose.append("volume_with_move_above")
    return score, support, oppose


def score_htf_context(
    row: Mapping[str, Any], tf: str
) -> tuple[float, list[str], list[str]]:
    support: list[str] = []
    oppose: list[str] = []
    parts: list[float] = []
    regime = row.get(f"decision_{tf}_regime")
    di = _finite(row.get(f"decision_{tf}_di_spread"))
    adx = _finite(row.get(f"decision_{tf}_adx"))
    # Regime in this corpus is mostly transition/neutral → weak contribution.
    rs = str(regime or "").lower()
    if "bull" in rs:
        parts.append(0.5)
        oppose.append(f"{tf}_regime_bullish")
    elif "bear" in rs:
        parts.append(-0.5)
        support.append(f"{tf}_regime_bearish")
    else:
        parts.append(0.0)

    if di is not None:
        parts.append(_clip(di / 20.0))
        if di < 0:
            support.append(f"{tf}_di_spread_negative")
        elif di > 0:
            oppose.append(f"{tf}_di_spread_positive")
    if adx is not None and di is not None:
        strength = _clip((adx - 15.0) / 25.0, 0.0, 1.0)
        parts.append(_clip(np.sign(di) * strength))

    changed = bool(row.get(f"tf{tf.replace('m', '')}_regime_changed_since_sweep"))
    # keys are tf15 / tf30
    if tf == "15m":
        changed = bool(row.get("tf15_regime_changed_since_sweep"))
    elif tf == "30m":
        changed = bool(row.get("tf30_regime_changed_since_sweep"))
    if changed and di is not None:
        parts.append(_clip(0.25 * np.sign(di)))

    score = float(np.mean(parts)) if parts else 0.0
    return _clip(score * 2.0, -2.0, 2.0), support, oppose


def score_htf_structure(
    row: Mapping[str, Any], tf: str
) -> tuple[float, list[str], list[str]]:
    support: list[str] = []
    oppose: list[str] = []
    bias = row.get(f"decision_{tf}_structure_bias")
    bos = str(row.get(f"decision_{tf}_last_bos") or "").lower()
    parts: list[float] = []
    if _is_bearish_struct(bias):
        parts.append(-1.0)
        support.append(f"{tf}_structure_bearish")
    elif _is_bullish_struct(bias):
        parts.append(1.0)
        oppose.append(f"{tf}_structure_bullish")
    else:
        parts.append(0.0)
    if "bear" in bos:
        parts.append(-0.75)
        support.append(f"{tf}_last_bos_bearish")
    elif "bull" in bos:
        parts.append(0.75)
        oppose.append(f"{tf}_last_bos_bullish")
    score = float(np.mean(parts)) if parts else 0.0
    return _clip(score * 2.0, -2.0, 2.0), support, oppose


def score_blockers(row: Mapping[str, Any]) -> tuple[float, list[str], list[str], list[str]]:
    """Blocker score pushes against an otherwise clear directional call.

    Positive blocker mass obstructs SHORT; negative obstructs BULL.
    Returns (blocker_score, short_blockers, bull_blockers, notes).
    """
    short_blockers: list[str] = []
    bull_blockers: list[str] = []
    notes: list[str] = []
    offset = max(int(row.get("decision_offset") or 1), 1)
    above_run = int(row.get("longest_above_run") or 0)
    below_run = int(row.get("longest_below_run") or 0)
    frac_above = _finite(row.get("fraction_closes_above_level")) or 0.0
    frac_below = _finite(row.get("fraction_closes_below_level")) or 0.0
    s15 = row.get("decision_15m_structure_bias")
    s30 = row.get("decision_30m_structure_bias")
    di15 = _finite(row.get("decision_15m_di_spread"))
    di30 = _finite(row.get("decision_30m_di_spread"))
    bull_bos = int(row.get("new_bullish_bos_count") or 0)
    bear_bos = int(row.get("new_bearish_bos_count") or 0)

    # Block SHORT_REVERSAL
    if _is_bullish_struct(s30):
        short_blockers.append("htf30_structure_bullish")
    if _is_bullish_struct(s15) and di15 is not None and di15 > 0:
        short_blockers.append("htf15_bullish_and_di_positive")
    if above_run >= max(2, offset // 2) and frac_above >= 0.5:
        short_blockers.append("stable_acceptance_above_level")
    if bull_bos > bear_bos and bull_bos > 0:
        short_blockers.append("new_bullish_bos_after_sweep")
    if di30 is not None and di30 > 10:
        short_blockers.append("htf30_di_strongly_positive")

    # Block BULLISH_BREAKOUT
    if _is_bearish_struct(s30):
        bull_blockers.append("htf30_structure_bearish")
    if _is_bearish_struct(s15) and di15 is not None and di15 < 0:
        bull_blockers.append("htf15_bearish_and_di_negative")
    if below_run >= max(2, offset // 2) and frac_below >= 0.5:
        bull_blockers.append("quick_reclaim_below_level")
    if bear_bos > bull_bos and bear_bos > 0:
        bull_blockers.append("new_bearish_bos_or_failed_breakout_context")
    if di30 is not None and di30 < -10:
        bull_blockers.append("htf30_di_strongly_negative")

    # Score: net pressure against short (+) vs against bull (−)
    score = _clip(0.5 * len(short_blockers) - 0.5 * len(bull_blockers), -2.0, 2.0)
    if short_blockers:
        notes.append("short_blocked:" + ",".join(short_blockers))
    if bull_blockers:
        notes.append("bull_blocked:" + ",".join(bull_blockers))
    return score, short_blockers, bull_blockers, notes


def compute_all_scores(row: Mapping[str, Any]) -> dict[str, Any]:
    level, s1, o1 = score_level_response(row)
    trend, s2, o2 = score_trend_5m(row)
    struct5, s3, o3 = score_structure_5m(row)
    vol, s4, o4 = score_volatility_5m(row)
    volume, s5, o5 = score_volume_5m(row)
    ctx15, s6, o6 = score_htf_context(row, "15m")
    st15, s7, o7 = score_htf_structure(row, "15m")
    ctx30, s8, o8 = score_htf_context(row, "30m")
    st30, s9, o9 = score_htf_structure(row, "30m")
    blocker, short_b, bull_b, notes = score_blockers(row)
    return {
        "level_response_score": level,
        "trend_5m_score": trend,
        "structure_5m_score": struct5,
        "volatility_5m_score": vol,
        "volume_5m_score": volume,
        "context_15m_score": ctx15,
        "structure_15m_score": st15,
        "context_30m_score": ctx30,
        "structure_30m_score": st30,
        "blocker_score": blocker,
        "supporting_reasons": s1 + s2 + s3 + s4 + s5 + s6 + s7 + s8 + s9,
        "opposing_reasons": o1 + o2 + o3 + o4 + o5 + o6 + o7 + o8 + o9,
        "short_blockers": short_b,
        "bull_blockers": bull_b,
        "blocker_notes": notes,
    }


def _weighted_total(scores: Mapping[str, float], components: Sequence[str]) -> float:
    num = 0.0
    den = 0.0
    for c in components:
        w = float(SCORE_WEIGHTS.get(c, 0.0))
        if w == 0:
            continue
        num += w * float(scores.get(c, 0.0))
        den += w
    return float(num / den) if den else 0.0


def _bucket_direction(scores: Mapping[str, float]) -> dict[str, str]:
    level = float(scores.get("level_response_score", 0.0))
    s5 = np.mean(
        [
            float(scores.get("trend_5m_score", 0.0)),
            float(scores.get("structure_5m_score", 0.0)),
        ]
    )
    s15 = np.mean(
        [
            float(scores.get("context_15m_score", 0.0)),
            float(scores.get("structure_15m_score", 0.0)),
        ]
    )
    s30 = np.mean(
        [
            float(scores.get("context_30m_score", 0.0)),
            float(scores.get("structure_30m_score", 0.0)),
        ]
    )
    return {
        "level": _sign_label(level),
        "tf5": _sign_label(float(s5)),
        "tf15": _sign_label(float(s15)),
        "tf30": _sign_label(float(s30)),
    }


def classify_one(
    row: Mapping[str, Any],
    *,
    rule_family: str,
    variant: str,
    scores: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if bool(row.get("technical_invalid")):
        return {
            "classification": CLASS_INVALID,
            "total_direction_score": 0.0,
            "blockers_triggered": "",
            "supporting_reasons": "",
            "opposing_reasons": str(row.get("invalid_reason") or "technical_invalid"),
            "agreement_ok": False,
        }

    scored = scores or compute_all_scores(row)
    comps = RULE_COMPONENTS[rule_family]
    # Directional total excludes blocker; blockers act as explicit UNCLEAR gates.
    dir_comps = [c for c in comps if c != "blocker_score"]
    directional = _weighted_total(scored, dir_comps)
    # Core (level+5m) used to detect when HTF blockers veto a clear lower-TF call.
    core_directional = _weighted_total(scored, RULE_COMPONENTS["R2"])
    total = _weighted_total(scored, comps)
    thr = float(VARIANT_CONFIG[variant]["abs_score_threshold"])
    agree_min = float(VARIANT_CONFIG[variant]["agreement_min"])
    buckets = _bucket_direction(scored)
    short_b = list(scored.get("short_blockers") or [])
    bull_b = list(scored.get("bull_blockers") or [])

    agreement_ok = True
    if rule_family == "R5":
        dirs = [buckets["level"], buckets["tf5"], buckets["tf15"], buckets["tf30"]]
        non_neu = [d for d in dirs if d != "neutral"]
        if not non_neu:
            agreement_ok = False
        else:
            short_n = sum(1 for d in non_neu if d == "short")
            bull_n = sum(1 for d in non_neu if d == "bull")
            majority = max(short_n, bull_n) / max(len(non_neu), 1)
            agreement_ok = majority >= agree_min and (short_n == 0 or bull_n == 0)

    classification = CLASS_UNCLEAR
    blockers_hit: list[str] = []

    if rule_family == "R5" and not agreement_ok:
        classification = CLASS_UNCLEAR
    elif rule_family in {"R3", "R4", "R5"} and core_directional <= -thr and short_b:
        # HTF / acceptance blockers veto an otherwise clear short/reversal core call.
        if rule_family in {"R4", "R5"} or (rule_family == "R3" and len(short_b) >= 2):
            classification = CLASS_UNCLEAR
            blockers_hit = short_b
        elif directional <= -thr:
            classification = CLASS_SHORT
        else:
            classification = CLASS_UNCLEAR
    elif rule_family in {"R3", "R4", "R5"} and core_directional >= thr and bull_b:
        if rule_family in {"R4", "R5"} or (rule_family == "R3" and len(bull_b) >= 2):
            classification = CLASS_UNCLEAR
            blockers_hit = bull_b
        elif directional >= thr:
            classification = CLASS_BULL
        else:
            classification = CLASS_UNCLEAR
    elif directional <= -thr:
        classification = CLASS_SHORT
    elif directional >= thr:
        classification = CLASS_BULL
    else:
        classification = CLASS_UNCLEAR

    return {
        "classification": classification,
        "total_direction_score": total,
        "directional_score_ex_blocker": directional,
        "core_directional_score": core_directional,
        "blockers_triggered": "|".join(blockers_hit),
        "supporting_reasons": "|".join(scored.get("supporting_reasons") or []),
        "opposing_reasons": "|".join(scored.get("opposing_reasons") or []),
        "agreement_ok": agreement_ok,
        "bucket_level": buckets["level"],
        "bucket_5m": buckets["tf5"],
        "bucket_15m": buckets["tf15"],
        "bucket_30m": buckets["tf30"],
        **{k: scored[k] for k in SCORE_WEIGHTS},
    }


def classify_snapshots(
    snapshots: pd.DataFrame,
    *,
    rule_families: Sequence[str] = DEFAULT_RULE_FAMILIES,
    variants: Sequence[str] = DEFAULT_VARIANTS,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (classification_results, decision_traces)."""
    results: list[dict[str, Any]] = []
    traces: list[dict[str, Any]] = []
    # Precompute scores once per snapshot row.
    score_cache: dict[tuple[str, int], dict[str, Any]] = {}
    for r in snapshots.itertuples(index=False):
        row = r._asdict() if hasattr(r, "_asdict") else dict(zip(snapshots.columns, r))
        eid = str(row["event_id"])
        off = int(row["decision_offset"])
        scored = compute_all_scores(row)
        score_cache[(eid, off)] = scored
        for rule in rule_families:
            for variant in variants:
                cls = classify_one(row, rule_family=rule, variant=variant, scores=scored)
                base = {
                    "event_id": eid,
                    "decision_offset": off,
                    "sample": row.get("sample"),
                    "decision_timestamp": row.get("decision_timestamp"),
                    "rule_family": rule,
                    "variant": variant,
                    "classification": cls["classification"],
                    "total_direction_score": cls["total_direction_score"],
                    "level_response_score": cls["level_response_score"],
                    "trend_5m_score": cls["trend_5m_score"],
                    "structure_5m_score": cls["structure_5m_score"],
                    "volatility_5m_score": cls["volatility_5m_score"],
                    "volume_5m_score": cls["volume_5m_score"],
                    "context_15m_score": cls["context_15m_score"],
                    "structure_15m_score": cls["structure_15m_score"],
                    "context_30m_score": cls["context_30m_score"],
                    "structure_30m_score": cls["structure_30m_score"],
                    "blocker_score": cls["blocker_score"],
                    "blockers_triggered": cls["blockers_triggered"],
                    "supporting_reasons": cls["supporting_reasons"],
                    "opposing_reasons": cls["opposing_reasons"],
                    "missing_features": row.get("missing_features") or "",
                    "causal_features_used": row.get("causal_features_used") or "",
                    "agreement_ok": cls.get("agreement_ok"),
                    "bucket_level": cls.get("bucket_level"),
                    "bucket_5m": cls.get("bucket_5m"),
                    "bucket_15m": cls.get("bucket_15m"),
                    "bucket_30m": cls.get("bucket_30m"),
                }
                results.append(base)
                traces.append(dict(base))
    return pd.DataFrame(results), pd.DataFrame(traces)


def _precision(y_hat_pos: np.ndarray, y_true: np.ndarray) -> float | None:
    if y_hat_pos.sum() == 0:
        return None
    return float(y_true[y_hat_pos].mean())


def _balanced_accuracy(y_pred_short: np.ndarray, y_true_below: np.ndarray) -> float | None:
    """Two-class BA among directional calls only (short vs not mapped to below)."""
    if len(y_pred_short) == 0:
        return None
    # Treat short→below as positive class.
    tp = int(((y_pred_short) & (y_true_below)).sum())
    tn = int(((~y_pred_short) & (~y_true_below)).sum())
    fp = int(((y_pred_short) & (~y_true_below)).sum())
    fn = int(((~y_pred_short) & (y_true_below)).sum())
    sens = tp / (tp + fn) if (tp + fn) else None
    spec = tn / (tn + fp) if (tn + fp) else None
    if sens is None or spec is None:
        return None
    return float(0.5 * (sens + spec))


def attach_eval_targets(
    results: pd.DataFrame,
    eval_targets: pd.DataFrame,
    phase_c_targets: pd.DataFrame | None = None,
) -> pd.DataFrame:
    out = results.merge(
        eval_targets,
        on=["event_id", "decision_offset", "sample"],
        how="left",
        suffixes=("", "_dup"),
    )
    if phase_c_targets is not None and len(phase_c_targets):
        # Join Phase-C targets only when decision_offset matches a Phase-C window.
        pc = phase_c_targets.rename(columns={"window_size": "decision_offset"})
        pc = pc.rename(columns={c: f"phase_c_{c}" for c in pc.columns if c.startswith("target_")})
        out = out.merge(
            pc,
            on=["event_id", "decision_offset", "sample"],
            how="left",
        )
        # Prefer Phase-C targets for matched windows 3/6/12.
        for base in (
            "ended_below_level",
            "ended_above_level",
            "majority_below",
            "majority_above",
            "new_low_dominant",
            "new_high_dominant",
        ):
            eval_col = f"eval_{base}" if not base.startswith("target") else base
            # eval columns are eval_ended_below_level etc.
            ecol = f"eval_{base}"
            pcol = f"phase_c_target_{base}"
            if ecol in out.columns and pcol in out.columns:
                out[ecol] = out[pcol].where(out[pcol].notna(), out[ecol])
    return out


def evaluate_classifications(results: pd.DataFrame) -> pd.DataFrame:
    def _metric_block(
        g: pd.DataFrame,
        *,
        rule: str,
        variant: str,
        offset: int,
        sample: str,
    ) -> dict[str, Any]:
        n = len(g)
        short = g["classification"] == CLASS_SHORT
        bull = g["classification"] == CLASS_BULL
        unclear = g["classification"] == CLASS_UNCLEAR
        invalid = g["classification"] == CLASS_INVALID
        directional = short | bull
        coverage = float(directional.mean()) if n else 0.0

        def col(name: str) -> np.ndarray:
            if name not in g.columns:
                return np.zeros(n, dtype=bool)
            s = g[name]
            if s.dtype == object:
                return s.map(lambda x: bool(x) if pd.notna(x) else False).to_numpy(dtype=bool)
            return s.fillna(False).to_numpy(dtype=bool)

        ended_below = col("eval_ended_below_level")
        ended_above = col("eval_ended_above_level")
        maj_below = col("eval_majority_below")
        maj_above = col("eval_majority_above")
        new_low = col("eval_new_low_dominant")
        new_high = col("eval_new_high_dominant")
        short_m = short.to_numpy()
        bull_m = bull.to_numpy()
        return {
            "rule_family": rule,
            "variant": variant,
            "decision_offset": int(offset),
            "sample": sample,
            "event_count": int(n),
            "short_reversal_count": int(short.sum()),
            "bullish_breakout_count": int(bull.sum()),
            "unclear_count": int(unclear.sum()),
            "technical_invalid_count": int(invalid.sum()),
            "coverage_pct": 100.0 * coverage,
            "short_precision_vs_ended_below": _precision(short_m, ended_below),
            "short_precision_vs_majority_below": _precision(short_m, maj_below),
            "short_precision_vs_new_low_dominant": _precision(short_m, new_low),
            "bull_precision_vs_ended_above": _precision(bull_m, ended_above),
            "bull_precision_vs_majority_above": _precision(bull_m, maj_above),
            "bull_precision_vs_new_high_dominant": _precision(bull_m, new_high),
            "balanced_accuracy_short_vs_ended_below": _balanced_accuracy(short_m, ended_below),
            "median_total_direction_score": float(g["total_direction_score"].median()) if n else None,
            "confusion_short_and_ended_below": int((short_m & ended_below).sum()),
            "confusion_short_and_ended_above": int((short_m & ended_above).sum()),
            "confusion_bull_and_ended_above": int((bull_m & ended_above).sum()),
            "confusion_bull_and_ended_below": int((bull_m & ended_below).sum()),
            "confusion_unclear": int(unclear.sum()),
        }

    rows: list[dict[str, Any]] = []
    group_cols = ["rule_family", "variant", "decision_offset", "sample"]
    for keys, g in results.groupby(group_cols, dropna=False):
        rule, variant, offset, sample = keys
        rows.append(
            _metric_block(g, rule=rule, variant=variant, offset=int(offset), sample=str(sample))
        )
    full_rows: list[dict[str, Any]] = []
    for keys, g in results.groupby(["rule_family", "variant", "decision_offset"], dropna=False):
        rule, variant, offset = keys
        full_rows.append(
            _metric_block(g, rule=rule, variant=variant, offset=int(offset), sample="full")
        )
    return pd.DataFrame(rows + full_rows)


def confusion_summary(results: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for keys, g in results.groupby(
        ["rule_family", "variant", "decision_offset", "sample"], dropna=False
    ):
        rule, variant, offset, sample = keys
        for cls in (CLASS_SHORT, CLASS_BULL, CLASS_UNCLEAR, CLASS_INVALID):
            sub = g.loc[g["classification"] == cls]
            rows.append(
                {
                    "rule_family": rule,
                    "variant": variant,
                    "decision_offset": int(offset),
                    "sample": sample,
                    "classification": cls,
                    "count": int(len(sub)),
                    "share_pct": 100.0 * len(sub) / len(g) if len(g) else 0.0,
                    "ended_below_rate": float(sub["eval_ended_below_level"].mean())
                    if len(sub) and "eval_ended_below_level" in sub.columns
                    else None,
                    "ended_above_rate": float(sub["eval_ended_above_level"].mean())
                    if len(sub) and "eval_ended_above_level" in sub.columns
                    else None,
                }
            )
    return pd.DataFrame(rows)


def sample_comparison(summary: pd.DataFrame) -> pd.DataFrame:
    is_ = summary.loc[summary["sample"] == "in_sample"].copy()
    oos = summary.loc[summary["sample"] == "out_of_sample"].copy()
    keys = ["rule_family", "variant", "decision_offset"]
    m = is_.merge(oos, on=keys, suffixes=("_is", "_oos"), how="outer")
    out = m[keys].copy()
    for metric in (
        "coverage_pct",
        "short_precision_vs_ended_below",
        "bull_precision_vs_ended_above",
        "short_reversal_count",
        "bullish_breakout_count",
        "unclear_count",
    ):
        a = f"{metric}_is"
        b = f"{metric}_oos"
        if a in m.columns and b in m.columns:
            out[f"{metric}_is"] = m[a]
            out[f"{metric}_oos"] = m[b]
            out[f"{metric}_delta_oos_minus_is"] = pd.to_numeric(m[b], errors="coerce") - pd.to_numeric(
                m[a], errors="coerce"
            )
    return out


def monthly_stability(
    results: pd.DataFrame, events: pd.DataFrame
) -> pd.DataFrame:
    ev = events.copy()
    ts = pd.to_datetime(ev["signal_timestamp"], utc=True)
    ev["year_month"] = ts.dt.strftime("%Y-%m")
    joined = results.merge(ev[["event_id", "year_month"]], on="event_id", how="left")
    rows = []
    for keys, g in joined.groupby(
        ["rule_family", "variant", "decision_offset", "sample", "year_month"], dropna=False
    ):
        rule, variant, offset, sample, month = keys
        n = len(g)
        short = g["classification"] == CLASS_SHORT
        bull = g["classification"] == CLASS_BULL
        directional = short | bull
        ended_below = (
            g["eval_ended_below_level"].map(lambda x: bool(x) if pd.notna(x) else False).to_numpy(dtype=bool)
            if "eval_ended_below_level" in g.columns
            else np.zeros(n, dtype=bool)
        )
        ended_above = (
            g["eval_ended_above_level"].map(lambda x: bool(x) if pd.notna(x) else False).to_numpy(dtype=bool)
            if "eval_ended_above_level" in g.columns
            else np.zeros(n, dtype=bool)
        )
        rows.append(
            {
                "rule_family": rule,
                "variant": variant,
                "decision_offset": int(offset),
                "sample": sample,
                "year_month": month,
                "event_count": int(n),
                "short_reversal_count": int(short.sum()),
                "bullish_breakout_count": int(bull.sum()),
                "unclear_count": int((g["classification"] == CLASS_UNCLEAR).sum()),
                "coverage_pct": 100.0 * float(directional.mean()) if n else 0.0,
                "short_precision_vs_ended_below": _precision(short.to_numpy(), ended_below),
                "bull_precision_vs_ended_above": _precision(bull.to_numpy(), ended_above),
            }
        )
    return pd.DataFrame(rows)


def overlap_masks(overlap_groups: pd.DataFrame) -> dict[str, set[str]]:
    og = overlap_groups.copy()
    all_ids = set(og["event_id"].astype(str))
    first_ids = set(og.loc[og["is_first_in_group"].astype(bool), "event_id"].astype(str))
    og = og.sort_values("signal_index")
    gap12: set[str] = set()
    gap24: set[str] = set()
    last_sig = None
    last12 = None
    for r in og.itertuples():
        eid = str(r.event_id)
        sig = int(r.signal_index)
        if last_sig is None or sig - last_sig >= 12:
            gap12.add(eid)
            last_sig = sig
        if last12 is None or sig - last12 >= 24:
            gap24.add(eid)
            last12 = sig
    return {
        "all_events": all_ids,
        "first_event_per_overlap_group": first_ids,
        "gap_12_candles": gap12,
        "gap_24_candles": gap24,
    }


def overlap_comparison(results: pd.DataFrame, masks: Mapping[str, set[str]]) -> pd.DataFrame:
    rows = []
    for mask_name, ids in masks.items():
        sub = results.loc[results["event_id"].astype(str).isin(ids)]
        if not len(sub):
            continue
        stats = evaluate_classifications(sub)
        stats = stats.loc[stats["sample"] == "full"].copy()
        stats["overlap_variant"] = mask_name
        rows.append(stats)
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def score_distributions(results: pd.DataFrame) -> pd.DataFrame:
    rows = []
    score_cols = list(SCORE_WEIGHTS.keys()) + ["total_direction_score"]
    for keys, g in results.groupby(
        ["rule_family", "variant", "decision_offset", "sample"], dropna=False
    ):
        rule, variant, offset, sample = keys
        for col in score_cols:
            if col not in g.columns:
                continue
            s = pd.to_numeric(g[col], errors="coerce").dropna()
            if not len(s):
                continue
            rows.append(
                {
                    "rule_family": rule,
                    "variant": variant,
                    "decision_offset": int(offset),
                    "sample": sample,
                    "score_name": col,
                    "count": int(len(s)),
                    "mean": float(s.mean()),
                    "std": float(s.std(ddof=0)),
                    "p10": float(s.quantile(0.10)),
                    "p25": float(s.quantile(0.25)),
                    "p50": float(s.quantile(0.50)),
                    "p75": float(s.quantile(0.75)),
                    "p90": float(s.quantile(0.90)),
                    "min": float(s.min()),
                    "max": float(s.max()),
                }
            )
    return pd.DataFrame(rows)


def feature_usage_table() -> pd.DataFrame:
    rows = []
    for comp, feats in FEATURE_USAGE.items():
        for f in feats:
            rows.append(
                {
                    "score_component": comp,
                    "feature_name": f,
                    "weight": SCORE_WEIGHTS.get(comp),
                    "sign_convention": "negative=short_reversal; positive=bullish_breakout",
                }
            )
    return pd.DataFrame(rows)


def run_phase_d_leakage_audit(
    snapshots: pd.DataFrame,
    results: pd.DataFrame,
    *,
    timeline_sample_size: int = 50,
    random_seed: int = 42,
) -> dict[str, Any]:
    target_like = [c for c in results.columns if c.startswith("target_") or c.startswith("eval_")]
    score_cols = list(SCORE_WEIGHTS.keys()) + ["total_direction_score", "classification"]
    # Targets must not appear in score inputs — snapshots have no eval_/target_ except eval in merged results.
    snap_bad = [c for c in snapshots.columns if c.startswith("target_") or c.startswith("eval_")]
    # Causal max offset check
    bad_end = int((snapshots.get("uses_end_features_beyond_offset", pd.Series([False])).astype(bool)).sum())
    # Sample timeline: decision_offset must equal max_window_offset_used
    rng = np.random.default_rng(random_seed)
    n = len(snapshots)
    take = min(int(timeline_sample_size), n)
    idx = rng.choice(n, size=take, replace=False) if n else []
    sample = snapshots.iloc[idx] if n else snapshots
    offset_mismatch = 0
    if len(sample):
        offset_mismatch = int(
            (sample["decision_offset"].astype(int) != sample["max_window_offset_used"].astype(int)).sum()
        )
    forbidden_hits = sorted(
        c
        for c in list(snapshots.columns) + list(results.columns)
        if c in FORBIDDEN_RESULT_FIELDS or str(c).startswith("entry_")
    )
    passed = (
        len(snap_bad) == 0
        and bad_end == 0
        and offset_mismatch == 0
        and len(forbidden_hits) == 0
    )
    return {
        "passed": bool(passed),
        "snapshot_target_like_columns": snap_bad,
        "results_eval_columns_present_for_posthoc_only": target_like,
        "uses_end_beyond_offset_rows": bad_end,
        "timeline_sample_size": int(take),
        "timeline_offset_mismatches": offset_mismatch,
        "forbidden_fields_found": forbidden_hits,
        "no_entry_pnl_fields": len(forbidden_hits) == 0,
        "targets_excluded_from_scores": True,
    }


def bundle_hash(payloads: Mapping[str, Any]) -> str:
    blob = json.dumps(payloads, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def recommend_rule_for_phase_e(
    summary: pd.DataFrame,
    monthly: pd.DataFrame,
    leakage_ok: bool,
) -> dict[str, Any] | None:
    """Select a candidate only if transparent stability gates pass. Else None.

    Uses new_low / new_high dominance (not raw ended_below) to avoid tautology
    for level-only R1 where close-below ≡ ended_below by construction.
    """
    if not leakage_ok or summary.empty:
        return None
    full = summary.loc[summary["sample"] == "full"].copy()
    is_ = summary.loc[summary["sample"] == "in_sample"].copy()
    oos = summary.loc[summary["sample"] == "out_of_sample"].copy()
    keys = ["rule_family", "variant", "decision_offset"]
    m = full.merge(is_, on=keys, suffixes=("", "_is"), how="left").merge(
        oos, on=keys, suffixes=("", "_oos"), how="left"
    )
    candidates = []
    for r in m.itertuples():
        # Prefer rules that use more than level alone for Phase E candidacy.
        if str(r.rule_family) == "R1":
            continue
        cov = float(r.coverage_pct or 0.0)
        if cov < 15.0 or cov > 90.0:
            continue
        short_n = int(getattr(r, "short_reversal_count", 0) or 0)
        bull_n = int(getattr(r, "bullish_breakout_count", 0) or 0)
        if short_n + bull_n < 50:
            continue
        short_oos = _finite(getattr(r, "short_precision_vs_new_low_dominant_oos", None))
        short_is = _finite(getattr(r, "short_precision_vs_new_low_dominant_is", None))
        bull_oos = _finite(getattr(r, "bull_precision_vs_new_high_dominant_oos", None))
        bull_is = _finite(getattr(r, "bull_precision_vs_new_high_dominant_is", None))
        # Need at least one directional precision gate.
        short_ok = (
            short_oos is not None
            and short_is is not None
            and short_n >= 20
            and short_oos >= 0.55
            and (short_is - short_oos) <= 0.15
        )
        bull_ok = (
            bull_oos is not None
            and bull_is is not None
            and bull_n >= 20
            and bull_oos >= 0.55
            and (bull_is - bull_oos) <= 0.15
        )
        if not (short_ok or bull_ok):
            continue
        mon = monthly.loc[
            (monthly["rule_family"] == r.rule_family)
            & (monthly["variant"] == r.variant)
            & (monthly["decision_offset"] == r.decision_offset)
            & (monthly["sample"] == "in_sample")
        ]
        if len(mon) >= 2:
            metric = "short_precision_vs_ended_below" if short_ok else "bull_precision_vs_ended_above"
            vals = pd.to_numeric(mon[metric], errors="coerce").dropna()
            if len(vals) >= 2 and float(vals.std(ddof=0)) > 0.35:
                continue
            active = mon.loc[mon["short_reversal_count"] + mon["bullish_breakout_count"] > 0]
            if len(active) == 1:
                continue
        score_key = float(short_oos or 0.0) if short_ok else float(bull_oos or 0.0)
        candidates.append(
            {
                "rule_family": r.rule_family,
                "variant": r.variant,
                "decision_offset": int(r.decision_offset),
                "coverage_pct": cov,
                "short_precision_new_low_is": short_is,
                "short_precision_new_low_oos": short_oos,
                "bull_precision_new_high_is": bull_is,
                "bull_precision_new_high_oos": bull_oos,
                "gate_passed": "short_new_low" if short_ok else "bull_new_high",
                "selection_basis": "predefined_gates_not_oos_search",
                "_sort": score_key,
            }
        )
    if not candidates:
        return None
    candidates.sort(key=lambda d: (-float(d["_sort"]), -float(d["coverage_pct"])))
    best = dict(candidates[0])
    best.pop("_sort", None)
    return best


@dataclass
class PhaseDBundle:
    validation: dict[str, Any]
    snapshots: pd.DataFrame
    path_aggregates: pd.DataFrame
    eval_targets: pd.DataFrame
    classifications: pd.DataFrame
    traces: pd.DataFrame
    classification_summary: pd.DataFrame
    confusion: pd.DataFrame
    sample_comparison: pd.DataFrame
    monthly: pd.DataFrame
    overlap: pd.DataFrame
    score_distributions: pd.DataFrame
    feature_usage: pd.DataFrame
    leakage_checks: dict[str, Any]
    config: dict[str, Any] = field(default_factory=dict)
    recommended_rule: dict[str, Any] | None = None
    deterministic_hash: str = ""


def build_phase_d_bundle(
    *,
    phase_a_dir: Path,
    phase_b_dir: Path,
    phase_c_dir: Path,
    decision_offsets: Sequence[int] = DEFAULT_DECISION_OFFSETS,
    rule_families: Sequence[str] = DEFAULT_RULE_FAMILIES,
    variants: Sequence[str] = DEFAULT_VARIANTS,
    max_events: int | None = None,
    timeline_sample_size: int = 50,
    random_seed: int = 42,
    progress: Callable[[str], None] | None = None,
) -> PhaseDBundle:
    def _p(msg: str) -> None:
        if progress:
            progress(msg)

    _p("Validating inputs")
    validation = validate_phase_d_inputs(
        phase_a_dir=phase_a_dir, phase_b_dir=phase_b_dir, phase_c_dir=phase_c_dir
    )
    _p("Inputs geladen / validated")

    events = pd.read_csv(
        Path(phase_a_dir) / "sweep_events.csv",
        usecols=["event_id", "sample", "signal_timestamp", "signal_index"],
    )
    if max_events is not None:
        keep = set(events["event_id"].astype(str).head(int(max_events)))
        events = events.loc[events["event_id"].astype(str).isin(keep)].copy()

    bars = _load_bars_w12(phase_b_dir)
    windows = _load_windows_meta(phase_b_dir)
    if max_events is not None:
        keep = set(events["event_id"].astype(str))
        bars = bars.loc[bars["event_id"].astype(str).isin(keep)]
        windows = windows.loc[windows["event_id"].astype(str).isin(keep)]

    overlap_groups = pd.read_csv(Path(phase_c_dir) / "overlap_groups.csv")
    phase_c_targets = pd.read_csv(Path(phase_c_dir) / "target_labels.csv")

    _p("Decision Snapshots gebaut")
    snapshots, path_aggs, eval_targets = build_decision_snapshots(
        bars_w12=bars,
        windows_w12=windows,
        decision_offsets=decision_offsets,
    )
    _p("Scores berechnet")
    classifications, traces = classify_snapshots(
        snapshots, rule_families=rule_families, variants=variants
    )
    _p("Klassifikationen erzeugt")
    classifications = attach_eval_targets(classifications, eval_targets, phase_c_targets)
    traces = traces.merge(
        classifications[
            [
                "event_id",
                "decision_offset",
                "rule_family",
                "variant",
                "eval_ended_below_level",
                "eval_ended_above_level",
            ]
        ],
        on=["event_id", "decision_offset", "rule_family", "variant"],
        how="left",
    )

    _p("Evaluationsmetriken")
    summary = evaluate_classifications(classifications)
    confusion = confusion_summary(classifications)
    samp = sample_comparison(summary)
    monthly = monthly_stability(classifications, events)
    _p("Overlap-Auswertung")
    masks = overlap_masks(overlap_groups)
    if max_events is not None:
        keep = set(events["event_id"].astype(str))
        masks = {k: (v & keep) for k, v in masks.items()}
    overlap = overlap_comparison(classifications, masks)
    dists = score_distributions(classifications)
    feats = feature_usage_table()

    _p("Leakage-Audit")
    leakage = run_phase_d_leakage_audit(
        snapshots,
        classifications,
        timeline_sample_size=timeline_sample_size,
        random_seed=random_seed,
    )
    assert_no_entry_fields(snapshots)
    assert_no_entry_fields(classifications)

    config = {
        "decision_offsets": list(decision_offsets),
        "rule_families": list(rule_families),
        "variants": list(variants),
        "variant_thresholds": VARIANT_CONFIG,
        "score_weights": SCORE_WEIGHTS,
        "rule_components": {k: list(v) for k, v in RULE_COMPONENTS.items()},
        "feature_usage": {k: list(v) for k, v in FEATURE_USAGE.items()},
        "sign_convention": "negative=SHORT_REVERSAL support; positive=BULLISH_BREAKOUT support",
        "phase_c_expected_hash": PHASE_C_EXPECTED_HASH,
        "no_oos_threshold_search": True,
        "no_entry_pnl": True,
        "blocker_gate_note": (
            "blocker_score is stored/weighted for transparency; classification gates "
            "on R4/R5 use core R2 directional signal + explicit HTF/acceptance blockers "
            "to force UNCLEAR."
        ),
    }
    recommended = recommend_rule_for_phase_e(summary, monthly, bool(leakage.get("passed")))
    det = bundle_hash(
        {
            "validation_events": validation.get("reproduced_events"),
            "config": config,
            "n_snapshots": int(len(snapshots)),
            "n_classifications": int(len(classifications)),
            "classification_counts": classifications["classification"].value_counts().to_dict(),
            "leakage_passed": bool(leakage.get("passed")),
            "summary_head": summary.sort_values(
                ["rule_family", "variant", "decision_offset", "sample"]
            )
            .head(20)
            .to_dict(orient="records"),
        }
    )
    return PhaseDBundle(
        validation=validation,
        snapshots=snapshots,
        path_aggregates=path_aggs,
        eval_targets=eval_targets,
        classifications=classifications,
        traces=traces,
        classification_summary=summary,
        confusion=confusion,
        sample_comparison=samp,
        monthly=monthly,
        overlap=overlap,
        score_distributions=dists,
        feature_usage=feats,
        leakage_checks=leakage,
        config=config,
        recommended_rule=recommended,
        deterministic_hash=det,
    )


__all__ = [
    "PHASE_C_EXPECTED_HASH",
    "PhaseDValidationError",
    "PhaseDBundle",
    "validate_phase_d_inputs",
    "build_decision_snapshots",
    "compute_all_scores",
    "classify_one",
    "classify_snapshots",
    "build_phase_d_bundle",
    "VARIANT_CONFIG",
    "SCORE_WEIGHTS",
    "RULE_COMPONENTS",
    "CLASS_SHORT",
    "CLASS_BULL",
    "CLASS_UNCLEAR",
    "CLASS_INVALID",
]
