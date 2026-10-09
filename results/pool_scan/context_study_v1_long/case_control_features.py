"""Entry-time-only features for long geometry case-control (descriptive)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def last_closed_index(bars: list[dict], moment: datetime) -> int | None:
    chosen = None
    for index, bar in enumerate(bars):
        if bar["close_time"] <= moment:
            chosen = index
        else:
            break
    return chosen


def active_pools(pools: list[dict], moment: datetime, side: str | None = None) -> list[dict]:
    rows = []
    for p in pools:
        if side and p.get("side") != side:
            continue
        kn = p["known"]
        if kn > moment:
            continue
        br = p.get("break_at")
        if br is not None and br <= moment:
            continue
        rows.append(p)
    return rows


def price_clusters(lowers: list[dict], gap_pct: float = 0.15) -> list[dict]:
    rows = sorted(lowers, key=lambda x: x["bottom"])
    clusters: list[list[dict]] = []
    for row in rows:
        placed = False
        for cl in clusters:
            hi = max(x["top"] for x in cl)
            lo = min(x["bottom"] for x in cl)
            if row["bottom"] <= hi * (1 + gap_pct / 100.0) and row["top"] >= lo * (1 - gap_pct / 100.0):
                cl.append(row)
                placed = True
                break
        if not placed:
            clusters.append([row])
    out = []
    for i, cl in enumerate(clusters, start=1):
        out.append(
            {
                "cluster_index": i,
                "lowest_bottom": min(x["bottom"] for x in cl),
                "highest_top": max(x["top"] for x in cl),
                "pool_count": len(cl),
                "earliest_known": min(x["known"] for x in cl),
            }
        )
    out.sort(key=lambda c: c["lowest_bottom"])
    return out


def rising_n(clusters_below: list[dict], n: int) -> bool:
    steps = [c for c in clusters_below if c["highest_top"] < 1e100]
    if len(steps) < n:
        return False
    tops = [steps[i]["highest_top"] for i in range(n)]
    return all(tops[i] < tops[i + 1] for i in range(n - 1))


def falling_n_upper(clusters_above: list[dict], n: int) -> bool:
    """Mirror of rising lowers: descending cluster bottoms among uppers above price."""
    if len(clusters_above) < n:
        return False
    bottoms = [clusters_above[i]["lowest_bottom"] for i in range(n)]
    return all(bottoms[i] > bottoms[i + 1] for i in range(n - 1))


def upper_clusters_above(pools: list[dict], moment: datetime, price: float) -> list[dict]:
    uppers = uppers_above_price(pools, moment, price)
    rows = [{"bottom": p["bottom"], "top": p["top"], "known": p["known"], "pool_id": p["pool_id"]} for p in uppers]
    clusters = price_clusters(rows)
    return sorted(clusters, key=lambda c: c["lowest_bottom"])


def lowers_under_price(pools: list[dict], moment: datetime, price: float) -> list[dict]:
    return [
        p
        for p in active_pools(pools, moment, "lower")
        if float(p["top"]) < price
    ]


def uppers_above_price(pools: list[dict], moment: datetime, price: float) -> list[dict]:
    return [
        p
        for p in active_pools(pools, moment, "upper")
        if float(p["bottom"]) > price
    ]


def count_within_pct(rows: list[dict], price: float, pct: float, side: str) -> int:
    n = 0
    for p in rows:
        if side == "lower":
            edge = float(p["top"])
            dist = (price - edge) / price * 100.0
        else:
            edge = float(p["bottom"])
            dist = (edge - price) / price * 100.0
        if 0 <= dist <= pct:
            n += 1
    return n


def pool_age_hours(moment: datetime, known: datetime) -> float:
    return (moment - known).total_seconds() / 3600.0


def ema_stack_label(bar: dict) -> str:
    vals = []
    for k in ("ema9", "ema20", "ema59", "ema200"):
        v = bar.get(k)
        if v is None:
            return "incomplete"
        vals.append((k, v))
    c = bar["close"]
    order = sorted(vals, key=lambda x: x[1], reverse=True)
    names = "".join(x[0].replace("ema", "") for x in order)
    if c > bar["ema200"]:
        pos = "above_200"
    elif c < bar["ema200"]:
        pos = "below_200"
    else:
        pos = "at_200"
    return f"{pos}:{names}"


def ema_distance_pct(close: float, ema: float | None) -> float | None:
    if ema is None or ema == 0:
        return None
    return (close - ema) / ema * 100.0


def ema_slope_pct(bars: list[dict], idx: int, field: str, lookback: int = 3) -> float | None:
    if idx is None or idx < lookback:
        return None
    a = bars[idx].get(field)
    b = bars[idx - lookback].get(field)
    if a is None or b is None or b == 0:
        return None
    return (a - b) / b * 100.0


def cross_age_bars(bars: list[dict], idx: int | None, bullish: bool) -> int | None:
    if idx is None:
        return None
    for j in range(idx, -1, -1):
        bar = bars[j]
        if bar["ema200"] is None or bar["ema20"] is None:
            continue
        was_below = bars[j - 1]["close"] < bars[j - 1]["ema200"] if j > 0 else False
        is_above = bar["close"] > bar["ema200"]
        if bullish and is_above and (j == 0 or bars[j - 1]["close"] <= bars[j - 1]["ema200"]):
            return idx - j
        if not bullish and bar["close"] < bar["ema200"] and (j == 0 or bars[j - 1]["close"] >= bars[j - 1]["ema200"]):
            return idx - j
    return None


def candle_features(bar: dict) -> dict[str, float]:
    o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
    rng = h - l
    body = abs(c - o)
    if rng <= 0:
        return {
            "body_pct": 0.0,
            "range_pct": 0.0,
            "lower_wick_pct": 0.0,
            "upper_wick_pct": 0.0,
            "body_range_ratio": 0.0,
            "close_location": 0.5,
        }
    mid = (h + l) / 2.0
    lower_wick = min(o, c) - l
    upper_wick = h - max(o, c)
    return {
        "body_pct": body / c * 100.0 if c else 0.0,
        "range_pct": rng / c * 100.0 if c else 0.0,
        "lower_wick_pct": lower_wick / c * 100.0 if c else 0.0,
        "upper_wick_pct": upper_wick / c * 100.0 if c else 0.0,
        "body_range_ratio": body / rng,
        "close_location": (c - l) / rng,
    }


def prior_return_pct(bars: list[dict], idx: int, n: int) -> float | None:
    if idx is None or idx < n:
        return None
    c0 = bars[idx]["close"]
    c1 = bars[idx - n]["close"]
    if c1 == 0:
        return None
    return (c0 - c1) / c1 * 100.0


def range_pct_window(bars: list[dict], idx: int, n: int) -> float | None:
    if idx is None or idx < n:
        return None
    window = bars[idx - n + 1 : idx + 1]
    hi = max(b["high"] for b in window)
    lo = min(b["low"] for b in window)
    c = bars[idx]["close"]
    if c == 0:
        return None
    return (hi - lo) / c * 100.0


def causal_touches_entry_pool(bars15: list[dict], entry_idx: int, pool_top: float) -> int:
    n = 0
    for i in range(entry_idx):
        bar = bars15[i]
        if bar["low"] <= pool_top and bar["close"] > pool_top:
            n += 1
    return n


def mtf_pool_snapshot(
    pools: list[dict],
    moment: datetime,
    price: float,
    prefix: str,
) -> dict[str, Any]:
    lowers = sorted(lowers_under_price(pools, moment, price), key=lambda p: -p["top"])
    uppers = sorted(uppers_above_price(pools, moment, price), key=lambda p: p["bottom"])
    clusters = price_clusters(
        [{"bottom": p["bottom"], "top": p["top"], "known": p["known"], "pool_id": p["pool_id"]} for p in lowers]
    )
    steps = [c for c in clusters if c["highest_top"] < price]
    out: dict[str, Any] = {f"{prefix}_lower_count": len(lowers), f"{prefix}_upper_count": len(uppers)}
    for pct in (0.5, 1.0, 2.0, 3.0, 5.0):
        out[f"{prefix}_lowers_within_{pct}pct"] = count_within_pct(lowers, price, pct, "lower")
        out[f"{prefix}_uppers_within_{pct}pct"] = count_within_pct(uppers, price, pct, "upper")
    if lowers:
        out[f"{prefix}_nearest_lower_gap_pct"] = (price - lowers[0]["top"]) / price * 100.0
        out[f"{prefix}_nearest_lower_age_h"] = pool_age_hours(moment, lowers[0]["known"])
    else:
        out[f"{prefix}_nearest_lower_gap_pct"] = None
        out[f"{prefix}_nearest_lower_age_h"] = None
    if uppers:
        out[f"{prefix}_nearest_upper_gap_pct"] = (uppers[0]["bottom"] - price) / price * 100.0
        out[f"{prefix}_nearest_upper_age_h"] = pool_age_hours(moment, uppers[0]["known"])
    else:
        out[f"{prefix}_nearest_upper_gap_pct"] = None
        out[f"{prefix}_nearest_upper_age_h"] = None
    out[f"{prefix}_rising_2"] = rising_n(steps, 2) if len(steps) >= 2 else False
    out[f"{prefix}_rising_3"] = rising_n(steps, 3) if len(steps) >= 3 else False
    out[f"{prefix}_rising_4"] = rising_n(steps, 4) if len(steps) >= 4 else False
    for k in (1, 2, 3):
        if len(lowers) >= k:
            out[f"{prefix}_lower_{k}_age_h"] = pool_age_hours(moment, lowers[k - 1]["known"])
        else:
            out[f"{prefix}_lower_{k}_age_h"] = None
    for k in (1, 2):
        if len(lowers) >= k + 1:
            gap = (lowers[k - 1]["top"] - lowers[k]["top"]) / price * 100.0
            out[f"{prefix}_lower_gap_{k}_{k+1}_pct"] = gap
        else:
            out[f"{prefix}_lower_gap_{k}_{k+1}_pct"] = None
    return out


def ema_feature_block(bars: list[dict], idx: int | None, prefix: str) -> dict[str, Any]:
    if idx is None:
        return {}
    bar = bars[idx]
    c = bar["close"]
    out: dict[str, Any] = {
        f"{prefix}_close": c,
        f"{prefix}_dist_ema9_pct": ema_distance_pct(c, bar.get("ema9")),
        f"{prefix}_dist_ema20_pct": ema_distance_pct(c, bar.get("ema20")),
        f"{prefix}_dist_ema59_pct": ema_distance_pct(c, bar.get("ema59")),
        f"{prefix}_dist_ema200_pct": ema_distance_pct(c, bar.get("ema200")),
        f"{prefix}_ema_stack": ema_stack_label(bar),
        f"{prefix}_close_above_ema200": c > bar["ema200"] if bar.get("ema200") else None,
        f"{prefix}_ema20_slope_3b_pct": ema_slope_pct(bars, idx, "ema20", 3),
        f"{prefix}_ema59_slope_3b_pct": ema_slope_pct(bars, idx, "ema59", 3),
        f"{prefix}_ema200_slope_3b_pct": ema_slope_pct(bars, idx, "ema200", 3),
        f"{prefix}_ema20_vs_ema200_pct": ema_distance_pct(bar.get("ema20"), bar.get("ema200")),
        f"{prefix}_ema59_vs_ema200_pct": ema_distance_pct(bar.get("ema59"), bar.get("ema200")),
        f"{prefix}_bull_ema200_cross_age_bars": cross_age_bars(bars, idx, True),
    }
    e59, e200 = bar.get("ema59"), bar.get("ema200")
    if e59 is not None and e200 is not None and e200 != 0:
        out[f"{prefix}_depth_59_200_pct"] = (e59 - e200) / e200 * 100.0
    else:
        out[f"{prefix}_depth_59_200_pct"] = None
    return out
