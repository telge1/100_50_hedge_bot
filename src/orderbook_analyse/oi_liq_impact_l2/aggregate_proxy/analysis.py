"""Per-cluster aggregate wall proxy analysis."""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

import pandas as pd

from orderbook_analyse.oi_liq_impact_l2.aggregate_proxy.constants import (
    AGGRESSIVE_NOTIONAL_COL,
    BASELINE_MINUTES,
    BTCUSDT_TICK,
    DIRECTIONAL_ADD_COL,
    DIRECTIONAL_DEPTH_COL,
    DIRECTIONAL_IMBALANCE_COL,
    DIRECTIONAL_REMOVE_COL,
    HORIZON_MINUTES,
    RECLAIM_ANCHORS,
    TICK_NEAR_SENSITIVITIES,
    TIME_MARKS_MINUTES,
    WALL_BPS_DIST_COLUMN,
    WALL_PRICE_COLUMN,
    WALL_QTY_COLUMN,
    WALL_STATUS_CHANGED,
    WALL_STATUS_EXACT,
    WALL_STATUS_INVALID,
    WALL_STATUS_MISSING,
    WALL_STATUS_NEAR,
)
from orderbook_analyse.oi_liq_impact_l2.wall_absorption.clusters import FlushCluster


def safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def safe_div(num: float | None, den: float | None) -> float | None:
    if num is None or den is None or den == 0:
        return None
    out = num / den
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def wall_status(
    wall_price: float | None,
    anchor_price: float | None,
    *,
    is_genuine: bool,
    tick: Decimal = BTCUSDT_TICK,
) -> str:
    if not is_genuine:
        return WALL_STATUS_INVALID
    if wall_price is None or wall_price <= 0:
        return WALL_STATUS_MISSING
    if anchor_price is None or anchor_price <= 0:
        return WALL_STATUS_CHANGED
    if wall_price == anchor_price:
        return WALL_STATUS_EXACT
    tick_f = float(tick)
    for n in TICK_NEAR_SENSITIVITIES:
        if abs(wall_price - anchor_price) <= n * tick_f:
            return WALL_STATUS_NEAR
    return WALL_STATUS_CHANGED


def near_within_ticks(
    wall_price: float | None, anchor_price: float | None, n_ticks: int
) -> bool:
    if wall_price is None or anchor_price is None or anchor_price <= 0:
        return False
    return abs(wall_price - anchor_price) <= n_ticks * float(BTCUSDT_TICK)


def _slice_ob(
    ob_1s: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp
) -> pd.DataFrame:
    if ob_1s.empty:
        return ob_1s
    mask = (ob_1s["bucket_start"] >= start) & (ob_1s["bucket_start"] < end)
    return ob_1s.loc[mask].copy()


def _slice_trades(
    trades_1s: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp
) -> pd.DataFrame:
    if trades_1s.empty:
        return trades_1s
    mask = (trades_1s["second"] >= start) & (trades_1s["second"] < end)
    return trades_1s.loc[mask].copy()


def _directional_depth(row: pd.Series, direction: str) -> float | None:
    return safe_float(row.get(DIRECTIONAL_DEPTH_COL[direction]))


def _directional_imbalance(row: pd.Series) -> float | None:
    return safe_float(row.get(DIRECTIONAL_IMBALANCE_COL))


def _directional_ofi(row: pd.Series, direction: str) -> float | None:
    ofi = safe_float(row.get("ofi"))
    if ofi is None:
        return None
    return ofi if direction == "LONG" else -ofi


def find_anchor_row(
    ob_1s: pd.DataFrame, cluster_start: pd.Timestamp
) -> pd.Series | None:
    before = ob_1s[ob_1s["bucket_start"] < cluster_start]
    genuine = before[before["is_genuine"]]
    if genuine.empty:
        return None
    return genuine.iloc[-1]


def build_timeline_rows(
    cluster: FlushCluster,
    ob_1s: pd.DataFrame,
    trades_1s: pd.DataFrame,
    anchor_row: pd.Series,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    direction = cluster.direction
    cluster_start = pd.Timestamp(cluster.cluster_start)
    window_start = cluster_start - pd.Timedelta(minutes=BASELINE_MINUTES)
    window_end = cluster_start + pd.Timedelta(minutes=HORIZON_MINUTES)
    ob_slice = _slice_ob(ob_1s, window_start, window_end)
    trade_slice = _slice_trades(trades_1s, window_start, window_end)

    price_col = WALL_PRICE_COLUMN[direction]
    qty_col = WALL_QTY_COLUMN[direction]
    bps_col = WALL_BPS_DIST_COLUMN[direction]
    anchor_price = safe_float(anchor_row.get(price_col))
    anchor_qty = safe_float(anchor_row.get(qty_col))

    timeline: list[dict[str, Any]] = []
    for _, row in ob_slice.iterrows():
        ts = row["bucket_start"]
        genuine = bool(row["is_genuine"])
        wp = safe_float(row.get(price_col))
        wq = safe_float(row.get(qty_col))
        status = wall_status(wp, anchor_price, is_genuine=genuine)
        agg_notional = 0.0
        if not trade_slice.empty and "second" in trade_slice.columns:
            trade_row = trade_slice[trade_slice["second"] == ts]
        else:
            trade_row = trade_slice.iloc[0:0]
        if not trade_row.empty:
            col = AGGRESSIVE_NOTIONAL_COL[direction]
            agg_notional = safe_float(trade_row.iloc[0].get(col)) or 0.0
        timeline.append(
            {
                "cluster_id": cluster.cluster_id,
                "symbol": cluster.symbol,
                "direction": direction,
                "second": ts.isoformat().replace("+00:00", "Z"),
                "phase": (
                    "BASELINE"
                    if ts < cluster_start
                    else "POST_CLUSTER"
                ),
                "is_genuine": genuine,
                "quality_flags": str(row.get("quality_flags") or ""),
                "mid_price": safe_float(row.get("mid_price")),
                "microprice": safe_float(row.get("microprice")),
                "spread_bps": safe_float(row.get("spread_bps")),
                "best_bid_price": safe_float(row.get("best_bid_price")),
                "best_ask_price": safe_float(row.get("best_ask_price")),
                "dominant_wall_price": wp,
                "dominant_wall_qty": wq,
                "dominant_wall_bps_dist": safe_float(row.get(bps_col)),
                "wall_status": status,
                "wall_near_1tick": near_within_ticks(wp, anchor_price, 1),
                "wall_near_2tick": near_within_ticks(wp, anchor_price, 2),
                "wall_near_3tick": near_within_ticks(wp, anchor_price, 3),
                "directional_depth_l50": _directional_depth(row, direction),
                "directional_imbalance_l50": _directional_imbalance(row),
                "directional_ofi": _directional_ofi(row, direction),
                "side_qty_added": safe_float(row.get(DIRECTIONAL_ADD_COL[direction])),
                "side_qty_removed": safe_float(row.get(DIRECTIONAL_REMOVE_COL[direction])),
                "aggressive_notional_1s": agg_notional,
                "processed_updates": int(row.get("processed_updates") or 0),
            }
        )

    post = [r for r in timeline if r["phase"] == "POST_CLUSTER" and r["is_genuine"]]
    if post:
        if direction == "LONG":
            adverse_row = min(post, key=lambda r: r["mid_price"] or float("inf"))
        else:
            adverse_row = max(post, key=lambda r: r["mid_price"] or float("-inf"))
    else:
        adverse_row = None

    anchor_meta = {
        "anchor_second": anchor_row["bucket_start"].isoformat().replace("+00:00", "Z"),
        "anchor_wall_price": anchor_price,
        "anchor_wall_qty": anchor_qty,
        "anchor_mid": safe_float(anchor_row.get("mid_price")),
        "anchor_spread_bps": safe_float(anchor_row.get("spread_bps")),
        "anchor_directional_depth_l50": _directional_depth(anchor_row, direction),
        "anchor_directional_imbalance_l50": _directional_imbalance(anchor_row),
        "anchor_directional_ofi": _directional_ofi(anchor_row, direction),
        "adverse_extreme_second": adverse_row["second"] if adverse_row else None,
        "adverse_extreme_mid": adverse_row["mid_price"] if adverse_row else None,
    }
    return timeline, anchor_meta


def wall_stability_metrics(
    cluster: FlushCluster,
    timeline: list[dict[str, Any]],
    anchor_meta: dict[str, Any],
) -> dict[str, Any]:
    post_genuine = [r for r in timeline if r["phase"] == "POST_CLUSTER" and r["is_genuine"]]
    anchor_price = anchor_meta.get("anchor_wall_price")
    exact_secs = sum(1 for r in post_genuine if r["wall_status"] == WALL_STATUS_EXACT)
    near1 = sum(1 for r in post_genuine if r.get("wall_near_1tick"))
    near2 = sum(1 for r in post_genuine if r.get("wall_near_2tick"))
    near3 = sum(1 for r in post_genuine if r.get("wall_near_3tick"))
    changes = sum(1 for r in post_genuine if r["wall_status"] == WALL_STATUS_CHANGED)
    missing = sum(1 for r in post_genuine if r["wall_status"] == WALL_STATUS_MISSING)
    invalid = sum(1 for r in post_genuine if r["wall_status"] == WALL_STATUS_INVALID)

    first_change_sec: str | None = None
    longest_stable = 0
    current_stable = 0
    for r in post_genuine:
        if r["wall_status"] in (WALL_STATUS_EXACT, WALL_STATUS_NEAR):
            current_stable += 1
            longest_stable = max(longest_stable, current_stable)
        else:
            if first_change_sec is None and r["wall_status"] == WALL_STATUS_CHANGED:
                first_change_sec = r["second"]
            current_stable = 0

    same_price_qty_after: float | None = None
    anchor_qty = anchor_meta.get("anchor_wall_qty")
    if anchor_price and post_genuine:
        same_price = [r for r in post_genuine if r["dominant_wall_price"] == anchor_price]
        if same_price:
            same_price_qty_after = safe_float(same_price[-1].get("dominant_wall_qty"))

    reappeared = False
    if first_change_sec and anchor_price:
        reappeared = any(
            r["second"] > first_change_sec and r["wall_status"] == WALL_STATUS_EXACT
            for r in post_genuine
        )

    return {
        "cluster_id": cluster.cluster_id,
        "direction": cluster.direction,
        "post_genuine_seconds": len(post_genuine),
        "exact_stable_fraction": safe_div(exact_secs, len(post_genuine)),
        "near_1tick_fraction": safe_div(near1, len(post_genuine)),
        "near_2tick_fraction": safe_div(near2, len(post_genuine)),
        "near_3tick_fraction": safe_div(near3, len(post_genuine)),
        "longest_exact_or_near_stable_seconds": longest_stable,
        "first_wall_change_second": first_change_sec,
        "wall_change_count": changes,
        "wall_missing_seconds": missing,
        "wall_invalid_seconds": invalid,
        "anchor_wall_price": anchor_price,
        "anchor_wall_qty": anchor_qty,
        "same_anchor_price_qty_ratio": safe_div(same_price_qty_after, anchor_qty),
        "dominant_wall_reappeared": reappeared,
    }


def aggregate_recovery_metrics(
    cluster: FlushCluster,
    timeline: list[dict[str, Any]],
    anchor_meta: dict[str, Any],
) -> list[dict[str, Any]]:
    direction = cluster.direction
    post_genuine = [r for r in timeline if r["phase"] == "POST_CLUSTER" and r["is_genuine"]]
    anchor_depth = anchor_meta.get("anchor_directional_depth_l50")
    adverse_row = None
    for r in post_genuine:
        if r["second"] == anchor_meta.get("adverse_extreme_second"):
            adverse_row = r
            break
    adverse_depth = adverse_row.get("directional_depth_l50") if adverse_row else None
    adverse_imb = adverse_row.get("directional_imbalance_l50") if adverse_row else None
    adverse_ofi = adverse_row.get("directional_ofi") if adverse_row else None
    rows: list[dict[str, Any]] = []

    for mark in TIME_MARKS_MINUTES:
        target = pd.Timestamp(cluster.cluster_start) + pd.Timedelta(minutes=mark)
        at_mark = [r for r in post_genuine if pd.Timestamp(r["second"]) <= target]
        if not at_mark:
            continue
        row = at_mark[-1]
        depth = row.get("directional_depth_l50")
        imb = row.get("directional_imbalance_l50")
        ofi = row.get("directional_ofi")
        micro = row.get("microprice")
        mid = row.get("mid_price")
        branches: list[str] = []
        if depth is not None and adverse_depth is not None and depth > adverse_depth:
            branches.append("DEPTH")
        if imb is not None and adverse_imb is not None and imb > adverse_imb:
            branches.append("IMBALANCE")
        if ofi is not None and adverse_ofi is not None and ofi > adverse_ofi:
            branches.append("OFI")
        if micro is not None and mid is not None:
            if direction == "LONG" and micro > mid:
                branches.append("MICROPRICE")
            elif direction == "SHORT" and micro < mid:
                branches.append("MICROPRICE")
        rows.append(
            {
                "cluster_id": cluster.cluster_id,
                "direction": direction,
                "mark_minutes": mark,
                "mark_second": row["second"],
                "depth_vs_anchor": (
                    (depth - anchor_depth)
                    if depth is not None and anchor_depth is not None
                    else None
                ),
                "depth_vs_adverse": (
                    (depth - adverse_depth)
                    if depth is not None and adverse_depth is not None
                    else None
                ),
                "recovery_branches": "+".join(branches) if branches else "",
                "aggregate_depth_recovery_observed": "DEPTH" in branches,
            }
        )
    return rows


def impact_compression_metrics(
    cluster: FlushCluster,
    timeline: list[dict[str, Any]],
    trades_1s: pd.DataFrame,
    *,
    data_abort: bool,
) -> dict[str, Any]:
    direction = cluster.direction
    cluster_start = pd.Timestamp(cluster.cluster_start)
    cluster_end = pd.Timestamp(cluster.cluster_end) + pd.Timedelta(minutes=1)
    if data_abort:
        return {
            "cluster_id": cluster.cluster_id,
            "direction": direction,
            "data_abort": True,
            "abort_reason": "TRADE_FEED_GAP",
        }
    post = _slice_trades(trades_1s, cluster_start, cluster_end)
    col = AGGRESSIVE_NOTIONAL_COL[direction]
    if post.empty:
        return {
            "cluster_id": cluster.cluster_id,
            "direction": direction,
            "data_abort": False,
            "zero_trade_activity": True,
        }
    post = post.sort_values("second")
    n = len(post)

    def impact_slice(frame: pd.DataFrame) -> tuple[float, float | None]:
        agg = float(frame[col].sum()) if col in frame else 0.0
        start_ts = frame["second"].min()
        end_ts = frame["second"].max()
        mids = [
            r["mid_price"]
            for r in timeline
            if start_ts <= pd.Timestamp(r["second"]) <= end_ts and r["mid_price"]
        ]
        if len(mids) < 2:
            return agg, None
        move = abs(mids[-1] - mids[0])
        return agg, safe_div(move, agg)

    agg_f5, imp_f5 = impact_slice(post.head(min(5, n)))
    agg_l5, imp_l5 = impact_slice(post.tail(min(5, n)))
    half = max(1, n // 2)
    agg_h1, imp_h1 = impact_slice(post.iloc[:half])
    agg_h2, imp_h2 = impact_slice(post.iloc[half:])

    return {
        "cluster_id": cluster.cluster_id,
        "direction": direction,
        "data_abort": False,
        "first5_impact_per_notional": imp_f5,
        "last5_impact_per_notional": imp_l5,
        "impact_ratio_last5_over_first5": safe_div(imp_l5, imp_f5),
        "impact_delta_last5_minus_first5": (
            (imp_l5 - imp_f5) if imp_l5 is not None and imp_f5 is not None else None
        ),
        "first_half_impact_per_notional": imp_h1,
        "second_half_impact_per_notional": imp_h2,
        "compression_observed_first5_vs_last5": (
            imp_l5 is not None and imp_f5 is not None and imp_l5 < imp_f5
        ),
    }


def orderflow_flip_metrics(
    cluster: FlushCluster,
    timeline: list[dict[str, Any]],
    anchor_meta: dict[str, Any],
    trades_1s: pd.DataFrame,
) -> dict[str, Any]:
    direction = cluster.direction
    post_genuine = [r for r in timeline if r["phase"] == "POST_CLUSTER" and r["is_genuine"]]
    adverse_second = anchor_meta.get("adverse_extreme_second")
    after_adverse = [r for r in post_genuine if adverse_second and r["second"] > adverse_second]
    flip_trade = flip_ofi = flip_micro = flip_imb = None
    for r in after_adverse:
        ts = pd.Timestamp(r["second"])
        tr = trades_1s[trades_1s["second"] == ts]
        buy = float(tr["buy_notional"].sum()) if not tr.empty else 0.0
        sell = float(tr["sell_notional"].sum()) if not tr.empty else 0.0
        if flip_trade is None:
            if direction == "LONG" and buy > sell:
                flip_trade = r["second"]
            elif direction == "SHORT" and sell > buy:
                flip_trade = r["second"]
        ofi = r.get("directional_ofi")
        if flip_ofi is None and ofi is not None:
            if direction == "LONG" and ofi > 0:
                flip_ofi = r["second"]
            elif direction == "SHORT" and ofi < 0:
                flip_ofi = r["second"]
        micro = r.get("microprice")
        mid = r.get("mid_price")
        if flip_micro is None and micro is not None and mid is not None:
            if direction == "LONG" and micro > mid:
                flip_micro = r["second"]
            elif direction == "SHORT" and micro < mid:
                flip_micro = r["second"]
        imb = r.get("directional_imbalance_l50")
        anchor_imb = anchor_meta.get("anchor_directional_imbalance_l50")
        if flip_imb is None and imb is not None and anchor_imb is not None and imb > anchor_imb:
            flip_imb = r["second"]
    components = [c for c in (flip_trade, flip_ofi, flip_micro, flip_imb) if c]
    return {
        "cluster_id": cluster.cluster_id,
        "direction": direction,
        "flip_tradeflow_second": flip_trade,
        "flip_ofi_second": flip_ofi,
        "flip_microprice_second": flip_micro,
        "flip_imbalance_second": flip_imb,
        "flip_component_count": len(components),
        "first_any_flip_second": min(components) if components else None,
    }


def proxy_reclaim_metrics(
    cluster: FlushCluster,
    timeline: list[dict[str, Any]],
    anchor_meta: dict[str, Any],
    candles_1m: pd.DataFrame,
    trades_1s: pd.DataFrame,
    pre_flush_close: float | None,
) -> list[dict[str, Any]]:
    direction = cluster.direction
    cluster_start = pd.Timestamp(cluster.cluster_start)
    post_genuine = [r for r in timeline if r["phase"] == "POST_CLUSTER" and r["is_genuine"]]
    adverse_mid = anchor_meta.get("adverse_extreme_mid")
    anchor_wall = anchor_meta.get("anchor_wall_price")
    cluster_trades = _slice_trades(
        trades_1s,
        cluster_start,
        pd.Timestamp(cluster.cluster_end) + pd.Timedelta(minutes=1),
    )
    vwap: float | None = None
    if not cluster_trades.empty:
        prices = [
            r["mid_price"]
            for r in timeline
            if cluster_start <= pd.Timestamp(r["second"]) <= pd.Timestamp(cluster.cluster_end)
            and r.get("mid_price")
        ]
        if prices:
            vwap = sum(prices) / len(prices)

    anchors = {
        "PRE_FLUSH_CLOSE": pre_flush_close,
        "DOMINANT_WALL_ANCHOR_PRICE": anchor_wall,
        "FLUSH_CLUSTER_VWAP": vwap,
        "ADVERSE_EXTREME_PRICE": adverse_mid,
    }
    rows: list[dict[str, Any]] = []
    for anchor_name in RECLAIM_ANCHORS:
        level = anchors.get(anchor_name)
        if level is None:
            continue
        first_1s: str | None = None
        for r in post_genuine:
            mid = r.get("mid_price")
            if mid is None:
                continue
            if direction == "LONG" and mid >= level:
                first_1s = r["second"]
                break
            if direction == "SHORT" and mid <= level:
                first_1s = r["second"]
                break
        first_1m: str | None = None
        if not candles_1m.empty:
            post_candles = candles_1m[candles_1m["open_time"] >= cluster_start]
            for _, c in post_candles.iterrows():
                close = safe_float(c.get("close"))
                if close is None:
                    continue
                if direction == "LONG" and close >= level:
                    first_1m = c["open_time"].isoformat().replace("+00:00", "Z")
                    break
                if direction == "SHORT" and close <= level:
                    first_1m = c["open_time"].isoformat().replace("+00:00", "Z")
                    break
        for mark in TIME_MARKS_MINUTES:
            reclaimed = False
            if first_1s:
                delta_min = (pd.Timestamp(first_1s) - cluster_start).total_seconds() / 60.0
                reclaimed = delta_min <= mark
            rows.append(
                {
                    "cluster_id": cluster.cluster_id,
                    "direction": direction,
                    "reclaim_anchor": anchor_name,
                    "anchor_price": level,
                    "mark_minutes": mark,
                    "first_1s_proxy_reclaim_at": first_1s,
                    "first_1m_close_reclaim_at": first_1m,
                    "minutes_to_1s_reclaim": (
                        (pd.Timestamp(first_1s) - cluster_start).total_seconds() / 60.0
                        if first_1s
                        else None
                    ),
                    "proxy_reclaim_within_mark": reclaimed,
                }
            )
    return rows


def continuation_metrics(
    cluster: FlushCluster,
    timeline: list[dict[str, Any]],
) -> dict[str, Any]:
    direction = cluster.direction
    post = [r for r in timeline if r["phase"] == "POST_CLUSTER" and r["is_genuine"]]
    flags: list[str] = []
    if any(r["wall_status"] == WALL_STATUS_CHANGED for r in post):
        flags.append("DOMINANT_WALL_CHANGED_AWAY")
    depths = [r["directional_depth_l50"] for r in post if r["directional_depth_l50"] is not None]
    if len(depths) >= 2 and depths[-1] < depths[0]:
        flags.append("AGGREGATE_DIRECTIONAL_DEPTH_FALLING")
    ofis = [r["directional_ofi"] for r in post if r["directional_ofi"] is not None]
    if ofis and (
        (direction == "LONG" and all(o <= 0 for o in ofis[-5:]))
        or (direction == "SHORT" and all(o >= 0 for o in ofis[-5:]))
    ):
        flags.append("OFI_REMAINS_ADVERSE")
    mids = [r["mid_price"] for r in post if r["mid_price"] is not None]
    if len(mids) >= 2:
        if direction == "LONG" and mids[-1] < min(mids):
            flags.append("NEW_ADVERSE_EXTREME")
        if direction == "SHORT" and mids[-1] > max(mids):
            flags.append("NEW_ADVERSE_EXTREME")
    spreads = [r["spread_bps"] for r in post if r["spread_bps"] is not None]
    if len(spreads) >= 2 and spreads[-1] > spreads[0]:
        flags.append("SPREAD_WIDENING")
    return {
        "cluster_id": cluster.cluster_id,
        "direction": cluster.direction,
        "continuation_flags": "|".join(flags),
        "continuation_flag_count": len(flags),
    }


def trade_feed_gap_in_range(
    minute_features: pd.DataFrame,
    direction: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> bool:
    if minute_features.empty or "minute" not in minute_features.columns:
        return False
    minutes = pd.date_range(start.floor("min"), end.floor("min"), freq="1min", tz="UTC")
    for minute in minutes:
        minute_str = minute.isoformat().replace("+00:00", "Z")
        subset = minute_features[
            (minute_features["minute"] == minute_str) & (minute_features["direction"] == direction)
        ]
        if subset.empty:
            continue
        row = subset.iloc[0]
        if bool(row.get("technical_gap", False)):
            return True
        if not bool(row.get("trades_present", False)):
            return True
    return False


def analyze_cluster(
    cluster: FlushCluster,
    ob_1s: pd.DataFrame,
    trades_1s: pd.DataFrame,
    candles_1m: pd.DataFrame,
    minute_features: pd.DataFrame,
) -> dict[str, Any]:
    cluster_start = pd.Timestamp(cluster.cluster_start)
    cluster_end = pd.Timestamp(cluster.cluster_end) + pd.Timedelta(minutes=1)
    direction = cluster.direction

    data_abort = trade_feed_gap_in_range(
        minute_features, direction, cluster_start, cluster_end
    )
    anchor_row = find_anchor_row(ob_1s, cluster_start)
    if anchor_row is None:
        return {
            "cluster_id": cluster.cluster_id,
            "data_abort": True,
            "abort_reason": "NO_GENUINE_ANCHOR",
        }

    timeline, anchor_meta = build_timeline_rows(cluster, ob_1s, trades_1s, anchor_row)
    pre_minute = (cluster_start - pd.Timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    pre_flush_close = None
    if not minute_features.empty and "minute" in minute_features.columns:
        pre_rows = minute_features[
            (minute_features["minute"] == pre_minute) & (minute_features["direction"] == direction)
        ]
        if not pre_rows.empty:
            pre_flush_close = safe_float(pre_rows.iloc[0]["close"])

    return {
        "cluster_id": cluster.cluster_id,
        "data_abort": data_abort,
        "abort_reason": "TRADE_FEED_GAP" if data_abort else None,
        "timeline": timeline,
        "anchor_meta": anchor_meta,
        "stability": wall_stability_metrics(cluster, timeline, anchor_meta),
        "recovery": aggregate_recovery_metrics(cluster, timeline, anchor_meta),
        "compression": impact_compression_metrics(
            cluster, timeline, trades_1s, data_abort=data_abort
        ),
        "flip": orderflow_flip_metrics(cluster, timeline, anchor_meta, trades_1s),
        "reclaims": proxy_reclaim_metrics(
            cluster, timeline, anchor_meta, candles_1m, trades_1s, pre_flush_close
        ),
        "continuation": continuation_metrics(cluster, timeline),
        "event": {
            "cluster_id": cluster.cluster_id,
            "symbol": cluster.symbol,
            "direction": direction,
            "cluster_start": cluster.cluster_start,
            "cluster_end": cluster.cluster_end,
            "primary_candidate_id": cluster.primary_candidate_id,
            "candidate_ids": "|".join(cluster.candidate_ids),
            "flush_minutes": cluster.flush_minutes,
            "data_abort": data_abort,
            "abort_reason": "TRADE_FEED_GAP" if data_abort else "",
            **anchor_meta,
        },
    }
