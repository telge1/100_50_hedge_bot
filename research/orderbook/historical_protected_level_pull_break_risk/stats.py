"""Descriptive stats, AUC, matched controls, decision logic."""

from __future__ import annotations

import math
import random
from collections import Counter, defaultdict
from typing import Any


BREAK = "LEVEL_BREAK"
HOLD = "LEVEL_HOLD_REJECT"


def _finite(xs: list[float]) -> list[float]:
    return [x for x in xs if x is not None and isinstance(x, (int, float)) and math.isfinite(float(x))]


def summarize(xs: list[float]) -> dict[str, Any]:
    vals = sorted(_finite([float(x) for x in xs]))
    n = len(vals)
    if n == 0:
        return {"n": 0, "mean": None, "median": None, "q25": None, "q75": None}
    def q(p: float) -> float:
        if n == 1:
            return vals[0]
        idx = p * (n - 1)
        lo = int(math.floor(idx))
        hi = int(math.ceil(idx))
        if lo == hi:
            return vals[lo]
        w = idx - lo
        return vals[lo] * (1 - w) + vals[hi] * w
    return {
        "n": n,
        "mean": sum(vals) / n,
        "median": q(0.5),
        "q25": q(0.25),
        "q75": q(0.75),
    }


def cliffs_delta(a: list[float], b: list[float]) -> float | None:
    aa, bb = _finite(a), _finite(b)
    if not aa or not bb:
        return None
    gt = lt = 0
    for x in aa:
        for y in bb:
            if x > y:
                gt += 1
            elif x < y:
                lt += 1
    return (gt - lt) / (len(aa) * len(bb))


def mann_whitney_auc(scores: list[float], labels: list[int]) -> float | None:
    """AUC treating label=1 as positive. Equivalent to Mann–Whitney U / (n0*n1)."""
    pairs = [(s, y) for s, y in zip(scores, labels) if s is not None and math.isfinite(float(s))]
    pos = [s for s, y in pairs if y == 1]
    neg = [s for s, y in pairs if y == 0]
    if not pos or not neg:
        return None
    # average ranks for ties
    from collections import defaultdict

    buckets: dict[float, list[int]] = defaultdict(list)
    sorted_vals = sorted(float(s) for s, _ in pairs)
    for i, v in enumerate(sorted_vals, start=1):
        buckets[v].append(i)
    avg_rank = {v: sum(idxs) / len(idxs) for v, idxs in buckets.items()}
    sum_pos = sum(avg_rank[float(s)] for s in pos)
    n1, n0 = len(pos), len(neg)
    u = sum_pos - n1 * (n1 + 1) / 2.0
    return u / (n1 * n0)


def feature_comparison(
    rows: list[dict[str, Any]],
    feature: str,
    *,
    label_key: str = "outcome",
) -> dict[str, Any]:
    breaks = [r[feature] for r in rows if r.get(label_key) == BREAK]
    holds = [r[feature] for r in rows if r.get(label_key) == HOLD]
    sb, sh = summarize(breaks), summarize(holds)
    med_diff = None
    if sb["median"] is not None and sh["median"] is not None:
        med_diff = sb["median"] - sh["median"]
    labels = []
    scores = []
    for r in rows:
        if r.get(label_key) not in {BREAK, HOLD}:
            continue
        if r.get(feature) is None:
            continue
        scores.append(float(r[feature]))
        labels.append(1 if r[label_key] == BREAK else 0)
    return {
        "feature": feature,
        "n_break": sb["n"],
        "n_hold": sh["n"],
        "median_break": sb["median"],
        "median_hold": sh["median"],
        "mean_break": sb["mean"],
        "mean_hold": sh["mean"],
        "q25_break": sb["q25"],
        "q75_break": sb["q75"],
        "q25_hold": sh["q25"],
        "q75_hold": sh["q75"],
        "median_diff_break_minus_hold": med_diff,
        "cliffs_delta": cliffs_delta(
            [float(x) for x in breaks if x is not None],
            [float(x) for x in holds if x is not None],
        ),
        "auc": mann_whitney_auc(scores, labels),
    }


def auc_distance_pull(
    rows: list[dict[str, Any]],
    pull_feat: str,
    dist_feat: str = "distance_to_level_bps",
) -> dict[str, Any]:
    """Simple AUCs: pull-only, distance-only (closer=higher risk → negate dist), combined rank."""
    usable = [
        r
        for r in rows
        if r.get("outcome") in {BREAK, HOLD}
        and r.get(pull_feat) is not None
        and r.get(dist_feat) is not None
    ]
    if len(usable) < 4:
        return {
            "pull_feature": pull_feat,
            "n": len(usable),
            "auc_pull_only": None,
            "auc_distance_only": None,
            "auc_distance_plus_pull": None,
        }
    y = [1 if r["outcome"] == BREAK else 0 for r in usable]
    pull = [float(r[pull_feat]) for r in usable]
    # smaller distance → higher break risk
    dist_score = [-float(r[dist_feat]) for r in usable]
    # z-ish combine via ranks
    def ranks(xs: list[float]) -> list[float]:
        order = sorted(range(len(xs)), key=lambda i: xs[i])
        out = [0.0] * len(xs)
        for rank, i in enumerate(order, start=1):
            out[i] = float(rank)
        return out
    rp, rd = ranks(pull), ranks(dist_score)
    combo = [a + b for a, b in zip(rp, rd)]
    return {
        "pull_feature": pull_feat,
        "n": len(usable),
        "auc_pull_only": mann_whitney_auc(pull, y),
        "auc_distance_only": mann_whitney_auc(dist_score, y),
        "auc_distance_plus_pull": mann_whitney_auc(combo, y),
    }


def bootstrap_median_diff(
    rows: list[dict[str, Any]],
    feature: str,
    *,
    n_boot: int = 400,
    seed: int = 42,
) -> dict[str, Any]:
    rng = random.Random(seed)
    b = [float(r[feature]) for r in rows if r.get("outcome") == BREAK and r.get(feature) is not None]
    h = [float(r[feature]) for r in rows if r.get("outcome") == HOLD and r.get(feature) is not None]
    if len(b) < 2 or len(h) < 2:
        return {"feature": feature, "ci_low": None, "ci_high": None, "n_boot": 0}
    diffs = []
    for _ in range(n_boot):
        bb = [b[rng.randrange(len(b))] for _ in range(len(b))]
        hh = [h[rng.randrange(len(h))] for _ in range(len(h))]
        diffs.append(sorted(bb)[len(bb) // 2] - sorted(hh)[len(hh) // 2])
    diffs.sort()
    return {
        "feature": feature,
        "ci_low": diffs[int(0.025 * len(diffs))],
        "ci_high": diffs[int(0.975 * len(diffs))],
        "n_boot": n_boot,
    }


def jackknife_auc(
    rows: list[dict[str, Any]],
    feature: str,
) -> dict[str, Any]:
    usable = [
        r
        for r in rows
        if r.get("outcome") in {BREAK, HOLD} and r.get(feature) is not None
    ]
    if len(usable) < 5:
        return {"feature": feature, "auc_full": None, "auc_min": None, "auc_max": None, "fragile": True}
    y = [1 if r["outcome"] == BREAK else 0 for r in usable]
    s = [float(r[feature]) for r in usable]
    full = mann_whitney_auc(s, y)
    leave = []
    for i in range(len(usable)):
        ss = s[:i] + s[i + 1 :]
        yy = y[:i] + y[i + 1 :]
        a = mann_whitney_auc(ss, yy)
        if a is not None:
            leave.append(a)
    return {
        "feature": feature,
        "auc_full": full,
        "auc_min": min(leave) if leave else None,
        "auc_max": max(leave) if leave else None,
        "fragile": (max(leave) - min(leave) > 0.15) if leave else True,
    }


def match_controls(
    rows: list[dict[str, Any]],
    *,
    dist_tol_bps: float = 15.0,
    speed_tol: float = 50.0,
) -> list[dict[str, Any]]:
    """Nearest hold control per break within symbol/direction/timeframe buckets."""
    breaks = [r for r in rows if r.get("outcome") == BREAK]
    holds = [r for r in rows if r.get("outcome") == HOLD]
    used_holds: set[str] = set()
    out: list[dict[str, Any]] = []
    for br in breaks:
        cands = []
        for h in holds:
            if h["approach_id"] in used_holds:
                continue
            if h["symbol"] != br["symbol"]:
                continue
            if h["direction"] != br["direction"]:
                continue
            if h["timeframe"] != br["timeframe"]:
                continue
            if br.get("distance_to_level_bps") is None or h.get("distance_to_level_bps") is None:
                continue
            dd = abs(float(br["distance_to_level_bps"]) - float(h["distance_to_level_bps"]))
            if dd > dist_tol_bps:
                continue
            sd = 0.0
            if br.get("approach_speed_bps_per_min") is not None and h.get("approach_speed_bps_per_min") is not None:
                sd = abs(float(br["approach_speed_bps_per_min"]) - float(h["approach_speed_bps_per_min"]))
                if sd > speed_tol:
                    continue
            vol_d = 0.0
            if br.get("short_term_vol_bps") is not None and h.get("short_term_vol_bps") is not None:
                vol_d = abs(float(br["short_term_vol_bps"]) - float(h["short_term_vol_bps"]))
            score = dd + 0.1 * sd + 0.5 * vol_d
            cands.append((score, h, dd, sd, vol_d))
        if not cands:
            out.append(
                {
                    "break_approach_id": br["approach_id"],
                    "hold_approach_id": None,
                    "matched": False,
                    "reason": "no_control",
                }
            )
            continue
        cands.sort(key=lambda x: x[0])
        score, h, dd, sd, vol_d = cands[0]
        used_holds.add(h["approach_id"])
        pull_b = br.get("primary_pull_feature")
        pull_h = h.get("primary_pull_feature")
        out.append(
            {
                "break_approach_id": br["approach_id"],
                "hold_approach_id": h["approach_id"],
                "matched": True,
                "symbol": br["symbol"],
                "direction": br["direction"],
                "timeframe": br["timeframe"],
                "dist_diff_bps": dd,
                "speed_diff": sd,
                "vol_diff": vol_d,
                "pull_break": pull_b,
                "pull_hold": pull_h,
                "pull_diff_break_minus_hold": (
                    float(pull_b) - float(pull_h)
                    if pull_b is not None and pull_h is not None
                    else None
                ),
                "distance_break": br.get("distance_to_level_bps"),
                "distance_hold": h.get("distance_to_level_bps"),
            }
        )
    return out


def subgroup_stats(
    rows: list[dict[str, Any]],
    feature: str,
) -> list[dict[str, Any]]:
    keys = [
        ("all", lambda r: True),
        ("bearish", lambda r: r.get("direction") == "bearish"),
        ("bullish", lambda r: r.get("direction") == "bullish"),
        ("APTUSDT", lambda r: r.get("symbol") == "APTUSDT"),
        ("DOGEUSDT", lambda r: r.get("symbol") == "DOGEUSDT"),
        ("1h", lambda r: r.get("timeframe") == "1h"),
        ("4h", lambda r: r.get("timeframe") == "4h"),
    ]
    out = []
    for name, pred in keys:
        sub = [r for r in rows if pred(r)]
        cmp_ = feature_comparison(sub, feature)
        cmp_["subgroup"] = name
        out.append(cmp_)
    return out


def earliest_separation(
    rows: list[dict[str, Any]],
    *,
    min_auc: float = 0.58,
    min_n: int = 8,
) -> str:
    """Earliest post-10bps-anchor window where pull beats distance-only incrementally.

    If Distance-only already dominates and Pull adds <2pp AUC, return NO_EARLY_SEPARATION
    (pull is not an early break warning beyond proximity).
    """
    base = auc_distance_pull(rows, "passive_removal_excess_pct_30s")
    if (
        base.get("auc_distance_only") is not None
        and base.get("auc_distance_plus_pull") is not None
        and (base["auc_distance_plus_pull"] - base["auc_distance_only"]) < 0.02
    ):
        return "NO_EARLY_SEPARATION"

    for off, tag in (
        (10, "AT_10BPS_APPROACH"),
        (30, "AT_10BPS_APPROACH"),
        (60, "AT_10BPS_APPROACH"),
    ):
        feat = f"passive_removal_excess_pct_{off}s"
        cmp_ = feature_comparison(rows, feat)
        aucs = auc_distance_pull(rows, feat)
        if (
            cmp_["auc"] is not None
            and cmp_["auc"] >= min_auc
            and cmp_["n_break"] + cmp_["n_hold"] >= min_n
            and aucs.get("auc_distance_only") is not None
            and aucs.get("auc_distance_plus_pull") is not None
            and (aucs["auc_distance_plus_pull"] - aucs["auc_distance_only"]) >= 0.02
        ):
            return tag
    return "NO_EARLY_SEPARATION"


def decide_primary(
    *,
    n_break: int,
    n_hold: int,
    n_ambiguous: int,
    auc_pull: float | None,
    auc_dist: float | None,
    auc_combo: float | None,
    matched_pull_diff_median: float | None,
    cliffs: float | None,
    fragile: bool,
    subgroup_spread: float | None,
) -> str:
    if n_break < 5 or n_hold < 5:
        return "PROTECTED_LEVEL_CONTROL_SAMPLE_INSUFFICIENT"
    if auc_pull is None:
        return "NO_ROBUST_PULL_BREAK_SIGNAL"
    # distance proxy: pull AUC ~ dist AUC and combo barely better
    if auc_dist is not None and auc_combo is not None:
        if abs(auc_pull - auc_dist) < 0.03 and (auc_combo - max(auc_pull, auc_dist)) < 0.02:
            if auc_pull < 0.58:
                return "PULL_IS_DISTANCE_PROXY"
    if subgroup_spread is not None and subgroup_spread > 0.20 and auc_pull >= 0.58:
        return "PULL_SIGNAL_SYMBOL_OR_DIRECTION_DEPENDENT"
    if cliffs is not None and abs(cliffs) < 0.15 and (auc_pull is None or 0.45 <= auc_pull <= 0.55):
        return "PULL_COMMON_IN_BREAK_AND_HOLD"
    if (
        auc_pull >= 0.62
        and auc_combo is not None
        and auc_dist is not None
        and (auc_combo - auc_dist) >= 0.05
        and not fragile
        and matched_pull_diff_median is not None
        and matched_pull_diff_median > 0
    ):
        return "PULL_IS_EARLY_BREAK_WARNING"
    if (
        auc_pull >= 0.55
        and auc_combo is not None
        and auc_dist is not None
        and (auc_combo - auc_dist) >= 0.02
        and not fragile
    ):
        return "PULL_ADDS_BREAK_RISK_CONTEXT_BUT_NOT_STANDALONE"
    if auc_dist is not None and auc_pull is not None and auc_dist > auc_pull + 0.05:
        return "PULL_IS_DISTANCE_PROXY"
    if 0.45 <= auc_pull <= 0.55:
        return "PULL_COMMON_IN_BREAK_AND_HOLD"
    return "NO_ROBUST_PULL_BREAK_SIGNAL"


def pick_examples(rows: list[dict[str, Any]], n: int = 8) -> list[dict[str, Any]]:
    feat = "primary_pull_feature"
    breaks = [r for r in rows if r.get("outcome") == BREAK and r.get(feat) is not None]
    holds = [r for r in rows if r.get("outcome") == HOLD and r.get(feat) is not None]
    breaks_sorted = sorted(breaks, key=lambda r: float(r[feat]), reverse=True)
    holds_sorted = sorted(holds, key=lambda r: float(r[feat]), reverse=True)
    picks: list[dict[str, Any]] = []

    def add(r: dict[str, Any], tag: str) -> None:
        picks.append(
            {
                "example_tag": tag,
                "approach_id": r["approach_id"],
                "symbol": r["symbol"],
                "direction": r["direction"],
                "timeframe": r["timeframe"],
                "outcome": r["outcome"],
                "pull": r.get(feat),
                "distance_to_level_bps": r.get("distance_to_level_bps"),
                "anchor_type": r.get("anchor_type"),
            }
        )

    if breaks_sorted:
        add(breaks_sorted[0], "BREAK_STRONG_PULL")
    if breaks_sorted:
        add(breaks_sorted[-1], "BREAK_WEAK_PULL")
    if holds_sorted:
        add(holds_sorted[0], "HOLD_STRONG_PULL")
    if holds_sorted:
        add(holds_sorted[-1], "HOLD_WEAK_PULL")
    # refill-ish hold: high consumption ratio
    holds_cons = [
        r
        for r in holds
        if r.get("consumption_ratio_30s") is not None and float(r["consumption_ratio_30s"]) > 0.5
    ]
    if holds_cons:
        add(sorted(holds_cons, key=lambda r: float(r["consumption_ratio_30s"]), reverse=True)[0], "HOLD_ABSORPTIONISH")
    # diversity
    for r in breaks_sorted[1:4]:
        if len(picks) >= n:
            break
        if r["approach_id"] not in {p["approach_id"] for p in picks}:
            add(r, "BREAK_EXTRA")
    for r in holds_sorted[1:4]:
        if len(picks) >= n:
            break
        if r["approach_id"] not in {p["approach_id"] for p in picks}:
            add(r, "HOLD_EXTRA")
    return picks[:n]


def count_outcomes(approaches: list[dict[str, Any]]) -> dict[str, Any]:
    c = Counter(a.get("outcome") for a in approaches)
    by_sym: dict[str, Counter] = defaultdict(Counter)
    by_dir: dict[str, Counter] = defaultdict(Counter)
    by_tf: dict[str, Counter] = defaultdict(Counter)
    for a in approaches:
        by_sym[a["symbol"]][a["outcome"]] += 1
        by_dir[a["direction"]][a["outcome"]] += 1
        by_tf[a["timeframe"]][a["outcome"]] += 1
    return {
        "total": len(approaches),
        "LEVEL_BREAK": c.get(BREAK, 0),
        "LEVEL_HOLD_REJECT": c.get(HOLD, 0),
        "AMBIGUOUS": c.get("AMBIGUOUS", 0),
        "by_symbol": {k: dict(v) for k, v in by_sym.items()},
        "by_direction": {k: dict(v) for k, v in by_dir.items()},
        "by_timeframe": {k: dict(v) for k, v in by_tf.items()},
    }
