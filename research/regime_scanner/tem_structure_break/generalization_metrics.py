"""Generalization metrics for frozen v2 structure-break evaluation."""

from __future__ import annotations

from typing import Any

import numpy as np

from research.regime_scanner.tem_structure_break.eval_common import AAVE_DEV_TRADE_ID, median


def _as_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    return str(v).lower() in {"1", "true", "yes"}


def split_rows(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    return {
        "aave_dev": [r for r in rows if r.get("trade_id") == AAVE_DEV_TRADE_ID],
        "blockers_all": [r for r in rows if r.get("cohort") == "blocker"],
        "blockers_holdout26": [
            r
            for r in rows
            if r.get("cohort") == "blocker" and r.get("trade_id") != AAVE_DEV_TRADE_ID
        ],
        "controls": [r for r in rows if r.get("cohort") == "control"],
    }


def rate(num: int, den: int) -> float | None:
    if den <= 0:
        return None
    return num / den


def confusion(blockers: list[dict], controls: list[dict], *, pred_key: str) -> dict[str, Any]:
    """Binary: positive label = blocker; prediction = pred_key truthy on row."""
    tp = sum(1 for r in blockers if _as_bool(r.get(pred_key)))
    fn = sum(1 for r in blockers if not _as_bool(r.get(pred_key)))
    fp = sum(1 for r in controls if _as_bool(r.get(pred_key)))
    tn = sum(1 for r in controls if not _as_bool(r.get(pred_key)))
    prec = rate(tp, tp + fp)
    rec = rate(tp, tp + fn)
    spec = rate(tn, tn + fp)
    fpr = rate(fp, fp + tn)
    fnr = rate(fn, tp + fn)
    return {
        "tp": tp,
        "fn": fn,
        "fp": fp,
        "tn": tn,
        "precision": prec,
        "recall": rec,
        "specificity": spec,
        "false_positive_rate": fpr,
        "false_negative_rate": fnr,
        "n_blockers": len(blockers),
        "n_controls": len(controls),
        "prediction": pred_key,
    }


def cohort_stats(rows: list[dict[str, Any]], label: str) -> dict[str, Any]:
    n = len(rows)
    warned = sum(1 for r in rows if r.get("first_warning_ts"))
    broken = sum(1 for r in rows if int(float(r.get("break_episode_count") or 0)) > 0)
    reclaimed = sum(1 for r in rows if int(float(r.get("reclaim_count") or 0)) > 0)
    rebreak = sum(1 for r in rows if r.get("second_break_ts"))
    at_risk = sum(1 for r in rows if r.get("first_structure_at_risk_ts"))
    invalidated = sum(1 for r in rows if r.get("final_invalidation_ts"))
    inv_c4 = sum(1 for r in rows if _as_bool(r.get("invalidated_before_cycle4")))
    inv_c5 = sum(1 for r in rows if _as_bool(r.get("invalidated_before_cycle5")))
    inv_exp = sum(1 for r in rows if _as_bool(r.get("invalidated_before_explosion")))
    warn_c4 = sum(1 for r in rows if _as_bool(r.get("warned_before_cycle4")))
    warn_c5 = sum(1 for r in rows if _as_bool(r.get("warned_before_cycle5")))
    # second break leads to invalidation: invalidated and episode_count>=2
    second_to_inv = sum(
        1
        for r in rows
        if r.get("final_invalidation_ts") and int(float(r.get("break_episode_count") or 0)) >= 2
    )
    return {
        "label": label,
        "n": n,
        "share_warning": rate(warned, n),
        "share_warning_before_c4": rate(warn_c4, n),
        "share_warning_before_c5": rate(warn_c5, n),
        "share_break_episode": rate(broken, n),
        "share_reclaim": rate(reclaimed, n),
        "share_rebreak": rate(rebreak, n),
        "share_at_risk": rate(at_risk, n),
        "share_invalidated": rate(invalidated, n),
        "share_invalidated_before_c4": rate(inv_c4, n),
        "share_invalidated_before_c5": rate(inv_c5, n),
        "share_invalidated_before_explosion": rate(inv_exp, n),
        "share_second_break_then_invalidated": rate(second_to_inv, n),
        "median_lead_inv_vs_c4": median([r.get("lead_hours_invalidation_vs_cycle4") for r in rows]),
        "median_lead_inv_vs_c5": median([r.get("lead_hours_invalidation_vs_cycle5") for r in rows]),
        "median_lead_inv_vs_explosion": median(
            [r.get("lead_hours_invalidation_vs_explosion") for r in rows]
        ),
        "median_lead_warning_vs_c4": median([r.get("lead_hours_warning_vs_cycle4") for r in rows]),
        "median_lead_warning_vs_c5": median([r.get("lead_hours_warning_vs_cycle5") for r in rows]),
        "median_break_episodes": median([r.get("break_episode_count") for r in rows]),
        "median_reclaims": median([r.get("reclaim_count") for r in rows]),
    }


def failure_mode_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from collections import Counter

    c: Counter[str] = Counter()
    for r in rows:
        raw = str(r.get("root_cause_if_no_signal") or "")
        if not raw:
            if r.get("final_invalidation_ts"):
                c["INVALIDATED_OK"] += 1
            else:
                c["OTHER"] += 1
            continue
        for part in raw.split("|"):
            if part:
                c[part] += 1
    return [{"failure_reason": k, "count": v} for k, v in c.most_common()]


def lead_time_distribution(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        out.append(
            {
                "trade_id": r.get("trade_id"),
                "cohort": r.get("cohort"),
                "holdout_bucket": r.get("holdout_bucket"),
                "lead_hours_invalidation_vs_cycle4": r.get("lead_hours_invalidation_vs_cycle4"),
                "lead_hours_invalidation_vs_cycle5": r.get("lead_hours_invalidation_vs_cycle5"),
                "lead_hours_invalidation_vs_explosion": r.get(
                    "lead_hours_invalidation_vs_explosion"
                ),
                "lead_hours_warning_vs_cycle4": r.get("lead_hours_warning_vs_cycle4"),
                "break_episode_count": r.get("break_episode_count"),
                "reclaim_count": r.get("reclaim_count"),
            }
        )
    return out


def build_comparison(parts: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    rows = []
    for key, label in [
        ("aave_dev", "AAVE_development"),
        ("blockers_holdout26", "blockers_holdout_26"),
        ("blockers_all", "blockers_all_27"),
        ("controls", "controls_profitable"),
    ]:
        st = cohort_stats(parts[key], label)
        rows.append(st)
    return rows
