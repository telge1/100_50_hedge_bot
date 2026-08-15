"""Deterministic AUC / bootstrap / jackknife helpers."""

from __future__ import annotations

import math
import random
from typing import Any, Sequence


def to_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return x


def quantile(xs: Sequence[float], q: float) -> float | None:
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


def best_auc(pos: list[float], neg: list[float]) -> tuple[float | None, str | None]:
    a = mann_whitney_auc(pos, neg)
    b = mann_whitney_auc(neg, pos)
    if a is None or b is None:
        return None, None
    if a >= b:
        return a, "higher→BREAK_ACCEPTED"
    return b, "higher→OTHER"


def bootstrap_auc_ci(
    pos: list[float],
    neg: list[float],
    *,
    orientation: str | None = None,
    n_boot: int = 500,
    seed: int = 42,
) -> dict[str, Any]:
    if not pos or not neg:
        return {"auc": None, "ci_low": None, "ci_high": None, "orientation": orientation}
    if orientation not in {"higher→BREAK_ACCEPTED", "higher→OTHER"}:
        _, orientation = best_auc(pos, neg)
        orientation = orientation or "higher→BREAK_ACCEPTED"

    def auc_fixed(p: list[float], n: list[float]) -> float | None:
        if orientation == "higher→BREAK_ACCEPTED":
            return mann_whitney_auc(p, n)
        return mann_whitney_auc(n, p)

    base = auc_fixed(pos, neg)
    rng = random.Random(seed)
    samples = []
    for _ in range(n_boot):
        pp = [pos[rng.randrange(len(pos))] for _ in range(len(pos))]
        nn = [neg[rng.randrange(len(neg))] for _ in range(len(neg))]
        u = auc_fixed(pp, nn)
        if u is not None:
            samples.append(u)
    samples.sort()
    if not samples:
        return {"auc": base, "ci_low": None, "ci_high": None, "orientation": orientation}
    lo = samples[int(0.025 * (len(samples) - 1))]
    hi = samples[int(0.975 * (len(samples) - 1))]
    return {"auc": base, "ci_low": lo, "ci_high": hi, "orientation": orientation}


def median_diff(a: list[float], b: list[float]) -> float | None:
    ma, mb = quantile(a, 0.5), quantile(b, 0.5)
    if ma is None or mb is None:
        return None
    return ma - mb


def zscore(xs: list[float]) -> list[float]:
    if not xs:
        return []
    m = sum(xs) / len(xs)
    var = sum((x - m) ** 2 for x in xs) / len(xs)
    sd = math.sqrt(var)
    if sd <= 1e-12:
        return [0.0 for _ in xs]
    return [(x - m) / sd for x in xs]


def average_rank_score(matrix: list[list[float | None]], *, higher_is_break: list[bool]) -> list[float | None]:
    """Per-row average of oriented ranks across columns. No fitted weights."""
    if not matrix:
        return []
    n_cols = len(matrix[0])
    n_rows = len(matrix)
    ranks = [[None] * n_cols for _ in range(n_rows)]
    for c in range(n_cols):
        vals = [(i, matrix[i][c]) for i in range(n_rows) if matrix[i][c] is not None]
        vals.sort(key=lambda t: t[1])  # type: ignore[arg-type, return-value]
        # average ranks for ties
        i = 0
        while i < len(vals):
            j = i
            while j < len(vals) and vals[j][1] == vals[i][1]:
                j += 1
            avg_rank = (i + j - 1) / 2.0
            for k in range(i, j):
                idx = vals[k][0]
                r = avg_rank
                if not higher_is_break[c]:
                    r = (len(vals) - 1) - r
                ranks[idx][c] = r
            i = j
    out: list[float | None] = []
    for i in range(n_rows):
        present = [ranks[i][c] for c in range(n_cols) if ranks[i][c] is not None]
        if not present:
            out.append(None)
        else:
            out.append(sum(present) / len(present))  # type: ignore[arg-type]
    return out


def jackknife_auc(
    event_scores: list[tuple[str, float, str]],
    *,
    pos_label: str = "BREAK_ACCEPTED",
    seed_note: str = "deterministic_loo",
) -> dict[str, Any]:
    """Leave-one-event-out AUC stability. event_scores: (event_id, score, outcome)."""
    if len(event_scores) < 5:
        return {
            "n": len(event_scores),
            "full_auc": None,
            "loo_auc_mean": None,
            "loo_auc_min": None,
            "loo_auc_max": None,
            "loo_auc_std": None,
            "max_drop": None,
            "note": seed_note,
        }
    pos = [s for _, s, y in event_scores if y == pos_label]
    neg = [s for _, s, y in event_scores if y != pos_label]
    full, ori = best_auc(pos, neg)
    loo = []
    for drop_id, _, _ in event_scores:
        kept = [(s, y) for eid, s, y in event_scores if eid != drop_id]
        p = [s for s, y in kept if y == pos_label]
        n = [s for s, y in kept if y != pos_label]
        if not p or not n:
            continue
        # fix orientation to full-sample orientation
        if ori == "higher→BREAK_ACCEPTED":
            u = mann_whitney_auc(p, n)
        else:
            u = mann_whitney_auc(n, p)
        if u is not None:
            loo.append(u)
    if not loo or full is None:
        return {
            "n": len(event_scores),
            "full_auc": full,
            "orientation": ori,
            "loo_auc_mean": None,
            "loo_auc_min": None,
            "loo_auc_max": None,
            "loo_auc_std": None,
            "max_drop": None,
            "note": seed_note,
        }
    mean = sum(loo) / len(loo)
    var = sum((x - mean) ** 2 for x in loo) / len(loo)
    return {
        "n": len(event_scores),
        "full_auc": full,
        "orientation": ori,
        "loo_auc_mean": mean,
        "loo_auc_min": min(loo),
        "loo_auc_max": max(loo),
        "loo_auc_std": math.sqrt(var),
        "max_drop": full - min(loo),
        "note": seed_note,
    }
