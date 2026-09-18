"""Feature comparison stats (Cliff's delta, Spearman) — descriptive only."""

from __future__ import annotations

import math
from typing import Any, Sequence


def _finite(xs: Sequence[Any]) -> list[float]:
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


def median(xs: Sequence[float]) -> float | None:
    ys = sorted(_finite(xs))
    if not ys:
        return None
    m = len(ys) // 2
    return ys[m] if len(ys) % 2 else 0.5 * (ys[m - 1] + ys[m])


def quantile(xs: Sequence[float], q: float) -> float | None:
    ys = sorted(_finite(xs))
    if not ys:
        return None
    if len(ys) == 1:
        return ys[0]
    pos = q * (len(ys) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return ys[lo]
    return ys[lo] * (hi - pos) + ys[hi] * (pos - lo)


def cliffs_delta(a: Sequence[float], b: Sequence[float]) -> float | None:
    """Cliff's delta: P(a>b) - P(a<b). Positive => a tends larger."""
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
    n = len(aa) * len(bb)
    return (gt - lt) / n if n else None


def spearman(x: Sequence[Any], y: Sequence[Any]) -> float | None:
    pairs = []
    for a, b in zip(x, y):
        try:
            fa, fb = float(a), float(b)
        except (TypeError, ValueError):
            continue
        if math.isfinite(fa) and math.isfinite(fb):
            pairs.append((fa, fb))
    n = len(pairs)
    if n < 3:
        return None
    # rank
    def ranks(vals: list[float]) -> list[float]:
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        r = [0.0] * len(vals)
        i = 0
        while i < len(vals):
            j = i
            while j + 1 < len(vals) and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg = 0.5 * (i + j) + 1.0
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    rx, ry = ranks(xs), ranks(ys)
    mx = sum(rx) / n
    my = sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    denx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    deny = math.sqrt(sum((b - my) ** 2 for b in ry))
    if denx <= 0 or deny <= 0:
        return None
    return num / (denx * deny)


def compare_feature(
    values_a: Sequence[Any],
    values_b: Sequence[Any],
    *,
    feature: str,
    group_a: str,
    group_b: str,
) -> dict[str, Any]:
    aa, bb = _finite(values_a), _finite(values_b)
    miss_a = 1.0 - (len(aa) / len(values_a) if values_a else 0.0)
    miss_b = 1.0 - (len(bb) / len(values_b) if values_b else 0.0)
    return {
        "feature": feature,
        "group_a": group_a,
        "group_b": group_b,
        "n_a": len(aa),
        "n_b": len(bb),
        "missing_rate_a": miss_a,
        "missing_rate_b": miss_b,
        "median_a": median(aa),
        "median_b": median(bb),
        "q25_a": quantile(aa, 0.25),
        "q75_a": quantile(aa, 0.75),
        "q25_b": quantile(bb, 0.25),
        "q75_b": quantile(bb, 0.75),
        "cliffs_delta": cliffs_delta(aa, bb),
        "units": "percent_or_raw_feature",
    }
