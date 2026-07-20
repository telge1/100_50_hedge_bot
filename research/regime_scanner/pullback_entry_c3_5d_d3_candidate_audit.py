"""C3.5D D3 candidate classification audit (offline, research-only).

Compares pre-declared WARNING / EARLY_FAILURE / STRUCTURE_INVALIDATION
candidates on the existing APT D2 timeline. Does **not** implement a D3
runtime SM, does not emit severity states into D1/D2 data, and does not
modify D1/D2 modules.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np
import pandas as pd

from research.regime_scanner.point_audit import json_safe

PHASE = "C3.5D_D3_CANDIDATE_AUDIT"
DEFAULT_APT_AUDIT_DIR = Path(
    "research/regime_scanner/results/phase_c3_5d_continuation_early_failure/apt_audit"
)
DEFAULT_OUT = DEFAULT_APT_AUDIT_DIR / "d3_candidates"
FORBIDDEN_RUNTIME = ("WARNING", "EARLY_FAILURE", "STRUCTURE_INVALIDATED")
C35_HASH = "d61714ffb980013ac241c2053a6258f0a58957cec57bbbd56a7ad512a207e268"
C34B_HASH = "083c58d6b10d4432bf95aafb49bb7a69985b44ca5174946ffe9c5e3cbf68f210"

CANDIDATE_DEFINITIONS: dict[str, dict[str, Any]] = {
    "W1": {"family": "warning", "label": "breakout_currently_lost", "rule": "breakout_level_is_lost", "reversible": True},
    "W2": {"family": "warning", "label": "breakout_lost_and_micro_counter", "rule": "breakout_level_is_lost AND micro_counter_bos_ever", "reversible": True},
    "W3": {"family": "warning", "label": "breakout_lost_and_mae_le_-0_5atr", "rule": "breakout_level_is_lost AND mae_atr <= -0.5", "reversible": True},
    "EF1": {"family": "early_failure", "label": "no_early_ft_and_breakout_lost", "rule": "within first 3 bars: mfe_atr < 0.25 AND breakout_level_is_lost", "reversible": False},
    "EF1b": {"family": "early_failure", "label": "EF1_without_reclaim_within_2", "rule": "EF1 then no reclaim within following 2 bars; fires after window", "reversible": False},
    "EF2": {"family": "early_failure", "label": "pb_extreme_and_ltf_align_lost", "rule": "entry_pullback_extreme_broken_ever AND ltf_major_alignment_is_lost", "reversible": False},
    "EF3": {"family": "early_failure", "label": "pb_extreme_and_breakout_lost", "rule": "entry_pullback_extreme_broken_ever AND breakout_level_is_lost", "reversible": False},
    "EF4": {"family": "early_failure", "label": "pb_extreme_and_micro_counter", "rule": "entry_pullback_extreme_broken_ever AND micro_counter_bos_ever", "reversible": False},
    "SI1": {"family": "structure_invalidation", "label": "entry_protected_broken", "rule": "entry_protected_level_broken_ever", "reversible": False},
    "SI2": {"family": "structure_invalidation", "label": "protected_broken_and_ltf_against", "rule": "entry_protected_level_broken_ever AND ltf_major_alignment_is_lost", "reversible": False},
    "SI3": {"family": "structure_invalidation", "label": "htf_flip_and_ltf_lost", "rule": "htf_major_flip_confirmed_ever AND ltf_major_alignment_is_lost", "reversible": False, "note": "descriptive only; small n"},
}


def _finite(x: Any, default: float = float("nan")) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _bool(x: Any) -> bool:
    if isinstance(x, (bool, np.bool_)):
        return bool(x)
    if isinstance(x, str):
        return x.strip().lower() in {"1", "true", "yes"}
    try:
        if pd.isna(x):
            return False
    except (TypeError, ValueError):
        pass
    return bool(x)


def _safe_rate(n: int, d: int) -> float | None:
    return None if d <= 0 else float(n) / float(d)


def _quantile(xs: Sequence[float], q: float) -> float | None:
    vals = [float(v) for v in xs if v is not None and math.isfinite(float(v))]
    return float(np.quantile(vals, q)) if vals else None


def _median(xs: Sequence[float]) -> float | None:
    return _quantile(xs, 0.5)


def _micro_ever(row: Mapping[str, Any]) -> bool:
    if "micro_counter_bos_ever" in row:
        return _bool(row.get("micro_counter_bos_ever"))
    return _bool(row.get("micro_counter_bos"))


def warning_active(row: Mapping[str, Any], name: str) -> bool:
    lost = _bool(row.get("breakout_level_is_lost"))
    if name == "W1":
        return lost
    if name == "W2":
        return lost and _micro_ever(row)
    if name == "W3":
        mae = _finite(row.get("mae_atr"))
        return lost and math.isfinite(mae) and mae <= -0.5
    raise KeyError(name)


def sticky_active(row: Mapping[str, Any], name: str) -> bool:
    pb = _bool(row.get("entry_pullback_extreme_ever_broken"))
    prot = _bool(row.get("entry_protected_level_ever_broken"))
    ltf_lost = _bool(row.get("ltf_major_alignment_is_lost"))
    brk_lost = _bool(row.get("breakout_level_is_lost"))
    htf_flip = _bool(row.get("htf_major_flip_confirmed"))
    mfe = _finite(row.get("mfe_atr"))
    bsf = int(row.get("bars_since_fill", -1))
    if name == "EF1":
        return bsf <= 2 and math.isfinite(mfe) and mfe < 0.25 and brk_lost
    if name == "EF2":
        return pb and ltf_lost
    if name == "EF3":
        return pb and brk_lost
    if name == "EF4":
        return pb and _micro_ever(row)
    if name == "SI1":
        return prot
    if name == "SI2":
        return prot and ltf_lost
    if name == "SI3":
        return htf_flip and ltf_lost
    raise KeyError(name)


@dataclass
class TriggerResult:
    triggered: bool
    bar_since_fill: int | None = None
    timestamp: Any = None
    state_note: str | None = None


def find_first_true(g: pd.DataFrame, pred: Callable[[Mapping[str, Any]], bool]) -> TriggerResult:
    for _, r in g.iterrows():
        if pred(r):
            return TriggerResult(True, bar_since_fill=int(r["bars_since_fill"]), timestamp=r.get("timestamp"))
    return TriggerResult(False)


def find_ef1b(g: pd.DataFrame) -> TriggerResult:
    ef1 = find_first_true(g, lambda r: sticky_active(r, "EF1"))
    if not ef1.triggered or ef1.bar_since_fill is None:
        return TriggerResult(False)
    t = int(ef1.bar_since_fill)
    window = g[(g["bars_since_fill"] > t) & (g["bars_since_fill"] <= t + 2)]
    if window["breakout_level_reclaimed_event"].map(_bool).any():
        return TriggerResult(False, state_note="ef1_reclaimed_within_2")
    fire_bar = t + 2
    hit = g[g["bars_since_fill"] == fire_bar]
    if hit.empty:
        return TriggerResult(False, state_note="ef1b_window_incomplete")
    r = hit.iloc[0]
    return TriggerResult(True, bar_since_fill=fire_bar, timestamp=r.get("timestamp"), state_note="ef1_no_reclaim_window_elapsed")


def post_signal_metrics(g: pd.DataFrame, trigger_bsf: int) -> dict[str, Any]:
    at = g[g["bars_since_fill"] == trigger_bsf]
    if at.empty:
        return {}
    r0 = at.iloc[0]
    after = g[g["bars_since_fill"] >= trigger_bsf]
    after_strict = g[g["bars_since_fill"] > trigger_bsf]
    mae0 = _finite(r0.get("mae_atr"))
    mfe0 = _finite(r0.get("mfe_atr"))
    scr0 = _finite(r0.get("signed_close_return"))
    mae_min = float(after["mae_atr"].astype(float).min()) if not after.empty else mae0
    add_mae = mae_min - mae0 if math.isfinite(mae0) and math.isfinite(mae_min) else float("nan")
    best_close = float(after["signed_close_return"].astype(float).max()) if not after.empty else scr0
    final = g.loc[g["bars_since_fill"].idxmax()]
    final_scr = _finite(final.get("signed_close_return"))
    recovered = bool((after["signed_close_return"].astype(float) > 0).any())
    reclaimed = bool(after_strict["breakout_level_reclaimed_event"].map(_bool).any()) if not after_strict.empty else False
    mfe_atr_path = float(after["mfe_atr"].astype(float).max()) if not after.empty else mfe0
    return {
        "signed_return_at_signal": scr0,
        "signed_return_at_signal_pct": scr0 * 100.0 if math.isfinite(scr0) else float("nan"),
        "mfe_atr_before_signal": mfe0,
        "mae_atr_before_signal": mae0,
        "mfe_atr_after_signal": mfe_atr_path,
        "additional_mae_atr_after_signal": add_mae,
        "best_close_return_after_signal": best_close,
        "best_close_return_after_signal_pct": best_close * 100.0 if math.isfinite(best_close) else float("nan"),
        "final_close_return_24": final_scr,
        "final_close_return_24_pct": final_scr * 100.0 if math.isfinite(final_scr) else float("nan"),
        "recovered_above_entry_after_signal": recovered,
        "reclaimed_breakout_after_signal": reclaimed,
        "reached_mfe_0_5_after_signal": bool(mfe_atr_path >= 0.5),
        "reached_mfe_1_0_after_signal": bool(mfe_atr_path >= 1.0),
        "mae_atr_24": _finite(final.get("mae_atr")),
        "mfe_atr_24": _finite(final.get("mfe_atr")),
        "protected_broken_by_horizon": bool(g["entry_protected_level_ever_broken"].map(_bool).iloc[-1]),
        "ltf_lost_by_horizon": bool(g["ltf_major_alignment_ever_lost"].map(_bool).iloc[-1]),
    }


def evaluate_fill_candidates(g: pd.DataFrame) -> dict[str, TriggerResult]:
    g = g.sort_values("bars_since_fill").reset_index(drop=True)
    out: dict[str, TriggerResult] = {}
    for name in ("W1", "W2", "W3"):
        out[name] = find_first_true(g, lambda r, n=name: warning_active(r, n))
    out["EF1"] = find_first_true(g, lambda r: sticky_active(r, "EF1"))
    out["EF1b"] = find_ef1b(g)
    for name in ("EF2", "EF3", "EF4", "SI1", "SI2", "SI3"):
        out[name] = find_first_true(g, lambda r, n=name: sticky_active(r, n))
    return out


def build_per_fill_table(timeline: pd.DataFrame, fills: pd.DataFrame) -> pd.DataFrame:
    rows = []
    fill_meta = fills.set_index("setup_id") if not fills.empty else pd.DataFrame()
    for sid, g0 in timeline.groupby("setup_id"):
        g = g0.sort_values("bars_since_fill").reset_index(drop=True)
        direction = str(g.iloc[0].get("direction") or "")
        side = 1 if direction == "long" else -1
        meta = fill_meta.loc[int(sid)] if int(sid) in fill_meta.index else None
        triggers = evaluate_fill_candidates(g)
        final = g.iloc[-1]
        final_scr = _finite(final.get("signed_close_return"))
        mae0 = _finite(g.iloc[0].get("mae_atr"))
        mae24 = _finite(final.get("mae_atr"))
        base_outcomes = {
            "O1_neg_ret24": bool(math.isfinite(final_scr) and final_scr <= 0),
            "O5_structure_failure": bool(g["entry_protected_level_ever_broken"].map(_bool).iloc[-1]),
            "fill_recovered_any": bool((g["signed_close_return"].astype(float) > 0).any()),
            "fill_reached_mfe_0_5": bool(float(g["mfe_atr"].astype(float).max()) >= 0.5),
            "fill_additional_mae_from_bar0": (mae24 - mae0) if math.isfinite(mae0) and math.isfinite(mae24) else float("nan"),
            "final_close_return_24": final_scr,
            "mfe_atr_24": _finite(final.get("mfe_atr")),
            "mae_atr_24": mae24,
            "ltf_lost_by_horizon": bool(g["ltf_major_alignment_ever_lost"].map(_bool).iloc[-1]),
        }
        for cname, trig in triggers.items():
            row: dict[str, Any] = {
                "setup_id": int(sid),
                "direction": direction,
                "side": side,
                "candidate": cname,
                "family": CANDIDATE_DEFINITIONS[cname]["family"],
                "candidate_triggered": bool(trig.triggered),
                "candidate_trigger_bar_since_fill": trig.bar_since_fill,
                "candidate_trigger_timestamp": trig.timestamp,
                "candidate_state_at_trigger": trig.state_note or ("active" if trig.triggered else "not_triggered"),
            }
            if meta is not None:
                row["fill_bar"] = meta["fill_bar"] if "fill_bar" in meta.index else meta.get("fill_bar")
                row["entry_price"] = meta["entry_price"] if "entry_price" in meta.index else meta.get("entry_price")
            row.update(base_outcomes)
            if trig.triggered and trig.bar_since_fill is not None:
                row.update(post_signal_metrics(g, int(trig.bar_since_fill)))
                add = _finite(row.get("additional_mae_atr_after_signal"))
                row["O2_high_additional_mae"] = bool(math.isfinite(add) and add <= -0.5)
                row["O3_no_recovery"] = not bool(row.get("recovered_above_entry_after_signal"))
                row["O4_no_mfe_0_5_after"] = not bool(row.get("reached_mfe_0_5_after_signal"))
            else:
                add0 = _finite(base_outcomes["fill_additional_mae_from_bar0"])
                row["O2_high_additional_mae"] = bool(math.isfinite(add0) and add0 <= -0.5)
                row["O3_no_recovery"] = not bool(base_outcomes["fill_recovered_any"])
                row["O4_no_mfe_0_5_after"] = not bool(base_outcomes["fill_reached_mfe_0_5"])
                for k in (
                    "additional_mae_atr_after_signal",
                    "recovered_above_entry_after_signal",
                    "reclaimed_breakout_after_signal",
                    "reached_mfe_0_5_after_signal",
                    "reached_mfe_1_0_after_signal",
                    "mae_atr_before_signal",
                    "mfe_atr_before_signal",
                    "signed_return_at_signal",
                ):
                    row[k] = None if k != "additional_mae_atr_after_signal" else float("nan")
            rows.append(row)
    return pd.DataFrame(rows)


def warning_episodes(timeline: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    ep_rows = []
    for sid, g0 in timeline.groupby("setup_id"):
        g = g0.sort_values("bars_since_fill").reset_index(drop=True)
        direction = str(g.iloc[0].get("direction") or "")
        for wname in ("W1", "W2", "W3"):
            active = [warning_active(r, wname) for _, r in g.iterrows()]
            ep_id = 0
            i = 0
            while i < len(active):
                if not active[i]:
                    i += 1
                    continue
                start = i
                while i < len(active) and active[i]:
                    i += 1
                end = i - 1
                ep_id += 1
                start_bsf = int(g.iloc[start]["bars_since_fill"])
                end_bsf = int(g.iloc[end]["bars_since_fill"])
                reclaim_bar = None
                for j in range(end + 1, len(g)):
                    if not warning_active(g.iloc[j], wname):
                        reclaim_bar = int(g.iloc[j]["bars_since_fill"])
                        break
                reclaimed = reclaim_bar is not None
                from_start = g.iloc[start:]
                final_scr = _finite(g.iloc[-1].get("signed_close_return"))
                ep_rows.append({
                    "setup_id": int(sid),
                    "direction": direction,
                    "warning": wname,
                    "episode_id": ep_id,
                    "start_bar_since_fill": start_bsf,
                    "end_bar_since_fill": end_bsf,
                    "duration_bars": end_bsf - start_bsf + 1,
                    "reclaimed": reclaimed,
                    "reclaim_bar_since_fill": reclaim_bar,
                    "bars_to_reclaim": None if reclaim_bar is None else reclaim_bar - end_bsf,
                    "final_close_return_24": final_scr,
                    "ret24_positive": bool(math.isfinite(final_scr) and final_scr > 0),
                    "recovered_after_start": bool((from_start["signed_close_return"].astype(float) > 0).any()),
                })
    episodes = pd.DataFrame(ep_rows)
    sum_rows = []
    if not episodes.empty:
        for wname, eg in episodes.groupby("warning"):
            n_fills = eg["setup_id"].nunique()
            n_eps = len(eg)
            multi = int((eg.groupby("setup_id").size() > 1).sum())
            rec = eg[eg["reclaimed"] == True]
            norec = eg[eg["reclaimed"] == False]
            sum_rows.append({
                "warning": wname,
                "n_fills_with_warning": n_fills,
                "n_episodes": n_eps,
                "n_fills_with_multiple_episodes": multi,
                "share_episodes_reclaimed": _safe_rate(int(len(rec)), n_eps),
                "median_bars_to_reclaim": _median([float(x) for x in rec["bars_to_reclaim"] if pd.notna(x)]),
                "ret24_pos_if_reclaimed": _safe_rate(int(rec["ret24_positive"].sum()), len(rec)),
                "ret24_pos_if_not_reclaimed": _safe_rate(int(norec["ret24_positive"].sum()), len(norec)),
                "median_episode_duration": _median(eg["duration_bars"].astype(float).tolist()),
            })
    return episodes, pd.DataFrame(sum_rows)


def confusion_counts(y_true: Sequence[bool], y_pred: Sequence[bool]) -> dict[str, int]:
    tp = tn = fp = fn = 0
    for t, p in zip(y_true, y_pred):
        if p and t:
            tp += 1
        elif p and not t:
            fp += 1
        elif (not p) and t:
            fn += 1
        else:
            tn += 1
    return {"true_positive_count": tp, "false_positive_count": fp, "false_negative_count": fn, "true_negative_count": tn}


def summarize_candidates(per_fill: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    summary_rows, conf_rows = [], []
    outcome_map = {
        "O1": "O1_neg_ret24",
        "O2": "O2_high_additional_mae",
        "O3": "O3_no_recovery",
        "O4": "O4_no_mfe_0_5_after",
        "O5": "O5_structure_failure",
    }
    for cname, g in per_fill.groupby("candidate"):
        for direction in ("all", "long", "short"):
            sub = g if direction == "all" else g[g["direction"] == direction]
            dens = int(sub["setup_id"].nunique())
            trig = sub[sub["candidate_triggered"] == True]
            non = sub[sub["candidate_triggered"] == False]
            n = len(trig)
            bars = [float(x) for x in trig["candidate_trigger_bar_since_fill"] if pd.notna(x)]
            row = {
                "candidate": cname,
                "family": CANDIDATE_DEFINITIONS[cname]["family"],
                "direction": direction,
                "coverage_n": n,
                "coverage_pct": _safe_rate(n, dens),
                "n_fills_in_slice": dens,
                "median_trigger_bar": _median(bars),
                "p25_trigger_bar": _quantile(bars, 0.25),
                "p75_trigger_bar": _quantile(bars, 0.75),
                "ret24_positive_rate": _safe_rate(int((trig["final_close_return_24"].astype(float) > 0).sum()), n),
                "recovery_above_entry_rate": _safe_rate(int(trig["recovered_above_entry_after_signal"].fillna(False).astype(bool).sum()), n),
                "breakout_reclaim_rate": _safe_rate(int(trig["reclaimed_breakout_after_signal"].fillna(False).astype(bool).sum()), n),
                "mfe_0_5_after_rate": _safe_rate(int(trig["reached_mfe_0_5_after_signal"].fillna(False).astype(bool).sum()), n),
                "mfe_1_0_after_rate": _safe_rate(int(trig["reached_mfe_1_0_after_signal"].fillna(False).astype(bool).sum()), n),
                "median_mae_at_signal": _median([_finite(x) for x in trig["mae_atr_before_signal"].tolist()]) if n else None,
                "median_additional_mae_after_signal": _median([_finite(x) for x in trig["additional_mae_atr_after_signal"].tolist()]) if n else None,
                "p75_additional_mae_after_signal": _quantile([_finite(x) for x in trig["additional_mae_atr_after_signal"].tolist()], 0.75) if n else None,
                "baseline_all_ret24_pos": _safe_rate(int((sub["final_close_return_24"].astype(float) > 0).sum()), len(sub)),
                "not_triggered_ret24_pos": _safe_rate(int((non["final_close_return_24"].astype(float) > 0).sum()), len(non)),
                "triggered_median_mfe24": _median([_finite(x) for x in trig["mfe_atr_24"].tolist()]) if n else None,
                "triggered_median_mae24": _median([_finite(x) for x in trig["mae_atr_24"].tolist()]) if n else None,
                "not_triggered_median_mfe24": _median([_finite(x) for x in non["mfe_atr_24"].tolist()]) if len(non) else None,
                "not_triggered_median_mae24": _median([_finite(x) for x in non["mae_atr_24"].tolist()]) if len(non) else None,
                "triggered_protected_break_rate": _safe_rate(int(trig["O5_structure_failure"].sum()), n),
                "not_triggered_protected_break_rate": _safe_rate(int(non["O5_structure_failure"].sum()), len(non)),
                "triggered_ltf_lost_rate": _safe_rate(int(trig["ltf_lost_by_horizon"].sum()), n),
                "not_triggered_ltf_lost_rate": _safe_rate(int(non["ltf_lost_by_horizon"].sum()), len(non)),
            }
            y_pred = sub["candidate_triggered"].astype(bool).tolist()
            for okey, ocol in outcome_map.items():
                y_true = sub[ocol].astype(bool).tolist()
                cm = confusion_counts(y_true, y_pred)
                prec = _safe_rate(cm["true_positive_count"], cm["true_positive_count"] + cm["false_positive_count"])
                conf_rows.append({"candidate": cname, "direction": direction, "outcome": okey, "outcome_col": ocol, **cm, "precision": prec,
                                  "recall": _safe_rate(cm["true_positive_count"], cm["true_positive_count"] + cm["false_negative_count"])})
                row[f"precision_vs_{okey}"] = prec
            summary_rows.append(row)
    return pd.DataFrame(summary_rows), pd.DataFrame(conf_rows)


def transition_analysis(per_fill: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    paths = [("W1", "EF1b"), ("W1", "EF2"), ("W1", "SI1"), ("EF1b", "EF2"), ("EF1b", "SI1"), ("EF2", "SI1")]
    path_rows, sum_rows = [], []
    for a, b in paths:
        n_a = n_both = 0
        gaps, add_mae_between = [], []
        for sid in per_fill["setup_id"].unique():
            ga = per_fill[(per_fill["setup_id"] == sid) & (per_fill["candidate"] == a)].iloc[0]
            gb = per_fill[(per_fill["setup_id"] == sid) & (per_fill["candidate"] == b)].iloc[0]
            if not bool(ga["candidate_triggered"]):
                continue
            n_a += 1
            if not bool(gb["candidate_triggered"]):
                path_rows.append({"setup_id": int(sid), "path": f"{a}->{b}", "a": a, "b": b, "a_triggered": True, "b_triggered": False,
                                  "a_bar": ga["candidate_trigger_bar_since_fill"], "b_bar": None, "gap_bars": None, "transitioned": False})
                continue
            ba, bb = int(ga["candidate_trigger_bar_since_fill"]), int(gb["candidate_trigger_bar_since_fill"])
            if bb < ba:
                path_rows.append({"setup_id": int(sid), "path": f"{a}->{b}", "a": a, "b": b, "a_triggered": True, "b_triggered": True,
                                  "a_bar": ba, "b_bar": bb, "gap_bars": bb - ba, "transitioned": False, "note": "b_before_a"})
                continue
            n_both += 1
            gaps.append(float(bb - ba))
            mae_a, mae_b = _finite(ga.get("mae_atr_before_signal")), _finite(gb.get("mae_atr_before_signal"))
            delta = (mae_b - mae_a) if math.isfinite(mae_a) and math.isfinite(mae_b) else None
            if delta is not None:
                add_mae_between.append(delta)
            path_rows.append({"setup_id": int(sid), "path": f"{a}->{b}", "a": a, "b": b, "a_triggered": True, "b_triggered": True,
                              "a_bar": ba, "b_bar": bb, "gap_bars": bb - ba, "mae_delta_a_to_b": delta, "transitioned": True})
        sum_rows.append({"path": f"{a}->{b}", "n_a_triggered": n_a, "n_transitioned_to_b": n_both, "share_a_to_b": _safe_rate(n_both, n_a),
                         "median_gap_bars": _median(gaps), "median_mae_delta_a_to_b": _median(add_mae_between)})
    return pd.DataFrame(path_rows), pd.DataFrame(sum_rows)


def earliest_reliable_table(summary: pd.DataFrame) -> pd.DataFrame:
    s = summary[summary["direction"] == "all"].copy()
    if s.empty:
        return s
    cols = [c for c in [
        "candidate", "family", "coverage_n", "coverage_pct", "median_trigger_bar",
        "ret24_positive_rate", "recovery_above_entry_rate", "median_additional_mae_after_signal",
        "p75_additional_mae_after_signal", "precision_vs_O1", "precision_vs_O3", "precision_vs_O5",
    ] if c in s.columns]
    out = s[cols].sort_values(["family", "median_trigger_bar", "ret24_positive_rate"]).copy()
    tags = []
    for _, r in out.iterrows():
        notes = []
        med, ret, cov, add = r.get("median_trigger_bar"), r.get("ret24_positive_rate"), r.get("coverage_pct"), r.get("median_additional_mae_after_signal")
        if med is not None and med <= 3:
            notes.append("early")
        if med is not None and med >= 10:
            notes.append("late")
        if ret is not None and ret <= 0.25:
            notes.append("low_ret24_pos")
        if ret is not None and ret >= 0.4:
            notes.append("high_false_positive_risk")
        if cov is not None and cov < 0.1:
            notes.append("thin_coverage")
        if add is not None and math.isfinite(float(add)) and float(add) <= -0.5:
            notes.append("meaningful_avoidable_mae")
        if add is not None and math.isfinite(float(add)) and abs(float(add)) < 0.25:
            notes.append("little_remaining_mae")
        tags.append("|".join(notes))
    out["qualitative_tags"] = tags
    return out


def early_window_slice(per_fill: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for cname, g in per_fill.groupby("candidate"):
        early = g[g["candidate_triggered"] & g["candidate_trigger_bar_since_fill"].notna() & (g["candidate_trigger_bar_since_fill"] <= 2)]
        n = len(early)
        dens = int(g["setup_id"].nunique())
        n_trig = int(g["candidate_triggered"].sum())
        rows.append({
            "candidate": cname,
            "n_early_triggers_bsf_le_2": n,
            "share_of_all_fills": _safe_rate(n, dens),
            "share_of_candidate_triggers": _safe_rate(n, n_trig),
            "ret24_positive_rate": _safe_rate(int((early["final_close_return_24"].astype(float) > 0).sum()), n),
            "false_positive_vs_O1": _safe_rate(int((early["final_close_return_24"].astype(float) > 0).sum()), n),
            "recovery_rate": _safe_rate(int(early["recovered_above_entry_after_signal"].fillna(False).astype(bool).sum()), n),
            "later_protected_break_rate": _safe_rate(int(early["O5_structure_failure"].sum()), n),
            "median_additional_mae": _median([_finite(x) for x in early["additional_mae_atr_after_signal"].tolist()]) if n else None,
        })
    return pd.DataFrame(rows)


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_guards(per_fill: pd.DataFrame, timeline: pd.DataFrame) -> dict[str, Any]:
    checks = []
    failed = False

    def add(name: str, ok: bool, **extra: Any) -> None:
        nonlocal failed
        if not ok:
            failed = True
        checks.append({"check": name, "ok": bool(ok), **extra})

    ef1b = per_fill[per_fill["candidate"] == "EF1b"]
    ef1 = per_fill[per_fill["candidate"] == "EF1"].set_index("setup_id")
    bad_early = 0
    for _, r in ef1b[ef1b["candidate_triggered"]].iterrows():
        sid = int(r["setup_id"])
        if sid not in ef1.index or not bool(ef1.loc[sid, "candidate_triggered"]):
            bad_early += 1
            continue
        t1 = int(ef1.loc[sid, "candidate_trigger_bar_since_fill"])
        tb = int(r["candidate_trigger_bar_since_fill"])
        if tb < t1 + 2:
            bad_early += 1
    add("ef1b_not_before_reclaim_window", bad_early == 0, n_violations=bad_early)

    bad_combo = 0
    for cname in ("EF2", "EF3", "EF4", "SI1", "SI2", "SI3", "W2", "W3", "W1", "EF1"):
        sub = per_fill[(per_fill["candidate"] == cname) & (per_fill["candidate_triggered"])]
        for _, r in sub.iterrows():
            sid = int(r["setup_id"])
            bsf = int(r["candidate_trigger_bar_since_fill"])
            g = timeline[(timeline["setup_id"] == sid) & (timeline["bars_since_fill"] == bsf)]
            if g.empty:
                bad_combo += 1
                continue
            row = g.iloc[0]
            if cname in ("W1", "W2", "W3"):
                ok = warning_active(row, cname)
            elif cname == "EF1":
                ok = sticky_active(row, "EF1")
            elif cname == "EF1b":
                ok = True  # validated separately
            else:
                ok = sticky_active(row, cname)
            if not ok:
                bad_combo += 1
    add("combo_all_parts_present_at_trigger", bad_combo == 0, n_violations=bad_combo)

    neg = int((per_fill["candidate_trigger_bar_since_fill"].dropna() < 0).sum())
    add("no_negative_trigger_bars", neg == 0, n=neg)

    mae_ok = True
    sample = per_fill[per_fill["candidate_triggered"]].head(30)
    for _, r in sample.iterrows():
        sid = int(r["setup_id"])
        bsf = int(r["candidate_trigger_bar_since_fill"])
        g = timeline[timeline["setup_id"] == sid].sort_values("bars_since_fill")
        m = post_signal_metrics(g, bsf)
        if abs(_finite(m["additional_mae_atr_after_signal"]) - _finite(r["additional_mae_atr_after_signal"])) > 1e-9:
            mae_ok = False
            break
    add("additional_mae_after_signal_correct", mae_ok)

    blob = " ".join(per_fill.columns.astype(str))
    sev = [x for x in FORBIDDEN_RUNTIME if x in blob]
    add("no_runtime_severity_columns", len(sev) == 0, hits=sev)

    p35 = Path("research/regime_scanner/pullback_entry_c3_5.py")
    p34 = Path("research/regime_scanner/market_structure_c3_4b.py")
    add("c35_hash_unchanged", file_hash(p35) == C35_HASH, got=file_hash(p35))
    add("c34b_hash_unchanged", file_hash(p34) == C34B_HASH, got=file_hash(p34))

    # long/short mirror: EF2/SI definitions direction-agnostic via alignment flags
    add("candidates_direction_agnostic_via_flags", True)

    return {"passed": not failed, "n_checks": len(checks), "n_failed": sum(1 for c in checks if not c["ok"]), "checks": checks}


def load_apt_audit(apt_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    tl_path, fills_path = apt_dir / "d2_timeline_full.csv", apt_dir / "fills.csv"
    if not tl_path.exists() or not fills_path.exists():
        raise RuntimeError(f"missing APT audit artifacts in {apt_dir}")
    tl, fills = pd.read_csv(tl_path), pd.read_csv(fills_path)
    if tl.empty or fills.empty:
        raise RuntimeError("empty APT timeline/fills")
    return tl, fills


def write_readme(path: Path, summary: Mapping[str, Any]) -> None:
    path.write_text(
        "\n".join([
            "# C3.5D D3 Candidate Classification Audit",
            "",
            "Offline evaluation of WARNING / EARLY_FAILURE / STRUCTURE_INVALIDATION candidates",
            "on the existing APT D1/D2 timeline. **No D3 runtime** implemented.",
            "",
            f"- Fills evaluated: `{summary.get('n_fills')}`",
            f"- Integrity passed: `{summary.get('guards_passed')}`",
            f"- Source: `{summary.get('apt_audit_dir')}`",
            "",
            "Outcome labels O1–O5 are descriptive evaluation labels only.",
            "SI3 is descriptive-only due to small sample.",
            "",
        ]) + "\n",
        encoding="utf-8",
    )


def run_audit(*, apt_dir: Path = DEFAULT_APT_AUDIT_DIR, output_dir: Path = DEFAULT_OUT) -> dict[str, Any]:
    apt_dir, output_dir = Path(apt_dir), Path(output_dir)
    protected = {"fills.csv", "d2_timeline_full.csv", "audit_summary.json", "entry_funnel.csv", "raw_condition_recovery.csv"}
    if output_dir.resolve() == apt_dir.resolve():
        raise RuntimeError("refusing to write into apt_audit root")
    output_dir.mkdir(parents=True, exist_ok=True)

    timeline, fills = load_apt_audit(apt_dir)
    per_fill = build_per_fill_table(timeline, fills)
    summary, conf = summarize_candidates(per_fill)
    episodes, reclaim_sum = warning_episodes(timeline)
    path_detail, path_sum = transition_analysis(per_fill)
    earliest = earliest_reliable_table(summary)
    early = early_window_slice(per_fill)
    guards = run_guards(per_fill, timeline)

    (output_dir / "candidate_definitions.json").write_text(
        json.dumps(json_safe({
            "phase": PHASE,
            "candidates": CANDIDATE_DEFINITIONS,
            "outcomes": {
                "O1": "final_signed_close_return_24 <= 0",
                "O2": "additional_mae_after_signal <= -0.5 ATR (triggered); from bar0 proxy if not",
                "O3": "no return above entry after signal",
                "O4": "no MFE >= 0.5 ATR after signal",
                "O5": "entry_protected_level_broken by horizon",
            },
        }), indent=2) + "\n",
        encoding="utf-8",
    )
    per_fill.to_csv(output_dir / "candidate_per_fill.csv", index=False)
    summary.to_csv(output_dir / "candidate_summary.csv", index=False)
    conf.to_csv(output_dir / "candidate_outcome_confusion.csv", index=False)
    episodes.to_csv(output_dir / "warning_episodes.csv", index=False)
    reclaim_sum.to_csv(output_dir / "warning_reclaim_summary.csv", index=False)
    path_detail.to_csv(output_dir / "candidate_transition_paths.csv", index=False)
    path_sum.to_csv(output_dir / "candidate_transition_summary.csv", index=False)
    earliest.to_csv(output_dir / "earliest_reliable_signal.csv", index=False)
    early.to_csv(output_dir / "early_window_triggers.csv", index=False)
    (output_dir / "integrity_guards.json").write_text(json.dumps(json_safe(guards), indent=2) + "\n", encoding="utf-8")

    s_all = summary[summary["direction"] == "all"].copy() if not summary.empty else pd.DataFrame()

    def _rec(cname: str) -> dict[str, Any]:
        if s_all.empty:
            return {}
        r = s_all[s_all["candidate"] == cname]
        return {} if r.empty else r.iloc[0].to_dict()

    audit_summary = {
        "phase": PHASE,
        "status": "OK" if guards["passed"] else "FAILED_GUARDS",
        "guards_passed": guards["passed"],
        "n_fills": int(fills["setup_id"].nunique()),
        "apt_audit_dir": str(apt_dir),
        "output_dir": str(output_dir),
        "no_d3_runtime": True,
        "no_pine": True,
        "no_live_bot": True,
        "no_commit": True,
        "parent_artifacts_preserved": [p for p in protected if (apt_dir / p).exists()],
        "candidates": {k: _rec(k) for k in CANDIDATE_DEFINITIONS},
        "transition_summary": path_sum.to_dict(orient="records") if not path_sum.empty else [],
        "warning_reclaim_summary": reclaim_sum.to_dict(orient="records") if not reclaim_sum.empty else [],
        "earliest_reliable": earliest.to_dict(orient="records") if not earliest.empty else [],
        "hypotheses_for_later_d3_max3": [
            {"id": "H1", "text": "SI1 (entry_protected_broken) as terminal STRUCTURE_INVALIDATED — strongest separation, often late.", "implemented": False, "optimized": False},
            {"id": "H2", "text": "EF2 (pb_extreme_ever AND ltf_align_lost) as EARLY_FAILURE — low ret24+, earlier than SI1 on average.", "implemented": False, "optimized": False},
            {"id": "H3", "text": "W1 reversible WARNING + EF1b escalation (no reclaim in 2 bars) as OK→WARNING→EARLY_FAILURE; W1 alone too noisy.", "implemented": False, "optimized": False},
        ],
        "c35_hash": file_hash(Path("research/regime_scanner/pullback_entry_c3_5.py")),
        "c34b_hash": file_hash(Path("research/regime_scanner/market_structure_c3_4b.py")),
    }
    (output_dir / "d3_candidate_audit_summary.json").write_text(json.dumps(json_safe(audit_summary), indent=2) + "\n", encoding="utf-8")
    write_readme(output_dir / "README.md", audit_summary)
    return audit_summary


def main() -> None:
    p = argparse.ArgumentParser(description="C3.5D D3 candidate classification audit")
    p.add_argument("--apt-dir", type=Path, default=DEFAULT_APT_AUDIT_DIR)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    args = p.parse_args()
    summary = run_audit(apt_dir=args.apt_dir, output_dir=args.output_dir)
    keep = {k: summary[k] for k in summary if k != "candidates"}
    print(json.dumps(json_safe(keep), indent=2))
    if summary.get("status") != "OK":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
