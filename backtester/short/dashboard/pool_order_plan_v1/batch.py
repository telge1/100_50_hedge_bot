"""Offline batch: one ClickHouse load and one pool-engine pass per symbol."""

from __future__ import annotations

import argparse
import resource
import sys
import time
import traceback
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from .candles import (
    FutureBarInFrame,
    LastFiveIncomplete,
    build_five_minute_series,
    causal_prefix,
    ensure_utc,
)
from .config import (
    FEE_PCT,
    LOOKBACK,
    REPLAY,
    STRATEGY_ID,
    WARMUP_DAYS,
    hold_minutes_for_tf,
    signal_generator_root,
)
from .coverage import coverage_row
from .dedupe import dedupe_signals
from .partial_exits import first_outcome_open, simulate_partial_exits
from .pin import aggregation_relevant_files, inspect_repo
from .planner_client import PlannerPinError, assert_planner_pin
from .pool_snapshot import causal_as_of, plan_from_snapshot, pool_engine_run_count, run_pools_once
from .schema import (
    REASON_ENTRY_AFTER,
    REASON_ENTRY_BEFORE,
    REASON_LAST_5M_INCOMPLETE,
    REASON_NO_CANDLES,
    REASON_PLANNER_ERROR,
    REASON_TZ,
    STATUS_NO_PLAN,
    STATUS_READY,
    TEST_FIXTURE_ONLY,
    clickhouse_candle_stamp,
    last_5m_close_from_open,
    pool_pipeline_stamp,
)
from .signals import clickhouse_source_public, load_candle_history_bounds, load_closed_1m, load_tier_a_signals
from .store import SourceRejected, abort_run, publish_latest, write_run
from .validity import classify_plan


class BatchAbort(RuntimeError):
    pass


def progress(msg: str) -> None:
    print(msg, flush=True)


def _iso(ts) -> str | None:
    if ts is None:
        return None
    return ensure_utc(ts).strftime("%Y-%m-%dT%H:%M:%SZ")


def _no_plan(signal: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "signal_id": signal.get("signal_id"),
        "symbol": signal.get("symbol"),
        "direction": signal.get("direction"),
        "timeframe": signal.get("timeframe"),
        "entry_time": signal.get("entry_time"),
        "entry_price": signal.get("entry_price"),
        "plan_status": STATUS_NO_PLAN,
        "no_plan_reason": reason,
        "strategy_id": STRATEGY_ID,
        "outcome": None,
        "gross_pnl_pct": None,
        "fees_pct": None,
        "net_pnl_pct": None,
    }


def _peak_rss_mb() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    rss = float(usage.ru_maxrss)
    if rss > 10**9:
        return rss / (1024.0 * 1024.0)
    return rss / 1024.0


def _signal_window(sigs: list[dict[str, Any]], *, outcome_as_of: datetime | None = None) -> tuple[datetime, datetime]:
    entries = [ensure_utc(s["entry_time"]) for s in sigs]
    earliest = min(entries)
    latest_end = earliest
    for sig in sigs:
        et = ensure_utc(sig["entry_time"])
        hold_end = first_outcome_open(et) + timedelta(minutes=hold_minutes_for_tf(sig.get("timeframe")))
        if hold_end > latest_end:
            latest_end = hold_end
        if et > latest_end:
            latest_end = et
    if outcome_as_of is not None:
        latest_end = min(latest_end, ensure_utc(outcome_as_of))
    candle_start = earliest - timedelta(days=WARMUP_DAYS)
    return candle_start, latest_end


def _fmt_px(v: Any) -> str:
    if v is None:
        return "n/a"
    return f"{float(v):.6g}"


def run_batch(
    *,
    symbols: list[str] | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int | None = None,
    publish: bool = False,
    signals: list[dict[str, Any]] | None = None,
    one_minute_by_symbol: dict[str, list[dict[str, Any]]] | None = None,
    skip_pin: bool = False,
    outcome_as_of: datetime | None = None,
) -> dict[str, Any]:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    t_all = time.perf_counter()
    timings: dict[str, Any] = {"symbols": {}, "total_s": None, "peak_rss_mb": None}
    pin = None if skip_pin else assert_planner_pin()
    sg_root = signal_generator_root()
    agg_pin = inspect_repo(sg_root, aggregation_relevant_files(sg_root))

    fixture_mode = one_minute_by_symbol is not None
    if fixture_mode and publish:
        raise BatchAbort("TEST_FIXTURE_ONLY batch cannot publish or update latest")

    raw = signals if signals is not None else load_tier_a_signals(start=start, end=end, symbols=symbols, limit=limit)
    split = dedupe_signals(raw)
    winners = split["winners"]
    ignored = split["ignored"]
    if not winners and not ignored:
        raise BatchAbort("no winner signals")

    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in winners:
        by_symbol[str(row["symbol"]).upper()].append(row)

    coverage_rows = []
    plans: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    preflight = {"symbols": {}, "subminute_entries": 0, "abort": None, "single_pass": True}

    if one_minute_by_symbol is None and not any(by_symbol):
        raise BatchAbort("no winner symbols")

    have_candles = False
    engine_runs_before = pool_engine_run_count()
    query_starts: list[datetime] = []
    query_ends: list[datetime] = []
    try:
        for symbol, sigs in sorted(by_symbol.items()):
            sigs.sort(key=lambda r: (ensure_utc(r["entry_time"]), str(r.get("signal_id") or "")))
            processed_order = [s["signal_id"] for s in sigs]
            preflight["symbols"].setdefault(symbol, {})["signal_order"] = processed_order
            n_sig = len(sigs)
            candle_start, candle_end = _signal_window(sigs, outcome_as_of=outcome_as_of)
            query_starts.append(candle_start)
            query_ends.append(candle_end)
            db_start = db_end = None
            if not fixture_mode:
                try:
                    db_start, db_end = load_candle_history_bounds(symbol)
                except Exception:  # noqa: BLE001
                    db_start, db_end = None, None
            preflight["symbols"][symbol]["query_start"] = _iso(candle_start)
            preflight["symbols"][symbol]["query_end"] = _iso(candle_end)
            preflight["symbols"][symbol]["warmup_start"] = _iso(candle_start)
            preflight["symbols"][symbol]["candle_end"] = _iso(candle_end)
            preflight["symbols"][symbol]["warmup_days"] = WARMUP_DAYS

            t0 = time.perf_counter()
            if fixture_mode:
                progress(f"[{symbol}] loading TEST_FIXTURE_ONLY candles")
                rows_1m = one_minute_by_symbol[symbol]
            else:
                progress(f"[{symbol}] loading ClickHouse candles")
                try:
                    rows_1m = load_closed_1m(symbol, start=candle_start, end=candle_end)
                except Exception as exc:  # noqa: BLE001
                    raise BatchAbort(
                        f"ClickHouse unavailable for {symbol}; CSV fallback is forbidden: {exc}"
                    ) from exc
            t_load = time.perf_counter() - t0

            progress(f"[{symbol}] aggregating 1m -> 5m")
            t1 = time.perf_counter()
            series = build_five_minute_series(symbol, rows_1m)
            t_agg = time.perf_counter() - t1
            cov = coverage_row(
                symbol,
                series,
                entry_count=len(sigs),
                query_start=candle_start,
                query_end=candle_end,
                database_history_start=db_start,
                database_history_end=db_end,
            )
            coverage_rows.append(cov)
            preflight["symbols"][symbol].update(cov)
            if series.one_minute_rows > 0:
                have_candles = True
            if cov["coverage_status"] == "DUPLICATES":
                raise BatchAbort(f"logical 1m duplicates for {symbol}")

            t2 = time.perf_counter()
            pools = []
            if series.one_minute_rows > 0 and not series.bars.empty:
                progress(f"[{symbol}] running pool engine once")
                pools = run_pools_once(series.bars)
            t_engine = time.perf_counter() - t2
            timings["symbols"][symbol] = {
                "clickhouse_load_s": t_load,
                "aggregate_1m_5m_s": t_agg,
                "pool_engine_s": t_engine,
                "pool_engine_runs": 1 if pools or series.one_minute_rows > 0 else 0,
                "signals": n_sig,
                "snapshot_plan_s": [],
                "outcome_s": [],
            }

            c1m = pd.DataFrame(rows_1m)
            if not c1m.empty:
                c1m = c1m.rename(columns={"open_time": "timestamp"})

            for idx, sig in enumerate(sigs, start=1):
                t_snap = time.perf_counter()
                try:
                    et = ensure_utc(sig["entry_time"])
                except Exception:
                    rec = _no_plan(sig, REASON_TZ)
                    outcomes.append(rec)
                    progress(f"[{symbol} {idx}/{n_sig}] NO_PLAN reason={REASON_TZ}")
                    continue
                if et.second or et.microsecond:
                    preflight["subminute_entries"] += 1
                if series.one_minute_rows <= 0:
                    outcomes.append(_no_plan(sig, REASON_NO_CANDLES))
                    progress(f"[{symbol} {idx}/{n_sig}] NO_PLAN reason={REASON_NO_CANDLES}")
                    continue
                hist_start = series.history_start
                hist_end = series.history_end
                if hist_start and et < hist_start:
                    outcomes.append(_no_plan(sig, REASON_ENTRY_BEFORE))
                    progress(f"[{symbol} {idx}/{n_sig}] NO_PLAN reason={REASON_ENTRY_BEFORE}")
                    continue
                if hist_end and et > hist_end + timedelta(minutes=1):
                    outcomes.append(_no_plan(sig, REASON_ENTRY_AFTER))
                    progress(f"[{symbol} {idx}/{n_sig}] NO_PLAN reason={REASON_ENTRY_AFTER}")
                    continue
                try:
                    prefix = causal_prefix(series, et)
                except LastFiveIncomplete:
                    outcomes.append(_no_plan(sig, REASON_LAST_5M_INCOMPLETE))
                    progress(f"[{symbol} {idx}/{n_sig}] NO_PLAN reason={REASON_LAST_5M_INCOMPLETE}")
                    continue
                except FutureBarInFrame as exc:
                    raise BatchAbort(str(exc)) from exc
                try:
                    plan = plan_from_snapshot(
                        pools,
                        symbol=symbol,
                        entry_time=et,
                        entry_price=float(sig["entry_price"]),
                        direction=str(sig["direction"]),
                        as_of=causal_as_of(prefix),
                        test_fixture_only=fixture_mode,
                    )
                except FutureBarInFrame as exc:
                    raise BatchAbort(str(exc)) from exc
                except Exception as exc:  # noqa: BLE001
                    rec = _no_plan(sig, REASON_PLANNER_ERROR)
                    rec["planner_error"] = str(exc)
                    rec["trace"] = traceback.format_exc(limit=3)
                    outcomes.append(rec)
                    progress(f"[{symbol} {idx}/{n_sig}] NO_PLAN reason={REASON_PLANNER_ERROR}")
                    continue
                timings["symbols"][symbol]["snapshot_plan_s"].append(time.perf_counter() - t_snap)
                judged = classify_plan(plan, replay=REPLAY)
                sl = plan.get("SL") or {}
                tp1 = plan.get("TP1") or {}
                tp2 = plan.get("TP2") or {}
                slim = {
                    "signal_id": sig["signal_id"],
                    "symbol": symbol,
                    "direction": sig["direction"],
                    "timeframe": sig.get("timeframe"),
                    "entry_time": _iso(et),
                    "entry_price": sig["entry_price"],
                    "plan_status": judged["status"],
                    "no_plan_reason": judged.get("reason"),
                    "initial_target_mode": plan.get("INITIAL_TARGET_MODE"),
                    "sl_price": sl.get("SL_PRICE"),
                    "sl_distance_pct": sl.get("SL_DISTANCE_PCT"),
                    "sl_too_wide": sl.get("SL_TOO_WIDE"),
                    "sl_cluster": sl.get("SL_CLUSTER"),
                    "tp1_price": tp1.get("TP1_PRICE"),
                    "tp1_size": tp1.get("TP1_SIZE"),
                    "tp1_cluster": tp1.get("TP1_CLUSTER"),
                    "tp2_price": tp2.get("TP2_PRICE"),
                    "tp2_size": tp2.get("TP2_SIZE"),
                    "tp2_cluster": tp2.get("TP2_CLUSTER"),
                    "tp2_skip_reason": plan.get("tp2_skip_reason"),
                    "primary_decision": plan.get("PRIMARY_DECISION"),
                    "causal_5m_bars": int(len(prefix)),
                    "last_5m_open": str(prefix.iloc[-1]["timestamp"]) if not prefix.empty else None,
                    "last_5m_close": str(prefix.iloc[-1]["close_time"]) if not prefix.empty else None,
                    "last_5m_close_derived": False,
                    "snapshot_as_of": str(causal_as_of(prefix)),
                    "signal_timeframe": sig.get("timeframe"),
                    "pool_timeframe": "5m",
                    "chart_timeframe": "separate",
                    "strategy_id": STRATEGY_ID,
                    "entry_pool_count": plan.get("active_pool_count"),
                }
                slim.update(pool_pipeline_stamp())
                if slim.get("last_5m_close") is None and slim.get("last_5m_open"):
                    slim["last_5m_close"] = str(last_5m_close_from_open(slim["last_5m_open"]))
                    slim["last_5m_close_derived"] = True
                if fixture_mode:
                    slim["pool_candle_source"] = TEST_FIXTURE_ONLY
                    slim["test_fixture_only"] = True
                else:
                    slim.update(clickhouse_candle_stamp())
                plans.append(slim)
                if judged["status"] == STATUS_READY:
                    progress(
                        f"[{symbol} {idx}/{n_sig}] READY SL={_fmt_px(sl.get('SL_PRICE'))} "
                        f"TP1={_fmt_px(tp1.get('TP1_PRICE'))} TP2={_fmt_px(tp2.get('TP2_PRICE'))}"
                    )
                else:
                    progress(f"[{symbol} {idx}/{n_sig}] NO_PLAN reason={judged.get('reason')}")
                if not judged["ready"]:
                    rec = dict(slim)
                    rec["outcome"] = None
                    rec["gross_pnl_pct"] = None
                    rec["fees_pct"] = None
                    rec["net_pnl_pct"] = None
                    outcomes.append(rec)
                    continue
                t_out = time.perf_counter()
                sim = simulate_partial_exits(
                    direction=str(sig["direction"]),
                    entry_time=et,
                    entry_price=float(sig["entry_price"]),
                    sl_price=float(sl["SL_PRICE"]),
                    tp1_price=float(tp1["TP1_PRICE"]),
                    tp1_size=float(tp1["TP1_SIZE"]),
                    tp2_price=tp2.get("TP2_PRICE"),
                    tp2_size=tp2.get("TP2_SIZE"),
                    candles_1m=c1m,
                    timeframe=sig.get("timeframe"),
                    fee_pct=FEE_PCT,
                    as_of=outcome_as_of,
                )
                timings["symbols"][symbol]["outcome_s"].append(time.perf_counter() - t_out)
                rec = dict(slim)
                rec.update(sim)
                outcomes.append(rec)
    except KeyboardInterrupt:
        abort_run(run_id, reason="KeyboardInterrupt")
        raise BatchAbort("interrupted; latest not updated") from None

    if not have_candles and one_minute_by_symbol is None:
        raise BatchAbort("no winner symbol possesses candles")

    engine_runs = pool_engine_run_count() - engine_runs_before
    counts = {
        "raw_signals": len(raw),
        "winners": len(winners),
        "duplicates": len(ignored),
        "ready": sum(1 for r in outcomes if r.get("plan_status") == STATUS_READY),
        "no_plan": sum(1 for r in outcomes if r.get("plan_status") == STATUS_NO_PLAN),
        "open": sum(1 for r in outcomes if r.get("outcome") == "OPEN"),
        "closed": sum(1 for r in outcomes if r.get("outcome") not in (None, "OPEN")),
        "pool_engine_runs": engine_runs,
        "symbols": len(by_symbol),
    }
    timings["total_s"] = time.perf_counter() - t_all
    timings["peak_rss_mb"] = _peak_rss_mb()
    win_start = min(query_starts) if query_starts else start
    win_end = max(query_ends) if query_ends else end
    if win_start is None and winners:
        win_start = min(ensure_utc(s["entry_time"]) for s in winners) - timedelta(days=WARMUP_DAYS)
    if win_end is None and winners:
        win_end = max(ensure_utc(s["entry_time"]) for s in winners)
    manifest = {
        "strategy_id": STRATEGY_ID,
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "signal_source": "signal_generator.signals FINAL tier_a=1",
        "clickhouse": clickhouse_source_public(),
        "window": {
            "start": _iso(win_start),
            "end": _iso(win_end),
            "symbols": symbols or sorted(by_symbol.keys()),
            "outcome_as_of": _iso(outcome_as_of),
        },
        "planner": pin,
        "signal_generator": agg_pin,
        "fee_pct": FEE_PCT,
        "lookback": LOOKBACK,
        "replay": REPLAY,
        "counts": counts,
        "timings": timings,
        "single_pass": True,
        "warmup_days": WARMUP_DAYS,
        "pool_engine_runs_per_symbol": 1,
        "pin_override": bool(pin and pin.get("pin_override")),
        "productive": bool(pin and pin.get("pin_ok") and not pin.get("pin_override")),
        "test_fixture_only": fixture_mode,
        "pool_candle_source": TEST_FIXTURE_ONLY if fixture_mode else "clickhouse",
    }
    manifest.update(pool_pipeline_stamp())
    if fixture_mode:
        manifest["TEST_FIXTURE_ONLY"] = True
    else:
        manifest.update(clickhouse_candle_stamp())
    preflight["counts"] = counts
    preflight["timings"] = timings
    coverage = {"symbols": coverage_rows}
    run_dir = write_run(
        run_id,
        manifest=manifest,
        preflight=preflight,
        coverage=coverage,
        plans=plans,
        outcomes=outcomes,
        ignored=ignored,
    )
    if publish:
        try:
            publish_latest(run_dir)
        except SourceRejected as exc:
            raise BatchAbort(str(exc)) from exc
    return {"run_id": run_id, "run_dir": str(run_dir), "manifest": manifest, "published": publish}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="POOL_ORDER_PLAN_V1 offline batch")
    parser.add_argument("--symbol", action="append", dest="symbols")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--skip-pin", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run_batch(symbols=args.symbols, limit=args.limit, publish=args.publish, skip_pin=args.skip_pin)
    except (BatchAbort, PlannerPinError) as exc:
        print(f"BATCH_ABORT: {exc}", file=sys.stderr, flush=True)
        return 2
    print(result["run_dir"], flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
