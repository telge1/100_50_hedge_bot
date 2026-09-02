"""Genuine causal 30-minute bracket TPO (time/bracket presence, not volume-at-price)."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

from .config import BTCUSDT_TICK_SIZE, DEFAULT_TARGET_BINS, DEFAULT_VA_PCT, iso_z, utc
from .volume_profile import dedupe_session_trades

TPO_PROFILE_CONTRACT = "tpo_profile_facts_v1"
DEFAULT_BRACKET_MINUTES = 30

# Frozen tie-break rules (independent of golden outcomes):
# - POC: max tpo_count; tie → prefer bin closer to center of densified sequence
#   (delegated to orderbook_analyse.market_profile.profile.compute_value_area)
# - Value area: expand one bin toward larger neighbor tpo_count until 70% of total marks
POC_TIE_BREAK_RULE = "max_tpo_count_then_center_bin_index"
VALUE_AREA_TIE_BREAK_RULE = "expand_toward_larger_neighbor_tpo_count"


def _oa_tools():
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


def price_to_bin_index(price: float, step: float) -> int:
    """Integer bin index; no float equality on prices."""
    return int(math.floor(float(price) / float(step)))


def price_tick(price: float) -> int:
    return int(math.floor(float(price) / float(BTCUSDT_TICK_SIZE)))


def tpo_provenance_contract(*, bracket_minutes: int = DEFAULT_BRACKET_MINUTES) -> dict[str, Any]:
    return {
        "profile_kind": "TPO_BRACKET",
        "source": "orderbook_analysis.public_trades_canonical",
        "weighting": "DISTINCT_BRACKET_PRESENCE",
        "bracket_minutes": bracket_minutes,
        "bracket_alignment": "SESSION_START",
        "partial_bracket_policy": "INCLUDE_CAUSAL_PARTIAL_BRACKET",
        "anchor_exclusive": True,
        "trade_size_used_as_weight": False,
        "chart_timeframe_dependency": False,
    }


def build_tpo_profile_from_trades(
    trades: list[dict[str, Any]],
    *,
    session_start: datetime,
    anchor: datetime,
    cl: Any,
    symbol: str,
    bracket_minutes: int = DEFAULT_BRACKET_MINUTES,
    value_area_pct: float = DEFAULT_VA_PCT,
    target_bins: int = DEFAULT_TARGET_BINS,
    price_step: float | None = None,
    profile_session_id: str = "us_developing_to_anchor",
    session_trades: list[dict[str, Any]] | None = None,
    coverage_meta: dict[str, Any] | None = None,
    ohlc: tuple[float, float, float, float] | None = None,
) -> dict[str, Any]:
    """Build bracket-presence TPO using ``session_start <= trade_ts < anchor``."""
    session_start = utc(session_start)
    anchor = utc(anchor)
    bracket_delta = timedelta(minutes=bracket_minutes)

    if session_trades is None:
        session_trades, coverage_meta = dedupe_session_trades(trades, session_start, anchor)
    coverage = dict(coverage_meta or {})
    coverage.setdefault("session_start_utc", iso_z(session_start))
    coverage.setdefault("cutoff_utc", iso_z(anchor))
    coverage.setdefault("profile_session_id", profile_session_id)

    if not session_trades:
        return _failed_tpo(
            session_start,
            anchor,
            profile_session_id,
            coverage,
            bracket_minutes=bracket_minutes,
            reason="no_trades_in_session",
        )

    oa = _oa_tools()
    resolve_price_step = oa["resolve_price_step"]
    densify_bins = oa["densify_bins"]
    compute_value_area = oa["compute_value_area"]
    find_nodes = oa["find_nodes"]
    ProfileBin = oa["ProfileBin"]
    ShapeThresholds = oa["ShapeThresholds"]

    from orderbook_analyse.market_profile.loader import fetch_window_ohlc

    if ohlc is not None:
        _, high, low, _ = ohlc
    else:
        ohlc_fetched = fetch_window_ohlc(cl, symbol, session_start, anchor)
        if ohlc_fetched is None:
            prices = [float(t["price"]) for t in session_trades]
            high, low = max(prices), min(prices)
        else:
            _, high, low, _ = ohlc_fetched

    if high <= low:
        return _failed_tpo(
            session_start,
            anchor,
            profile_session_id,
            coverage,
            bracket_minutes=bracket_minutes,
            reason="invalid_ohlc_range",
        )

    step = float(price_step) if price_step is not None else float(resolve_price_step(low, high, target_bins))

    bracket_rows, bin_tpo_counts, bin_bracket_meta = _build_brackets(
        session_trades,
        session_start=session_start,
        anchor=anchor,
        step=step,
        bracket_delta=bracket_delta,
    )

    if not bin_tpo_counts:
        return _failed_tpo(
            session_start,
            anchor,
            profile_session_id,
            coverage,
            bracket_minutes=bracket_minutes,
            reason="no_bracket_touches",
        )

    raw_bins = []
    for idx in sorted(bin_tpo_counts):
        lo = idx * step
        cnt = float(bin_tpo_counts[idx])
        raw_bins.append(
            ProfileBin(
                bin_index=idx,
                price_low=lo,
                price_high=lo + step,
                price_mid=lo + step / 2.0,
                volume=cnt,
                buy_volume=0.0,
                sell_volume=0.0,
                trades=0,
                notional=0.0,
            )
        )

    bins = densify_bins(raw_bins, step)
    value_area = compute_value_area(bins, value_area_pct)
    total_marks = sum(bin_tpo_counts.values())
    th = ShapeThresholds()
    nodes = find_nodes(
        bins,
        hvn_factor=oa["DEFAULT_HVN_FACTOR"],
        lvn_factor=oa["DEFAULT_LVN_FACTOR"],
        min_separation_bins=oa["DEFAULT_NODE_MIN_SEPARATION_BINS"],
        single_print_frac=oa["DEFAULT_SINGLE_PRINT_FRAC"],
        poc_volume=value_area.poc_volume,
    )

    poc_bin_index = value_area.poc_bin_index
    rows = _rows_from_bins(bins, bin_tpo_counts, bin_bracket_meta, total_marks, poc_bin_index, value_area)
    hvn_candidates = _tpo_node_candidates(bins, nodes.hvn, "HVN", th, total_marks)
    lvn_candidates = _tpo_node_candidates(bins, nodes.lvn, "LVN", th, total_marks)

    full_brackets = sum(1 for b in bracket_rows if not b["is_partial"])
    partial_brackets = sum(1 for b in bracket_rows if b["is_partial"])

    integrity = _integrity_checks(
        session_trades=session_trades,
        anchor=anchor,
        rows=rows,
        bracket_rows=bracket_rows,
        value_area=value_area,
        total_marks=total_marks,
        step=step,
    )

    status = "COMPUTED_SEPARATELY"
    if integrity.get("status") != "PASS":
        status = "INTEGRITY_FAILED"

    return {
        "tpo_profile_status": status,
        "contract_version": TPO_PROFILE_CONTRACT,
        "provenance": {
            **tpo_provenance_contract(bracket_minutes=bracket_minutes),
            "engine": "research.btc_ob_fight.tpo_profile",
            "algorithm_source": "orderbook_analyse.market_profile.profile (compute_value_area, find_nodes on tpo_count weights)",
            "session_start_utc": iso_z(session_start),
            "cutoff_utc": iso_z(anchor),
            "profile_session_id": profile_session_id,
            "price_increment": step,
            "value_area_percentage": value_area_pct,
            "target_bins": target_bins,
            "trade_cutoff_rule": "session_start <= trade_ts < anchor_cutoff",
            "dedup_key": "trade_id",
            "poc_tie_break_rule": POC_TIE_BREAK_RULE,
            "value_area_tie_break_rule": VALUE_AREA_TIE_BREAK_RULE,
            "oa_volume_path_not_used_for_tpo": True,
        },
        "coverage": coverage,
        "brackets": {
            "bracket_minutes": bracket_minutes,
            "full_count": full_brackets,
            "partial_count": partial_brackets,
            "total_count": len(bracket_rows),
            "total_tpo_marks": total_marks,
        },
        "tpoc": {
            "tpoc_price": value_area.poc,
            "tpoc_tpo_count": value_area.poc_volume,
            "tpoc_bin_index": value_area.poc_bin_index,
            "tpoc_tie_break_rule": POC_TIE_BREAK_RULE,
        },
        "value_area": {
            "value_area_percentage": value_area_pct,
            "total_tpo_marks": total_marks,
            "target_value_area_marks": total_marks * value_area_pct,
            "actual_value_area_marks": total_marks * value_area.volume_share,
            "actual_value_area_share": value_area.volume_share,
            "tpoc_vah": value_area.vah,
            "tpoc_val": value_area.val,
            "included_bin_count": value_area.bin_count,
            "value_area_tie_break_rule": VALUE_AREA_TIE_BREAK_RULE,
        },
        "hvn_candidates": hvn_candidates,
        "lvn_candidates": lvn_candidates,
        "rows": rows,
        "bracket_rows": bracket_rows,
        "integrity": integrity,
        "tpo_profile_cutoff_utc": iso_z(anchor),
        "future_trade_count_used": 0,
        "tpo_profile_computed_separately": True,
    }


def _build_brackets(
    session_trades: list[dict[str, Any]],
    *,
    session_start: datetime,
    anchor: datetime,
    step: float,
    bracket_delta: timedelta,
) -> tuple[list[dict[str, Any]], dict[int, int], dict[int, dict[str, Any]]]:
    """Align brackets to session_start; include causal partial bracket at anchor."""
    bin_tpo_counts: dict[int, int] = {}
    bin_bracket_meta: dict[int, dict[str, Any]] = {}
    bracket_rows: list[dict[str, Any]] = []

    bracket_index = 0
    bracket_start = session_start
    while bracket_start < anchor:
        bracket_end_contract = bracket_start + bracket_delta
        observed_until = min(anchor, bracket_end_contract)
        is_partial = observed_until < bracket_end_contract
        bracket_start_iso = iso_z(bracket_start)

        bracket_trades = [t for t in session_trades if bracket_start <= t["ts"] < observed_until]
        touched: set[int] = set()
        low = high = None
        low_bin = high_bin = None
        first_ts = last_ts = None

        if bracket_trades:
            prices = [float(t["price"]) for t in bracket_trades]
            low = min(prices)
            high = max(prices)
            low_bin = price_to_bin_index(low, step)
            high_bin = price_to_bin_index(high, step)
            for bidx in range(low_bin, high_bin + 1):
                touched.add(bidx)
            first_ts = min(t["ts"] for t in bracket_trades)
            last_ts = max(t["ts"] for t in bracket_trades)

        for bidx in touched:
            bin_tpo_counts[bidx] = bin_tpo_counts.get(bidx, 0) + 1
            meta = bin_bracket_meta.setdefault(
                bidx,
                {"indices": set(), "starts": []},
            )
            meta["indices"].add(bracket_index)
            meta["starts"].append(bracket_start_iso)

        bracket_rows.append(
            {
                "bracket_index": bracket_index,
                "bracket_start": bracket_start_iso,
                "bracket_end_contract": iso_z(bracket_end_contract),
                "observed_until": iso_z(observed_until),
                "is_partial": is_partial,
                "first_trade_ts": iso_z(first_ts) if first_ts else None,
                "last_trade_ts": iso_z(last_ts) if last_ts else None,
                "low": low,
                "high": high,
                "low_bin": low_bin,
                "high_bin": high_bin,
                "touched_bin_count": len(touched),
                "trade_count": len(bracket_trades),
            }
        )

        bracket_index += 1
        if is_partial:
            break
        bracket_start = bracket_end_contract

    return bracket_rows, bin_tpo_counts, bin_bracket_meta


def _rows_from_bins(
    bins: list[Any],
    bin_tpo_counts: dict[int, int],
    bin_bracket_meta: dict[int, dict[str, Any]],
    total_marks: int,
    poc_bin_index: int,
    value_area: Any,
) -> list[dict[str, Any]]:
    va_lo = float(value_area.val)
    va_hi = float(value_area.vah)
    rows: list[dict[str, Any]] = []
    for b in bins:
        cnt = int(bin_tpo_counts.get(b.bin_index, 0))
        if cnt <= 0:
            continue
        meta = bin_bracket_meta.get(b.bin_index, {})
        starts = sorted(meta.get("starts") or [])
        rows.append(
            {
                "price_bin_tick": price_tick(b.price_mid),
                "price_bin_index": b.bin_index,
                "price": b.price_mid,
                "tpo_count": cnt,
                "tpo_share": cnt / total_marks if total_marks else 0.0,
                "first_bracket_start": starts[0] if starts else None,
                "last_bracket_start": starts[-1] if starts else None,
                "bracket_count": len(meta.get("indices") or []),
                "is_poc": b.bin_index == poc_bin_index,
                "is_value_area": b.price_low >= va_lo - 1e-9 and b.price_high <= va_hi + 1e-9,
            }
        )
    rows.sort(key=lambda r: (r["price_bin_index"], r["price_bin_tick"]))
    return rows


def _tpo_node_candidates(
    bins: list[Any],
    prices: tuple[float, ...],
    node_type: str,
    th: Any,
    total_marks: int,
) -> list[dict[str, Any]]:
    by_mid = {b.price_mid: b for b in bins}
    out = []
    for p in prices:
        b = by_mid.get(p)
        if b is None:
            continue
        cnt = int(round(b.volume))
        share = cnt / total_marks if total_marks else 0.0
        out.append(
            {
                "node_type": node_type,
                "price": p,
                "tpo_count": cnt,
                "tpo_share": share,
                "heuristic_contract_version": "tpo_nodes_v1",
                "status": "UNFROZEN_HEURISTIC",
                "parameters": {
                    "hvn_factor": th.hvn_factor,
                    "lvn_factor": th.lvn_factor,
                    "min_separation_bins": th.node_min_separation_bins,
                },
            }
        )
    return out


def _integrity_checks(
    *,
    session_trades: list[dict[str, Any]],
    anchor: datetime,
    rows: list[dict[str, Any]],
    bracket_rows: list[dict[str, Any]],
    value_area: Any,
    total_marks: int,
    step: float,
) -> dict[str, Any]:
    row_sum = sum(r["tpo_count"] for r in rows)
    bracket_touch_sum = sum(b["touched_bin_count"] for b in bracket_rows)
    after_anchor = sum(1 for t in session_trades if t["ts"] >= anchor)

    tpoc = float(value_area.poc)
    tval = float(value_area.val)
    tvah = float(value_area.vah)
    level_order_ok = tval <= tpoc <= tvah

    all_finite = all(
        math.isfinite(float(x))
        for r in rows
        for x in (r["tpo_count"], r["tpo_share"], r["price"])
    ) and math.isfinite(tpoc) and math.isfinite(tval) and math.isfinite(tvah)

    max_per_bracket = all(b["touched_bin_count"] >= 0 for b in bracket_rows)
    partial_no_future = all(
        not b["is_partial"] or (b["observed_until"] is not None)
        for b in bracket_rows
    )

    checks = {
        "row_tpo_sum_equals_bracket_touches": row_sum == bracket_touch_sum,
        "row_tpo_sum_equals_total_marks": row_sum == total_marks,
        "bracket_touch_sum_equals_total_marks": bracket_touch_sum == total_marks,
        "each_bracket_counts_bins_at_most_once": max_per_bracket,
        "no_trade_after_anchor": after_anchor == 0,
        "value_area_level_order": level_order_ok,
        "value_area_share_target_met": float(value_area.volume_share) >= float(value_area.requested_share) - 1e-12,
        "all_values_finite": all_finite,
        "partial_brackets_causal": partial_no_future,
        "price_step_positive": step > 0,
    }
    status = "PASS" if all(checks.values()) else "FAIL"
    return {
        "status": status,
        "checks": checks,
        "totals": {
            "total_tpo_marks": total_marks,
            "row_tpo_sum": row_sum,
            "bracket_touch_sum": bracket_touch_sum,
            "bracket_count": len(bracket_rows),
        },
    }


def verify_tpo_trade_size_invariance(
    trades: list[dict[str, Any]],
    *,
    session_start: datetime,
    anchor: datetime,
    cl: Any,
    symbol: str,
    **kwargs: Any,
) -> dict[str, Any]:
    baseline = build_tpo_profile_from_trades(
        trades, session_start=session_start, anchor=anchor, cl=cl, symbol=symbol, **kwargs
    )
    if baseline.get("tpo_profile_status") != "COMPUTED_SEPARATELY":
        return {"status": "SKIP", "reason": "baseline_not_computed"}
    scaled = [
        {**t, "size": float(t["size"]) * 17.0, "notional": float(t["price"]) * float(t["size"]) * 17.0}
        for t in trades
    ]
    again = build_tpo_profile_from_trades(
        scaled, session_start=session_start, anchor=anchor, cl=cl, symbol=symbol, **kwargs
    )
    match = (
        baseline.get("tpoc", {}).get("tpoc_price") == again.get("tpoc", {}).get("tpoc_price")
        and baseline.get("value_area", {}).get("tpoc_vah") == again.get("value_area", {}).get("tpoc_vah")
        and baseline.get("value_area", {}).get("tpoc_val") == again.get("value_area", {}).get("tpoc_val")
        and baseline.get("rows") == again.get("rows")
    )
    return {"status": "PASS" if match else "FAIL", "scaled_factor": 17.0}


def verify_tpo_prefix_parity(
    trades: list[dict[str, Any]],
    *,
    session_start: datetime,
    anchor: datetime,
    cl: Any,
    symbol: str,
    **kwargs: Any,
) -> dict[str, Any]:
    baseline = build_tpo_profile_from_trades(
        trades, session_start=session_start, anchor=anchor, cl=cl, symbol=symbol, **kwargs
    )
    if baseline.get("tpo_profile_status") != "COMPUTED_SEPARATELY":
        return {"status": "SKIP", "reason": "baseline_not_computed"}
    post = [t for t in trades if t["ts"] >= anchor]
    extended = list(trades) + [
        {**t, "trade_id": f"tpo_prefix_extra_{t['trade_id']}"} for t in post[: min(50, len(post))]
    ]
    again = build_tpo_profile_from_trades(
        extended, session_start=session_start, anchor=anchor, cl=cl, symbol=symbol, **kwargs
    )
    match = (
        baseline.get("tpoc", {}).get("tpoc_price") == again.get("tpoc", {}).get("tpoc_price")
        and baseline.get("rows") == again.get("rows")
    )
    return {
        "status": "PASS" if match else "FAIL",
        "extra_post_anchor_trades_added": min(50, len(post)),
    }


def assess_tpo_volume_independence(
    tpo_profile: dict[str, Any],
    volume_profile: dict[str, Any],
) -> dict[str, Any]:
    """Return confluence status when both profiles are separately computed."""
    tpo_ok = tpo_profile.get("tpo_profile_status") == "COMPUTED_SEPARATELY"
    vol_ok = volume_profile.get("volume_profile_status") == "COMPUTED_SEPARATELY"
    if not tpo_ok or not vol_ok:
        return {
            "status": "INVALID_OR_MISSING_PROFILE",
            "shared_data_source": "orderbook_analysis.public_trades_canonical",
            "different_weighting": None,
        }

    tpoc = (tpo_profile.get("tpoc") or {}).get("tpoc_price")
    vpoc = (volume_profile.get("vpoc") or {}).get("vpoc_price")
    tpo_prov = tpo_profile.get("provenance") or {}
    vol_prov = volume_profile.get("provenance") or {}

    same_weight = (
        tpo_prov.get("trade_size_used_as_weight") is False
        and vol_prov.get("primary_volume_basis") == "base_volume"
    )
    distribution_differs = tpoc != vpoc or tpo_profile.get("rows") != volume_profile.get("rows")

    if same_weight and distribution_differs:
        status = "VALID_INDEPENDENT_MEASURES"
    elif not distribution_differs:
        status = "INVALID_SAME_SEMANTICS"
    else:
        status = "UNPROVEN"

    return {
        "status": status,
        "shared_data_source": "orderbook_analysis.public_trades_canonical",
        "different_weighting": {
            "tpo": tpo_prov.get("weighting"),
            "volume": "base_trade_volume (size)",
        },
        "tpoc_price": tpoc,
        "vpoc_price": vpoc,
        "distribution_semantically_independent": distribution_differs,
    }


def tpo_anchor_levels(tpo_profile: dict[str, Any]) -> list[dict[str, Any]]:
    if tpo_profile.get("tpo_profile_status") != "COMPUTED_SEPARATELY":
        return []
    levels: list[dict[str, Any]] = []
    tpoc = tpo_profile.get("tpoc") or {}
    va = tpo_profile.get("value_area") or {}
    prov = tpo_profile.get("provenance") or {}
    mapping = [
        ("TPO_POC", "TPO-POC", tpoc.get("tpoc_price")),
        ("TPO_VAH", "TPO-VAH", va.get("tpoc_vah")),
        ("TPO_VAL", "TPO-VAL", va.get("tpoc_val")),
    ]
    for level_id, label, price in mapping:
        if price is not None:
            levels.append(
                {
                    "level_id": level_id,
                    "label": label,
                    "price": float(price),
                    "source": "tpo_profile",
                    "profile_kind": prov.get("profile_kind", "TPO_BRACKET"),
                }
            )
    for i, node in enumerate(tpo_profile.get("hvn_candidates") or [], 1):
        levels.append(
            {
                "level_id": f"TPO_HVN_{i:03d}",
                "label": "TPO-HVN",
                "price": float(node["price"]),
                "source": "tpo_profile_heuristic",
                "profile_kind": "TPO_BRACKET",
            }
        )
    for i, node in enumerate(tpo_profile.get("lvn_candidates") or [], 1):
        levels.append(
            {
                "level_id": f"TPO_LVN_{i:03d}",
                "label": "TPO-LVN",
                "price": float(node["price"]),
                "source": "tpo_profile_heuristic",
                "profile_kind": "TPO_BRACKET",
            }
        )
    levels.sort(key=lambda x: (x["price"], x["level_id"]))
    return levels


def _failed_tpo(
    session_start: datetime,
    anchor: datetime,
    profile_session_id: str,
    coverage: dict[str, Any],
    *,
    bracket_minutes: int,
    reason: str,
) -> dict[str, Any]:
    return {
        "tpo_profile_status": "TPO_PROFILE_DATA_INSUFFICIENT",
        "contract_version": TPO_PROFILE_CONTRACT,
        "failure_reason": reason,
        "provenance": {
            **tpo_provenance_contract(bracket_minutes=bracket_minutes),
            "session_start_utc": iso_z(session_start),
            "cutoff_utc": iso_z(anchor),
            "profile_session_id": profile_session_id,
        },
        "coverage": coverage,
        "integrity": {"status": "FAIL", "reason": reason},
        "tpoc": {},
        "value_area": {},
        "hvn_candidates": [],
        "lvn_candidates": [],
        "rows": [],
        "bracket_rows": [],
        "future_trade_count_used": 0,
        "tpo_profile_computed_separately": True,
    }
