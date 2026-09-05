"""Rate estimation with honest uncertainty.

A pooled Wilson interval over ~2000 crypto windows implies a precision that
does not exist: 51 USDT perpetuals share most of their variance, and all
windows on one date share a market regime. The cluster bootstraps resample
whole symbols and whole dates, which keeps that dependence in the interval.
"""

from __future__ import annotations

import math
import random
from typing import Callable, Sequence

from . import OUTCOME_AMBIGUOUS, OUTCOME_TIMEOUT
from .contracts import RateEstimate

Z95 = 1.959963984540054


def wilson_interval(successes: int, trials: int, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval. Independence assumed — usually optimistic here."""
    if trials <= 0:
        return 0.0, 0.0
    p = successes / trials
    denom = 1.0 + (z * z) / trials
    centre = p + (z * z) / (2 * trials)
    spread = z * math.sqrt(p * (1 - p) / trials + (z * z) / (4 * trials * trials))
    return max(0.0, (centre - spread) / denom), min(1.0, (centre + spread) / denom)


def cluster_bootstrap_interval(
    groups: dict[str, tuple[int, int]],
    *,
    iters: int,
    seed: int,
    alpha: float = 0.05,
) -> tuple[float, float]:
    """Percentile CI from resampling whole clusters with replacement.

    `groups` maps a cluster key to ``(successes, trials)``. Clusters, not
    individual events, are the sampling unit.
    """
    keys = [k for k, (_, t) in groups.items() if t > 0]
    if len(keys) < 2:
        return 0.0, 1.0
    rng = random.Random(seed)
    rates: list[float] = []
    for _ in range(max(1, iters)):
        s = t = 0
        for _ in range(len(keys)):
            k = keys[rng.randrange(len(keys))]
            gs, gt = groups[k]
            s += gs
            t += gt
        if t > 0:
            rates.append(s / t)
    if not rates:
        return 0.0, 1.0
    rates.sort()
    lo_i = int(math.floor((alpha / 2) * (len(rates) - 1)))
    hi_i = int(math.ceil((1 - alpha / 2) * (len(rates) - 1)))
    return rates[lo_i], rates[hi_i]


def estimate_rate(
    label: str,
    events: Sequence,
    *,
    success_outcome: str,
    failure_outcome: str,
    symbol_key: Callable[[object], str],
    date_key: Callable[[object], str],
    iters: int,
    seed: int,
) -> RateEstimate:
    """Rate of `success_outcome` among resolved (success or failure) events.

    Timeouts and ambiguous races are excluded from the denominator and
    reported separately. `worst_case_rate` folds every ambiguous race into the
    failure column, which bounds how much they could matter.
    """
    resolved = [e for e in events if e.outcome in (success_outcome, failure_outcome)]
    ambiguous = sum(1 for e in events if e.outcome == OUTCOME_AMBIGUOUS)
    timeouts = sum(1 for e in events if e.outcome == OUTCOME_TIMEOUT)

    trials = len(resolved)
    successes = sum(1 for e in resolved if e.outcome == success_outcome)
    rate = (successes / trials) if trials else 0.0

    by_symbol: dict[str, tuple[int, int]] = {}
    by_date: dict[str, tuple[int, int]] = {}
    for e in resolved:
        ok = 1 if e.outcome == success_outcome else 0
        for bucket, key in ((by_symbol, symbol_key(e)), (by_date, date_key(e))):
            s, t = bucket.get(key, (0, 0))
            bucket[key] = (s + ok, t + 1)

    w_lo, w_hi = wilson_interval(successes, trials)
    s_lo, s_hi = cluster_bootstrap_interval(by_symbol, iters=iters, seed=seed)
    d_lo, d_hi = cluster_bootstrap_interval(by_date, iters=iters, seed=seed + 1)

    worst_denom = trials + ambiguous
    worst = (successes / worst_denom) if worst_denom else 0.0

    return RateEstimate(
        label=label,
        successes=successes,
        trials=trials,
        rate=rate,
        wilson_low=w_lo,
        wilson_high=w_hi,
        cluster_symbol_low=s_lo,
        cluster_symbol_high=s_hi,
        cluster_date_low=d_lo,
        cluster_date_high=d_hi,
        ambiguous=ambiguous,
        timeouts=timeouts,
        worst_case_rate=worst,
    )


def estimate_binary_rate(
    label: str,
    items: Sequence,
    *,
    flag: Callable[[object], bool],
    symbol_key: Callable[[object], str],
    date_key: Callable[[object], str],
    iters: int,
    seed: int,
) -> RateEstimate:
    """Rate for a plain boolean outcome (no barrier race, so no ambiguity)."""
    trials = len(items)
    successes = sum(1 for x in items if flag(x))

    by_symbol: dict[str, tuple[int, int]] = {}
    by_date: dict[str, tuple[int, int]] = {}
    for x in items:
        ok = 1 if flag(x) else 0
        for bucket, key in ((by_symbol, symbol_key(x)), (by_date, date_key(x))):
            s, t = bucket.get(key, (0, 0))
            bucket[key] = (s + ok, t + 1)

    w_lo, w_hi = wilson_interval(successes, trials)
    s_lo, s_hi = cluster_bootstrap_interval(by_symbol, iters=iters, seed=seed)
    d_lo, d_hi = cluster_bootstrap_interval(by_date, iters=iters, seed=seed + 1)
    rate = (successes / trials) if trials else 0.0

    return RateEstimate(
        label=label,
        successes=successes,
        trials=trials,
        rate=rate,
        wilson_low=w_lo,
        wilson_high=w_hi,
        cluster_symbol_low=s_lo,
        cluster_symbol_high=s_hi,
        cluster_date_low=d_lo,
        cluster_date_high=d_hi,
        ambiguous=0,
        timeouts=0,
        worst_case_rate=rate,
    )


def difference_bootstrap(
    group_a: Sequence,
    group_b: Sequence,
    *,
    flag: Callable[[object], bool],
    cluster_key: Callable[[object], str],
    iters: int,
    seed: int,
    alpha: float = 0.05,
) -> tuple[float, float, float]:
    """CI for ``rate(A) - rate(B)`` under a shared cluster resample.

    Clusters are drawn once per iteration and applied to both groups, so the
    common market regime cancels instead of inflating the difference.
    """
    keys = sorted({cluster_key(x) for x in list(group_a) + list(group_b)})
    if len(keys) < 2:
        return 0.0, -1.0, 1.0

    a_by: dict[str, list[bool]] = {k: [] for k in keys}
    b_by: dict[str, list[bool]] = {k: [] for k in keys}
    for x in group_a:
        a_by[cluster_key(x)].append(bool(flag(x)))
    for x in group_b:
        b_by[cluster_key(x)].append(bool(flag(x)))

    def rate(vals: list[bool]) -> float | None:
        return (sum(vals) / len(vals)) if vals else None

    point_a = rate([v for k in keys for v in a_by[k]])
    point_b = rate([v for k in keys for v in b_by[k]])
    point = (point_a or 0.0) - (point_b or 0.0)

    rng = random.Random(seed)
    diffs: list[float] = []
    for _ in range(max(1, iters)):
        drawn = [keys[rng.randrange(len(keys))] for _ in range(len(keys))]
        a_vals = [v for k in drawn for v in a_by[k]]
        b_vals = [v for k in drawn for v in b_by[k]]
        ra, rb = rate(a_vals), rate(b_vals)
        if ra is not None and rb is not None:
            diffs.append(ra - rb)
    if not diffs:
        return point, -1.0, 1.0
    diffs.sort()
    lo_i = int(math.floor((alpha / 2) * (len(diffs) - 1)))
    hi_i = int(math.ceil((1 - alpha / 2) * (len(diffs) - 1)))
    return point, diffs[lo_i], diffs[hi_i]
