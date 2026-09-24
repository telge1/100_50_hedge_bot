"""Causal pool snapshots from one chronological BigBeluga run. No formula copies."""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from typing import Any

import pandas as pd

from .candles import FutureBarInFrame, ensure_utc
from .config import LOOKBACK, REPLAY, planner_root


class FiveMinuteFrameError(ValueError):
    """Input is not a closed 5m bar frame suitable for BigBeluga."""


def assert_five_minute_frame(
    candles: pd.DataFrame,
    *,
    max_close=None,
) -> pd.DataFrame:
    """Reject 1m series and misaligned bars. Gaps of N*5m are allowed (dropped incomplete buckets)."""
    if candles is None or candles.empty:
        raise FiveMinuteFrameError("empty 5m frame")
    if "timestamp" not in candles.columns:
        raise FiveMinuteFrameError("5m frame needs timestamp")
    out = candles.copy()
    opens = [ensure_utc(ts) for ts in out["timestamp"].tolist()]
    if len(opens) != len(set(opens)):
        raise FiveMinuteFrameError("duplicate 5m opens")
    ordered = sorted(opens)
    if opens != ordered:
        raise FiveMinuteFrameError("5m opens must be strictly ascending")
    for ot in opens:
        if ot.second or ot.microsecond:
            raise FiveMinuteFrameError("5m open is not UTC-aligned")
        if ot.tzinfo is None:
            raise FiveMinuteFrameError("5m timestamps must be UTC")
        if ot.minute % 5 != 0:
            raise FiveMinuteFrameError("timestamp is not on a 5m boundary")
    deltas = [(opens[i] - opens[i - 1]).total_seconds() / 60.0 for i in range(1, len(opens))]
    if any(d <= 0 for d in deltas):
        raise FiveMinuteFrameError("5m opens must be strictly ascending")
    if any(abs(d - 1.0) < 1e-9 for d in deltas) or (deltas and max(deltas) <= 1.0 + 1e-9):
        raise FiveMinuteFrameError("1m bar spacing is not allowed for the pool engine")
    if any(d < 5.0 - 1e-9 or abs(d % 5.0) > 1e-6 for d in deltas):
        raise FiveMinuteFrameError("bar spacing is not a 5m multiple")
    closes = []
    if "close_time" in out.columns:
        raw_closes = out["close_time"].tolist()
    else:
        raw_closes = [None] * len(opens)
    for ot, raw in zip(opens, raw_closes):
        expected = ot + timedelta(minutes=5)
        if raw is None or (isinstance(raw, float) and pd.isna(raw)):
            closes.append(expected)
            continue
        got = ensure_utc(raw)
        if got != expected:
            raise FiveMinuteFrameError("close_time must equal open + 5m")
        closes.append(got)
    out["close_time"] = closes
    if max_close is not None:
        limit = ensure_utc(max_close)
        if any(c > limit for c in closes):
            raise FutureBarInFrame("FUTURE_BAR_IN_FRAME")
    return out

_ENGINE_RUNS = 0


def reset_pool_engine_run_count() -> None:
    global _ENGINE_RUNS
    _ENGINE_RUNS = 0


def pool_engine_run_count() -> int:
    return _ENGINE_RUNS


def _ensure_planner_path() -> None:
    import sys

    root = str(planner_root())
    if root not in sys.path:
        sys.path.insert(0, root)


def causal_as_of(prefix: pd.DataFrame) -> pd.Timestamp:
    last = prefix.iloc[-1]["timestamp"]
    return pd.Timestamp(ensure_utc(last))


def snapshot_pools(pools: list, as_of) -> list:
    """Pools known and not yet invalidated at as_of. Future invalidation is not applied."""
    _ensure_planner_path()
    from dataclasses import replace

    from research.liquidity.bigbeluga_pools import _pool_active_at

    ts = pd.Timestamp(ensure_utc(as_of))
    out = []
    for p in pools:
        if not _pool_active_at(p, ts, known_only=True):
            continue
        if p.invalidated_at is not None and p.invalidated_at > ts:
            out.append(replace(p, invalidated_at=None, invalidated_price=None, active=True))
        else:
            out.append(p)
    return out


def run_pools_once(candles: pd.DataFrame, *, lookback: int = LOOKBACK) -> list:
    """Run the planner pool engine exactly once on a 5m frame."""
    global _ENGINE_RUNS
    _ensure_planner_path()
    from research.liquidity.bigbeluga_pools import run_bigbeluga_pools
    from research.liquidity.order_planner import _localize

    if candles is None or candles.empty:
        raise FiveMinuteFrameError("empty 5m frame")
    guarded = assert_five_minute_frame(candles)
    engine_df = guarded.copy()
    engine_df["timestamp"] = pd.to_datetime(engine_df["timestamp"], utc=True)
    engine_df["timestamp"] = engine_df["timestamp"].dt.tz_convert("UTC").dt.tz_localize(None)
    _ENGINE_RUNS += 1
    result = run_bigbeluga_pools(
        engine_df,
        lookback=lookback,
        collect_research_events=False,
    )
    return [_localize(p) for p in result.pools]


def plan_from_snapshot(
    pools: list,
    *,
    symbol: str,
    entry_time,
    entry_price: float,
    direction: str,
    as_of,
    test_fixture_only: bool = False,
) -> dict[str, Any]:
    """Planner target selection on a causal snapshot. replay=False. Freeze at entry."""
    _ensure_planner_path()
    from research.liquidity.order_planner import (
        RELEVANCE_POOL_COUNT,
        RELEVANCE_STRENGTH_MAX,
        RELEVANCE_STRENGTH_SUM,
        SL_MAX_ABS_PCT,
        ADJACENT_GAP_PCT,
        EMPTY_GAP_MAX_PCT,
        MICRO_HEIGHT_PCT,
        NEAR_MAX_PCT,
        SL_BUFFER,
        TP1_BUFFER,
        TP2_BUFFER,
        Side,
        _apply_exit_sizes,
        _cluster_brief,
        _dist_pct,
        _iso,
        _pool_row,
        _sl_long,
        _sl_short,
        _tp_long,
        _tp_out,
        _tp_short,
        build_clusters,
        select_tp_clusters,
    )

    if REPLAY:
        raise RuntimeError("replay must stay False for V1 freeze")
    direction = str(direction).upper().strip()
    if direction not in ("LONG", "SHORT"):
        raise ValueError("direction must be LONG or SHORT")
    entry_ts = pd.Timestamp(ensure_utc(entry_time))
    entry = float(entry_price)
    as_of_ts = pd.Timestamp(ensure_utc(as_of))
    active = snapshot_pools(pools, as_of_ts)
    future_in_snapshot = [p.pool_id for p in active if p.created_at > as_of_ts]
    if future_in_snapshot:
        raise FutureBarInFrame(f"future pools leaked into snapshot: {future_in_snapshot}")

    uppers = [p for p in active if p.side == Side.UPPER]
    lowers = [p for p in active if p.side == Side.LOWER]
    upper_clusters = sorted(build_clusters(uppers, entry), key=lambda c: c.bottom)
    lower_clusters = sorted(build_clusters(lowers, entry), key=lambda c: -c.top)

    upper_above = [c for c in upper_clusters if c.bottom > entry]
    lower_below = [c for c in lower_clusters if c.top < entry]
    rel_upper = [c for c in upper_above if c.relevant]
    rel_lower = [c for c in lower_below if c.relevant]

    if direction == "LONG":
        first, second, tp2_skip_reason = select_tp_clusters(
            upper_above, entry=entry, direction=direction
        )
        sl_cands = rel_lower
        tp_fn = _tp_long
        sl_fn = _sl_long
    else:
        first, second, tp2_skip_reason = select_tp_clusters(
            lower_below, entry=entry, direction=direction
        )
        sl_cands = rel_upper
        tp_fn = _tp_short
        sl_fn = _sl_short

    sl_cluster = sl_cands[0] if sl_cands else None
    sanity = {
        "future_pools_in_snapshot": 0,
        "tp_from_known_pools_only": True,
        "sl_from_entry_snapshot_only": True,
        "original_tp2_immutable": True,
        "dynamic_tp1_not_before_created_at": True,
        "snapshot_as_of": _iso(as_of_ts),
    }

    if sl_cluster is None:
        sl_block = {
            "available": False,
            "SL_PRICE": None,
            "SL_DISTANCE_PCT": None,
            "SL_CLUSTER": None,
            "SL_POOL_COUNT": 0,
            "SL_STRENGTH_SUM": None,
            "SL_STRENGTH_MAX": None,
            "raw_pool_sl_distance_pct": None,
            "SL_TOO_WIDE": False,
            "POOL_STRUCTURAL_SL": None,
            "DEFAULT_MAX_SL_PCT": SL_MAX_ABS_PCT,
            "SL_DECISION_REQUIRES_POLICY": True,
            "reason": "no relevant opposite-side cluster at entry",
        }
    else:
        sl_price = sl_fn(sl_cluster)
        sl_dist = _dist_pct(sl_price, entry)
        too_wide = abs(sl_dist) > SL_MAX_ABS_PCT
        sl_block = {
            "available": True,
            "SL_PRICE": sl_price,
            "SL_DISTANCE_PCT": sl_dist,
            "SL_CLUSTER": _cluster_brief(sl_cluster),
            "SL_POOL_COUNT": sl_cluster.pool_count,
            "SL_STRENGTH_SUM": sl_cluster.strength_sum,
            "SL_STRENGTH_MAX": sl_cluster.strength_max,
            "raw_pool_sl_distance_pct": sl_dist,
            "SL_TOO_WIDE": too_wide,
            "POOL_STRUCTURAL_SL": sl_price,
            "DEFAULT_MAX_SL_PCT": SL_MAX_ABS_PCT,
            "SL_DECISION_REQUIRES_POLICY": bool(too_wide),
            "reason": (
                "first relevant opposite-side cluster; SL 0.20% beyond cluster edge"
                + ("; SL_TOO_WIDE abs(dist)>1.5%" if too_wide else "")
            ),
        }

    original_target_price = None
    if first is not None and second is not None:
        mode = "TWO_VISIBLE_TARGETS"
        tp1_px = tp_fn(first, "TP1")
        tp2_px = tp_fn(second, "TP2")
        original_target_price = tp2_px
        tp1_block = _tp_out("TP1", tp1_px, entry, first)
        tp2_block = _tp_out("TP2", tp2_px, entry, second)
    elif first is not None:
        mode = "ONE_VISIBLE_TARGET"
        tp1_px = tp_fn(first, "TP1")
        original_target_price = tp1_px
        tp1_block = _tp_out("TP1", tp1_px, entry, first)
        tp2_block = {
            "available": False,
            "TP2_PRICE": None,
            "TP2_DISTANCE_PCT": None,
            "TP2_SIZE": None,
            "TP2_CLUSTER": None,
            "reason": tp2_skip_reason or "no_further_sensible_cluster",
        }
    else:
        mode = "ONE_VISIBLE_TARGET_WAIT_FOR_DYNAMIC_TP1"
        tp1_block = {
            "available": False,
            "TP1_PRICE": None,
            "TP1_DISTANCE_PCT": None,
            "TP1_SIZE": None,
            "pending_dynamic": True,
            "reason": "no sensible target-side cluster at entry",
        }
        tp2_block = {
            "available": False,
            "TP2_PRICE": None,
            "TP2_DISTANCE_PCT": None,
            "TP2_SIZE": None,
            "reason": "no sensible target-side cluster at entry",
        }

    has_sl = bool(sl_block.get("available"))
    has_tp1 = bool(tp1_block.get("available"))
    has_tp2 = bool(tp2_block.get("available"))
    complete = has_sl and (has_tp1 or has_tp2)
    tp1_block, tp2_block, size_tp1, size_tp2 = _apply_exit_sizes(tp1_block, tp2_block)

    plan = {
        "symbol": str(symbol).strip().upper(),
        "timeframe": "5m",
        "ENTRY": entry,
        "ENTRY_TIME": _iso(entry_ts),
        "DIRECTION": direction,
        "INITIAL_TARGET_MODE": mode,
        "ORIGINAL_TARGET_PRICE": original_target_price,
        "SL": sl_block,
        "TP1": tp1_block,
        "TP2": tp2_block,
        "TP1_SIZE": size_tp1,
        "TP2_SIZE": size_tp2,
        "TP1_UPDATED_FROM_DYNAMIC_POOL": False,
        "relevance_rule": {
            "pool_count_gte": RELEVANCE_POOL_COUNT,
            "strength_max_gte": RELEVANCE_STRENGTH_MAX,
            "strength_sum_gte": RELEVANCE_STRENGTH_SUM,
            "skip_isolated_micro": True,
            "near_max_pct": NEAR_MAX_PCT,
            "empty_gap_max_pct": EMPTY_GAP_MAX_PCT,
            "micro_height_pct": MICRO_HEIGHT_PCT,
            "nearby_clear_pool_not_skipped_for_low_strength": True,
        },
        "tp2_skip_reason": tp2_skip_reason,
        "buffers": {
            "tp1_pct": TP1_BUFFER * 100.0,
            "tp2_pct": TP2_BUFFER * 100.0,
            "sl_pct": SL_BUFFER * 100.0,
            "adjacent_gap_pct": ADJACENT_GAP_PCT,
        },
        "active_pool_count": len(active),
        "sanity": sanity,
        "PRIMARY_DECISION": "POOL_ORDER_PLAN_READY" if complete else "POOL_ORDER_PLAN_INCOMPLETE",
        "dynamic_events": [],
        "upper_clusters": [c.to_dict() for c in sorted(upper_clusters, key=lambda x: abs(x.distance_from_entry_pct))],
        "lower_clusters": [c.to_dict() for c in sorted(lower_clusters, key=lambda x: abs(x.distance_from_entry_pct))],
        "entry_pools": [_pool_row(p, entry) for p in active],
    }
    if test_fixture_only:
        plan["pool_candle_source"] = "TEST_FIXTURE_ONLY"
        plan["test_fixture_only"] = True
    return plan


def structural_pool_keys(plan: dict[str, Any]) -> list[tuple]:
    rows = []
    for p in plan.get("entry_pools") or []:
        rows.append(
            (
                str(p.get("created_at")),
                str(p.get("side")),
                round(float(p["top"]), 10),
                round(float(p["bottom"]), 10),
                None if p.get("strength") is None else round(float(p["strength"]), 10),
                str(p.get("invalidated_at")),
            )
        )
    return sorted(rows)


def plan_parity_core(plan: dict[str, Any]) -> dict[str, Any]:
    sl = plan.get("SL") or {}
    tp1 = plan.get("TP1") or {}
    tp2 = plan.get("TP2") or {}

    def _rnd(v):
        if v is None:
            return None
        return round(float(v), 10)

    def _strip_cluster(c):
        if not c:
            return None
        out = deepcopy(c)
        out.pop("pool_ids", None)
        out.pop("cluster_id", None)
        return out

    return {
        "INITIAL_TARGET_MODE": plan.get("INITIAL_TARGET_MODE"),
        "PRIMARY_DECISION": plan.get("PRIMARY_DECISION"),
        "tp2_skip_reason": plan.get("tp2_skip_reason"),
        "TP1_SIZE": plan.get("TP1_SIZE"),
        "TP2_SIZE": plan.get("TP2_SIZE"),
        "TP1_UPDATED_FROM_DYNAMIC_POOL": plan.get("TP1_UPDATED_FROM_DYNAMIC_POOL"),
        "SL_PRICE": _rnd(sl.get("SL_PRICE")),
        "SL_TOO_WIDE": sl.get("SL_TOO_WIDE"),
        "SL_CLUSTER": _strip_cluster(sl.get("SL_CLUSTER")),
        "TP1_PRICE": _rnd(tp1.get("TP1_PRICE")),
        "TP1_SIZE_BLOCK": tp1.get("TP1_SIZE"),
        "TP1_CLUSTER": _strip_cluster(tp1.get("TP1_CLUSTER")),
        "TP2_PRICE": _rnd(tp2.get("TP2_PRICE")),
        "TP2_SIZE_BLOCK": tp2.get("TP2_SIZE"),
        "TP2_CLUSTER": _strip_cluster(tp2.get("TP2_CLUSTER")),
        "pools": structural_pool_keys(plan),
    }
