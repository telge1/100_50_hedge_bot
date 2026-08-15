"""Descriptive stats, simple AUC, earliest useful time."""

from __future__ import annotations

import math
import random
from collections import defaultdict
from typing import Any, Sequence

from research.orderbook.ch_break_reclaim_microstructure_audit.features import EARLIEST_TIME_ORDER

# Features evaluated for discrimination (direction-normalized / causal)
CANDIDATE_FEATURES = (
    "support_near_depth",
    "break_side_near_depth",
    "support_minus_break_depth",
    "imbalance_0_10",
    "imbalance_0_25",
    "support_wall_notional",
    "break_wall_notional",
    "support_depth_change_5s",
    "support_depth_change_10s",
    "support_depth_change_30s",
    "support_pull_10s",
    "support_refill_10s",
    "support_persistence_30s",
    "support_persistence_60s",
    "spread_bps",
    "signed_distance_beyond_bps",
    "bbo_beyond_level",
    "flow_5s_signed_break",
    "flow_10s_signed_break",
    "flow_30s_signed_break",
    "flow_60s_signed_break",
    "flow_30s_n_trades",
    "flow_30s_largest",
    "flow_30s_signed_move_bps",
    "abs_move_per_1k_flow",
    "abs_support_refill_30s",
    "bid_depth_0_10",
    "ask_depth_0_10",
    "bid_depth_bps_0_5",
    "ask_depth_bps_0_5",
)

EARLY_TIMEPOINTS = (
    "PRE_TOUCH_30S",
    "PRE_TOUCH_10S",
    "FIRST_TOUCH",
    "FIRST_BREAK",
    "BREAK_PLUS_5S",
    "BREAK_PLUS_10S",
    "BREAK_PLUS_20S",
    "BREAK_PLUS_30S",
    "BREAK_PLUS_60S",
)


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return x


def _quantile(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    ys = sorted(xs)
    if len(ys) == 1:
        return ys[0]
    pos = (len(ys) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return ys[lo]
    return ys[lo] * (1 - (pos - lo)) + ys[hi] * (pos - lo)


def summarize(xs: Sequence[float | None]) -> dict[str, Any]:
    vals = [x for x in (_to_float(v) for v in xs) if x is not None]
    n = len(vals)
    if n == 0:
        return {"n": 0, "mean": None, "median": None, "q25": None, "q75": None, "std": None}
    mean = sum(vals) / n
    var = sum((x - mean) ** 2 for x in vals) / n
    return {
        "n": n,
        "mean": mean,
        "median": _quantile(vals, 0.5),
        "q25": _quantile(vals, 0.25),
        "q75": _quantile(vals, 0.75),
        "std": math.sqrt(var),
    }


def cohens_d(a: list[float], b: list[float]) -> float | None:
    if len(a) < 2 or len(b) < 2:
        return None
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    va = sum((x - ma) ** 2 for x in a) / (len(a) - 1)
    vb = sum((x - mb) ** 2 for x in b) / (len(b) - 1)
    pooled = math.sqrt(((len(a) - 1) * va + (len(b) - 1) * vb) / (len(a) + len(b) - 2))
    if pooled <= 1e-12:
        return 0.0
    return (ma - mb) / pooled


def mann_whitney_auc(pos: list[float], neg: list[float]) -> float | None:
    """AUC treating higher feature → positive class. Ties count 0.5."""
    if not pos or not neg:
        return None
    score = 0.0
    for p in pos:
        for n in neg:
            if p > n:
                score += 1.0
            elif p == n:
                score += 0.5
    return score / (len(pos) * len(neg))


def bootstrap_median_diff_ci(
    a: list[float],
    b: list[float],
    *,
    n_boot: int = 400,
    seed: int = 7,
) -> dict[str, Any]:
    if not a or not b:
        return {"median_diff": None, "ci_low": None, "ci_high": None}
    rng = random.Random(seed)
    base = _quantile(a, 0.5) - _quantile(b, 0.5)
    diffs = []
    for _ in range(n_boot):
        aa = [a[rng.randrange(len(a))] for _ in range(len(a))]
        bb = [b[rng.randrange(len(b))] for _ in range(len(b))]
        diffs.append(_quantile(aa, 0.5) - _quantile(bb, 0.5))
    diffs.sort()
    lo = diffs[int(0.025 * (len(diffs) - 1))]
    hi = diffs[int(0.975 * (len(diffs) - 1))]
    return {"median_diff": base, "ci_low": lo, "ci_high": hi}


def group_feature_values(
    rows: list[dict[str, Any]],
    *,
    feature: str,
    timepoint: str,
    outcome_a: str,
    outcome_b: str,
    valid_only: bool = True,
) -> tuple[list[float], list[float]]:
    a: list[float] = []
    b: list[float] = []
    for r in rows:
        if r.get("timepoint") != timepoint:
            continue
        if valid_only and r.get("data_quality") not in {None, "DATA_VALID", "DATA_WARNING"}:
            # prefer VALID; allow WARNING in sensitivity — main path filters VALID in caller
            pass
        if valid_only and r.get("data_quality") == "DATA_INVALID":
            continue
        if valid_only and r.get("data_quality") == "DATA_WARNING":
            continue
        v = _to_float(r.get(feature))
        if v is None:
            continue
        lab = r.get("outcome_label")
        if lab == outcome_a:
            a.append(v)
        elif lab == outcome_b:
            b.append(v)
    return a, b


def compute_timepoint_statistics(
    feature_rows: list[dict[str, Any]],
    *,
    outcomes: tuple[str, str] = ("BREAK_ACCEPTED", "RECLAIM_FAST"),
    features: Sequence[str] = CANDIDATE_FEATURES,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    timepoints = sorted({r["timepoint"] for r in feature_rows}, key=lambda t: EARLIEST_TIME_ORDER.index(t) if t in EARLIEST_TIME_ORDER else 99)
    for tp in timepoints:
        for feat in features:
            pos, neg = group_feature_values(
                feature_rows,
                feature=feat,
                timepoint=tp,
                outcome_a=outcomes[0],
                outcome_b=outcomes[1],
                valid_only=True,
            )
            sp = summarize(pos)
            sn = summarize(neg)
            ci = bootstrap_median_diff_ci(pos, neg)
            d = cohens_d(pos, neg)
            auc = mann_whitney_auc(pos, neg)
            # also AUC with flipped orientation (max)
            auc_flip = mann_whitney_auc(neg, pos)
            auc_best = None
            orientation = None
            if auc is not None and auc_flip is not None:
                if auc >= auc_flip:
                    auc_best, orientation = auc, f"higher→{outcomes[0]}"
                else:
                    auc_best, orientation = auc_flip, f"higher→{outcomes[1]}"
            out.append(
                {
                    "timepoint": tp,
                    "feature": feat,
                    "outcome_a": outcomes[0],
                    "outcome_b": outcomes[1],
                    "n_a": sp["n"],
                    "n_b": sn["n"],
                    "median_a": sp["median"],
                    "median_b": sn["median"],
                    "mean_a": sp["mean"],
                    "mean_b": sn["mean"],
                    "q25_a": sp["q25"],
                    "q75_a": sp["q75"],
                    "q25_b": sn["q25"],
                    "q75_b": sn["q75"],
                    "median_diff_a_minus_b": ci["median_diff"],
                    "median_diff_ci_low": ci["ci_low"],
                    "median_diff_ci_high": ci["ci_high"],
                    "cohens_d": d,
                    "auc": auc_best,
                    "auc_orientation": orientation,
                    "separable": int(
                        auc_best is not None
                        and auc_best >= 0.65
                        and sp["n"] >= 3
                        and sn["n"] >= 3
                        and ci["ci_low"] is not None
                        and ci["ci_high"] is not None
                        and (ci["ci_low"] > 0 or ci["ci_high"] < 0)
                    ),
                }
            )
    return out


def compute_group_statistics(feature_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per outcome × timepoint × feature descriptive stats."""
    buckets: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    for r in feature_rows:
        if r.get("data_quality") != "DATA_VALID":
            continue
        lab = r.get("outcome_label")
        tp = r.get("timepoint")
        if not lab or not tp:
            continue
        for feat in CANDIDATE_FEATURES:
            v = _to_float(r.get(feat))
            if v is None:
                continue
            buckets[(lab, tp, feat)].append(v)
    rows = []
    for (lab, tp, feat), vals in sorted(buckets.items()):
        s = summarize(vals)
        rows.append({"outcome_label": lab, "timepoint": tp, "feature": feat, **s})
    return rows


def earliest_useful_times(timepoint_stats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_feat: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in timepoint_stats:
        by_feat[r["feature"]].append(r)

    results = []
    for feat, rows in by_feat.items():
        ranked = []
        for r in rows:
            tp = r["timepoint"]
            if tp not in EARLY_TIMEPOINTS and tp != "PRE_TOUCH_1M" and tp != "PRE_TOUCH_2M" and tp != "PRE_TOUCH_5M":
                if tp == "POSTMORTEM_PLUS_5M":
                    continue
            if tp == "BREAK_PLUS_120S":
                continue
            ranked.append(r)
        ranked.sort(
            key=lambda r: EARLIEST_TIME_ORDER.index(r["timepoint"])
            if r["timepoint"] in EARLIEST_TIME_ORDER
            else 99
        )
        earliest = None
        best_auc = None
        for r in ranked:
            if r.get("separable"):
                earliest = r["timepoint"]
                best_auc = r["auc"]
                break
        if earliest is None:
            # any AUC>=0.6 with n>=3 each as weak
            for r in ranked:
                if r.get("auc") is not None and r["auc"] >= 0.60 and r["n_a"] >= 3 and r["n_b"] >= 3:
                    earliest = "TOO_LATE" if r["timepoint"] in {"BREAK_PLUS_60S", "POSTMORTEM_PLUS_5M"} else r["timepoint"]
                    # if only post-break 60s+, mark TOO_LATE when auc only there
                    best_auc = r["auc"]
                    # keep searching for earlier — this sets first weak; continue for strong only already done
                    break
            if earliest is None:
                earliest = "NO_SIGNAL"
        # refine TOO_LATE: if first separable/weak is only after confirmation horizons
        if earliest in {"BREAK_PLUS_60S", "POSTMORTEM_PLUS_5M"}:
            label = "TOO_LATE"
        elif earliest == "NO_SIGNAL":
            label = "NO_SIGNAL"
        else:
            label = earliest
        results.append(
            {
                "feature": feat,
                "earliest_useful_time": label,
                "best_auc_at_earliest": best_auc,
                "comparison": "BREAK_ACCEPTED_vs_RECLAIM_FAST",
            }
        )
    return results


def top_features(timepoint_stats: list[dict[str, Any]], *, k: int = 5) -> list[dict[str, Any]]:
    """Best features by max AUC across early timepoints."""
    best: dict[str, dict[str, Any]] = {}
    for r in timepoint_stats:
        if r["timepoint"] not in EARLY_TIMEPOINTS and r["timepoint"] not in {
            "PRE_TOUCH_1M",
            "PRE_TOUCH_2M",
            "PRE_TOUCH_5M",
        }:
            continue
        if r["auc"] is None or r["n_a"] < 3 or r["n_b"] < 3:
            continue
        prev = best.get(r["feature"])
        if prev is None or (r["auc"] or 0) > (prev["auc"] or 0):
            best[r["feature"]] = r
    ranked = sorted(best.values(), key=lambda r: r["auc"] or 0, reverse=True)
    return ranked[:k]


def stratum_auc_table(
    feature_rows: list[dict[str, Any]],
    *,
    feature: str,
    timepoint: str,
) -> list[dict[str, Any]]:
    """Symbol / direction strata for one feature@timepoint."""
    out = []
    for stratum_key, stratum_val in (
        ("all", None),
        ("symbol", "APTUSDT"),
        ("symbol", "DOGEUSDT"),
        ("break_direction", "bearish"),
        ("break_direction", "bullish"),
    ):
        subset = feature_rows
        if stratum_key != "all":
            subset = [r for r in feature_rows if r.get(stratum_key) == stratum_val]
        pos, neg = group_feature_values(
            subset,
            feature=feature,
            timepoint=timepoint,
            outcome_a="BREAK_ACCEPTED",
            outcome_b="RECLAIM_FAST",
            valid_only=True,
        )
        # also vs all reclaim/hold
        pos2, neg2 = [], []
        for r in subset:
            if r.get("timepoint") != timepoint or r.get("data_quality") != "DATA_VALID":
                continue
            v = _to_float(r.get(feature))
            if v is None:
                continue
            if r.get("outcome_label") == "BREAK_ACCEPTED":
                pos2.append(v)
            elif r.get("outcome_label") in {"RECLAIM_FAST", "RECLAIM_SLOW", "HOLD_NO_BREAK"}:
                neg2.append(v)

        def best_auc(a: list[float], b: list[float]) -> tuple[float | None, str | None]:
            u = mann_whitney_auc(a, b)
            uf = mann_whitney_auc(b, a)
            if u is None or uf is None:
                return None, None
            if u >= uf:
                return u, "higher→BREAK_ACCEPTED"
            return uf, "higher→other"

        auc_rf, ori_rf = best_auc(pos, neg)
        auc_rest, ori_rest = best_auc(pos2, neg2)
        out.append(
            {
                "stratum": "all" if stratum_key == "all" else f"{stratum_key}={stratum_val}",
                "feature": feature,
                "timepoint": timepoint,
                "n_break": len(pos),
                "n_reclaim_fast": len(neg),
                "auc_vs_reclaim_fast": auc_rf,
                "auc_vs_reclaim_fast_orientation": ori_rf,
                "n_break_vs_rest": len(pos2),
                "n_rest": len(neg2),
                "auc_vs_reclaim_hold": auc_rest,
                "auc_vs_reclaim_hold_orientation": ori_rest,
            }
        )
    return out


def decide_primary(
    *,
    n_valid: int,
    n_events: int,
    earliest_rows: list[dict[str, Any]],
    top: list[dict[str, Any]],
    stratum_rows: list[dict[str, Any]] | None = None,
) -> str:
    if n_valid < 10:
        return "DATA_INSUFFICIENT"
    strong_early = [
        r
        for r in earliest_rows
        if r["earliest_useful_time"]
        in {"PRE_TOUCH_30S", "PRE_TOUCH_10S", "PRE_TOUCH_1M", "PRE_TOUCH_2M", "PRE_TOUCH_5M"}
        and (r.get("best_auc_at_earliest") or 0) >= 0.65
    ]
    at_touch = [
        r
        for r in earliest_rows
        if r["earliest_useful_time"] == "FIRST_TOUCH" and (r.get("best_auc_at_earliest") or 0) >= 0.65
    ]
    within_10 = [
        r
        for r in earliest_rows
        if r["earliest_useful_time"]
        in {"FIRST_BREAK", "BREAK_PLUS_5S", "BREAK_PLUS_10S"}
        and (r.get("best_auc_at_earliest") or 0) >= 0.65
    ]
    late = [
        r
        for r in earliest_rows
        if r["earliest_useful_time"]
        in {"BREAK_PLUS_20S", "BREAK_PLUS_30S", "BREAK_PLUS_60S", "TOO_LATE"}
        and (r.get("best_auc_at_earliest") or 0) >= 0.65
    ]

    # Stratum consistency: if top feature fails in a major stratum, mark dependent.
    dependent = False
    if stratum_rows and top:
        feat = top[0]["feature"]
        tp = top[0]["timepoint"]
        key_strata = [
            s
            for s in stratum_rows
            if s.get("feature") == feat and s.get("timepoint") == tp and s.get("stratum") != "all"
        ]
        aucs = [s.get("auc_vs_reclaim_fast") for s in key_strata if s.get("auc_vs_reclaim_fast") is not None]
        if aucs:
            if min(aucs) < 0.60 or (max(aucs) - min(aucs)) > 0.25:
                dependent = True

    if dependent and (strong_early or at_touch or within_10 or late or top):
        return "OB_FLOW_SIGNAL_SYMBOL_OR_DIRECTION_DEPENDENT"
    if strong_early:
        return "OB_FLOW_SIGNAL_VISIBLE_PRE_TOUCH"
    if at_touch:
        return "OB_FLOW_SIGNAL_VISIBLE_AT_TOUCH"
    if within_10:
        return "OB_FLOW_SIGNAL_VISIBLE_WITHIN_10S_AFTER_BREAK"
    if late:
        return "OB_FLOW_SIGNAL_VISIBLE_ONLY_AFTER_CONFIRMATION"
    if top and any((t.get("auc") or 0) >= 0.65 for t in top):
        return "OB_FLOW_SIGNAL_SYMBOL_OR_DIRECTION_DEPENDENT"
    return "OB_FLOW_NO_ROBUST_SIGNAL"
