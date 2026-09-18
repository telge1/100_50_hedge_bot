"""Paired case-control stats, overlap groups, ablation (descriptive only)."""

from __future__ import annotations

import math
import random
from typing import Any, Sequence

from obfull_research_engine.mp_big_move_case_control_v1.stats import cliffs_delta, median


# Documented hypothesized positive direction BEFORE evaluation (winner − control).
# Positive paired_diff means winner larger on that feature.
FEATURE_POSITIVE_DIRECTION: dict[str, str] = {
    "attributed_fill_qty": "higher_fill_may_indicate_aggression_through_wall",
    "residual_pull_qty": "higher_pull_may_indicate_wall_withdrawal_before_move",
    "fill_share_of_book_decrease": "higher_fill_share_vs_pull",
    "qdh_at_decision": "higher_qdh_at_decision",
    "qdh_max": "higher_peak_qdh",
    "qdh_slope": "steeper_qdh_rise",
    "queue_recovery_fraction": "lower_recovery_may_favor_continuation",  # note: sign flipped in preferred_sign
    "wall_move_net_ticks": "side-dependent; use ticks_path_opened instead",
    "ticks_path_opened": "more_path_opening_in_trade_direction",
    "wall_opens_path_in_trade_direction": "path_opening_flag",
    "mid_change_favorable_signed": "more_favorable_mid_move_by_decision",
    "same_side_depth_inside_2bps_at_decision": "lower_same_side_depth_vacuum",  # preferred smaller
}

PREFERRED_SIGN: dict[str, int] = {
    # +1 => expect winner > control; -1 => expect winner < control
    "attributed_fill_qty": 1,
    "residual_pull_qty": 1,
    "fill_share_of_book_decrease": 1,
    "pull_share_of_book_decrease": 1,
    "qdh_at_decision": 1,
    "qdh_max": 1,
    "qdh_slope": 1,
    "qdh_auc": 1,
    "queue_recovery_fraction": -1,
    "queue_min_fraction": -1,
    "ticks_path_opened": 1,
    "wall_opens_path_in_trade_direction": 1,
    "wall_blocks_trade_direction": -1,
    "mid_change_favorable_signed": 1,
    "favorable_price_response_after_aggression": 1,
    "same_side_depth_inside_2bps_at_decision": -1,
    "gross_book_churn": 1,
    "refill_qty": -1,
}


def _to_float(v: Any) -> float | None:
    if v is None or v == "NOT_AVAILABLE" or v == "NOT_AVAILABLE_AT_DECISION":
        return None
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def bootstrap_ci(
    diffs: Sequence[float],
    *,
    n_boot: int = 1000,
    seed: int = 17,
    alpha: float = 0.05,
) -> tuple[float | None, float | None]:
    xs = [float(x) for x in diffs if x is not None and math.isfinite(float(x))]
    if len(xs) < 3:
        return None, None
    rng = random.Random(seed)
    boots = []
    n = len(xs)
    for _ in range(n_boot):
        sample = [xs[rng.randrange(n)] for _ in range(n)]
        boots.append(sorted(sample)[n // 2])
    boots.sort()
    lo = boots[int(alpha / 2 * n_boot)]
    hi = boots[int((1 - alpha / 2) * n_boot)]
    return lo, hi


def paired_feature_rows(
    pairs: list[dict[str, Any]],
    features_by_event: dict[str, dict[str, Any]],
    feature_keys: Sequence[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for feat in feature_keys:
        w_vals: list[float] = []
        c_vals: list[float] = []
        diffs: list[float] = []
        n_gt = n_lt = n_eq = 0
        n_valid = 0
        for p in pairs:
            wf = features_by_event.get(p["winner_event_id"]) or {}
            cf = features_by_event.get(p["control_event_id"]) or {}
            wv = _to_float(wf.get(feat))
            cv = _to_float(cf.get(feat))
            if wv is None or cv is None:
                continue
            n_valid += 1
            w_vals.append(wv)
            c_vals.append(cv)
            d = wv - cv
            diffs.append(d)
            if d > 1e-15:
                n_gt += 1
            elif d < -1e-15:
                n_lt += 1
            else:
                n_eq += 1
        lo, hi = bootstrap_ci(diffs)
        pref = PREFERRED_SIGN.get(feat)
        aligned = None
        if pref is not None and diffs:
            # fraction of pairs where signed diff agrees with preferred direction
            aligned = sum(1 for d in diffs if d * pref > 0) / len(diffs)
        rows.append(
            {
                "feature": feat,
                "n_valid_pairs": n_valid,
                "n_winner_gt_control": n_gt,
                "n_winner_lt_control": n_lt,
                "n_ties": n_eq,
                "median_winner": median(w_vals),
                "median_control": median(c_vals),
                "median_paired_diff": median(diffs),
                "cliffs_delta_winner_vs_control": cliffs_delta(w_vals, c_vals),
                "bootstrap_diff_ci_lo": lo,
                "bootstrap_diff_ci_hi": hi,
                "preferred_sign": pref,
                "fraction_aligned_with_preferred": aligned,
                "positive_direction_note": FEATURE_POSITIVE_DIRECTION.get(feat, ""),
                "low_sample": n_valid < 10,
                "missing_policy": "pairwise_complete_only_never_impute_zero",
            }
        )
    return rows


def subgroup_rows(
    pairs: list[dict[str, Any]],
    features_by_event: dict[str, dict[str, Any]],
    meta_by_event: dict[str, dict[str, Any]],
    feature_keys: Sequence[str],
) -> list[dict[str, Any]]:
    dims = {
        "label_price_only": lambda m: m.get("label_price_only"),
        "trade_side": lambda m: m.get("trade_side"),
        "event_role": lambda m: m.get("event_role"),
        "linkage_status": lambda m: m.get("linkage_status"),
        "confluence_class": lambda m: m.get("confluence_class"),
    }
    out: list[dict[str, Any]] = []
    for dim, fn in dims.items():
        groups: dict[str, list[dict[str, Any]]] = {}
        for p in pairs:
            key = str(fn(meta_by_event.get(p["winner_event_id"]) or {}))
            groups.setdefault(key, []).append(p)
        for gname, gpairs in sorted(groups.items()):
            for feat in feature_keys:
                diffs = []
                for p in gpairs:
                    wv = _to_float((features_by_event.get(p["winner_event_id"]) or {}).get(feat))
                    cv = _to_float((features_by_event.get(p["control_event_id"]) or {}).get(feat))
                    if wv is None or cv is None:
                        continue
                    diffs.append(wv - cv)
                out.append(
                    {
                        "subgroup_dim": dim,
                        "subgroup": gname,
                        "feature": feat,
                        "n_pairs": len(gpairs),
                        "n_valid": len(diffs),
                        "median_paired_diff": median(diffs),
                        "descriptive_only": len(gpairs) < 10,
                        "low_sample": len(gpairs) < 10,
                    }
                )
    return out


def overlap_groups(event_rows: list[dict[str, Any]], window_s: float = 600.0) -> list[dict[str, Any]]:
    """Deterministic overlap groups by overlapping ±window_s around reference entry."""
    items = []
    for r in event_rows:
        if r.get("case_role") not in ("WINNER", "CONTROL"):
            continue
        ts = int(r.get("reference_entry_ts_ns") or 0)
        items.append((str(r["event_id"]), ts, r.get("pair_id"), r.get("case_role")))
    items.sort(key=lambda x: (x[1], x[0]))
    parent = {eid: eid for eid, _, _, _ in items}

    def find(a: str) -> str:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        # Deterministic: keep lexicographically smaller root.
        if ra < rb:
            parent[rb] = ra
        else:
            parent[ra] = rb

    ns_w = int(window_s * 1e9)
    for i, (eid, ts, _, _) in enumerate(items):
        for j in range(i + 1, len(items)):
            eid2, ts2, _, _ = items[j]
            if ts2 - ts > ns_w:
                break
            # overlap if intervals [ts-w, ts+w] intersect
            if abs(ts2 - ts) <= ns_w:
                union(eid, eid2)
    groups: dict[str, list[tuple]] = {}
    for eid, ts, pair_id, role in items:
        groups.setdefault(find(eid), []).append((eid, ts, pair_id, role))
    rows = []
    for gi, (root, members) in enumerate(sorted(groups.items(), key=lambda x: x[0])):
        if len(members) < 2:
            continue
        rows.append(
            {
                "overlap_group_id": f"OG{gi + 1:03d}",
                "n_events": len(members),
                "event_ids": "|".join(m[0] for m in sorted(members)),
                "pair_ids": "|".join(sorted({str(m[2]) for m in members if m[2]})),
                "window_s": window_s,
                "sensitivity_keep_event_id": sorted(members, key=lambda m: (m[1], m[0]))[0][0],
            }
        )
    return rows


ABLATION_GROUPS: dict[str, list[str]] = {
    "A_MP_CONTEXT": ["label_price_only", "event_role", "trade_side"],  # context only — separation N/A numeric
    "B_MP_STATIC_WALL": ["queue_at_touch", "linkage_present_flag"],
    "C_MP_FILL_PULL_REFILL": [
        "attributed_fill_qty",
        "residual_pull_qty",
        "refill_qty",
        "fill_share_of_book_decrease",
    ],
    "D_MP_QDH_TRAJECTORY": ["qdh_at_decision", "qdh_max", "qdh_slope", "qdh_auc"],
    "E_MP_WALL_MOVEMENT": [
        "ticks_path_opened",
        "wall_opens_path_in_trade_direction",
        "wall_move_net_ticks",
        "wall_move_count",
    ],
    "F_MP_QDH_WALL_MICRO_VACUUM": [
        "qdh_at_decision",
        "ticks_path_opened",
        "mid_change_favorable_signed",
        "same_side_depth_inside_2bps_at_decision",
    ],
}


def ablation_summary(
    pairs: list[dict[str, Any]],
    features_by_event: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = []
    for name, feats in ABLATION_GROUPS.items():
        if name == "A_MP_CONTEXT":
            rows.append(
                {
                    "ablation": name,
                    "features": "|".join(feats),
                    "mean_abs_median_diff": None,
                    "mean_abs_cliffs": None,
                    "n_features_scored": 0,
                    "note": "context-only; no numeric separation score",
                }
            )
            continue
        abs_diffs = []
        abs_cliffs = []
        for feat in feats:
            w_vals, c_vals, diffs = [], [], []
            for p in pairs:
                wv = _to_float((features_by_event.get(p["winner_event_id"]) or {}).get(feat))
                cv = _to_float((features_by_event.get(p["control_event_id"]) or {}).get(feat))
                if wv is None or cv is None:
                    continue
                w_vals.append(wv)
                c_vals.append(cv)
                diffs.append(wv - cv)
            md = median(diffs)
            cd = cliffs_delta(w_vals, c_vals)
            if md is not None:
                abs_diffs.append(abs(md))
            if cd is not None:
                abs_cliffs.append(abs(cd))
        rows.append(
            {
                "ablation": name,
                "features": "|".join(feats),
                "mean_abs_median_diff": (sum(abs_diffs) / len(abs_diffs)) if abs_diffs else None,
                "mean_abs_cliffs": (sum(abs_cliffs) / len(abs_cliffs)) if abs_cliffs else None,
                "n_features_scored": len(abs_diffs),
                "note": "descriptive_group_separation_not_a_model",
            }
        )
    return rows
