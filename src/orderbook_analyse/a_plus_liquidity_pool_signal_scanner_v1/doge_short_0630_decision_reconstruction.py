"""Causal decision-time reconstruction: DOGE pullback short ~06:30 UTC."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from orderbook_analyse.cluster_sweep_research.clickhouse_source import fetch_candles_1m
from orderbook_analyse.liquidity_location_causal.availability import pool_lifecycle_status
from orderbook_analyse.liquidity_location_pool_lifecycle.ema_context import attach_context
from orderbook_analyse.orderbook_v2.ch_client import get_clickhouse_client

from .config import (
    DEFAULT_OUT_DIR,
    MIN_GROSS_RR,
    MIN_TARGET_DISTANCE_ATR,
    TF_CONFIRM,
    TF_ENTRY_POOL,
    TF_LIQUIDITY,
    TF_MACRO,
    TF_STRUCTURE,
)
from .gates import estimated_net_rr, evaluate_gates, gross_rr
from .models import PoolRecord, _utc_naive
from .pool_selection import pullback_limit_price, select_pullback_entry_pools
from .pools import (
    eligible_target_pools,
    load_engine_pools_at,
    load_pools_at,
    pool_from_engine,
    pool_valid_at,
    resolve_pool_lifecycle,
)
from .runner import build_candles_by_tf
from .setups import (
    _intermediate_blocks,
    _liquidity_asymmetry_short,
    _select_target_below,
    _stop_target_levels,
    atr_available,
    bearish_5m_structural,
    detect_pullback_short_candidates,
    finalize_levels,
)
from .scanner import PoolSignalScanner

SYMBOL = "DOGEUSDT"
WARMUP_START = datetime(2026, 8, 25, 0, 0, 0)
WINDOW_START = datetime(2026, 8, 28, 5, 45, 0)
WINDOW_END = datetime(2026, 8, 28, 6, 45, 0)

OLD_SIGNAL_ID = "73b66b73675e35c6df7efa88"
OLD_EPISODE_ID = "A_PLUS_PULLBACK_SHORT:lld:DOGEUSDT:15m:upper:1787886900"
ENTRY_POOL_ID = "lld:DOGEUSDT:15m:upper:1787886900"
REF_TARGET_15M = "lld:DOGEUSDT:15m:lower:1787825700"
TARGET_30M_0415 = "lld:DOGEUSDT:30m:lower:1787853600"

DISPLAY_SNAPSHOTS = [
    "2026-08-28 04:15:00",
    "2026-08-28 04:30:00",
    "2026-08-28 06:00:00",
    "2026-08-28 06:15:00",
    "2026-08-28 06:25:00",
    "2026-08-28 06:30:00",
    "2026-08-28 06:35:00",
    "2026-08-28 06:40:00",
]

VERDICT_NEW = "NEW_CAUSAL_A_PLUS_SHORT"
VERDICT_SAME_EP = "SAME_EPISODE_REENTRY_FORBIDDEN"
VERDICT_NO_TARGET = "NO_CAUSAL_TARGET_AT_DECISION"
VERDICT_STRUCTURE = "STRUCTURE_GATE_BLOCKED"
VERDICT_MANUAL = "MANUAL_REFERENCE_NOT_MACHINE_REPRODUCIBLE"


def _iso(ts: Any) -> str:
    if ts is None:
        return ""
    return _utc_naive(pd.Timestamp(ts).to_pydatetime()).isoformat()


def _distance_atr(price: float, pool: PoolRecord, atr: float) -> float:
    if not atr_available(atr):
        return float("inf")
    return abs(price - pool.near_edge) / atr


def _engine_pool_map(
    candles_by_tf: dict[str, pd.DataFrame], *, as_of: datetime
) -> dict[str, PoolRecord]:
    out: dict[str, PoolRecord] = {}
    for tf, pools in load_engine_pools_at(
        candles_by_tf, symbol=SYMBOL, as_of=as_of, timeframes=(TF_ENTRY_POOL, TF_LIQUIDITY, TF_MACRO, TF_STRUCTURE)
    ).items():
        for p in pools:
            out[str(p.pool_id)] = pool_from_engine(p)
    return out


def _pool_lifecycle_row(pool: PoolRecord | None, as_of: datetime) -> dict[str, Any]:
    if pool is None:
        return {"status": "ABSENT", "as_of": _iso(as_of)}
    return {
        "status": pool.status_at(as_of),
        "available_at": _iso(pool.available_at),
        "invalidated_at": _iso(pool.invalidated_at),
        "lower_edge": pool.lower_edge,
        "upper_edge": pool.upper_edge,
        "midpoint": pool.midpoint,
        "strength": pool.strength,
        "component_count": pool.component_count,
    }


def _rank_targets(
    limit_px: float,
    pools_15m: list[PoolRecord],
    pools_30m: list[PoolRecord],
    *,
    as_of: datetime,
    atr: float,
    entry_pool: PoolRecord,
) -> list[dict[str, Any]]:
    """Full target ranking using frozen _select_target_below contract."""
    combined = pools_30m + [p for p in pools_15m if p.side == "BID"]
    rows: list[dict[str, Any]] = []
    for p in combined:
        row: dict[str, Any] = {
            "pool_id": p.pool_id,
            "timeframe": p.timeframe,
            "available_at": _iso(p.available_at),
            "invalidated_at": _iso(p.invalidated_at),
            "status_at_as_of": p.status_at(as_of),
            "lower_edge": p.lower_edge,
            "upper_edge": p.upper_edge,
            "strength": p.strength,
            "component_count": p.component_count,
            "midpoint": p.midpoint,
            "side": p.side,
            "distance_from_entry": limit_px - p.midpoint,
            "distance_from_entry_atr": _distance_atr(limit_px, p, atr),
            "eligible": False,
            "exclusion_reason": "",
            "rank": None,
        }
        if p.side != "BID":
            row["exclusion_reason"] = "not_bid"
        elif not pool_valid_at(p, as_of):
            row["exclusion_reason"] = "not_valid_at_as_of"
        elif p.midpoint >= limit_px:
            row["exclusion_reason"] = "midpoint_not_below_entry"
        elif row["distance_from_entry_atr"] < MIN_TARGET_DISTANCE_ATR:
            row["exclusion_reason"] = f"within_min_distance_atr_{MIN_TARGET_DISTANCE_ATR}"
        else:
            row["eligible"] = True
            row["exclusion_reason"] = ""
        rows.append(row)
    eligible = [r for r in rows if r["eligible"]]
    eligible.sort(key=lambda r: r["distance_from_entry"])
    for i, r in enumerate(eligible, start=1):
        r["rank"] = i
        tgt = next(x for x in combined if x.pool_id == r["pool_id"])
        stop, target = _stop_target_levels(
            direction="SHORT",
            symbol=SYMBOL,
            entry=limit_px,
            entry_pool=entry_pool,
            target_pool=tgt,
            atr=atr,
            sweep_high=None,
            sweep_low=None,
        )
        grr = gross_rr("SHORT", limit_px, stop, target)
        r["expected_tp"] = target
        r["stop_loss"] = stop
        r["gross_rr"] = grr
        r["net_rr"] = estimated_net_rr(grr)
        r["selected_by_contract"] = i == 1
    return rows


def _asymmetry_detail(price: float, pools_30m: list[PoolRecord], atr: float) -> dict[str, Any]:
    below = [p for p in pools_30m if p.side == "BID" and p.midpoint < price]
    above = [p for p in pools_30m if p.side == "ASK" and p.midpoint > price]
    nearest_below = min(below, key=lambda p: price - p.midpoint) if below else None
    nearest_above = min(above, key=lambda p: p.midpoint - price) if above else None
    nearer_above = []
    if nearest_below and above:
        nearer_above = [
            p for p in above if (p.midpoint - price) < (price - nearest_below.midpoint)
        ]
    gate_pass = _liquidity_asymmetry_short(price, pools_30m, atr)
    return {
        "gate_pass": gate_pass,
        "nearest_bid_below_id": None if nearest_below is None else nearest_below.pool_id,
        "nearest_bid_below_dist_atr": None
        if nearest_below is None
        else (price - nearest_below.midpoint) / atr,
        "nearest_bid_below_strength": None if nearest_below is None else nearest_below.strength,
        "nearest_ask_above_id": None if nearest_above is None else nearest_above.pool_id,
        "nearest_ask_above_dist_atr": None
        if nearest_above is None
        else (nearest_above.midpoint - price) / atr,
        "n_bid_below": len(below),
        "n_ask_above": len(above),
        "n_ask_nearer_than_nearest_bid": len(nearer_above),
        "bid_strength_vs_nearer_ask_max": gate_pass,
    }


def _ema5m_row(row: pd.Series, prev: pd.Series | None, entry_pool: PoolRecord | None) -> dict[str, Any]:
    if row is None or row.empty:
        return {"bearish_pass": False, "reason": "no_5m_row"}
    close = float(row["close"])
    e9, e20, e59 = row.get("ema_9"), row.get("ema_20"), row.get("ema_59")
    s9, s20 = row.get("ema_9_slope_1"), row.get("ema_20_slope_1")
    reasons = []
    if entry_pool is None:
        bearish = False
        reasons.append("no_entry_pool")
    else:
        bearish = bearish_5m_structural(row, entry_pool, prev)
        if any(x is None or (isinstance(x, float) and pd.isna(x)) for x in (e9, e20, e59)):
            reasons.append("missing_ema")
        if entry_pool and close > entry_pool.upper_edge:
            reasons.append("close_above_entry_pool_upper")
        if e59 is not None and close > float(e59):
            reasons.append("close_above_ema59")
    return {
        "close": close,
        "ema_9": None if e9 is None or pd.isna(e9) else float(e9),
        "ema_20": None if e20 is None or pd.isna(e20) else float(e20),
        "ema_59": None if e59 is None or pd.isna(e59) else float(e59),
        "ema_9_slope_1": None if s9 is None or pd.isna(s9) else float(s9),
        "ema_20_slope_1": None if s20 is None or pd.isna(s20) else float(s20),
        "bearish_5m_pass": bearish,
        "reason_codes": "|".join(reasons) if reasons and not bearish else ("ok" if bearish else "|".join(reasons)),
    }


@dataclass
class ReconstructionState:
    seen_episodes: set[str] = field(default_factory=set)
    old_plan_armed_at: datetime | None = None
    old_plan_invalidated_at: datetime | None = None
    old_plan_final: str = "UNKNOWN"


def run_reconstruction(*, out_dir: Path | None = None) -> dict[str, Any]:
    run_id = int(time.time())
    out = Path(out_dir or DEFAULT_OUT_DIR) / f"doge_short_0630_decision_reconstruction_{run_id}"
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f"refusing to overwrite: {out}")
    out.mkdir(parents=True, exist_ok=False)

    client = get_clickhouse_client()
    candles = build_candles_by_tf(SYMBOL, WARMUP_START, WINDOW_END + timedelta(minutes=1), client=client)
    df1 = attach_context(candles[TF_STRUCTURE].sort_values("open_time").reset_index(drop=True))
    candles[TF_STRUCTURE] = df1
    candles[TF_CONFIRM] = attach_context(candles[TF_CONFIRM].sort_values("open_time").reset_index(drop=True))

    # Full-day scanner trace for old plan lifecycle
    full_end = datetime(2026, 8, 28, 7, 0, 0)
    full_candles = build_candles_by_tf(SYMBOL, WARMUP_START, full_end, client=client)
    full_candles[TF_STRUCTURE] = attach_context(full_candles[TF_STRUCTURE].sort_values("open_time"))
    full_candles[TF_CONFIRM] = attach_context(full_candles[TF_CONFIRM].sort_values("open_time"))
    scanner = PoolSignalScanner(symbol=SYMBOL)
    full_result = scanner.scan(full_candles)

    old_inv = [
        c
        for c in full_result.get("invalidated", [])
        if (c.setup_id if hasattr(c, "setup_id") else c.get("setup_id")) == OLD_SIGNAL_ID
        or (getattr(c, "signal_id", None) or c.get("signal_id")) == OLD_SIGNAL_ID
    ]
    old_plan = None
    if old_inv:
        old_plan = old_inv[0].to_dict() if hasattr(old_inv[0], "to_dict") else old_inv[0]

    state = ReconstructionState(
        seen_episodes=set(scanner._seen_episodes),
        old_plan_armed_at=_utc_naive("2026-08-28 04:15:00"),
        old_plan_invalidated_at=_utc_naive("2026-08-28 04:30:00"),
        old_plan_final="INVALIDATED_UNFILLED",
    )

    causal_rows: list[dict[str, Any]] = []
    entry_timeline: list[dict[str, Any]] = []
    target_by_minute: list[dict[str, Any]] = []
    asym_rows: list[dict[str, Any]] = []
    ema_rows: list[dict[str, Any]] = []
    episode_rows: list[dict[str, Any]] = []
    display_exports: dict[str, Any] = {}

    df1w = candles[TF_CONFIRM]
    df1w = df1w[(pd.to_datetime(df1w["open_time"]) >= WINDOW_START) & (pd.to_datetime(df1w["open_time"]) <= WINDOW_END)]

    scanner_ref = PoolSignalScanner(symbol=SYMBOL)
    scanner_ref._seen_episodes = set(state.seen_episodes)

    for _, row in df1w.iterrows():
        bar_open = _utc_naive(row["open_time"])
        bar_close = bar_open + timedelta(minutes=1)
        as_of = bar_close
        price = float(row["close"])
        high = float(row["high"])
        low = float(row["low"])
        atr = float(row.get("atr_14") or float("nan"))

        active = load_pools_at(candles, symbol=SYMBOL, as_of=as_of)
        engine_map = _engine_pool_map(candles, as_of=as_of)
        entry_pool = engine_map.get(ENTRY_POOL_ID)

        row_5m = scanner_ref._last_closed_row(candles.get(TF_STRUCTURE), pd.Timestamp(bar_close))
        prev_5m = scanner_ref._prev_closed_row(candles.get(TF_STRUCTURE), pd.Timestamp(bar_close))

        limit_px = pullback_limit_price(entry_pool, direction="SHORT") if entry_pool else None

        causal_rows.append(
            {
                "as_of": _iso(as_of),
                "close_1m": price,
                "high_1m": high,
                "low_1m": low,
                "atr_14": atr,
                "n_active_5m": len(active.get("5m", [])),
                "n_active_15m": len(active.get(TF_ENTRY_POOL, [])),
                "n_active_30m": len(active.get(TF_LIQUIDITY, [])),
                "n_active_1h": len(active.get(TF_MACRO, [])),
                "entry_pool_status": None if entry_pool is None else entry_pool.status_at(as_of),
                "entry_limit_px": limit_px,
                "old_plan_active": as_of <= state.old_plan_invalidated_at if state.old_plan_invalidated_at else False,
            }
        )

        if entry_pool:
            ep = entry_pool
            pos = "inside" if ep.lower_edge <= price <= ep.upper_edge else ("above" if price > ep.upper_edge else "below")
            depth_60 = ep.lower_edge + 0.6 * (ep.upper_edge - ep.lower_edge)
            entry_timeline.append(
                {
                    "as_of": _iso(as_of),
                    "pool_id": ENTRY_POOL_ID,
                    "status": ep.status_at(as_of),
                    "available_at": _iso(ep.available_at),
                    "invalidated_at": _iso(ep.invalidated_at),
                    "lower_edge": ep.lower_edge,
                    "upper_edge": ep.upper_edge,
                    "midpoint": ep.midpoint,
                    "price": price,
                    "position_vs_pool": pos,
                    "in_lower_half": price <= ep.midpoint,
                    "at_or_below_60pct_depth": price <= depth_60,
                    "limit_entry_price": limit_px,
                    "price_vs_limit": None if limit_px is None else price - limit_px,
                    "episode_seen": OLD_EPISODE_ID in state.seen_episodes,
                }
            )

        if limit_px is not None and atr_available(atr) and entry_pool is not None:
            ranks = _rank_targets(
                limit_px,
                active.get(TF_ENTRY_POOL, []),
                active.get(TF_LIQUIDITY, []),
                as_of=as_of,
                atr=atr,
                entry_pool=entry_pool,
            )
            for r in ranks:
                r["as_of"] = _iso(as_of)
                target_by_minute.append(r)

        asym = _asymmetry_detail(price, active.get(TF_LIQUIDITY, []), atr)
        asym["as_of"] = _iso(as_of)
        asym_rows.append(asym)

        ema = _ema5m_row(row_5m, prev_5m, entry_pool)
        ema["as_of"] = _iso(as_of)
        ema_rows.append(ema)

        # Episode / spawn check
        cands = detect_pullback_short_candidates(
            symbol=SYMBOL,
            price=price,
            approach_at=as_of,
            pools_15m=active.get(TF_ENTRY_POOL, []),
            pools_30m=active.get(TF_LIQUIDITY, []),
            row_5m=row_5m,
            prev_row_5m=prev_5m,
            atr=atr,
        )
        ep_blocked = OLD_EPISODE_ID in state.seen_episodes
        would_spawn = bool(cands) and any(c.entry_pool.pool_id == ENTRY_POOL_ID for c in cands)
        register_blocked = ep_blocked and would_spawn
        episode_rows.append(
            {
                "as_of": _iso(as_of),
                "episode_id": OLD_EPISODE_ID,
                "seen_episodes_contains": ep_blocked,
                "detect_candidates_n": len(cands),
                "detect_for_ref_entry_pool": would_spawn,
                "register_would_block": register_blocked,
                "old_plan_still_open": as_of <= state.old_plan_invalidated_at,
            }
        )

        if _iso(as_of) in {_iso(t) for t in DISPLAY_SNAPSHOTS}:
            display_exports[_iso(as_of)] = {
                "active_pools": {
                    tf: [p.to_dict() for p in ps]
                    for tf, ps in active.items()
                },
                "entry_pool": _pool_lifecycle_row(entry_pool, as_of),
                "ref_target_15m": _pool_lifecycle_row(engine_map.get(REF_TARGET_15M), as_of),
                "target_30m_0415": _pool_lifecycle_row(engine_map.get(TARGET_30M_0415), as_of),
            }

    # 04:15 target selection trace
    trace_0415 = _target_trace_at(candles, "2026-08-28 04:15:00", scanner_ref)

    # Decision at 06:30
    decision_at = _utc_naive("2026-08-28 06:30:00")
    decision = _decision_at(candles, decision_at, state, scanner_ref)

    verdict = _classify_verdict(decision, state)

    manifest = {
        "run_id": run_id,
        "symbol": SYMBOL,
        "window_start": _iso(WINDOW_START),
        "window_end": _iso(WINDOW_END),
        "verdict": verdict,
        "old_plan": {
            "signal_id": OLD_SIGNAL_ID,
            "episode_id": OLD_EPISODE_ID,
            "armed_at": _iso(state.old_plan_armed_at),
            "invalidated_at": _iso(state.old_plan_invalidated_at),
            "final_state": state.old_plan_final,
            "from_scanner": old_plan,
        },
        "pool_time_semantics_version": "closed_confirmation_bar_v2",
        "decision_at": _iso(decision_at),
        "decision_summary": decision,
    }

    _write_csv(out / "causal_snapshots_1m.csv", causal_rows)
    _write_csv(out / "entry_pool_timeline.csv", entry_timeline)
    _write_csv(out / "eligible_targets_by_minute.csv", target_by_minute)
    _write_csv(out / "target_selection_trace.csv", trace_0415 + decision.get("target_trace_0630", []))
    _write_csv(out / "asymmetry_timeline.csv", asym_rows)
    _write_csv(out / "ema5m_structure.csv", ema_rows)
    _write_csv(out / "episode_rearm_audit.csv", episode_rows)
    (out / "decision_result.json").write_text(json.dumps(decision, indent=2, default=str), encoding="utf-8")
    (out / "display_snapshots.json").write_text(json.dumps(display_exports, indent=2, default=str), encoding="utf-8")
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    (out / "methodology.md").write_text(_methodology(), encoding="utf-8")
    (out / "report.md").write_text(_report(manifest, decision, trace_0415), encoding="utf-8")

    return {"out_dir": str(out), "manifest": manifest, "verdict": verdict}


def _target_trace_at(candles: dict, ts: str, scanner: PoolSignalScanner) -> list[dict[str, Any]]:
    as_of = _utc_naive(ts)
    row = candles[TF_CONFIRM]
    row = row[pd.to_datetime(row["open_time"]) + pd.Timedelta(minutes=1) == pd.Timestamp(as_of)]
    if row.empty:
        return []
    r = row.iloc[0]
    price = float(r["close"])
    atr = float(r.get("atr_14") or float("nan"))
    active = load_pools_at(candles, symbol=SYMBOL, as_of=as_of)
    entry = next((p for p in active.get(TF_ENTRY_POOL, []) if p.pool_id == ENTRY_POOL_ID), None)
    if entry is None:
        em = _engine_pool_map(candles, as_of=as_of).get(ENTRY_POOL_ID)
        entry = em
    if entry is None:
        return [{"as_of": _iso(as_of), "note": "entry_pool_not_active"}]
    limit_px = pullback_limit_price(entry, direction="SHORT")
    ranks = _rank_targets(
        limit_px, active.get(TF_ENTRY_POOL, []), active.get(TF_LIQUIDITY, []), as_of=as_of, atr=atr, entry_pool=entry
    )
    for x in ranks:
        x["context"] = "trace_at_" + ts.replace(" ", "T").replace(":", "")
    return ranks


def _decision_at(
    candles: dict,
    as_of: datetime,
    state: ReconstructionState,
    scanner: PoolSignalScanner,
) -> dict[str, Any]:
    row = candles[TF_CONFIRM]
    row = row[pd.to_datetime(row["open_time"]) + pd.Timedelta(minutes=1) == pd.Timestamp(as_of)]
    if row.empty:
        return {"error": "no_bar"}
    r = row.iloc[0]
    price = float(r["close"])
    atr = float(r.get("atr_14") or float("nan"))
    active = load_pools_at(candles, symbol=SYMBOL, as_of=as_of)
    engine_map = _engine_pool_map(candles, as_of=as_of)
    entry = engine_map.get(ENTRY_POOL_ID)
    row_5m = scanner._last_closed_row(candles.get(TF_STRUCTURE), pd.Timestamp(as_of))
    prev_5m = scanner._prev_closed_row(candles.get(TF_STRUCTURE), pd.Timestamp(as_of))

    limit_px = pullback_limit_price(entry, direction="SHORT") if entry else None
    cands = detect_pullback_short_candidates(
        symbol=SYMBOL,
        price=price,
        approach_at=as_of,
        pools_15m=active.get(TF_ENTRY_POOL, []),
        pools_30m=active.get(TF_LIQUIDITY, []),
        row_5m=row_5m,
        prev_row_5m=prev_5m,
        atr=atr,
    )
    ref_cands = [c for c in cands if c.entry_pool.pool_id == ENTRY_POOL_ID]

    gate_breakdown = {
        "entry_reachable": bool(
            select_pullback_entry_pools(
                active.get(TF_ENTRY_POOL, []),
                price=price,
                approach_at=as_of,
                direction="SHORT",
                atr=atr,
            )
        ),
        "asymmetry_pass": _liquidity_asymmetry_short(price, active.get(TF_LIQUIDITY, []), atr),
        "bearish_5m_pass": bearish_5m_structural(row_5m, entry, prev_5m) if entry else False,
        "target_exists": _select_target_below(
            limit_px or price,
            active.get(TF_LIQUIDITY, []) + [p for p in active.get(TF_ENTRY_POOL, []) if p.side == "BID"],
            atr,
            as_of=as_of,
        )
        is not None
        if limit_px
        else False,
        "detect_produces_candidate": bool(ref_cands),
        "episode_seen_blocks_register": OLD_EPISODE_ID in state.seen_episodes,
        "old_plan_closed": as_of > state.old_plan_invalidated_at,
    }

    target_trace = []
    if limit_px and atr_available(atr) and entry:
        target_trace = _rank_targets(
            limit_px,
            active.get(TF_ENTRY_POOL, []),
            active.get(TF_LIQUIDITY, []),
            as_of=as_of,
            atr=atr,
            entry_pool=entry,
        )
        for x in target_trace:
            x["context"] = "decision_0630"

    new_plan = None
    if ref_cands and not gate_breakdown["episode_seen_blocks_register"]:
        cand = ref_cands[0]
        finalize_levels(cand, symbol=SYMBOL, atr=atr)
        new_plan = cand.to_dict()

    return {
        "decision_at": _iso(as_of),
        "price": price,
        "atr": atr,
        "limit_px": limit_px,
        "entry_pool": None if entry is None else entry.to_dict(),
        "gate_breakdown": gate_breakdown,
        "n_candidates": len(ref_cands),
        "target_trace_0630": target_trace,
        "hypothetical_new_plan": new_plan,
        "asymmetry": _asymmetry_detail(price, active.get(TF_LIQUIDITY, []), atr),
        "ema5m": _ema5m_row(row_5m, prev_5m, entry),
    }


def _classify_verdict(decision: dict[str, Any], state: ReconstructionState) -> str:
    gb = decision.get("gate_breakdown") or {}
    if gb.get("episode_seen_blocks_register"):
        return VERDICT_SAME_EP
    if decision.get("hypothetical_new_plan"):
        return VERDICT_NEW
    if gb.get("detect_produces_candidate") and not gb.get("target_exists"):
        return VERDICT_NO_TARGET
    if not gb.get("asymmetry_pass") or not gb.get("bearish_5m_pass"):
        return VERDICT_STRUCTURE
    if not gb.get("detect_produces_candidate"):
        return VERDICT_MANUAL
    return VERDICT_MANUAL


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    pd.DataFrame(rows).to_csv(path, index=False)


def _methodology() -> str:
    return """# DOGE Short 06:30 decision reconstruction

Minute-by-minute causal snapshots using closed 1m prefix → HTF pools (v2 semantics).
No threshold changes. Old plan 04:15–04:30 kept terminal.
Episode contract: `A_PLUS_PULLBACK_SHORT:{entry_pool_id}` — `_seen_episodes` never cleared on invalidation.
"""


def _report(manifest: dict, decision: dict, trace_0415: list[dict]) -> str:
    sel_0415 = [r for r in trace_0415 if r.get("selected_by_contract")]
    sel_0630 = [r for r in decision.get("target_trace_0630", []) if r.get("selected_by_contract")]
    ref_0630 = next((r for r in decision.get("target_trace_0630", []) if r.get("pool_id") == REF_TARGET_15M), None)
    return "\n".join(
        [
            f"# {manifest['verdict']}",
            "",
            "## Old plan 04:15–04:30",
            json.dumps(manifest["old_plan"], indent=2, default=str),
            "",
            "## Decision 06:30",
            json.dumps(decision.get("gate_breakdown"), indent=2),
            "",
            "## Target @ 04:15 (contract winner)",
            json.dumps(sel_0415[:1], indent=2, default=str),
            "",
            "## Target @ 06:30 (contract winner)",
            json.dumps(sel_0630[:1], indent=2, default=str),
            "",
            "## Reference 15m target @ 06:30",
            json.dumps(ref_0630, indent=2, default=str),
            "",
            "## Episode",
            f"seen_episodes contains {OLD_EPISODE_ID}: {decision.get('gate_breakdown', {}).get('episode_seen_blocks_register')}",
        ]
    )


if __name__ == "__main__":
    print(json.dumps(run_reconstruction(), indent=2, default=str))
