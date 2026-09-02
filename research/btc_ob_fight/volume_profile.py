"""Causal volume-at-price profile from deduplicated public trades (separate from TPO labels)."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import BTCUSDT_TICK_SIZE, DEFAULT_TARGET_BINS, DEFAULT_VA_PCT, iso_z, utc

VOLUME_PROFILE_CONTRACT = "volume_profile_facts_v1"
PRIMARY_VOLUME_BASIS = "base_volume"
INTEGRITY_TOLERANCE_ABS = 1e-6
INTEGRITY_TOLERANCE_REL = 1e-9


def _oa_profile_tools():
    from orderbook_analyse.market_profile import (
        DEFAULT_HVN_FACTOR,
        DEFAULT_LVN_FACTOR,
        DEFAULT_NODE_MIN_SEPARATION_BINS,
        DEFAULT_SINGLE_PRINT_FRAC,
    )
    from orderbook_analyse.market_profile.contracts import ProfileBin, ShapeThresholds
    from orderbook_analyse.market_profile.loader import densify_bins, resolve_price_step
    from orderbook_analyse.market_profile.profile import compute_value_area, find_nodes

    return {
        "DEFAULT_HVN_FACTOR": DEFAULT_HVN_FACTOR,
        "DEFAULT_LVN_FACTOR": DEFAULT_LVN_FACTOR,
        "DEFAULT_NODE_MIN_SEPARATION_BINS": DEFAULT_NODE_MIN_SEPARATION_BINS,
        "DEFAULT_SINGLE_PRINT_FRAC": DEFAULT_SINGLE_PRINT_FRAC,
        "ProfileBin": ProfileBin,
        "ShapeThresholds": ShapeThresholds,
        "densify_bins": densify_bins,
        "resolve_price_step": resolve_price_step,
        "compute_value_area": compute_value_area,
        "find_nodes": find_nodes,
    }


def profile_session_window(anchor: datetime) -> tuple[datetime, datetime, str]:
    """US-developing-to-anchor session window (causal resolver, not hardcoded in callers)."""
    anchor = utc(anchor)
    us_start = anchor.replace(hour=13, minute=30, second=0, microsecond=0)
    if us_start > anchor:
        us_start = us_start - timedelta(days=1)
    return us_start, anchor, "us_developing_to_anchor"


def dedupe_session_trades(
    trades: list[dict[str, Any]],
    session_start: datetime,
    anchor: datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Trades with ``session_start <= ts < anchor``, deduplicated by ``trade_id``."""
    session_start = utc(session_start)
    anchor = utc(anchor)
    session_trades_raw = [
        t
        for t in trades
        if session_start <= t["ts"] < anchor and float(t.get("size") or 0) > 0
    ]
    seen: set[str] = set()
    session_trades: list[dict[str, Any]] = []
    dedup_removed = 0
    for t in sorted(session_trades_raw, key=lambda x: (x["ts"], x["trade_id"])):
        tid = str(t["trade_id"])
        if tid in seen:
            dedup_removed += 1
            continue
        seen.add(tid)
        session_trades.append(t)

    future_count = sum(1 for t in trades if t["ts"] >= anchor and session_start <= t["ts"])
    coverage = {
        "session_start_utc": iso_z(session_start),
        "cutoff_utc": iso_z(anchor),
        "raw_trade_rows_in_session": len(session_trades_raw),
        "dedup_removed_duplicates": dedup_removed,
        "deduped_trade_rows_used": len(session_trades),
        "future_trade_count_excluded": future_count,
        "min_trade_ts": iso_z(session_trades[0]["ts"]) if session_trades else None,
        "max_trade_ts": iso_z(session_trades[-1]["ts"]) if session_trades else None,
    }
    return session_trades, coverage


def build_volume_profile_from_trades(
    trades: list[dict[str, Any]],
    *,
    session_start: datetime,
    anchor: datetime,
    cl: Any,
    symbol: str,
    value_area_pct: float = DEFAULT_VA_PCT,
    target_bins: int = DEFAULT_TARGET_BINS,
    profile_session_id: str = "us_developing_to_anchor",
    compute_prefix: bool = True,
    price_step: float | None = None,
    session_trades: list[dict[str, Any]] | None = None,
    coverage_meta: dict[str, Any] | None = None,
    ohlc: tuple[float, float, float, float] | None = None,
) -> dict[str, Any]:
    """Build causal volume profile using trades with ``session_start <= ts < anchor``."""
    session_start = utc(session_start)
    anchor = utc(anchor)
    if session_trades is None:
        session_trades, coverage_meta = dedupe_session_trades(trades, session_start, anchor)
    coverage = dict(coverage_meta or {})
    coverage.setdefault("profile_session_id", profile_session_id)

    if not session_trades:
        return _failed_profile(
            session_start,
            anchor,
            profile_session_id,
            coverage,
            reason="no_trades_in_session",
        )

    from orderbook_analyse.market_profile.loader import fetch_window_ohlc

    oa = _oa_profile_tools()
    densify_bins = oa["densify_bins"]
    resolve_price_step = oa["resolve_price_step"]
    compute_value_area = oa["compute_value_area"]
    find_nodes = oa["find_nodes"]
    ShapeThresholds = oa["ShapeThresholds"]
    if ohlc is not None:
        open_p, high, low, close_p = ohlc
    else:
        ohlc_fetched = fetch_window_ohlc(cl, symbol, session_start, anchor)
        if ohlc_fetched is None:
            open_p = session_trades[0]["price"]
            close_p = session_trades[-1]["price"]
            prices = [t["price"] for t in session_trades]
            high, low = max(prices), min(prices)
        else:
            open_p, high, low, close_p = ohlc_fetched

    if high <= low:
        return _failed_profile(
            session_start,
            anchor,
            profile_session_id,
            coverage,
            reason="invalid_ohlc_range",
        )

    step = float(price_step) if price_step is not None else resolve_price_step(low, high, target_bins)
    raw_bins, bin_meta = _aggregate_trades_to_bins(session_trades, step)
    if not raw_bins:
        return _failed_profile(
            session_start,
            anchor,
            profile_session_id,
            coverage,
            reason="no_bins",
        )

    bins = densify_bins(raw_bins, step)
    value_area = compute_value_area(bins, value_area_pct)
    th = ShapeThresholds()
    nodes = find_nodes(
        bins,
        hvn_factor=oa["DEFAULT_HVN_FACTOR"],
        lvn_factor=oa["DEFAULT_LVN_FACTOR"],
        min_separation_bins=oa["DEFAULT_NODE_MIN_SEPARATION_BINS"],
        single_print_frac=oa["DEFAULT_SINGLE_PRINT_FRAC"],
        poc_volume=value_area.poc_volume,
    )

    rows = [_bin_to_row(b, bin_meta.get(b.bin_index, {})) for b in bins]
    rows.sort(key=lambda r: (r["price_bin_index"], r["price_bin_tick"]))
    integrity = _integrity_checks(session_trades, rows, anchor, value_area)

    vpoc_tie_count = sum(1 for b in bins if abs(b.volume - value_area.poc_volume) < 1e-12 and b.volume > 0)
    status = "COMPUTED_SEPARATELY"
    if integrity.get("status") != "PASS":
        status = "INTEGRITY_FAILED"

    hvn_candidates = _node_candidates(bins, nodes.hvn, "HVN", th)
    lvn_candidates = _node_candidates(bins, nodes.lvn, "LVN", th)

    total_vol = sum(b.volume for b in bins)
    result = {
        "volume_profile_status": status,
        "contract_version": VOLUME_PROFILE_CONTRACT,
        "provenance": {
            "source_table": "orderbook_analysis.public_trades_canonical",
            "engine": "research.btc_ob_fight.volume_profile",
            "algorithm_source": "orderbook_analyse.market_profile.profile (compute_value_area, find_nodes)",
            "session_start_utc": iso_z(session_start),
            "cutoff_utc": iso_z(anchor),
            "profile_session_id": profile_session_id,
            "session_timezone": "UTC",
            "primary_volume_basis": PRIMARY_VOLUME_BASIS,
            "available_volume_bases": ["base_volume", "quote_notional"],
            "source_quantity_field": "size",
            "quote_notional_formula": "price * size (canonical notional when present)",
            "price_increment": step,
            "value_area_percentage": value_area_pct,
            "target_bins": target_bins,
            "aggressor_semantics": "side=Buy/Sell is taker/aggressor",
            "trade_cutoff_rule": "session_start <= trade_ts < anchor_cutoff",
            "dedup_key": "trade_id",
            "tpo_values_not_copied": True,
        },
        "coverage": coverage,
        "integrity": integrity,
        "vpoc": {
            "vpoc_price": value_area.poc,
            "vpoc_volume": value_area.poc_volume,
            "vpoc_trade_count": _trade_count_at_price_mid(bins, value_area.poc_bin_index),
            "vpoc_tie_count": vpoc_tie_count,
            "vpoc_tie_break_rule": "max_volume_then_center_bin_index",
            "vpoc_bin_index": value_area.poc_bin_index,
        },
        "value_area": {
            "value_area_percentage": value_area_pct,
            "total_profile_volume": total_vol,
            "target_value_area_volume": total_vol * value_area_pct,
            "actual_value_area_volume": total_vol * value_area.volume_share,
            "actual_value_area_share": value_area.volume_share,
            "vvah": value_area.vah,
            "vval": value_area.val,
            "included_bin_count": value_area.bin_count,
            "value_area_tie_break_rule": "expand_toward_larger_neighbor_bin_volume",
        },
        "hvn_candidates": hvn_candidates,
        "lvn_candidates": lvn_candidates,
        "rows": rows,
        "summary_ohlc": {
            "open": open_p,
            "high": high,
            "low": low,
            "close": close_p,
            "price_low": low,
            "price_high": high,
        },
        "volume_profile_cutoff_utc": iso_z(anchor),
        "max_trade_ts_used": coverage["max_trade_ts"],
        "future_trade_count_used": 0,
        "volume_profile_computed_separately": True,
    }
    if compute_prefix:
        result["prefix_parity"] = verify_prefix_parity(
            trades,
            session_start=session_start,
            anchor=anchor,
            cl=cl,
            symbol=symbol,
            value_area_pct=value_area_pct,
            target_bins=target_bins,
        )
    return result


def verify_prefix_parity(
    trades: list[dict[str, Any]],
    *,
    session_start: datetime,
    anchor: datetime,
    cl: Any,
    symbol: str,
    value_area_pct: float,
    target_bins: int,
) -> dict[str, Any]:
    """Extended trade pool must not change profile at anchor cutoff."""
    baseline = build_volume_profile_from_trades(
        trades,
        session_start=session_start,
        anchor=anchor,
        cl=cl,
        symbol=symbol,
        value_area_pct=value_area_pct,
        target_bins=target_bins,
        compute_prefix=False,
    )
    if baseline.get("volume_profile_status") != "COMPUTED_SEPARATELY":
        return {"status": "SKIP", "reason": "baseline_not_computed"}

    post_anchor = [t for t in trades if t["ts"] >= anchor]
    extended_pool = list(trades) + [
        {
            **t,
            "trade_id": f"prefix_extra_{t['trade_id']}",
        }
        for t in post_anchor[: min(50, len(post_anchor))]
    ]
    again = build_volume_profile_from_trades(
        extended_pool,
        session_start=session_start,
        anchor=anchor,
        cl=cl,
        symbol=symbol,
        value_area_pct=value_area_pct,
        target_bins=target_bins,
        compute_prefix=False,
    )
    match = (
        baseline.get("vpoc", {}).get("vpoc_price") == again.get("vpoc", {}).get("vpoc_price")
        and baseline.get("value_area", {}).get("vvah") == again.get("value_area", {}).get("vvah")
        and baseline.get("value_area", {}).get("vval") == again.get("value_area", {}).get("vval")
    )
    return {
        "status": "PASS" if match else "FAIL",
        "anchor_cutoff_utc": iso_z(anchor),
        "extra_post_anchor_trades_added": min(50, len(post_anchor)),
    }


def compare_with_oa_profile(
    cl: Any,
    symbol: str,
    session_start: datetime,
    anchor: datetime,
    local: dict[str, Any],
    *,
    value_area_pct: float = DEFAULT_VA_PCT,
    target_bins: int = DEFAULT_TARGET_BINS,
) -> dict[str, Any]:
    """Parity check against OA ClickHouse-aggregated profile (may differ on dedup)."""
    from orderbook_analyse.market_profile.anchor import build_windows
    from orderbook_analyse.market_profile.build import build_profile

    wins = build_windows(anchor_mode="composite", start=session_start, end=anchor)
    if not wins:
        return {"status": "NOT_COMPARABLE", "reason": "no_window"}
    prof = build_profile(
        cl,
        symbol,
        wins[0],
        value_area_pct=value_area_pct,
        target_bins=target_bins,
        use_final=True,
    )
    if prof is None:
        return {"status": "NOT_COMPARABLE", "reason": "oa_profile_none"}

    oa_va = prof.value_area
    loc_va = local.get("value_area") or {}
    loc_vpoc = local.get("vpoc") or {}
    tol = max(1.0, prof.price_step * 0.51)
    vpoc_diff = abs(float(loc_vpoc.get("vpoc_price") or 0) - oa_va.poc)
    vah_diff = abs(float(loc_va.get("vvah") or 0) - oa_va.vah)
    val_diff = abs(float(loc_va.get("vval") or 0) - oa_va.val)

    if vpoc_diff < 1e-9 and vah_diff < 1e-9 and val_diff < 1e-9:
        status = "EXACT"
    elif vpoc_diff <= tol and vah_diff <= tol and val_diff <= tol:
        status = "WITHIN_DOCUMENTED_TOLERANCE"
    else:
        status = "DIFFERENT_CONTRACT"
    return {
        "status": status,
        "oa_poc": oa_va.poc,
        "oa_vah": oa_va.vah,
        "oa_val": oa_va.val,
        "local_vpoc": loc_vpoc.get("vpoc_price"),
        "local_vvah": loc_va.get("vvah"),
        "local_vval": loc_va.get("vval"),
        "price_step_oa": prof.price_step,
        "price_step_local": local.get("provenance", {}).get("price_increment"),
        "note": "OA aggregates in CH; local pipeline dedups trade_id in Python first",
    }


def _aggregate_trades_to_bins(
    trades: list[dict[str, Any]], step: float
) -> tuple[list[Any], dict[int, dict[str, Any]]]:
    ProfileBin = _oa_profile_tools()["ProfileBin"]
    step = float(step)
    agg: dict[int, dict[str, Any]] = defaultdict(
        lambda: {
            "volume": 0.0,
            "buy_volume": 0.0,
            "sell_volume": 0.0,
            "buy_notional": 0.0,
            "sell_notional": 0.0,
            "trades": 0,
            "notional": 0.0,
            "first_ts": None,
            "last_ts": None,
        }
    )
    for t in trades:
        price = float(t["price"])
        size = float(t["size"])
        if size <= 0 or not math.isfinite(price) or not math.isfinite(size):
            continue
        idx = int(math.floor(price / step))
        slot = agg[idx]
        slot["volume"] += size
        notional = float(t.get("notional") or price * size)
        if t["side"] == "Buy":
            slot["buy_volume"] += size
            slot["buy_notional"] += notional
        elif t["side"] == "Sell":
            slot["sell_volume"] += size
            slot["sell_notional"] += notional
        slot["notional"] += notional
        slot["trades"] += 1
        ts = t["ts"]
        if slot["first_ts"] is None or ts < slot["first_ts"]:
            slot["first_ts"] = ts
        if slot["last_ts"] is None or ts > slot["last_ts"]:
            slot["last_ts"] = ts

    bin_meta: dict[int, dict[str, Any]] = {}
    out: list[Any] = []
    for idx in sorted(agg):
        lo = idx * step
        data = agg[idx]
        bin_meta[idx] = {
            "buy_notional": data["buy_notional"],
            "sell_notional": data["sell_notional"],
            "first_ts": data["first_ts"],
            "last_ts": data["last_ts"],
        }
        out.append(
            ProfileBin(
                bin_index=idx,
                price_low=lo,
                price_high=lo + step,
                price_mid=lo + step / 2.0,
                volume=data["volume"],
                buy_volume=data["buy_volume"],
                sell_volume=data["sell_volume"],
                trades=data["trades"],
                notional=data["notional"],
            )
        )
    return out, bin_meta


def _price_tick(price: float) -> int:
    return int(math.floor(float(price) / float(BTCUSDT_TICK_SIZE)))


def _bin_to_row(b: Any, meta: dict[str, Any]) -> dict[str, Any]:
    buy_n = meta.get("buy_notional", 0.0)
    sell_n = meta.get("sell_notional", 0.0)
    first_ts = meta.get("first_ts")
    last_ts = meta.get("last_ts")
    return {
        "price_bin_tick": _price_tick(b.price_mid),
        "price_bin_index": b.bin_index,
        "price_bin_low": b.price_low,
        "price_bin_high": b.price_high,
        "display_price": b.price_mid,
        "base_volume": b.volume,
        "quote_notional": b.notional,
        "trade_count": b.trades,
        "taker_buy_base_volume": b.buy_volume,
        "taker_sell_base_volume": b.sell_volume,
        "taker_buy_notional": buy_n,
        "taker_sell_notional": sell_n,
        "delta_base_volume": b.delta,
        "delta_notional": buy_n - sell_n,
        "first_trade_ts": iso_z(first_ts) if first_ts is not None else None,
        "last_trade_ts": iso_z(last_ts) if last_ts is not None else None,
    }


def _integrity_checks(
    session_trades: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    anchor: datetime,
    value_area: Any,
) -> dict[str, Any]:
    sum_base = sum(float(t["size"]) for t in session_trades)
    sum_notional = sum(float(t.get("notional") or t["price"] * t["size"]) for t in session_trades)
    row_base = sum(r["base_volume"] for r in rows)
    row_notional = sum(r["quote_notional"] for r in rows)
    row_trades = sum(r["trade_count"] for r in rows)
    row_buy = sum(r["taker_buy_base_volume"] for r in rows)
    row_sell = sum(r["taker_sell_base_volume"] for r in rows)
    row_delta = sum(r["delta_base_volume"] for r in rows)
    after_anchor = sum(1 for t in session_trades if t["ts"] >= anchor)

    vpoc = float(value_area.poc)
    vval = float(value_area.val)
    vvah = float(value_area.vah)
    level_order_ok = vval <= vpoc <= vvah
    all_finite = all(
        math.isfinite(float(x))
        for r in rows
        for x in (
            r["base_volume"],
            r["quote_notional"],
            r["delta_base_volume"],
            r["delta_notional"],
        )
    ) and math.isfinite(vpoc) and math.isfinite(vval) and math.isfinite(vvah)

    def ok(a: float, b: float) -> bool:
        tol = max(INTEGRITY_TOLERANCE_ABS, INTEGRITY_TOLERANCE_REL * max(abs(a), abs(b), 1.0))
        return abs(a - b) <= tol

    checks = {
        "base_volume_conservation": ok(sum_base, row_base),
        "quote_notional_conservation": ok(sum_notional, row_notional),
        "trade_count_conservation": row_trades == len(session_trades),
        "buy_sell_sum_equals_total": ok(row_buy + row_sell, row_base),
        "delta_equals_buy_minus_sell": ok(row_delta, row_buy - row_sell),
        "no_trade_after_anchor": after_anchor == 0,
        "deduped_trade_count": len(session_trades),
        "value_area_level_order": level_order_ok,
        "all_values_finite": all_finite,
        "value_area_share_target_met": float(value_area.volume_share) >= float(value_area.requested_share) - 1e-12,
    }
    status = "PASS" if all(checks.values()) else "FAIL"
    return {
        "status": status,
        "checks": checks,
        "totals": {
            "trade_base_volume": sum_base,
            "row_base_volume": row_base,
            "trade_notional": sum_notional,
            "row_notional": row_notional,
            "trade_count": len(session_trades),
            "row_trade_count": row_trades,
        },
        "tolerance_abs": INTEGRITY_TOLERANCE_ABS,
        "tolerance_rel": INTEGRITY_TOLERANCE_REL,
    }


def _node_candidates(
    bins: list[Any],
    prices: tuple[float, ...],
    node_type: str,
    th: Any,
) -> list[dict[str, Any]]:
    total = sum(b.volume for b in bins) or 1.0
    mean = total / len(bins) if bins else 0.0
    by_mid = {b.price_mid: b for b in bins}
    out = []
    for p in prices:
        b = by_mid.get(p)
        if b is None:
            continue
        share = b.volume / total
        neighbor = _neighbor_comparison(bins, b.bin_index)
        out.append(
            {
                "node_type": node_type,
                "price": p,
                "volume": b.volume,
                "volume_share": share,
                "neighbor_comparison": neighbor,
                "prominence": b.volume / mean if mean > 0 else None,
                "heuristic_contract_version": "volume_nodes_v1",
                "status": "UNFROZEN_HEURISTIC",
                "parameters": {
                    "hvn_factor": th.hvn_factor,
                    "lvn_factor": th.lvn_factor,
                    "min_separation_bins": th.node_min_separation_bins,
                },
            }
        )
    return out


def _neighbor_comparison(bins: list[Any], idx: int) -> dict[str, float | None]:
    pos = next(i for i, b in enumerate(bins) if b.bin_index == idx)
    below = bins[pos - 1].volume if pos > 0 else None
    above = bins[pos + 1].volume if pos < len(bins) - 1 else None
    return {"below_volume": below, "above_volume": above}


def _trade_count_at_price_mid(bins: list[Any], poc_bin_index: int) -> int:
    for b in bins:
        if b.bin_index == poc_bin_index:
            return b.trades
    return 0


def _failed_profile(
    session_start: datetime,
    anchor: datetime,
    profile_session_id: str,
    coverage: dict[str, Any],
    *,
    reason: str,
) -> dict[str, Any]:
    return {
        "volume_profile_status": "INTEGRITY_FAILED",
        "contract_version": VOLUME_PROFILE_CONTRACT,
        "failure_reason": reason,
        "provenance": {
            "source_table": "orderbook_analysis.public_trades_canonical",
            "session_start_utc": iso_z(session_start),
            "cutoff_utc": iso_z(anchor),
            "profile_session_id": profile_session_id,
        },
        "coverage": coverage,
        "integrity": {"status": "FAIL", "reason": reason},
        "vpoc": {},
        "value_area": {},
        "hvn_candidates": [],
        "lvn_candidates": [],
        "rows": [],
        "volume_profile_computed_separately": True,
        "future_trade_count_used": 0,
    }


def volume_anchor_levels(volume_profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Level descriptors for episode engine."""
    if volume_profile.get("volume_profile_status") != "COMPUTED_SEPARATELY":
        return []
    levels: list[dict[str, Any]] = []
    vpoc = volume_profile.get("vpoc") or {}
    va = volume_profile.get("value_area") or {}
    mapping = [
        ("VOLUME_VPOC", "VPOC", vpoc.get("vpoc_price")),
        ("VOLUME_VVAH", "VVAH", va.get("vvah")),
        ("VOLUME_VVAL", "VVAL", va.get("vval")),
    ]
    for level_id, label, price in mapping:
        if price is not None:
            levels.append({"level_id": level_id, "label": label, "price": float(price), "source": "volume_profile"})
    for i, node in enumerate(volume_profile.get("hvn_candidates") or [], 1):
        levels.append(
            {
                "level_id": f"VOLUME_HVN_{i:03d}",
                "label": "Volume-HVN",
                "price": float(node["price"]),
                "source": "volume_profile_heuristic",
            }
        )
    for i, node in enumerate(volume_profile.get("lvn_candidates") or [], 1):
        levels.append(
            {
                "level_id": f"VOLUME_LVN_{i:03d}",
                "label": "Volume-LVN",
                "price": float(node["price"]),
                "source": "volume_profile_heuristic",
            }
        )
    levels.sort(key=lambda x: x["price"])
    return levels
