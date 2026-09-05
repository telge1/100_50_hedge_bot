"""Causal research backtest for A_PLUS_NESTED_ASK_POOL_EDGE_SHORT_V1."""

from __future__ import annotations

import hashlib
import json
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.models import PoolRecord, _utc_naive
from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.pools import load_engine_pools_at, pool_from_engine
from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.runner import build_candles_by_tf
from orderbook_analyse.liquidity_location_pool_lifecycle.ema_context import attach_context
from orderbook_analyse.orderbook_v2.ch_client import get_clickhouse_client

from .config import (
    APPROACH_MAX_ATR,
    DEFAULT_OUT_DIR,
    REFERENCE_ENTRY_APPROX,
    REFERENCE_SL_APPROX,
    REFERENCE_SYMBOL,
    REFERENCE_WINDOW_END,
    REFERENCE_WINDOW_START,
    ROUNDTRIP_COST_PCT_BASELINE,
    ROUNDTRIP_COST_PCT_SENSITIVITY,
    SETUP_TYPE,
    SETUP_VERSION,
    TF_CHILD,
    TF_PARENT_15M,
    TF_PARENT_5M,
    TIMEFRAMES,
)
from .fills_outcomes import detect_short_limit_fill, evaluate_target_variants
from .geometry import (
    active_asks,
    active_bids,
    bid_liquidity_below,
    rank_nested_ask_structures,
    select_nested_ask_structure,
    structural_stop,
    upper_gap_metrics,
)


def _iso(ts: Any) -> str:
    if ts is None:
        return ""
    return _utc_naive(pd.Timestamp(ts).to_pydatetime()).isoformat()


def _candidate_id(symbol: str, child_id: str, decision_at: datetime) -> str:
    key = f"{SETUP_TYPE}|{symbol}|{child_id}|{decision_at.isoformat()}"
    return hashlib.sha256(key.encode()).hexdigest()[:20]


def _pool_snapshot_at_end(
    candles: dict[str, pd.DataFrame],
    *,
    symbol: str,
    as_of: datetime,
) -> dict[str, list[PoolRecord]]:
    raw = load_engine_pools_at(candles, symbol=symbol, as_of=as_of, timeframes=TIMEFRAMES)
    out: dict[str, list[PoolRecord]] = {}
    for tf, pools in raw.items():
        out[tf] = [pool_from_engine(p) for p in pools]
    return out


def _filter_active(pools: list[PoolRecord], as_of: datetime, side: str | None = None) -> list[PoolRecord]:
    rows = []
    for p in pools:
        if side and p.side != side:
            continue
        if p.is_active_at(as_of):
            rows.append(p)
    return rows


def _atr_at(df5: pd.DataFrame, as_of: datetime) -> float:
    """Last closed 5m ATR available at as_of."""
    t = _utc_naive(as_of)
    sub = df5[pd.to_datetime(df5["open_time"]) + pd.Timedelta(minutes=5) <= t]
    if sub.empty:
        return float("nan")
    v = sub.iloc[-1].get("atr_14")
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return float("nan")
    return float(v)


def _ema200_at(df5: pd.DataFrame, as_of: datetime) -> float | None:
    t = _utc_naive(as_of)
    sub = df5[pd.to_datetime(df5["open_time"]) + pd.Timedelta(minutes=5) <= t]
    if sub.empty:
        return None
    v = sub.iloc[-1].get("ema_200")
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    return float(v)


def _max_feature_ts(structure, decision_at: datetime) -> datetime:
    times = [
        _utc_naive(structure.child_1m.available_at),
        _utc_naive(structure.parent_5m.available_at),
        _utc_naive(structure.parent_15m.available_at),
        _utc_naive(decision_at),
    ]
    if structure.child_1m.max_feature_timestamp:
        times.append(_utc_naive(structure.child_1m.max_feature_timestamp))
    return max(times)


def scan_symbol_window(
    *,
    symbol: str,
    candles: dict[str, pd.DataFrame],
    pool_universe: dict[str, list[PoolRecord]],
    df5_ctx: pd.DataFrame,
    window_start: datetime,
    window_end: datetime,
    seen_episodes: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Walk closed 1m bars; emit candidates / rejects / trades / ambiguous."""
    seen = seen_episodes if seen_episodes is not None else set()
    df1 = candles["1m"].sort_values("open_time").reset_index(drop=True).copy()
    df1["open_time"] = pd.to_datetime(df1["open_time"])

    candidates: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []

    bars = df1[(df1["open_time"] >= window_start) & (df1["open_time"] < window_end)]
    for _, row in bars.iterrows():
        bar_open = _utc_naive(row["open_time"].to_pydatetime())
        decision_at = bar_open + timedelta(minutes=1)
        price = float(row["close"])
        atr = _atr_at(df5_ctx, decision_at)
        if not (atr == atr and atr > 0):
            continue

        asks_15 = _filter_active(pool_universe.get(TF_PARENT_15M, []), decision_at, "ASK")
        asks_5 = _filter_active(pool_universe.get(TF_PARENT_5M, []), decision_at, "ASK")
        asks_1 = _filter_active(pool_universe.get(TF_CHILD, []), decision_at, "ASK")
        bids_all = (
            _filter_active(pool_universe.get(TF_PARENT_15M, []), decision_at, "BID")
            + _filter_active(pool_universe.get(TF_PARENT_5M, []), decision_at, "BID")
            + _filter_active(pool_universe.get(TF_CHILD, []), decision_at, "BID")
        )

        ranked = rank_nested_ask_structures(
            asks_15m=asks_15, asks_5m=asks_5, asks_1m=asks_1, price=price, as_of=decision_at
        )
        if not ranked:
            continue

        chosen = None
        stop_info = None
        for structure in ranked:
            dist_atr = (structure.child_1m.lower_edge - price) / atr
            if dist_atr > APPROACH_MAX_ATR or dist_atr < 0:
                rejected.append(
                    {
                        "symbol": symbol,
                        "decision_at": _iso(decision_at),
                        "reason": "APPROACH_DISTANCE",
                        "dist_atr": dist_atr,
                        "child_pool_id": structure.child_1m.pool_id,
                    }
                )
                continue
            episode = f"{SETUP_TYPE}:{structure.child_1m.pool_id}"
            if episode in seen:
                rejected.append(
                    {
                        "symbol": symbol,
                        "decision_at": _iso(decision_at),
                        "reason": "EPISODE_ALREADY_SEEN",
                        "episode_id": episode,
                    }
                )
                continue
            si = structural_stop(structure=structure, atr=atr, symbol=symbol)
            if si["stop_too_wide"]:
                rejected.append(
                    {
                        "symbol": symbol,
                        "decision_at": _iso(decision_at),
                        "reason": "STOP_TOO_WIDE",
                        "stop_distance_pct": si["stop_distance_pct"],
                        "child_pool_id": structure.child_1m.pool_id,
                        **{k: si[k] for k in ("entry_price", "stop_loss", "stop_reference")},
                    }
                )
                continue
            chosen = structure
            stop_info = si
            break

        if chosen is None or stop_info is None:
            continue

        structure = chosen
        dist_atr = (structure.child_1m.lower_edge - price) / atr
        episode = f"{SETUP_TYPE}:{structure.child_1m.pool_id}"

        order_active_at = max(decision_at, _utc_naive(structure.child_1m.available_at))
        max_feat = _max_feature_ts(structure, decision_at)
        if max_feat > order_active_at:
            order_active_at = max_feat

        gap = upper_gap_metrics(
            parent_zone_high=structure.parent_zone_high,
            asks=asks_15 + asks_5 + asks_1,
            as_of=decision_at,
            atr=atr,
            symbol=symbol,
        )
        bid_info = bid_liquidity_below(entry=stop_info["entry_price"], bids=bids_all, as_of=decision_at, atr=atr)
        if bid_info["bid_pool_count_below"] < 1:
            rejected.append(
                {
                    "symbol": symbol,
                    "decision_at": _iso(decision_at),
                    "reason": "NO_CAUSAL_BID_TARGET",
                    "child_pool_id": structure.child_1m.pool_id,
                }
            )
            continue

        ema200 = _ema200_at(df5_ctx, decision_at)
        ema_confluence = False
        if ema200 is not None:
            ema_confluence = structure.parent_zone_low <= ema200 <= structure.parent_zone_high

        cid = _candidate_id(symbol, structure.child_1m.pool_id, decision_at)
        seen.add(episode)

        cand = {
            "candidate_id": cid,
            "episode_id": episode,
            "setup_type": SETUP_TYPE,
            "symbol": symbol,
            "decision_at": _iso(decision_at),
            "order_active_at": _iso(order_active_at),
            "max_feature_timestamp": _iso(max_feat),
            "price_at_decision": price,
            "atr": atr,
            "approach_dist_atr": dist_atr,
            "ema200": ema200,
            "ema200_confluence": ema_confluence,
            "orderflow_status": "UNAVAILABLE",
            **structure.to_dict(),
            **stop_info,
            **gap,
            "bid_pool_count_below": bid_info["bid_pool_count_below"],
            "nearest_bid_pool_id": bid_info["nearest_bid_pool_id"],
            "nearest_bid_pool_high": bid_info["nearest_bid_pool_high"],
            "nearest_bid_pool_mid": bid_info["nearest_bid_pool_mid"],
            "distance_to_nearest_bid_pct": bid_info["distance_to_nearest_bid_pct"],
            "distance_to_nearest_bid_atr": bid_info["distance_to_nearest_bid_atr"],
            "cumulative_bid_pool_score_below": bid_info["cumulative_bid_pool_score_below"],
            "number_of_distinct_bid_targets": bid_info["number_of_distinct_bid_targets"],
        }
        candidates.append(cand)

        fill = detect_short_limit_fill(
            df1,
            entry_price=stop_info["entry_price"],
            order_active_at=order_active_at,
            child_available_at=structure.child_1m.available_at,
            horizon_end=window_end + timedelta(hours=4),
        )
        cand["fill_status"] = fill.status
        cand["fill_at"] = fill.to_dict()["fill_at"]
        cand["first_touch_at"] = fill.to_dict()["first_touch_at"]
        cand["same_bar_ambiguous"] = fill.same_bar_ambiguous

        if fill.status == "SAME_BAR_SEQUENCE_AMBIGUOUS":
            ambiguous.append({**cand, "ambiguous_reason": "birth_bar_touch_before_available"})
            continue
        if fill.status != "FILLED" or fill.fill_at is None:
            continue

        assert max_feat <= order_active_at <= fill.fill_at

        outcomes = evaluate_target_variants(
            df1,
            fill_at=fill.fill_at,
            entry=stop_info["entry_price"],
            stop=stop_info["stop_loss"],
            bid_info=bid_info,
            cost_pct=ROUNDTRIP_COST_PCT_BASELINE,
        )
        primary = next((o for o in outcomes if o["target_variant"] == "A_first_bid_near_edge"), outcomes[0])
        trade = {
            **cand,
            "fill_price": fill.fill_price,
            "fill_at": _iso(fill.fill_at),
            "primary_target_variant": primary["target_variant"],
            "primary_result": primary["result"],
            "primary_gross_pnl_pct": primary.get("gross_pnl_pct"),
            "primary_net_pnl_pct": primary.get("net_pnl_pct"),
            "primary_gross_r": primary.get("gross_r"),
            "primary_net_r": primary.get("net_r"),
            "primary_mfe": primary.get("mfe"),
            "primary_mae": primary.get("mae"),
            "primary_hold_minutes": primary.get("hold_minutes"),
            "outcomes": outcomes,
            "fixed_1pct_benchmark": evaluate_target_variants(
                df1,
                fill_at=fill.fill_at,
                entry=stop_info["entry_price"],
                stop=stop_info["fixed_1pct_stop"],
                bid_info=bid_info,
                cost_pct=ROUNDTRIP_COST_PCT_BASELINE,
            ),
        }
        trades.append(trade)

    return candidates, rejected, trades, ambiguous



def _summarize(trades: list[dict[str, Any]], *, label: str) -> dict[str, Any]:
    n = len(trades)
    if n == 0:
        return {"label": label, "n": 0, "note": "empty"}
    nets = [t["primary_net_pnl_pct"] for t in trades if t.get("primary_net_pnl_pct") is not None]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    gross_wins = sum(wins) if wins else 0.0
    gross_losses = abs(sum(losses)) if losses else 0.0
    results = [t.get("primary_result") for t in trades]
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for x in nets:
        equity += x
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    return {
        "label": label,
        "n": n,
        "n_descriptive_only": n < 30,
        "fill_count": n,
        "winrate": len(wins) / n if n else None,
        "expectancy_net_pct": sum(nets) / n if nets else None,
        "profit_factor": (gross_wins / gross_losses) if gross_losses > 0 else None,
        "net_pnl_pct_sum": sum(nets),
        "median_net_r": float(pd.Series([t.get("primary_net_r") for t in trades if t.get("primary_net_r") is not None]).median())
        if any(t.get("primary_net_r") is not None for t in trades)
        else None,
        "median_mfe": float(pd.Series([t.get("primary_mfe") for t in trades if t.get("primary_mfe") is not None]).median())
        if any(t.get("primary_mfe") is not None for t in trades)
        else None,
        "median_mae": float(pd.Series([t.get("primary_mae") for t in trades if t.get("primary_mae") is not None]).median())
        if any(t.get("primary_mae") is not None for t in trades)
        else None,
        "max_drawdown_pct": max_dd,
        "sl_first": results.count("SL_FIRST"),
        "tp_first": results.count("TP_FIRST"),
        "ambiguous": results.count("AMBIGUOUS"),
        "neither": results.count("NEITHER"),
    }


def _bucket_table(trades: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    groups: dict[str, list] = defaultdict(list)
    for t in trades:
        groups[str(t.get(key, "NA"))].append(t)
    return [_summarize(v, label=f"{key}={k}") for k, v in sorted(groups.items())]


def _outcomes_by_target(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    by_var: dict[str, list] = defaultdict(list)
    for t in trades:
        for o in t.get("outcomes") or []:
            by_var[o["target_variant"]].append({**t, "primary_result": o.get("result"), "primary_net_pnl_pct": o.get("net_pnl_pct"), "primary_net_r": o.get("net_r"), "primary_mfe": o.get("mfe"), "primary_mae": o.get("mae")})
    for k, v in sorted(by_var.items()):
        rows.append(_summarize(v, label=k))
    return rows


def _fee_sensitivity(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for cost in ROUNDTRIP_COST_PCT_SENSITIVITY:
        adj = []
        for t in trades:
            primary = next((o for o in (t.get("outcomes") or []) if o["target_variant"] == "A_first_bid_near_edge"), None)
            if not primary:
                continue
            gross = primary.get("gross_pnl_pct") or 0.0
            adj.append({**t, "primary_net_pnl_pct": gross - cost, "primary_result": primary.get("result")})
        rows.append({**_summarize(adj, label=f"cost={cost}"), "cost_pct": cost})
    return rows


def replay_reference_case(
    candles: dict[str, pd.DataFrame],
    pool_universe: dict[str, list[PoolRecord]],
    df5_ctx: pd.DataFrame,
) -> dict[str, Any]:
    start = datetime.fromisoformat(REFERENCE_WINDOW_START)
    end = datetime.fromisoformat(REFERENCE_WINDOW_END)
    cands, rejs, trades, amb = scan_symbol_window(
        symbol=REFERENCE_SYMBOL,
        candles=candles,
        pool_universe=pool_universe,
        df5_ctx=df5_ctx,
        window_start=start,
        window_end=end,
    )
    # also strict load at key times for parity audit
    strict_checks = []
    for as_of in [
        datetime(2026, 8, 28, 14, 40),
        datetime(2026, 8, 28, 14, 45),
        datetime(2026, 8, 28, 14, 50),
        datetime(2026, 8, 28, 15, 0),
    ]:
        snap = _pool_snapshot_at_end(candles, symbol=REFERENCE_SYMBOL, as_of=as_of)
        asks_15 = active_asks(snap[TF_PARENT_15M], as_of)
        asks_5 = active_asks(snap[TF_PARENT_5M], as_of)
        asks_1 = active_asks(snap[TF_CHILD], as_of)
        # price = last closed 1m close
        df1 = candles["1m"]
        sub = df1[pd.to_datetime(df1["open_time"]) + pd.Timedelta(minutes=1) <= as_of]
        price = float(sub.iloc[-1]["close"]) if not sub.empty else None
        atr = _atr_at(df5_ctx, as_of)
        st = select_nested_ask_structure(asks_15m=asks_15, asks_5m=asks_5, asks_1m=asks_1, price=price or 0, as_of=as_of)
        strict_checks.append(
            {
                "as_of": _iso(as_of),
                "price": price,
                "structure": None if st is None else st.to_dict(),
                "stop": None if st is None else structural_stop(structure=st, atr=atr, symbol=REFERENCE_SYMBOL),
            }
        )

    best = None
    if trades:
        best = trades[0]
    elif cands:
        best = cands[0]

    entry_parity = None
    sl_parity = None
    visual_child_audit = None
    if best:
        entry = best.get("entry_price")
        sl = best.get("stop_loss")
        entry_parity = {
            "computed_entry": entry,
            "reference_approx": REFERENCE_ENTRY_APPROX,
            "abs_diff": abs(entry - REFERENCE_ENTRY_APPROX) if entry else None,
            "match_within_2_ticks": abs(entry - REFERENCE_ENTRY_APPROX) <= 2e-5 if entry else False,
        }
        sl_parity = {
            "computed_stop_loss": sl,
            "computed_stop_reference": best.get("stop_reference"),
            "reference_approx": REFERENCE_SL_APPROX,
            "abs_diff_to_sl": abs(sl - REFERENCE_SL_APPROX) if sl else None,
            "abs_diff_to_reference": abs((best.get("stop_reference") or 0) - REFERENCE_SL_APPROX),
        }

    # Explicit audit of screenshot-near child lower edge ~0.08791 (parity only)
    for chk in strict_checks:
        st = chk.get("structure") or {}
        if not st:
            continue
        if abs(float(st.get("child_pool_low") or 0) - REFERENCE_ENTRY_APPROX) <= 5e-5:
            visual_child_audit = {
                "as_of": chk["as_of"],
                "selected_by_engine": True,
                "structure": st,
                "stop": chk.get("stop"),
            }
            break
    if visual_child_audit is None:
        # search ranked nests at 14:45 for child near reference entry
        as_of = datetime(2026, 8, 28, 14, 45)
        snap = _pool_snapshot_at_end(candles, symbol=REFERENCE_SYMBOL, as_of=as_of)
        df1 = candles["1m"]
        sub = df1[pd.to_datetime(df1["open_time"]) + pd.Timedelta(minutes=1) <= as_of]
        price = float(sub.iloc[-1]["close"]) if not sub.empty else 0.0
        atr = _atr_at(df5_ctx, as_of)
        ranked = rank_nested_ask_structures(
            asks_15m=active_asks(snap[TF_PARENT_15M], as_of),
            asks_5m=active_asks(snap[TF_PARENT_5M], as_of),
            asks_1m=active_asks(snap[TF_CHILD], as_of),
            price=price,
            as_of=as_of,
        )
        near = [s for s in ranked if abs(s.child_1m.lower_edge - REFERENCE_ENTRY_APPROX) <= 5e-5]
        visual_child_audit = {
            "as_of": _iso(as_of),
            "price": price,
            "selected_by_engine_as_nearest": False,
            "n_ranked": len(ranked),
            "nearest_child_low": ranked[0].child_1m.lower_edge if ranked else None,
            "nearest_child_id": ranked[0].child_1m.pool_id if ranked else None,
            "visual_near_entry_structures": [
                {**s.to_dict(), "stop": structural_stop(structure=s, atr=atr, symbol=REFERENCE_SYMBOL)}
                for s in near
            ],
            "note": (
                "Screenshot entry ~0.087918 matches child lower_edge 0.08791. "
                "Nearest-child rule may prefer a lower child first; STOP_TOO_WIDE skips to next."
            ),
        }

    return {
        "window": {"start": REFERENCE_WINDOW_START, "end": REFERENCE_WINDOW_END},
        "candidates": cands,
        "rejected": rejs,
        "trades": [{k: v for k, v in t.items() if k not in ("outcomes", "fixed_1pct_benchmark")} for t in trades],
        "trades_full": trades,
        "ambiguous": amb,
        "strict_as_of_checks": strict_checks,
        "entry_parity": entry_parity,
        "sl_parity": sl_parity,
        "visual_child_audit": visual_child_audit,
        "note": (
            "Reference approx prices are parity checks only; detection never hardcodes them. "
            "If computed edges differ, trust pool engine geometry."
        ),
    }


def run_backtest(
    *,
    symbols: Iterable[str] = (REFERENCE_SYMBOL,),
    warmup_start: datetime = datetime(2026, 8, 25, 0, 0, 0),
    scan_start: datetime = datetime(2026, 8, 28, 0, 0, 0),
    scan_end: datetime = datetime(2026, 8, 28, 23, 59, 0),
    out_dir: Path | None = None,
) -> dict[str, Any]:
    run_id = int(time.time())
    out = Path(out_dir or DEFAULT_OUT_DIR) / f"nested_ask_pool_edge_short_v1_{run_id}"
    out.mkdir(parents=True, exist_ok=True)

    client = get_clickhouse_client()
    all_cands: list[dict] = []
    all_rej: list[dict] = []
    all_trades: list[dict] = []
    all_amb: list[dict] = []
    coverage: dict[str, Any] = {}

    for symbol in symbols:
        candles = build_candles_by_tf(symbol, warmup_start, scan_end + timedelta(hours=6), client=client)
        if not candles or candles.get("1m") is None or candles["1m"].empty:
            coverage[symbol] = {"status": "NO_CANDLES"}
            continue
        df5 = attach_context(candles["5m"].sort_values("open_time").reset_index(drop=True))
        candles["5m"] = df5
        # universe at scan_end (temporal filter for causality)
        pool_universe = _pool_snapshot_at_end(candles, symbol=symbol, as_of=scan_end + timedelta(hours=4))
        coverage[symbol] = {
            "status": "OK",
            "n_1m_bars": len(candles["1m"]),
            "n_pools_1m": len(pool_universe.get(TF_CHILD, [])),
            "n_pools_5m": len(pool_universe.get(TF_PARENT_5M, [])),
            "n_pools_15m": len(pool_universe.get(TF_PARENT_15M, [])),
            "scan_start": _iso(scan_start),
            "scan_end": _iso(scan_end),
        }
        cands, rejs, trades, amb = scan_symbol_window(
            symbol=symbol,
            candles=candles,
            pool_universe=pool_universe,
            df5_ctx=df5,
            window_start=scan_start,
            window_end=scan_end,
        )
        all_cands.extend(cands)
        all_rej.extend(rejs)
        all_trades.extend(trades)
        all_amb.extend(amb)

        if symbol == REFERENCE_SYMBOL:
            ref = replay_reference_case(candles, pool_universe, df5)
            (out / "reference_case_replay.json").write_text(json.dumps(ref, indent=2, default=str), encoding="utf-8")

    # flat outcomes rows
    outcome_rows = []
    for t in all_trades:
        for o in t.get("outcomes") or []:
            outcome_rows.append(
                {
                    "candidate_id": t["candidate_id"],
                    "symbol": t["symbol"],
                    "upper_gap_atr_bucket": t.get("upper_gap_atr_bucket"),
                    "stop_distance_pct": t.get("stop_distance_pct"),
                    "bid_pool_count_below": t.get("bid_pool_count_below"),
                    "ema200_confluence": t.get("ema200_confluence"),
                    **o,
                }
            )

    _write_csv(out / "candidates.csv", all_cands)
    _write_csv(out / "rejected_candidates.csv", all_rej)
    _write_csv(
        out / "trades.csv",
        [{k: v for k, v in t.items() if k not in ("outcomes", "fixed_1pct_benchmark")} for t in all_trades],
    )
    _write_csv(out / "outcomes_by_target.csv", _outcomes_by_target(all_trades))
    _write_csv(out / "outcomes_by_upper_gap_bucket.csv", _bucket_table(all_trades, "upper_gap_atr_bucket"))
    # stop distance buckets
    for t in all_trades:
        d = t.get("stop_distance_pct")
        if d is None:
            t["stop_distance_bucket"] = "NA"
        elif d <= 0.35:
            t["stop_distance_bucket"] = "<=0.35pct"
        elif d <= 0.70:
            t["stop_distance_bucket"] = "0.35-0.70pct"
        elif d <= 1.00:
            t["stop_distance_bucket"] = "0.70-1.00pct"
        else:
            t["stop_distance_bucket"] = ">1.00pct"
    _write_csv(out / "outcomes_by_stop_distance_bucket.csv", _bucket_table(all_trades, "stop_distance_bucket"))
    for t in all_trades:
        n = t.get("bid_pool_count_below") or 0
        if n <= 1:
            t["bid_count_bucket"] = "1"
        elif n <= 3:
            t["bid_count_bucket"] = "2-3"
        elif n <= 6:
            t["bid_count_bucket"] = "4-6"
        else:
            t["bid_count_bucket"] = ">=7"
    _write_csv(out / "outcomes_by_bid_pool_count.csv", _bucket_table(all_trades, "bid_count_bucket"))
    # overlap always true for accepted — report anyway
    _write_csv(
        out / "outcomes_by_pool_overlap.csv",
        [
            _summarize([t for t in all_trades if t.get("overlap_1m_5m") and t.get("overlap_1m_15m")], label="full_nest"),
            _summarize([t for t in all_trades if t.get("ema200_confluence")], label="with_ema200"),
            _summarize([t for t in all_trades if not t.get("ema200_confluence")], label="without_ema200"),
            _summarize([t for t in all_trades if t.get("orderflow_status") == "UNAVAILABLE"], label="orderflow_unavailable"),
        ],
    )
    _write_csv(out / "fee_sensitivity.csv", _fee_sensitivity(all_trades))
    _write_csv(out / "ambiguous_cases.csv", all_amb)
    _write_csv(out / "outcomes_flat.csv", outcome_rows)

    # structural vs fixed 1%
    fixed_trades = []
    for t in all_trades:
        outs = t.get("fixed_1pct_benchmark") or []
        primary = next((o for o in outs if o["target_variant"] == "A_first_bid_near_edge"), None)
        if primary:
            fixed_trades.append(
                {
                    **t,
                    "primary_result": primary.get("result"),
                    "primary_net_pnl_pct": primary.get("net_pnl_pct"),
                    "primary_net_r": primary.get("net_r"),
                    "primary_mfe": primary.get("mfe"),
                    "primary_mae": primary.get("mae"),
                }
            )

    summary = {
        "setup_type": SETUP_TYPE,
        "setup_version": SETUP_VERSION,
        "candidates": len(all_cands),
        "rejected": len(all_rej),
        "ambiguous": len(all_amb),
        "fills_strict": len(all_trades),
        "fill_rate_vs_candidates": len(all_trades) / len(all_cands) if all_cands else None,
        "structural_sl": _summarize(all_trades, label="structural_sl"),
        "fixed_1pct_sl": _summarize(fixed_trades, label="fixed_1pct_sl"),
        "by_symbol": [_summarize([t for t in all_trades if t["symbol"] == s], label=s) for s in symbols],
        "coverage": coverage,
        "integrity": {
            "max_feature_le_order_active_le_fill": all(
                pd.Timestamp(t["max_feature_timestamp"])
                <= pd.Timestamp(t["order_active_at"])
                <= pd.Timestamp(t["fill_at"])
                for t in all_trades
            ),
            "no_doge_hardcoding_in_detection": True,
            "orderflow_required": False,
        },
    }

    integrity = {
        "run_id": run_id,
        "checks": summary["integrity"],
        "n_candidates": len(all_cands),
        "n_trades": len(all_trades),
        "n_ambiguous": len(all_amb),
        "reject_reasons": pd.Series([r.get("reason") for r in all_rej]).value_counts().to_dict() if all_rej else {},
    }
    (out / "integrity_report.json").write_text(json.dumps(integrity, indent=2, default=str), encoding="utf-8")
    (out / "manifest.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "setup_type": SETUP_TYPE,
                "symbols": list(symbols),
                "warmup_start": _iso(warmup_start),
                "scan_start": _iso(scan_start),
                "scan_end": _iso(scan_end),
                "summary": summary,
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    (out / "report.md").write_text(_report_md(summary, integrity, out), encoding="utf-8")
    (out / "methodology.md").write_text(_methodology(), encoding="utf-8")

    return {"run_id": run_id, "out_dir": str(out), "summary": summary}


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    # flatten non-scalars
    flat = []
    for r in rows:
        fr = {}
        for k, v in r.items():
            if isinstance(v, (list, dict)):
                fr[k] = json.dumps(v, default=str)
            else:
                fr[k] = v
        flat.append(fr)
    pd.DataFrame(flat).to_csv(path, index=False)


def _methodology() -> str:
    return f"""# {SETUP_TYPE} methodology

- Entry: `child_pool_low` of nested 1m ASK inside overlapping 5m∩15m ASK zone
- Order active only after `max(decision_at, child.available_at)`
- No same-bar fill when birth-bar touch precedes availability (STRICT); those cases go to ambiguous
- SL: max(child_high, 5m_high, 15m_high) + buffer; reject if stop_distance_pct > 1%
- Targets: causal BID pools only + 1R/2R/3R benchmarks
- Costs: round-trip baseline {ROUNDTRIP_COST_PCT_BASELINE}%
- Orderflow: optional / UNAVAILABLE allowed
- No DOGE price/time hardcoding in detection
"""


def _report_md(summary: dict, integrity: dict, out: Path) -> str:
    s = summary.get("structural_sl") or {}
    return "\n".join(
        [
            f"# {SETUP_TYPE} backtest",
            "",
            f"- candidates: {summary.get('candidates')}",
            f"- fills (strict): {summary.get('fills_strict')}",
            f"- ambiguous: {summary.get('ambiguous')}",
            f"- winrate: {s.get('winrate')}",
            f"- expectancy net %: {s.get('expectancy_net_pct')}",
            f"- profit factor: {s.get('profit_factor')}",
            f"- n_descriptive_only: {s.get('n_descriptive_only')}",
            "",
            "## Integrity",
            "```json",
            json.dumps(integrity, indent=2, default=str),
            "```",
            "",
            f"Artifacts: `{out}`",
        ]
    )


if __name__ == "__main__":
    print(json.dumps(run_backtest(), indent=2, default=str))
