"""Trade windows and wall-size dynamic proxies for attack episodes."""

from __future__ import annotations

import bisect
from typing import Any

import pandas as pd

from orderbook_analyse.l2_wall_attack_discovery.models import (
    ATTACK_SIDE_BY_WALL,
    empty_proxy,
    safe_div,
    safe_float,
    tick_size,
)
from orderbook_analyse.ob200_v3_raw_discovery.reconstruct import SampleRow

PRE_WINDOWS = (
    ("m60_m30", -60, -30),
    ("m30_m10", -30, -10),
    ("m10_m5", -10, -5),
    ("m5_0", -5, 0),
)
POST_WINDOWS = (
    ("p0_1", 0, 1),
    ("p0_3", 0, 3),
    ("p0_5", 0, 5),
    ("p0_10", 0, 10),
    ("p0_30", 0, 30),
    ("p0_60", 0, 60),
)
PHASE_WINDOWS = (
    ("first_1s", 0, 1),
    ("seconds_1_to_3", 1, 3),
    ("seconds_3_to_5", 3, 5),
    ("seconds_5_to_10", 5, 10),
    ("seconds_10_to_30", 10, 30),
    ("seconds_30_to_60", 30, 60),
)
PROXY_HORIZONS_S = (1, 3, 5, 10, 30, 60)


def _sample_at(samples: list[SampleRow], ts_ms: int) -> SampleRow | None:
    if not samples:
        return None
    lo, hi = 0, len(samples) - 1
    ans = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if samples[mid].ts_ms <= ts_ms:
            ans = samples[mid]
            lo = mid + 1
        else:
            hi = mid - 1
    return ans


def _qty(sample: SampleRow | None, side: str) -> float | None:
    if sample is None:
        return None
    return safe_float(sample.bid_wall_qty if side == "BID" else sample.ask_wall_qty)


def _mid(sample: SampleRow | None) -> float | None:
    return None if sample is None else safe_float(sample.mid)


def window_trade_stats(
    trades: pd.DataFrame,
    *,
    start_ms: int,
    end_ms: int,
    wall_price: float,
    side: str,
    symbol: str,
    ts_list: list[int] | None = None,
) -> dict[str, Any]:
    empty = {
        "window_start_ms": start_ms,
        "window_end_ms": end_ms,
        "trades_present": False,
        "trade_count": None,
        "aggressive_buy_notional": None,
        "aggressive_sell_notional": None,
        "attack_side_notional": None,
        "opposite_side_notional": None,
        "total_notional": None,
        "net_aggressive_notional": None,
        "trade_imbalance": None,
        "vwap": None,
        "trade_price_min": None,
        "trade_price_max": None,
        "trades_at_wall_price": None,
        "trades_within_1_tick": None,
        "trades_within_2_tick": None,
        "trades_within_3_tick": None,
        "notional_at_wall_band_3t": None,
        "trade_intensity_per_s": None,
        "mean_inter_trade_ms": None,
        "burstiness": None,
        "max_trade_notional": None,
        "p50_trade_notional": None,
        "p90_trade_notional": None,
        "p99_trade_notional": None,
    }
    if trades.empty or end_ms <= start_ms:
        return empty
    if ts_list is not None:
        i0 = bisect.bisect_left(ts_list, start_ms)
        i1 = bisect.bisect_left(ts_list, end_ms)
        sub = trades.iloc[i0:i1]
    else:
        sub = trades[(trades["ts_ms"] >= start_ms) & (trades["ts_ms"] < end_ms)]
    if sub.empty:
        return empty
    buy = float(sub.loc[sub["side"] == "Buy", "notional"].sum())
    sell = float(sub.loc[sub["side"] == "Sell", "notional"].sum())
    attack = ATTACK_SIDE_BY_WALL[side]
    attack_n = sell if attack == "Sell" else buy
    opp_n = buy if attack == "Sell" else sell
    total = buy + sell
    tick = tick_size(symbol)
    dist = (sub["price"] - wall_price).abs()
    at_px = int((dist <= tick * 0.5).sum())
    w1 = int((dist <= tick).sum())
    w2 = int((dist <= 2 * tick).sum())
    w3 = int((dist <= 3 * tick).sum())
    band_n = float(sub.loc[dist <= 3 * tick, "notional"].sum())
    notionals = sub["notional"].astype(float)
    ts = sub["ts_ms"].astype(int).tolist()
    gaps = [ts[i] - ts[i - 1] for i in range(1, len(ts))] if len(ts) > 1 else []
    mean_gap = sum(gaps) / len(gaps) if gaps else None
    burst = None
    if gaps and mean_gap and mean_gap > 0:
        var = sum((g - mean_gap) ** 2 for g in gaps) / len(gaps)
        burst = (var**0.5) / mean_gap
    dur_s = max((end_ms - start_ms) / 1000.0, 1e-9)
    qs = notionals.quantile([0.5, 0.9, 0.99])
    size_sum = float(sub["size"].sum())
    px_sum = float((sub["price"] * sub["size"]).sum())
    return {
        "window_start_ms": start_ms,
        "window_end_ms": end_ms,
        "trades_present": True,
        "trade_count": int(len(sub)),
        "aggressive_buy_notional": buy,
        "aggressive_sell_notional": sell,
        "attack_side_notional": attack_n,
        "opposite_side_notional": opp_n,
        "total_notional": total,
        "net_aggressive_notional": buy - sell,
        "trade_imbalance": safe_div(buy - sell, total),
        "vwap": safe_div(px_sum, size_sum),
        "trade_price_min": float(sub["price"].min()),
        "trade_price_max": float(sub["price"].max()),
        "trades_at_wall_price": at_px,
        "trades_within_1_tick": w1,
        "trades_within_2_tick": w2,
        "trades_within_3_tick": w3,
        "notional_at_wall_band_3t": band_n,
        "trade_intensity_per_s": len(sub) / dur_s,
        "mean_inter_trade_ms": mean_gap,
        "burstiness": burst,
        "max_trade_notional": float(notionals.max()),
        "p50_trade_notional": float(qs.loc[0.5]),
        "p90_trade_notional": float(qs.loc[0.9]),
        "p99_trade_notional": float(qs.loc[0.99]),
    }


def compute_trade_windows(
    episode: dict[str, Any],
    trades: pd.DataFrame,
    *,
    ts_list: list[int] | None = None,
) -> list[dict[str, Any]]:
    fc = episode.get("first_contact_at")
    if fc is None:
        return []
    out = []
    wall_price = float(episode.get("wall_price_at_contact") or 0)
    side = episode["side"]
    symbol = episode["symbol"]
    tsl = ts_list
    if tsl is None and not trades.empty:
        tsl = trades["ts_ms"].astype(int).tolist()
    for name, a, b in PRE_WINDOWS + POST_WINDOWS + PHASE_WINDOWS:
        st = window_trade_stats(
            trades,
            start_ms=int(fc) + a * 1000,
            end_ms=int(fc) + b * 1000,
            wall_price=wall_price,
            side=side,
            symbol=symbol,
            ts_list=tsl,
        )
        out.append({"attack_id": episode["attack_id"], "window": name, "semantic_role": "causal_feature", **st})
    return out


def compute_size_dynamics(
    episode: dict[str, Any],
    samples: list[SampleRow],
    trades: pd.DataFrame,
    *,
    trade_ts_list: list[int] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    fc = episode.get("first_contact_at")
    side = episode["side"]
    symbol = episode["symbol"]
    wall_price = float(episode.get("wall_price_at_contact") or 0)
    if fc is None:
        return [], []

    s0 = _sample_at(samples, int(fc))
    q0 = _qty(s0, side)
    m0 = _mid(s0)
    dyn_rows: list[dict[str, Any]] = []
    proxy_rows: list[dict[str, Any]] = []
    tsl = trade_ts_list
    if tsl is None and not trades.empty:
        tsl = trades["ts_ms"].astype(int).tolist()

    # precompute sample slice peaks with bisect on sample times
    sample_ts = [s.ts_ms for s in samples]

    for h in PROXY_HORIZONS_S:
        t1 = int(fc) + h * 1000
        s1 = _sample_at(samples, t1)
        q1 = _qty(s1, side)
        m1 = _mid(s1)
        tw = window_trade_stats(
            trades,
            start_ms=int(fc),
            end_ms=t1,
            wall_price=wall_price,
            side=side,
            symbol=symbol,
            ts_list=tsl,
        )
        removed = None if q0 is None or q1 is None else max(0.0, q0 - q1)
        added = None if q0 is None or q1 is None else max(0.0, q1 - q0)
        peak_after = q0
        i0 = bisect.bisect_right(sample_ts, int(fc))
        i1 = bisect.bisect_right(sample_ts, t1)
        for s in samples[i0:i1]:
            q = _qty(s, side)
            if q is not None and (peak_after is None or q > peak_after):
                peak_after = q
        refill_from_trough = None
        if q0 is not None and removed is not None and peak_after is not None:
            refill_from_trough = max(0.0, peak_after - (q0 - removed))

        band_qty_proxy = None
        if tw.get("notional_at_wall_band_3t") is not None and wall_price > 0:
            band_qty_proxy = tw["notional_at_wall_band_3t"] / wall_price

        raw_bps = None if m0 is None or m1 is None or m0 <= 0 else (m1 - m0) / m0 * 10000
        if episode["side"] == "BID":
            side_bps = None if raw_bps is None else -raw_bps
        else:
            side_bps = raw_bps

        deplete = safe_div(removed, q0)
        refill = safe_div(
            refill_from_trough if refill_from_trough is not None else added,
            max(removed or 0, 1e-12) if removed else None,
        )
        if removed is not None and removed <= 0:
            refill = 0.0 if (added is None or added <= 0) else 1.0
        t2d = safe_div(band_qty_proxy, q0)
        resili = safe_div(q1, q0)
        prn = safe_div(abs(side_bps) if side_bps is not None else None, tw.get("attack_side_notional"))

        pull = False
        if q0 and removed is not None and removed / q0 >= 0.5:
            if (band_qty_proxy or 0) < 0.25 * removed:
                pull = True
        absorb = False
        if (tw.get("attack_side_notional") or 0) > 0 and q1 is not None and q0 is not None:
            if q1 >= 0.5 * q0 and side_bps is not None and abs(side_bps) < 3.0:
                absorb = True

        conf = "LOW"
        unexplained = None
        if q0 is not None and removed is not None and band_qty_proxy is not None:
            unexplained = removed - band_qty_proxy
            frac = abs(unexplained) / max(q0, 1e-12)
            if frac < 0.35 and tw.get("trades_present"):
                conf = "HIGH"
            elif frac < 0.75 and tw.get("trades_present"):
                conf = "MEDIUM"

        align = 0 if tw.get("trades_present") and tw.get("trade_count") else None

        dyn_rows.append(
            {
                "attack_id": episode["attack_id"],
                "horizon_s": h,
                "semantic_role": "causal_feature",
                "visible_size_at_contact": q0,
                "visible_size_at_horizon": q1,
                "visible_size_removed": removed,
                "visible_size_added": added,
                "peak_size_in_window": peak_after,
                "mid_at_contact": m0,
                "mid_at_horizon": m1,
                "raw_price_change_bps": raw_bps,
                "side_adjusted_price_change_bps": side_bps,
                "attack_side_notional": tw.get("attack_side_notional"),
                "trades_present": tw.get("trades_present"),
            }
        )
        proxy = empty_proxy()
        proxy.update(
            {
                "attack_id": episode["attack_id"],
                "horizon_s": h,
                "semantic_role": "causal_feature",
                "visible_size_at_contact": q0,
                "visible_size_removed": removed,
                "traded_at_level_proxy": band_qty_proxy,
                "depletion_ratio": deplete,
                "refill_ratio": refill,
                "trade_to_display_ratio": t2d,
                "resilience_ratio": resili,
                "price_response_per_notional": prn,
                "pull_proxy": pull,
                "absorption_proxy": absorb,
                "attribution_confidence": conf,
                "timing_alignment_ms": align,
                "unexplained_size_change": unexplained,
            }
        )
        proxy_rows.append(proxy)
    return dyn_rows, proxy_rows
