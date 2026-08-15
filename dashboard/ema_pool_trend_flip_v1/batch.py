"""Offline ACE frozen batch. ClickHouse read-only. One pool-engine pass per symbol."""

from __future__ import annotations

import argparse
import json
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from pool_order_plan_v1.candles import build_five_minute_series, causal_prefix, ensure_utc
from pool_order_plan_v1.dedupe import dedupe_signals
from pool_order_plan_v1.pin import aggregation_relevant_files, inspect_repo
from pool_order_plan_v1.planner_client import assert_planner_pin
from pool_order_plan_v1.pool_snapshot import pool_engine_run_count, reset_pool_engine_run_count, run_pools_once, snapshot_pools
from pool_order_plan_v1.signals import clickhouse_source_public, load_closed_1m, load_tier_a_signals

from .config import (
    ACE_FROZEN_END,
    ACE_FROZEN_START,
    ATR_AUDIT_LEVELS,
    EMA_CROSS_CONFIRMATION_BARS,
    EMA_CROSS_MIN_SEPARATION_ATR,
    EMA_FAST,
    EMA_SLOW,
    EXPECTED_PLANNER_COMMIT,
    FEE_PCT,
    FILTER_STRATEGY_ID,
    LOOKBACK,
    RATCHET_VARIANT,
    STATIC_VARIANT,
    STRATEGY_ID,
    WARMUP_DAYS,
    artifacts_dir,
    expected_planner_commit,
    signal_generator_root,
)
from .decision import decide, filter_variant_decision
from .ema_regime import confirmed_strong_crosses, indicators_for_bars, regime_at_index
from .episodes import episode_ids, stochastic_k
from .pool_bias import pool_context
from .protection import select_thin_protection, sl_from_cluster
from .schema import DECISION_ALIGNED, DECISION_FLIPPED, DECISION_NO_TRADE, REASON_EPISODE, clickhouse_candle_stamp
from .simulate import simulate_baseline, simulate_path
from .store import abort_run, publish_latest, write_run
from .tf_bars import aggregate_signal_tf, causal_tf_prefix


def _iso(ts) -> str | None:
    if ts is None:
        return None
    return ensure_utc(ts).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts: str) -> datetime:
    return ensure_utc(ts)


def _stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    trades = [r for r in rows if r.get("decision") in (DECISION_FLIPPED, DECISION_ALIGNED) and r.get("outcome")]
    closed = [r for r in trades if r.get("outcome") not in (None, "OPEN")]
    wins = [r for r in closed if r.get("gross_pnl_pct") is not None and float(r["gross_pnl_pct"]) > 0]
    losses = [r for r in closed if r.get("gross_pnl_pct") is not None and float(r["gross_pnl_pct"]) < 0]
    gp = sum(float(r["gross_pnl_pct"]) for r in wins)
    gl = sum(float(r["gross_pnl_pct"]) for r in losses)
    fees = sum(float(r["fees_pct"] or 0) for r in closed)
    net = sum(float(r["net_pnl_pct"] or 0) for r in closed)
    abs_l = abs(gl) if gl else 0.0
    pf = (gp / abs_l) if abs_l else None
    eq = 0.0
    peak = 0.0
    mdd = 0.0
    ordered = sorted(closed, key=lambda r: str(r.get("entry_time") or ""))
    for r in ordered:
        eq += float(r.get("net_pnl_pct") or 0)
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
    holds = []
    for r in closed:
        if r.get("entry_time") and r.get("exit_time"):
            holds.append((ensure_utc(r["exit_time"]) - ensure_utc(r["entry_time"])).total_seconds() / 60.0)
    return {
        "trades": len(trades),
        "closed": len(closed),
        "open": sum(1 for r in trades if r.get("outcome") == "OPEN"),
        "flipped": sum(1 for r in rows if r.get("decision") == DECISION_FLIPPED),
        "aligned": sum(1 for r in rows if r.get("decision") == DECISION_ALIGNED),
        "no_trade": sum(1 for r in rows if r.get("decision") == DECISION_NO_TRADE),
        "blocked": sum(1 for r in rows if r.get("decision") == "BLOCKED"),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": (100.0 * len(wins) / len(closed)) if closed else None,
        "gross_pnl_pct": gp + gl,
        "fees_pct": fees,
        "net_pnl_pct": net,
        "profit_factor": pf,
        "max_drawdown_pct": mdd,
        "avg_win_pct": (sum(float(r["gross_pnl_pct"]) for r in wins) / len(wins)) if wins else None,
        "max_win_pct": max((float(r["gross_pnl_pct"]) for r in wins), default=None),
        "avg_loss_pct": (sum(float(r["gross_pnl_pct"]) for r in losses) / len(losses)) if losses else None,
        "max_loss_pct": min((float(r["gross_pnl_pct"]) for r in losses), default=None),
        "avg_hold_minutes": (sum(holds) / len(holds)) if holds else None,
        "exit_sl": sum(1 for r in closed if r.get("outcome") == "SL"),
        "exit_ema_cross": sum(1 for r in closed if r.get("outcome") == "EMA_CROSS"),
        "sl_too_wide": sum(1 for r in trades if r.get("sl_too_wide")),
    }


def run_batch(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    publish: bool = True,
    min_sep: float = EMA_CROSS_MIN_SEPARATION_ATR,
) -> Path:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    pin = assert_planner_pin()
    sg = inspect_repo(signal_generator_root(), aggregation_relevant_files(signal_generator_root()))
    ch = clickhouse_source_public()
    raw = load_tier_a_signals(start=start, end=end - timedelta(milliseconds=1), symbols=[symbol])
    raw = [s for s in raw if start <= ensure_utc(s["entry_time"]) < end]
    raw_n = len(raw)
    deduped = dedupe_signals(raw)
    winners = deduped["winners"]
    ignored = deduped["ignored"]
    if not winners:
        abort_run(run_id, "no_signals")
        raise RuntimeError("no signals")

    candle_start = start - timedelta(days=WARMUP_DAYS)
    m1_rows = load_closed_1m(symbol, start=candle_start, end=end)
    m1_df = pd.DataFrame(m1_rows)
    if m1_df.empty:
        abort_run(run_id, "no_candles")
        raise RuntimeError("no candles")
    series = build_five_minute_series(symbol, m1_rows)
    reset_pool_engine_run_count()
    all_pools = run_pools_once(series.bars)
    engine_runs = pool_engine_run_count()
    if engine_runs != 1:
        abort_run(run_id, "pool_engine_not_once")
        raise RuntimeError("pool engine must run once per symbol")

    tf_cache: dict[str, pd.DataFrame] = {}
    episode_used: set[tuple] = set()
    trades: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    audit_rows: dict[float, list] = {lvl: [] for lvl in ATR_AUDIT_LEVELS}

    for sig in sorted(winners, key=lambda s: (_iso(s["entry_time"]), str(s.get("signal_id")))):
        et = ensure_utc(sig["entry_time"])
        tf = str(sig.get("timeframe") or "15m")
        if tf not in tf_cache:
            tf_cache[tf] = aggregate_signal_tf(m1_rows, tf)
        prefix = causal_tf_prefix(tf_cache[tf], et)
        if prefix is None or prefix.empty or len(prefix) < 30:
            blocked.append({**sig, "decision": DECISION_NO_TRADE, "no_trade_reason": "TREND_CONTEXT_NOT_CONFIRMED"})
            continue
        closes = prefix["close"].tolist()
        highs = prefix["high"].tolist()
        lows = prefix["low"].tolist()
        inds = indicators_for_bars(closes, highs, lows)
        events = confirmed_strong_crosses(inds, min_sep=min_sep)
        idx = len(inds) - 1
        regime = regime_at_index(inds, events, idx)
        try:
            five_prefix = causal_prefix(series, et)
        except Exception:
            blocked.append({**sig, "decision": DECISION_NO_TRADE, "no_trade_reason": "TREND_CONTEXT_NOT_CONFIRMED"})
            continue
        as_of = five_prefix.iloc[-1]["close_time"]
        snap = snapshot_pools(all_pools, as_of)
        ctx = pool_context(snap, float(sig["entry_price"]))
        k = stochastic_k(highs, lows, closes)
        orig = str(sig["direction"]).upper()
        ep = episode_ids(k, for_short=(orig == "SHORT"))
        episode_id = ep[-1] if ep else None
        d0 = decide(
            original_direction=orig,
            unique_up=bool(regime["unique_up"]),
            unique_down=bool(regime["unique_down"]),
            bullish_pool=bool(ctx["bullish_pool_context"]),
            bearish_pool=bool(ctx["bearish_pool_context"]),
            protection={"tmp": True},
        )
        intended = d0.get("executed_direction") or d0.get("intended_direction")
        prot = None
        sl_info = None
        if intended:
            prot = select_thin_protection(ctx["clusters"], entry=float(sig["entry_price"]), executed_direction=intended)
            if prot is not None:
                sl_info = sl_from_cluster(prot, executed_direction=intended, entry=float(sig["entry_price"]))
        decision = decide(
            original_direction=orig,
            unique_up=bool(regime["unique_up"]),
            unique_down=bool(regime["unique_down"]),
            bullish_pool=bool(ctx["bullish_pool_context"]),
            bearish_pool=bool(ctx["bearish_pool_context"]),
            protection=prot if intended else None,
        )
        base_row = {
            "signal_id": sig.get("signal_id"),
            "symbol": symbol,
            "signal_time": _iso(sig.get("available_at")),
            "entry_time": _iso(et),
            "available_at": _iso(sig.get("available_at")),
            "entry_price": sig["entry_price"],
            "signal_timeframe": tf,
            "original_direction": orig,
            "executed_direction": decision.get("executed_direction"),
            "decision": decision["decision"],
            "entry_reason": decision.get("entry_reason"),
            "no_trade_reason": decision.get("no_trade_reason"),
            "ema9": regime.get("ema9"),
            "ema20": regime.get("ema20"),
            "ema_sep_atr": regime.get("sep_atr"),
            "ema_trend": regime.get("ema_trend"),
            "last_confirmed_cross": regime.get("last_confirmed_cross"),
            "pool_timeframe": "5m",
            "upper_pool_bias_score": ctx["upper_pool_bias_score"],
            "lower_pool_bias_score": ctx["lower_pool_bias_score"],
            "upper_pool_count": ctx["upper_pool_count"],
            "lower_pool_count": ctx["lower_pool_count"],
            "upper_pool_strength_sum": ctx["upper_pool_strength_sum"],
            "lower_pool_strength_sum": ctx["lower_pool_strength_sum"],
            "nearest_upper_pool_distance_pct": ctx["nearest_upper_pool_distance_pct"],
            "nearest_lower_pool_distance_pct": ctx["nearest_lower_pool_distance_pct"],
            "protection_pool": prot,
            "sl_price": None if sl_info is None else sl_info["sl_price"],
            "sl_distance_pct": None if sl_info is None else sl_info["sl_distance_pct"],
            "sl_too_wide": False if sl_info is None else sl_info["sl_too_wide"],
            "sl_cluster": None if sl_info is None else sl_info["sl_cluster"],
            "tp1_disabled": True,
            "tp2_disabled": True,
            "stochastic_episode_id": episode_id,
            "weak_cross_candidates": [],
            "confirmed_cross_events": [e for e in events if e["index"] == idx or e["index"] == idx - 1],
            "active_upper_pools": [c for c in ctx["clusters"] if c.get("side") == "UPPER" and float(c["bottom"]) > float(sig["entry_price"])],
            "active_lower_pools": [c for c in ctx["clusters"] if c.get("side") == "LOWER" and float(c["top"]) < float(sig["entry_price"])],
            "snapshot_as_of": _iso(as_of),
            "strategy_id": STRATEGY_ID,
        }
        if decision["decision"] == DECISION_NO_TRADE:
            blocked.append(base_row)
            continue
        key = (symbol, tf, decision["executed_direction"], episode_id)
        if episode_id is not None and key in episode_used:
            blocked.append({**base_row, "decision": DECISION_NO_TRADE, "no_trade_reason": REASON_EPISODE})
            continue
        if episode_id is not None:
            episode_used.add(key)

        ema_exit = (
            "CONFIRMED_STRONG_BEARISH_EMA_CROSS"
            if decision["executed_direction"] == "LONG"
            else "CONFIRMED_STRONG_BULLISH_EMA_CROSS"
        )
        for variant in (STATIC_VARIANT, RATCHET_VARIANT):
            sim = simulate_path(
                executed_direction=decision["executed_direction"],
                entry_time=et,
                entry_price=float(sig["entry_price"]),
                initial_sl=float(sl_info["sl_price"]),
                one_minute=m1_df,
                five_minute=series.bars,
                all_pools=all_pools,
                signal_tf_bars=tf_cache[tf],
                variant=variant,
                window_end=end,
                ema_exit_kind=ema_exit,
            )
            hold = None
            if sim.get("exit_time"):
                hold = (ensure_utc(sim["exit_time"]) - et).total_seconds() / 60.0
            row = {**base_row, **sim, "variant": variant, "hold_minutes": hold}
            trades.append(row)

        bl = simulate_baseline(
            direction=orig,
            entry_time=et,
            entry_price=float(sig["entry_price"]),
            sl=sig.get("baseline_sl"),
            tp=sig.get("baseline_tp"),
            one_minute=m1_df,
            window_end=end,
        )
        trades.append(
            {
                **base_row,
                **bl,
                "variant": "BASELINE",
                "executed_direction": orig,
                "decision": DECISION_ALIGNED,
                "strategy_id": "wave_fade_no_be50_v1",
            }
        )
        filt = filter_variant_decision({**base_row, "variant": FILTER_STRATEGY_ID})
        if filt.get("decision") in (DECISION_ALIGNED,):
            simf = simulate_path(
                executed_direction=filt["executed_direction"],
                entry_time=et,
                entry_price=float(sig["entry_price"]),
                initial_sl=float(sl_info["sl_price"]),
                one_minute=m1_df,
                five_minute=series.bars,
                all_pools=all_pools,
                signal_tf_bars=tf_cache[tf],
                variant=STATIC_VARIANT,
                window_end=end,
                ema_exit_kind=ema_exit,
            )
            filt = {**filt, **simf, "variant": FILTER_STRATEGY_ID}
        trades.append(filt)

    static_rows = [r for r in trades if r.get("variant") == STATIC_VARIANT]
    ratchet_rows = [r for r in trades if r.get("variant") == RATCHET_VARIANT]
    baseline_rows = [r for r in trades if r.get("variant") == "BASELINE"]
    filter_rows = [r for r in trades if r.get("variant") == FILTER_STRATEGY_ID]
    flipped_static = [r for r in static_rows if r.get("decision") == DECISION_FLIPPED]
    flip_vs_base = []
    by_sid = {str(r.get("signal_id")): r for r in baseline_rows}
    for r in flipped_static:
        b = by_sid.get(str(r.get("signal_id")))
        if b and r.get("net_pnl_pct") is not None and b.get("net_pnl_pct") is not None:
            flip_vs_base.append(
                {
                    "signal_id": r.get("signal_id"),
                    "flip_net": r.get("net_pnl_pct"),
                    "baseline_net": b.get("net_pnl_pct"),
                    "delta_net": float(r["net_pnl_pct"]) - float(b["net_pnl_pct"]),
                }
            )

    summary = {
        "raw_signals": raw_n,
        "deduped_signals": len(winners),
        "ignored_duplicates": len(ignored),
        "pool_engine_runs": engine_runs,
        "STATIC": _stats(static_rows),
        "RATCHET": _stats(ratchet_rows),
        "BASELINE": _stats(baseline_rows),
        "DIRECTION_FILTER": _stats(filter_rows),
        "flipped_vs_baseline": {
            "n": len(flip_vs_base),
            "sum_delta_net": sum(x["delta_net"] for x in flip_vs_base),
            "rows": flip_vs_base,
        },
        "by_symbol": {symbol: _stats(static_rows)},
        "ema_cross_min_separation_atr_primary": min_sep,
        "atr_audit_note": list(ATR_AUDIT_LEVELS),
    }
    manifest = {
        "run_id": run_id,
        "strategy_id": STRATEGY_ID,
        "complete": True,
        "productive": True,
        "research_only": True,
        "live_trading": False,
        "test_fixture_only": False,
        "pool_candle_source": "clickhouse",
        "clickhouse": {**clickhouse_candle_stamp(), **ch},
        "window": {"start": _iso(start), "end": _iso(end), "symbols": [symbol], "end_exclusive": True},
        "ema": {
            "fast": EMA_FAST,
            "slow": EMA_SLOW,
            "cross_confirmation_bars": EMA_CROSS_CONFIRMATION_BARS,
            "cross_min_separation_atr": min_sep,
        },
        "pool": {"lookback": LOOKBACK, "interval": "5m", "engine_runs": engine_runs},
        "planner": pin,
        "fees_pct": FEE_PCT,
        "signal_generator": sg,
        "variants": [STATIC_VARIANT, RATCHET_VARIANT, FILTER_STRATEGY_ID, "BASELINE"],
        "tp1": "disabled",
        "tp2": "disabled",
    }
    preflight = {"planner_pin_ok": pin.get("pin_ok"), "clickhouse": ch, "raw_signals": raw_n}
    coverage = {
        "symbol": symbol,
        "one_minute_rows": len(m1_rows),
        "five_minute_bars": int(len(series.bars)),
        "signals_raw": raw_n,
        "signals_deduped": len(winners),
    }
    root = write_run(
        run_id,
        manifest=manifest,
        preflight=preflight,
        coverage=coverage,
        trades=trades,
        blocked=blocked,
        ignored=ignored,
        summary=summary,
    )
    if publish:
        publish_latest(root, manifest)
        _write_registry(root, manifest, symbol, start, end)
    print(json.dumps({"run_dir": str(root), "summary": summary}, default=str, indent=2))
    return root


def _write_registry(root, manifest, symbol, start, end) -> None:
    from pathlib import Path

    from .config import DASHBOARD_ROOT, REPO_ROOT

    rel = str(root.resolve().relative_to(REPO_ROOT.resolve()))
    payload = {
        "strategy_id": STRATEGY_ID,
        "research_only": True,
        "live_trading": False,
        "research_run_id": manifest["run_id"],
        "artifact_relpath": rel,
        "symbol": symbol,
        "window_start": _iso(start),
        "window_end": _iso(end),
        "planner_version": expected_planner_commit(),
        "pool_interval": "5m",
    }
    path = DASHBOARD_ROOT / "ema_pool_trend_flip_v1" / "research_registry.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frozen-ace", action="store_true")
    parser.add_argument("--symbol", default="ACEUSDT")
    parser.add_argument("--no-publish", action="store_true")
    args = parser.parse_args(argv)
    if args.frozen_ace:
        start = _parse(ACE_FROZEN_START)
        end = _parse(ACE_FROZEN_END)
        run_batch(symbol="ACEUSDT", start=start, end=end, publish=not args.no_publish)
        return 0
    raise SystemExit("use --frozen-ace")


if __name__ == "__main__":
    raise SystemExit(main())
