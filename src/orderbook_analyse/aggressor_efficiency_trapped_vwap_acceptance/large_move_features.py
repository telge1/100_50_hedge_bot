"""Path labels and causal pre-entry features for large-move discovery."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from orderbook_analyse.aggressor_efficiency_flip.models import Trade
from orderbook_analyse.aggressor_efficiency_flip.timeutil import ensure_utc, iso_z
from orderbook_analyse.ob200_v3_raw_discovery.reconstruct import SampleRow


def _ms(ts: datetime) -> int:
    return int(ensure_utc(ts).timestamp() * 1000)


def directional_bps(side: str, entry_px: float, path_px: float) -> float:
    if entry_px <= 0 or path_px <= 0:
        return 0.0
    if side == "LONG":
        return (path_px / entry_px - 1.0) * 1e4
    return (entry_px / path_px - 1.0) * 1e4


def compute_path_outcomes(
    samples: list[SampleRow],
    *,
    side: str,
    entry_ts: datetime,
    entry_px: float,
    horizons_s: tuple[int, ...] = (60, 180, 300, 600, 900, 1800),
) -> dict[str, Any]:
    """Executable path metrics from entry using bid (LONG) / ask (SHORT)."""
    entry_ts = ensure_utc(entry_ts)
    t0 = _ms(entry_ts)
    # binary search
    lo, hi = 0, len(samples)
    while lo < hi:
        mid = (lo + hi) // 2
        if samples[mid].ts_ms < t0:
            lo = mid + 1
        else:
            hi = mid

    max_h = max(horizons_s)
    t_end = t0 + max_h * 1000
    # per-second first appearance of thresholds
    mfe = {h: 0.0 for h in horizons_s}
    mae = {h: 0.0 for h in horizons_s}
    last_bps = 0.0
    time_to = {20: None, 25: None, 30: None}
    # for clean path at 15m / 30m
    first_pos_25_ms = None
    first_neg_15_ms = None
    first_pos_25_sec = None
    first_neg_15_sec = None
    ret_at: dict[int, Optional[float]] = {h: None for h in horizons_s}
    last_by_h: dict[int, float] = {}

    i = lo
    while i < len(samples) and samples[i].ts_ms <= t_end:
        s = samples[i]
        px = s.best_bid if side == "LONG" else s.best_ask
        if px is None or px <= 0:
            i += 1
            continue
        bps = directional_bps(side, entry_px, float(px))
        elapsed_s = (s.ts_ms - t0) / 1000.0
        sec_floor = s.ts_ms // 1000
        for h in horizons_s:
            if elapsed_s <= h:
                mfe[h] = max(mfe[h], bps)
                mae[h] = min(mae[h], bps)
                last_by_h[h] = bps
        for thr in (20, 25, 30):
            if time_to[thr] is None and bps >= thr:
                time_to[thr] = elapsed_s
        if first_pos_25_ms is None and bps >= 25:
            first_pos_25_ms = s.ts_ms
            first_pos_25_sec = sec_floor
        if first_neg_15_ms is None and bps <= -15:
            first_neg_15_ms = s.ts_ms
            first_neg_15_sec = sec_floor
        last_bps = bps
        i += 1

    for h in horizons_s:
        ret_at[h] = last_by_h.get(h)

    # clean 15m / 30m classification
    def _clean(horizon_s: int) -> dict[str, Any]:
        h_ms = t0 + horizon_s * 1000
        hit25 = first_pos_25_ms is not None and first_pos_25_ms <= h_ms
        hit15 = first_neg_15_ms is not None and first_neg_15_ms <= h_ms
        if hit25 and hit15 and first_pos_25_sec == first_neg_15_sec:
            return {
                "label": False,
                "path_class": "SAME_BUCKET_AMBIGUOUS",
                "target_before_adverse": False,
                "adverse_before_target": False,
            }
        if hit25 and (not hit15 or first_pos_25_ms < first_neg_15_ms):
            return {
                "label": True,
                "path_class": "TARGET_BEFORE_ADVERSE",
                "target_before_adverse": True,
                "adverse_before_target": False,
            }
        if hit15 and (not hit25 or first_neg_15_ms < first_pos_25_ms):
            return {
                "label": False,
                "path_class": "ADVERSE_BEFORE_TARGET",
                "target_before_adverse": False,
                "adverse_before_target": True,
            }
        if hit25:
            return {
                "label": True,
                "path_class": "TARGET_BEFORE_ADVERSE",
                "target_before_adverse": True,
                "adverse_before_target": False,
            }
        return {
            "label": False,
            "path_class": "NEITHER",
            "target_before_adverse": False,
            "adverse_before_target": False,
        }

    clean15 = _clean(900)
    clean30 = _clean(1800)
    return {
        "mfe_bps_5m": mfe.get(300),
        "mfe_bps_15m": mfe.get(900),
        "mfe_bps_30m": mfe.get(1800),
        "mae_bps_5m": mae.get(300),
        "mae_bps_15m": mae.get(900),
        "mae_bps_30m": mae.get(1800),
        "ret_bps_1m": ret_at.get(60),
        "ret_bps_3m": ret_at.get(180),
        "ret_bps_5m": ret_at.get(300),
        "ret_bps_10m": ret_at.get(600),
        "ret_bps_15m": ret_at.get(900),
        "ret_bps_30m": ret_at.get(1800),
        "time_to_20bps_s": time_to[20],
        "time_to_25bps_s": time_to[25],
        "time_to_30bps_s": time_to[30],
        "LARGE_MOVE_20BPS_15M": bool(mfe.get(900, 0) >= 20),
        "LARGE_MOVE_25BPS_15M": bool(mfe.get(900, 0) >= 25),
        "LARGE_MOVE_30BPS_15M": bool(mfe.get(900, 0) >= 30),
        "LARGE_MOVE_25BPS_30M": bool(mfe.get(1800, 0) >= 25),
        "CLEAN_LARGE_MOVE_25_15": bool(clean15["label"]),
        "path_class_15m": clean15["path_class"],
        "target_before_adverse_15m": clean15["target_before_adverse"],
        "adverse_before_target_15m": clean15["adverse_before_target"],
        "CLEAN_LARGE_MOVE_25_30": bool(clean30["label"]),
        "path_class_30m": clean30["path_class"],
    }


def _trades_in_window(
    trades: list[Trade], end: datetime, window_s: float
) -> list[Trade]:
    end = ensure_utc(end)
    start = end - timedelta(seconds=window_s)
    out = []
    for t in trades:
        ts = t.trade_ts if t.trade_ts.tzinfo else t.trade_ts.replace(tzinfo=timezone.utc)
        if start < ts <= end:
            out.append(t)
    return out


def trade_flow_features(trades: list[Trade], *, entry_ts: datetime, windows=(5, 15, 30, 60)) -> dict[str, Any]:
    feats: dict[str, Any] = {}
    meta: dict[str, Any] = {}
    for w in windows:
        sub = _trades_in_window(trades, entry_ts, w)
        buy = sum(t.notional for t in sub if t.side == "Buy")
        sell = sum(t.notional for t in sub if t.side == "Sell")
        tot = buy + sell
        n = len(sub)
        sizes = [t.notional for t in sub]
        large = sum(1 for x in sizes if x >= 25000) / n if n else 0.0
        max_buy = max((t.notional for t in sub if t.side == "Buy"), default=0.0)
        max_sell = max((t.notional for t in sub if t.side == "Sell"), default=0.0)
        feats[f"flow_buy_notional_{w}s"] = buy
        feats[f"flow_sell_notional_{w}s"] = sell
        feats[f"flow_signed_imbalance_{w}s"] = ((buy - sell) / tot) if tot > 0 else 0.0
        feats[f"flow_trade_count_{w}s"] = float(n)
        feats[f"flow_avg_trade_notional_{w}s"] = (tot / n) if n else 0.0
        feats[f"flow_large_share_{w}s"] = large
        feats[f"flow_max_buy_bubble_{w}s"] = max_buy
        feats[f"flow_max_sell_bubble_{w}s"] = max_sell
        meta[f"flow_{w}s"] = {
            "source_end_ts": iso_z(entry_ts),
            "source_start_ts": iso_z(entry_ts - timedelta(seconds=w)),
            "feature_available_ts": iso_z(entry_ts),
            "causal_ok": True,
            "family": "public_trade_flow",
        }
    return feats, meta


def book_features_at_entry(samples: list[SampleRow], *, entry_ts: datetime) -> tuple[dict[str, Any], dict[str, Any]]:
    entry_ts = ensure_utc(entry_ts)
    t0 = _ms(entry_ts)
    # last sample at or before entry
    lo, hi = 0, len(samples) - 1
    best = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if samples[mid].ts_ms <= t0:
            best = samples[mid]
            lo = mid + 1
        else:
            hi = mid - 1
    feats: dict[str, Any] = {}
    meta: dict[str, Any] = {}
    if best is None or best.best_bid <= 0 or best.best_ask <= 0:
        return {
            "book_spread_bps": None,
            "book_imbalance_l10": None,
            "book_micro_dev_bps": None,
            "book_bid_qty_bps10": None,
            "book_ask_qty_bps10": None,
        }, {
            "book": {
                "causal_ok": False,
                "missing_reason": "no_sample_at_or_before_entry",
                "feature_available_ts": None,
                "family": "orderbook",
            }
        }
    mid = best.mid
    feats["book_spread_bps"] = (best.best_ask - best.best_bid) / mid * 1e4 if mid else None
    feats["book_imbalance_l10"] = best.imbalance_l10
    feats["book_micro_dev_bps"] = (best.microprice - mid) / mid * 1e4 if mid else None
    feats["book_bid_qty_bps10"] = best.bid_qty_bps10
    feats["book_ask_qty_bps10"] = best.ask_qty_bps10
    feats["book_depth_asym"] = (
        (best.bid_qty_bps10 - best.ask_qty_bps10) / (best.bid_qty_bps10 + best.ask_qty_bps10)
        if (best.bid_qty_bps10 + best.ask_qty_bps10) > 0
        else 0.0
    )
    avail = datetime.fromtimestamp(best.ts_ms / 1000.0, tz=timezone.utc)
    meta["book"] = {
        "source_end_ts": iso_z(avail),
        "feature_available_ts": iso_z(avail),
        "causal_ok": avail <= entry_ts,
        "family": "orderbook",
    }
    return feats, meta


def pool_distance_features(
    samples: list[SampleRow],
    *,
    entry_ts: datetime,
    entry_mid: float,
    side: str,
    matched_edge_price: Optional[float],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Next opposing wall distance using last sample at/before entry."""
    entry_ts = ensure_utc(entry_ts)
    t0 = _ms(entry_ts)
    best = None
    lo, hi = 0, len(samples) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if samples[mid].ts_ms <= t0:
            best = samples[mid]
            lo = mid + 1
        else:
            hi = mid - 1
    feats = {
        "edge_distance_entry_bps": None,
        "next_opposing_wall_distance_bps": None,
        "free_path_bps": None,
        "target_25_feasible_at_entry": None,
        "liquidity_obstacle_inside_10bps": None,
        "liquidity_obstacle_inside_15bps": None,
        "liquidity_obstacle_inside_25bps": None,
    }
    meta = {
        "pool_distance": {
            "family": "pool_edge_geometry",
            "feature_available_ts": None,
            "causal_ok": False,
            "missing_reason": "no_book",
        }
    }
    if best is None or entry_mid <= 0:
        return feats, meta
    avail = datetime.fromtimestamp(best.ts_ms / 1000.0, tz=timezone.utc)
    meta["pool_distance"] = {
        "family": "pool_edge_geometry",
        "feature_available_ts": iso_z(avail),
        "source_end_ts": iso_z(avail),
        "causal_ok": avail <= entry_ts,
        "missing_reason": None,
    }
    if matched_edge_price and matched_edge_price > 0:
        feats["edge_distance_entry_bps"] = abs(entry_mid - matched_edge_price) / entry_mid * 1e4
    # opposing wall: LONG wants ask wall above; SHORT wants bid wall below
    if side == "LONG":
        wall = best.ask_far_wall_price or best.ask_wall_price
        if wall and wall > entry_mid:
            dist = (wall - entry_mid) / entry_mid * 1e4
        else:
            dist = None
    else:
        wall = best.bid_far_wall_price or best.bid_wall_price
        if wall and wall < entry_mid:
            dist = (entry_mid - wall) / entry_mid * 1e4
        else:
            dist = None
    feats["next_opposing_wall_distance_bps"] = dist
    feats["free_path_bps"] = dist
    feats["target_25_feasible_at_entry"] = bool(dist is not None and dist >= 25.0)
    feats["liquidity_obstacle_inside_10bps"] = bool(dist is not None and dist <= 10.0)
    feats["liquidity_obstacle_inside_15bps"] = bool(dist is not None and dist <= 15.0)
    feats["liquidity_obstacle_inside_25bps"] = bool(dist is not None and dist <= 25.0)
    return feats, meta


def context_features(samples: list[SampleRow], *, entry_ts: datetime) -> tuple[dict[str, Any], dict[str, Any]]:
    """Backward mid returns / simple vol ending at entry."""
    entry_ts = ensure_utc(entry_ts)
    t0 = _ms(entry_ts)
    feats: dict[str, Any] = {}
    meta: dict[str, Any] = {"market_context": {"family": "market_context", "causal_ok": True}}

    def mid_at_or_before(target_ms: int) -> Optional[float]:
        lo, hi = 0, len(samples) - 1
        best = None
        while lo <= hi:
            mid = (lo + hi) // 2
            if samples[mid].ts_ms <= target_ms:
                best = samples[mid].mid
                lo = mid + 1
            else:
                hi = mid - 1
        return float(best) if best and best > 0 else None

    m0 = mid_at_or_before(t0)
    for w in (60, 180, 300, 900):
        m1 = mid_at_or_before(t0 - w * 1000)
        if m0 and m1:
            feats[f"ctx_ret_bps_{w}s"] = (m0 / m1 - 1.0) * 1e4
        else:
            feats[f"ctx_ret_bps_{w}s"] = None
    # realized vol proxy: std of 1s mid changes over last 5m
    rets = []
    prev = None
    lo = 0
    # walk last 5m samples
    start_ms = t0 - 300_000
    i = 0
    # find start
    lo, hi = 0, len(samples)
    while lo < hi:
        mid = (lo + hi) // 2
        if samples[mid].ts_ms < start_ms:
            lo = mid + 1
        else:
            hi = mid
    i = lo
    while i < len(samples) and samples[i].ts_ms <= t0:
        m = samples[i].mid
        if m and m > 0 and prev:
            rets.append((m / prev - 1.0) * 1e4)
        if m and m > 0:
            prev = m
        i += 1
    if len(rets) >= 5:
        mu = sum(rets) / len(rets)
        var = sum((x - mu) ** 2 for x in rets) / len(rets)
        feats["ctx_realized_vol_5m"] = math.sqrt(var)
        feats["ctx_range_bps_5m"] = max(rets) - min(rets) if rets else None
    else:
        feats["ctx_realized_vol_5m"] = None
        feats["ctx_range_bps_5m"] = None
    feats["ctx_hour_utc"] = float(entry_ts.hour)
    feats["ctx_minute_utc"] = float(entry_ts.hour * 60 + entry_ts.minute)
    meta["market_context"]["feature_available_ts"] = iso_z(entry_ts)
    meta["market_context"]["source_end_ts"] = iso_z(entry_ts)
    return feats, meta


def acceptance_features(row: dict[str, Any], *, entry_ts: datetime) -> tuple[dict[str, Any], dict[str, Any]]:
    signal = row.get("signal_available_ts")
    feats = {
        "acc_is_above": 1.0 if row.get("acceptance_state") == "ACCEPTED_ABOVE" else 0.0,
        "acc_is_below": 1.0 if row.get("acceptance_state") == "ACCEPTED_BELOW" else 0.0,
        "acc_rearm": 1.0 if str(row.get("migration_class") or row.get("episode_action") or "").find("REARM") >= 0 else 0.0,
        "acc_secs_signal_to_entry": None,
        "acc_spread_at_entry_bps": float(row["spread_bps"]) if row.get("spread_bps") not in (None, "") else None,
    }
    if signal:
        try:
            from orderbook_analyse.aggressor_efficiency_flip.timeutil import parse_utc

            st = parse_utc(signal) if isinstance(signal, str) else signal
            feats["acc_secs_signal_to_entry"] = (ensure_utc(entry_ts) - ensure_utc(st)).total_seconds()
        except Exception:
            pass
    meta = {
        "acceptance_quality": {
            "family": "acceptance_quality",
            "feature_available_ts": iso_z(entry_ts),
            "causal_ok": True,
        }
    }
    return feats, meta
