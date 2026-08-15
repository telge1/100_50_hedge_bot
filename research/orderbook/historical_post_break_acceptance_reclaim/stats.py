"""Descriptive stats / AUC / baselines for post-break acceptance audit."""

from __future__ import annotations

import math
import random
from collections import Counter, defaultdict
from typing import Any

from research.orderbook.historical_post_break_acceptance_reclaim import (
    CONFIRMATION_CUTOFFS_S,
    OUTCOME_ACCEPTED,
    OUTCOME_RECLAIM,
    PRIMARY_CUTOFFS_S,
)

PRICE_FEATURES = [
    "distance_beyond_level_bps",
    "max_distance_beyond_so_far",
    "fraction_of_time_beyond_level",
    "velocity_away_bps_per_s",
    "n_recrosses",
]
OB_FEATURES = [
    "new_side_blocking_depth",
    "old_level_defensive_depth",
    "flip_depth_ratio",
    "near_depth_imbalance",
    "break_side_depth_change",
    "gross_refill",
    "net_refill",
    "refill_ratio",
]
FLOW_FEATURES = [
    "signed_aggressive_flow",
    "flow_imbalance",
    "flow_reversal_ratio",
    "break_flow",
    "reclaim_flow",
    "fraction_volume_beyond_level",
    "volume_beyond_level",
    "burst_intensity",
]


def _finite(xs: list[Any]) -> list[float]:
    out = []
    for x in xs:
        if x is None:
            continue
        try:
            v = float(x)
        except (TypeError, ValueError):
            continue
        if math.isfinite(v):
            out.append(v)
    return out


def summarize(xs: list[Any]) -> dict[str, Any]:
    vals = sorted(_finite(xs))
    n = len(vals)
    if n == 0:
        return {"n": 0, "mean": None, "median": None, "q25": None, "q75": None}

    def q(p: float) -> float:
        idx = p * (n - 1)
        lo, hi = int(math.floor(idx)), int(math.ceil(idx))
        if lo == hi:
            return vals[lo]
        w = idx - lo
        return vals[lo] * (1 - w) + vals[hi] * w

    return {"n": n, "mean": sum(vals) / n, "median": q(0.5), "q25": q(0.25), "q75": q(0.75)}


def mann_whitney_auc(scores: list[float], labels: list[int]) -> float | None:
    pairs = [
        (float(s), y)
        for s, y in zip(scores, labels)
        if s is not None and math.isfinite(float(s))
    ]
    pos = [s for s, y in pairs if y == 1]
    neg = [s for s, y in pairs if y == 0]
    if not pos or not neg:
        return None
    buckets: dict[float, list[int]] = defaultdict(list)
    for i, v in enumerate(sorted(float(s) for s, _ in pairs), start=1):
        buckets[v].append(i)
    avg_rank = {v: sum(ix) / len(ix) for v, ix in buckets.items()}
    sum_pos = sum(avg_rank[float(s)] for s in pos)
    n1, n0 = len(pos), len(neg)
    return (sum_pos - n1 * (n1 + 1) / 2.0) / (n1 * n0)


def feature_auc_at_cutoff(
    rows: list[dict[str, Any]],
    feature: str,
    *,
    cutoff: int,
) -> dict[str, Any]:
    sub = [
        r
        for r in rows
        if int(r["cutoff"]) == cutoff and r.get("outcome") in {OUTCOME_ACCEPTED, OUTCOME_RECLAIM}
    ]
    scores, labels = [], []
    for r in sub:
        if r.get(feature) is None:
            continue
        try:
            scores.append(float(r[feature]))
        except (TypeError, ValueError):
            continue
        labels.append(1 if r["outcome"] == OUTCOME_ACCEPTED else 0)
    acc = [s for s, y in zip(scores, labels) if y == 1]
    rec = [s for s, y in zip(scores, labels) if y == 0]
    sa, sr = summarize(acc), summarize(rec)
    return {
        "feature": feature,
        "cutoff": cutoff,
        "family": _family(feature),
        "n_accepted": sa["n"],
        "n_reclaim": sr["n"],
        "median_accepted": sa["median"],
        "median_reclaim": sr["median"],
        "auc": mann_whitney_auc(scores, labels),
    }


def _family(feature: str) -> str:
    if feature in PRICE_FEATURES:
        return "price"
    if feature in OB_FEATURES:
        return "ob"
    if feature in FLOW_FEATURES:
        return "flow"
    return "other"


def _rank_combine(rows: list[dict[str, Any]], features: list[str]) -> list[float] | None:
    usable = []
    for r in rows:
        vals = []
        ok = True
        for f in features:
            if r.get(f) is None:
                ok = False
                break
            vals.append(float(r[f]))
        if ok:
            usable.append((r, vals))
    if len(usable) < 4:
        return None
    # rank each feature across usable, sum
    n = len(usable)
    ranks = [[0.0] * len(features) for _ in range(n)]
    for j in range(len(features)):
        order = sorted(range(n), key=lambda i: usable[i][1][j])
        for rank, i in enumerate(order, start=1):
            ranks[i][j] = float(rank)
    return [sum(rr) for rr in ranks]


def scorecard_auc(
    rows: list[dict[str, Any]],
    features: list[str],
    *,
    cutoff: int,
    name: str,
) -> dict[str, Any]:
    sub = [
        r
        for r in rows
        if int(r["cutoff"]) == cutoff and r.get("outcome") in {OUTCOME_ACCEPTED, OUTCOME_RECLAIM}
    ]
    labels = [1 if r["outcome"] == OUTCOME_ACCEPTED else 0 for r in sub]
    # filter to rows with all features
    keep_idx = [
        i
        for i, r in enumerate(sub)
        if all(r.get(f) is not None for f in features)
    ]
    if len(keep_idx) < 4:
        return {"scorecard": name, "cutoff": cutoff, "features": "|".join(features), "n": len(keep_idx), "auc": None}
    sub2 = [sub[i] for i in keep_idx]
    y = [labels[i] for i in keep_idx]
    scores = _rank_combine(sub2, features)
    if scores is None:
        return {"scorecard": name, "cutoff": cutoff, "features": "|".join(features), "n": len(sub2), "auc": None}
    return {
        "scorecard": name,
        "cutoff": cutoff,
        "features": "|".join(features),
        "n": len(sub2),
        "n_accepted": sum(y),
        "n_reclaim": len(y) - sum(y),
        "auc": mann_whitney_auc(scores, y),
    }


def distance_control_rows(
    rows: list[dict[str, Any]],
    *,
    cutoff: int,
    dist_feat: str = "distance_beyond_level_bps",
    ob_feats: list[str] | None = None,
    flow_feats: list[str] | None = None,
) -> dict[str, Any]:
    ob_feats = ob_feats or ["flip_depth_ratio", "near_depth_imbalance"]
    flow_feats = flow_feats or ["signed_aggressive_flow", "flow_reversal_ratio"]
    sub = [
        r
        for r in rows
        if int(r["cutoff"]) == cutoff and r.get("outcome") in {OUTCOME_ACCEPTED, OUTCOME_RECLAIM}
    ]
    out = {"cutoff": cutoff}
    for name, feats in (
        ("distance_only", [dist_feat]),
        ("ob_only", ob_feats),
        ("flow_only", flow_feats),
        ("distance_plus_ob", [dist_feat] + ob_feats),
        ("distance_plus_flow", [dist_feat] + flow_feats),
        ("distance_plus_ob_flow", [dist_feat] + ob_feats + flow_feats),
        ("ob_plus_flow", ob_feats + flow_feats),
    ):
        sc = scorecard_auc(sub, feats, cutoff=cutoff, name=name)
        out[f"auc_{name}"] = sc["auc"]
        out[f"n_{name}"] = sc["n"]
    return out


def bootstrap_auc_ci(
    rows: list[dict[str, Any]],
    feature: str,
    *,
    cutoff: int,
    n_boot: int = 400,
    seed: int = 7,
) -> dict[str, Any]:
    sub = [
        r
        for r in rows
        if int(r["cutoff"]) == cutoff
        and r.get("outcome") in {OUTCOME_ACCEPTED, OUTCOME_RECLAIM}
        and r.get(feature) is not None
    ]
    if len(sub) < 5:
        return {"feature": feature, "cutoff": cutoff, "ci_low": None, "ci_high": None}
    rng = random.Random(seed)
    aucs = []
    for _ in range(n_boot):
        sample = [sub[rng.randrange(len(sub))] for _ in range(len(sub))]
        scores = [float(r[feature]) for r in sample]
        labels = [1 if r["outcome"] == OUTCOME_ACCEPTED else 0 for r in sample]
        a = mann_whitney_auc(scores, labels)
        if a is not None:
            aucs.append(a)
    if not aucs:
        return {"feature": feature, "cutoff": cutoff, "ci_low": None, "ci_high": None}
    aucs.sort()
    return {
        "feature": feature,
        "cutoff": cutoff,
        "auc_full": feature_auc_at_cutoff(rows, feature, cutoff=cutoff)["auc"],
        "ci_low": aucs[int(0.025 * len(aucs))],
        "ci_high": aucs[int(0.975 * len(aucs))],
        "n_boot": n_boot,
    }


def jackknife_auc(
    rows: list[dict[str, Any]],
    feature: str,
    *,
    cutoff: int,
) -> dict[str, Any]:
    sub = [
        r
        for r in rows
        if int(r["cutoff"]) == cutoff
        and r.get("outcome") in {OUTCOME_ACCEPTED, OUTCOME_RECLAIM}
        and r.get(feature) is not None
    ]
    # leave-one-event-out
    by_eid: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in sub:
        by_eid[r["event_id"]].append(r)
    eids = list(by_eid.keys())
    if len(eids) < 5:
        return {"feature": feature, "cutoff": cutoff, "fragile": True, "auc_full": None}
    full_scores = [float(r[feature]) for r in sub]
    full_y = [1 if r["outcome"] == OUTCOME_ACCEPTED else 0 for r in sub]
    full = mann_whitney_auc(full_scores, full_y)
    leave = []
    for eid in eids:
        keep = [r for r in sub if r["event_id"] != eid]
        scores = [float(r[feature]) for r in keep]
        y = [1 if r["outcome"] == OUTCOME_ACCEPTED else 0 for r in keep]
        a = mann_whitney_auc(scores, y)
        if a is not None:
            leave.append(a)
    return {
        "feature": feature,
        "cutoff": cutoff,
        "auc_full": full,
        "auc_min": min(leave) if leave else None,
        "auc_max": max(leave) if leave else None,
        "fragile": (max(leave) - min(leave) > 0.15) if leave else True,
        "n_events": len(eids),
    }


def earliest_useful_time(
    dist_control: list[dict[str, Any]],
    *,
    min_n: int = 8,
    min_auc: float = 0.62,
    min_lift: float = 0.03,
) -> str:
    """EARLY <=30s only if combined beats distance with enough sample."""
    by_c = {int(r["cutoff"]): r for r in dist_control}
    for c in PRIMARY_CUTOFFS_S:
        r = by_c.get(c)
        if not r:
            continue
        n = r.get("n_distance_plus_ob_flow") or 0
        a_d = r.get("auc_distance_only")
        a_c = r.get("auc_distance_plus_ob_flow")
        if n is None or n < min_n or a_d is None or a_c is None:
            continue
        if a_c >= min_auc and (a_c - a_d) >= min_lift:
            return f"BREAK_PLUS_{c}S"
        if a_d >= min_auc and a_c >= min_auc and (a_c - a_d) < min_lift:
            # price alone works early
            if c <= 30:
                return f"BREAK_PLUS_{c}S"  # may be reclassified as price-only by decide
    for c in CONFIRMATION_CUTOFFS_S:
        r = by_c.get(c)
        if not r:
            continue
        a_d = r.get("auc_distance_only")
        a_c = r.get("auc_distance_plus_ob_flow")
        n = r.get("n_distance_plus_ob_flow") or 0
        if n >= min_n and a_d is not None and a_d >= min_auc:
            return f"BREAK_PLUS_{c}S"
        if n >= min_n and a_c is not None and a_c >= min_auc:
            return f"BREAK_PLUS_{c}S"
    return "NO_ROBUST_SEPARATION"


def decide_primary(
    *,
    n_accepted: int,
    n_reclaim: int,
    earliest: str,
    dist_control_primary: dict[str, Any] | None,
    subgroup_spread: float | None,
    best_price_auc: float | None,
    best_ob_auc: float | None,
    best_flow_auc: float | None,
) -> str:
    if n_accepted < 5 or n_reclaim < 5:
        return "SAMPLE_INSUFFICIENT"

    d_only = dist_control_primary.get("auc_distance_only") if dist_control_primary else None
    d_combo = dist_control_primary.get("auc_distance_plus_ob_flow") if dist_control_primary else None
    ob_only = dist_control_primary.get("auc_ob_only") if dist_control_primary else None
    flow_only = dist_control_primary.get("auc_flow_only") if dist_control_primary else None

    if subgroup_spread is not None and subgroup_spread > 0.25 and n_accepted >= 6 and n_reclaim >= 6:
        # Only flag dependence when both classes are reasonably represented overall
        return "POST_BREAK_SIGNAL_SYMBOL_OR_DIRECTION_DEPENDENT"

    if earliest in {"BREAK_PLUS_60S", "BREAK_PLUS_120S"}:
        return "POST_BREAK_SIGNAL_ONLY_AFTER_CONFIRMATION"

    if earliest == "NO_ROBUST_SEPARATION":
        if d_only is not None and d_only >= 0.62 and (
            d_combo is None or (d_combo - d_only) < 0.025
        ):
            return "POST_BREAK_SIGNAL_IS_PRICE_ONLY"
        if d_only is not None and d_combo is not None and (d_combo - d_only) < 0.02:
            return "POST_BREAK_OB_FLOW_ADDS_NO_ROBUST_VALUE"
        return "POST_BREAK_OB_FLOW_ADDS_NO_ROBUST_VALUE"

    # Price-only: distance already strong; tiny-sample perfect combo is not trustworthy.
    if d_only is not None and d_only >= 0.75:
        if d_combo is not None and d_combo >= 0.99 and (n_accepted + n_reclaim) < 20:
            return "POST_BREAK_SIGNAL_IS_PRICE_ONLY"
        if d_combo is not None and (d_combo - d_only) < 0.05:
            if (best_ob_auc or 0) <= d_only + 0.02 and (best_flow_auc or 0) < d_only - 0.05:
                return "POST_BREAK_SIGNAL_IS_PRICE_ONLY"
            if (d_combo - d_only) < 0.025:
                return "POST_BREAK_SIGNAL_IS_PRICE_ONLY"

    mapping = {
        "BREAK_PLUS_1S": "POST_BREAK_ACCEPTANCE_SIGNAL_WITHIN_5S",
        "BREAK_PLUS_2S": "POST_BREAK_ACCEPTANCE_SIGNAL_WITHIN_5S",
        "BREAK_PLUS_5S": "POST_BREAK_ACCEPTANCE_SIGNAL_WITHIN_5S",
        "BREAK_PLUS_10S": "POST_BREAK_ACCEPTANCE_SIGNAL_WITHIN_10S",
        "BREAK_PLUS_20S": "POST_BREAK_ACCEPTANCE_SIGNAL_WITHIN_20S",
        "BREAK_PLUS_30S": "POST_BREAK_ACCEPTANCE_SIGNAL_WITHIN_30S",
    }
    if earliest in mapping:
        if d_only is not None and d_combo is not None and (d_combo - d_only) < 0.025 and d_only >= 0.62:
            return "POST_BREAK_SIGNAL_IS_PRICE_ONLY"
        return mapping[earliest]

    return "POST_BREAK_OB_FLOW_ADDS_NO_ROBUST_VALUE"


def subgroup_auc(
    rows: list[dict[str, Any]],
    feature: str,
    *,
    cutoff: int,
) -> list[dict[str, Any]]:
    keys = [
        ("all", lambda r: True),
        ("APTUSDT", lambda r: r.get("symbol") == "APTUSDT"),
        ("DOGEUSDT", lambda r: r.get("symbol") == "DOGEUSDT"),
        ("bearish", lambda r: r.get("direction") == "bearish"),
        ("bullish", lambda r: r.get("direction") == "bullish"),
        ("1h", lambda r: r.get("timeframe") == "1h"),
        ("4h", lambda r: r.get("timeframe") == "4h"),
    ]
    out = []
    for name, pred in keys:
        sub = [r for r in rows if pred(r)]
        cmp_ = feature_auc_at_cutoff(sub, feature, cutoff=cutoff)
        cmp_["subgroup"] = name
        out.append(cmp_)
    return out


def cutoff_snapshot(
    rows: list[dict[str, Any]],
    *,
    cutoff: int,
    price_feats: list[str],
    ob_feats: list[str],
    flow_feats: list[str],
) -> dict[str, Any]:
    best_p = max((feature_auc_at_cutoff(rows, f, cutoff=cutoff) for f in price_feats), key=lambda x: x["auc"] or 0)
    best_o = max((feature_auc_at_cutoff(rows, f, cutoff=cutoff) for f in ob_feats), key=lambda x: x["auc"] or 0)
    best_f = max((feature_auc_at_cutoff(rows, f, cutoff=cutoff) for f in flow_feats), key=lambda x: x["auc"] or 0)
    dc = distance_control_rows(rows, cutoff=cutoff)
    return {
        "cutoff": cutoff,
        "best_price_feature": best_p["feature"],
        "best_price_auc": best_p["auc"],
        "best_ob_feature": best_o["feature"],
        "best_ob_auc": best_o["auc"],
        "best_flow_feature": best_f["feature"],
        "best_flow_auc": best_f["auc"],
        "n_accepted": best_p["n_accepted"],
        "n_reclaim": best_p["n_reclaim"],
        **{k: v for k, v in dc.items() if k != "cutoff"},
    }


def count_outcomes(inventory: list[dict[str, Any]]) -> dict[str, Any]:
    c = Counter(r.get("outcome") for r in inventory)
    return {
        "n": len(inventory),
        "BREAK_ACCEPTED": c.get(OUTCOME_ACCEPTED, 0),
        "RECLAIM": c.get(OUTCOME_RECLAIM, 0),
        "AMBIGUOUS": c.get("AMBIGUOUS", 0),
    }
