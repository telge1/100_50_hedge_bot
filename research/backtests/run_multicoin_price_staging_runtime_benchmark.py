"""Runtime benchmark for multicoin price-staging runner (research-only).

Times APTUSDT / BTCUSDT / NEARUSDT × 4 profiles without changing audit semantics.
Does not write into protected result dirs.
"""

from __future__ import annotations

import cProfile
import csv
import io
import json
import pstats
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.backtests.candle_loader import load_candles_for_symbol
from research.backtests.historical_backtest import normalize_candles
from research.backtests.multicoin_blocker_price_staging import (
    analyze_blocker_run,
    assert_output_dir_safe,
    run_isolated_blocker,
)
from research.backtests.recovery_reentry_policy import load_baseline_blockers
from research.backtests.run_inventory_mtm_neg1_policy_audit import (
    BASELINE_DIR,
    FULL_HISTORY_CANDLE_LIMIT,
)
from research.backtests.second_leg_price_staging import resolve_profile

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "research/backtests/results/multicoin_price_staging_runtime_benchmark_20260721"
BENCH_COINS = ("APTUSDT", "BTCUSDT", "NEARUSDT")
PROFILES = ("legacy", "linear4", "conservative3", "small_early4")


def _count_stage_intents(result: Any) -> int:
    n = 0
    for intent in result.intent_log or []:
        meta = dict(intent.get("metadata_excerpt") or {})
        if meta.get("research_price_staging") or meta.get("is_staged_second_leg_tp"):
            n += 1
    return n


def _bench_one(
    *,
    coin: str,
    candles: list[Any],
    start_index: int,
    trade_number: int,
    profile: str,
    baseline_row: dict[str, Any],
    with_cprofile: bool = False,
) -> dict[str, Any]:
    cfg = resolve_profile(profile)
    t0 = time.perf_counter()
    load_t0 = time.perf_counter()
    # candles already loaded — measure only backtest
    preload_note_ms = (time.perf_counter() - load_t0) * 1000.0

    def _run():
        return run_isolated_blocker(
            coin=coin,
            candles=candles,
            start_index=start_index,
            staging_config=cfg,
            trade_number=trade_number,
        )

    if with_cprofile:
        pr = cProfile.Profile()
        pr.enable()
        result = _run()
        pr.disable()
        buf = io.StringIO()
        stats = pstats.Stats(pr, stream=buf).sort_stats("cumulative")
        stats.print_stats(25)
        profile_text = buf.getvalue()
    else:
        result = _run()
        profile_text = ""

    backtest_s = time.perf_counter() - t0

    t1 = time.perf_counter()
    row = analyze_blocker_run(
        coin=coin,
        trade_number=trade_number,
        start_index=start_index,
        profile=profile,
        result=result,
        candles=candles,
        baseline_row=baseline_row,
    )
    analyze_s = time.perf_counter() - t1

    window_len = len(candles) - int(start_index)
    return {
        "coin": coin,
        "profile": profile,
        "trade_number": trade_number,
        "start_index": start_index,
        "candle_series_len": len(candles),
        "window_len_from_start": window_len,
        "candles_processed": int(result.candles_processed or 0),
        "backtest_seconds": round(backtest_s, 3),
        "analyze_seconds": round(analyze_s, 3),
        "total_seconds": round(backtest_s + analyze_s, 3),
        "candles_per_second": round(
            (float(result.candles_processed or 0) / backtest_s) if backtest_s > 0 else 0.0,
            1,
        ),
        "fills_count": int(result.fills_count or len(result.fill_log or [])),
        "orders_submitted": int(result.orders_submitted or 0),
        "order_log_events": len(result.order_log or []),
        "intent_log_events": len(result.intent_log or []),
        "stage_intents": _count_stage_intents(result),
        "final_status": result.final_status,
        "exit_reason": result.exit_reason,
        "trade_flat": row.get("trade_flat"),
        "final_mtm": row.get("final_mtm"),
        "max_cycle": row.get("max_cycle"),
        "staging_activated": row.get("staging_activated"),
        "preload_note_ms": preload_note_ms,
        "is_full_window_open_blocker": bool(
            result.final_status == "open"
            and int(result.candles_processed or 0) >= max(window_len - 2, 0)
        ),
        "cprofile_top": profile_text,
    }


def main() -> int:
    assert_output_dir_safe(OUT)
    OUT.mkdir(parents=True, exist_ok=True)

    blockers = {
        str(r["coin"]).upper(): r
        for r in load_baseline_blockers(BASELINE_DIR / "blocker_trades.csv")
    }
    rows: list[dict[str, Any]] = []
    cprofile_dump: dict[str, str] = {}

    print("[bench] loading candles once per coin...", flush=True)
    t_load = time.perf_counter()
    coin_candles: dict[str, list[Any]] = {}
    load_rows: list[dict[str, Any]] = []
    for coin in BENCH_COINS:
        t0 = time.perf_counter()
        raw = load_candles_for_symbol(coin, limit=FULL_HISTORY_CANDLE_LIMIT)
        candles = normalize_candles(coin, raw)
        elapsed = time.perf_counter() - t0
        coin_candles[coin] = candles
        load_rows.append(
            {
                "coin": coin,
                "load_normalize_seconds": round(elapsed, 3),
                "candle_count": len(candles),
            }
        )
        print(f"  loaded {coin}: {len(candles)} candles in {elapsed:.2f}s", flush=True)
    total_load = time.perf_counter() - t_load

    for coin in BENCH_COINS:
        b = blockers[coin]
        start_index = int(b["start_index"])
        trade_number = int(b["trade_number"])
        candles = coin_candles[coin]
        for i, profile in enumerate(PROFILES):
            # cProfile once on the slowest expected case: BTC legacy (full 50k open)
            use_prof = coin == "BTCUSDT" and profile == "legacy"
            print(f"[bench] {coin} {profile} ...", flush=True)
            row = _bench_one(
                coin=coin,
                candles=candles,
                start_index=start_index,
                trade_number=trade_number,
                profile=profile,
                baseline_row=b,
                with_cprofile=use_prof,
            )
            if use_prof:
                cprofile_dump[f"{coin}:{profile}"] = row.pop("cprofile_top")
            else:
                row.pop("cprofile_top", None)
            rows.append(row)
            print(
                f"  -> {row['backtest_seconds']:.1f}s  candles={row['candles_processed']}/"
                f"{row['window_len_from_start']}  cps={row['candles_per_second']}  "
                f"orders={row['orders_submitted']} intents={row['intent_log_events']}  "
                f"status={row['final_status']}",
                flush=True,
            )

    # Extrapolations
    by_coin = {c: [r for r in rows if r["coin"] == c] for c in BENCH_COINS}
    mean_per_profile = sum(r["total_seconds"] for r in rows) / max(len(rows), 1)
    # 27 coins × 4 profiles; APT gate adds 4 extra APT runs unless reused
    est_naive_27 = mean_per_profile * 27 * 4
    # If open blockers dominate: weight by remaining window
    blockers_all = list(load_baseline_blockers(BASELINE_DIR / "blocker_trades.csv"))
    # Estimate using BTC cps on each coin's window length (open-blocker worst case)
    btc_legacy = next(r for r in rows if r["coin"] == "BTCUSDT" and r["profile"] == "legacy")
    cps = float(btc_legacy["candles_per_second"] or 1.0)
    est_by_window = 0.0
    for b in blockers_all:
        window = FULL_HISTORY_CANDLE_LIMIT - int(b["start_index"])
        # 4 profiles each process ~window candles if stays open
        est_by_window += 4 * (window / cps)
    # APT gate duplicate: currently 4 APT runs then reused — OK if reused
    apt_gate_extra = 0.0  # reused in runner

    # Preferred optimizations estimate:
    # 1) early-stop / max_candles cap not in scope without semantics change — note only
    # 2) candle load once: already done in runner (~minutes saved)
    # 3) skip re-running APT in matrix: already done
    # 4) if we could stop at "blocker confirmed" earlier — semantics change
    # Realistic: cps improvement via less logging ~10-30%; main win is recognizing
    # open trades process full remainder — document that 27×4×~50k is inherent
    # unless max_candles / early abort is added.

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "candle_load_seconds_total": round(total_load, 3),
        "candle_loads": load_rows,
        "runs": rows,
        "mean_seconds_per_coin_profile": round(mean_per_profile, 3),
        "sum_benchmark_seconds": round(sum(r["total_seconds"] for r in rows), 3),
        "extrapolation": {
            "naive_mean_x_27_x_4_seconds": round(est_naive_27, 1),
            "naive_mean_x_27_x_4_minutes": round(est_naive_27 / 60.0, 1),
            "window_weighted_open_blocker_seconds": round(est_by_window, 1),
            "window_weighted_open_blocker_minutes": round(est_by_window / 60.0, 1),
            "apt_gate_extra_seconds_if_not_reused": round(apt_gate_extra, 1),
            "btc_legacy_cps": cps,
        },
        "answers": {
            "1_runtime_per_coin_profile": "see runs[].total_seconds",
            "2_candles_processed": "see runs[].candles_processed vs window_len_from_start",
            "3_trades_orders_intents": "single isolated trade; orders_submitted / intent_log_events / stage_intents",
            "4_candles_reloaded_per_profile": False,
            "5_continuous_chain_or_isolated": "isolated single-trade window candles[start:]; NOT continuous multi-trade. Open blockers still process nearly ALL remaining candles until series_end.",
            "6_largest_time_share": "run_historical_backtest candle loop (process_candle) for open/full-window trades",
            "7_apt_gate_double_count": "gate runs 4 APT profiles once; matrix reuses those rows (no second APT backtests)",
            "8_fork_shared_state": "not implemented; each profile cold-starts a new simulator from start_index",
            "9_checkpoint_resume": "not implemented",
            "10_pathological_orders": "see order_log_events; BTC open-blocker has many rebuild events across ~50k candles",
        },
        "optimization_estimate": {
            "already_done": [
                "candles loaded once per coin in runner",
                "APT gate results reused in matrix",
            ],
            "high_impact_without_semantics_change": [
                "checkpoint/--resume to avoid redoing finished coins after interrupt",
                "defer heavy analyze/bounce scans until after backtest (minor)",
                "optional lighter intent/order logging for matrix mode (needs flag; may affect metrics that read logs)",
            ],
            "high_impact_needs_explicit_policy": [
                "cap max_candles after N with still-open status (changes duration metrics)",
                "stop when inventory_mtm confirms blocker earlier (changes research definition)",
                "shared warm state fork across profiles after common pre-cycle path (complex; only_cycles=4 helps but entry→C4 still shared)",
            ],
            "estimated_total_after_resume_only_minutes": round(est_by_window / 60.0, 1),
            "estimated_if_cps_plus_30pct_minutes": round((est_by_window / 1.3) / 60.0, 1),
            "note": "Dominant cost is inherent: open blockers @1000/500 replay ~full remaining history ×4 profiles. 1000+ min estimates come from that, not from continuous chains.",
        },
    }

    # Write artifacts
    with (OUT / "runtime_per_run.csv").open("w", encoding="utf-8", newline="") as fh:
        fields = [k for k in rows[0].keys() if k != "cprofile_top"]
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    with (OUT / "candle_load.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(load_rows[0].keys()))
        w.writeheader()
        w.writerows(load_rows)

    (OUT / "benchmark_summary.json").write_text(
        json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8"
    )
    if cprofile_dump:
        (OUT / "cprofile_btc_legacy.txt").write_text(
            next(iter(cprofile_dump.values())), encoding="utf-8"
        )

    report = [
        "# Multicoin price-staging runtime benchmark",
        "",
        f"Coins: {', '.join(BENCH_COINS)} × profiles {', '.join(PROFILES)}",
        "",
        "## Per-run timings",
        "",
        "| coin | profile | backtest_s | candles_proc | window_len | cps | orders | intents | status | full_window_open |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        report.append(
            f"| {r['coin']} | {r['profile']} | {r['backtest_seconds']} | {r['candles_processed']} | "
            f"{r['window_len_from_start']} | {r['candles_per_second']} | {r['orders_submitted']} | "
            f"{r['intent_log_events']} | {r['final_status']} | {r['is_full_window_open_blocker']} |"
        )
    report.extend(
        [
            "",
            "## Bottleneck",
            "",
            "Isolated blocker runs use `candles[start_index:]`. For **open** blockers the trade never flats,",
            "so the backtest processes essentially the **entire remaining series** (often ~50k candles).",
            "That is **not** a continuous multi-trade chain, but it is almost as expensive.",
            "",
            f"- Mean s/run (3×4): **{mean_per_profile:.1f}s**",
            f"- Naive 27×4 extrapolate: **{est_naive_27/60:.0f} min**",
            f"- Window-weighted open-blocker extrapolate @ BTC cps: **{est_by_window/60:.0f} min**",
            "",
            "## Answers (short)",
            "",
            "1. See table above.",
            "2. Open blockers: candles_processed ≈ window_len_from_start.",
            "3. One trade per run; order/intent/stage counts in CSV.",
            "4. Candles loaded **once per coin** in this bench and in the main runner.",
            "5. **Isolated** single-trade path — but open trades run to series end.",
            "6. `run_historical_backtest` / `process_candle` loop (see cprofile).",
            "7. APT gate once; matrix reuses APT rows (no double backtest).",
            "8. No shared fork today — each profile cold-starts.",
            "9. No checkpoint/--resume today.",
            "10. BTC open path has large order_log from repeated exit rebuilds over ~50k candles.",
            "",
            "## Preferred optimizations (not implemented here)",
            "",
            "- `--resume` + per-coin checkpoint (safe, no semantics change)",
            "- optional matrix mode with reduced logging",
            "- shared pre-C4 state fork across profiles (complex)",
            "- explicit max_candles / early-stop policy (semantics — needs approval)",
            "",
            "**No live/runtime change. No commit.**",
            "",
        ]
    )
    (OUT / "REPORT.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    print(f"wrote {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
