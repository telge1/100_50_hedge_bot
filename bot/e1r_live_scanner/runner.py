"""Serial dry-run scanner loop (no orders)."""

from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone

from bot.e1r_live_scanner.config import ScannerConfig
from bot.e1r_live_scanner.logging_store import JsonlSignalLog
from bot.e1r_live_scanner.processing import SymbolProcessor, data_readiness_row
from bot.e1r_live_scanner.state import Readiness
from bot.e1r_live_scanner.universe import log_universe_header, universe_meta
from bot.long_v1_live_scanner.data_1m import load_1m_bars
from bot.shadow_signal_registry import ShadowRegistry
from bot.shadow_signal_registry.ch_config import apply_live_scanner_runtime_env


def run_dry_loop(config: ScannerConfig | None = None, once: bool = False) -> None:
    config = config or ScannerConfig(live=True)
    if config.live:
        apply_live_scanner_runtime_env()
    if not config.live:
        config = ScannerConfig(
            pane_from=config.pane_from,
            pane_to=config.pane_to,
            e1r_sim_from=config.e1r_sim_from,
            report_from=config.report_from,
            report_to=config.report_to,
            history_weeks=config.history_weeks,
            poll_seconds=config.poll_seconds,
            live=True,
            max_closed_15m_lag_minutes=config.max_closed_15m_lag_minutes,
            log_path=config.log_path,
        )
    meta = universe_meta()
    print(log_universe_header(meta), flush=True)
    symbols = list(meta.symbols)
    log = JsonlSignalLog(config.log_path)
    short_registry = ShadowRegistry("short")
    proc = SymbolProcessor(config, registry=short_registry)
    states: dict[str, object] = {}
    now0 = datetime.now(timezone.utc)
    for sym in symbols:
        st = proc.load_state(sym, now0)
        if st.readiness == Readiness.DATA_MISSING:
            print(f"SKIP {sym}: {st.data_missing_reason}", flush=True)
            continue
        if st.readiness == Readiness.WAITING_FOR_DATA:
            print(f"WAIT {sym}: {st.data_missing_reason}", flush=True)
            states[sym] = st
            continue
        proc.warmup(st)
        states[sym] = st
        print(f"READY {sym} last={st.last_processed_15m_close}", flush=True)

    while True:
        detected_at = datetime.now(timezone.utc)
        t0 = time.perf_counter()
        processed = 0
        waiting = 0
        failed = 0
        errors: list[str] = []
        max_lat = 0.0
        catchup_bars = 0
        duplicate_bars = 0
        missed_bars = 0
        out_of_order = 0
        allowed = 0
        blocked = 0
        floor_blocked = 0
        wave_bar_close: str | None = None
        symbol_rows: list[dict] = []

        for sym, st in states.items():
            if st.readiness == Readiness.DATA_MISSING:
                failed += 1
                continue
            t_sym = time.perf_counter()
            try:
                result = proc.poll_symbol_live(st, detected_at)
                lat_ms = (time.perf_counter() - t_sym) * 1000
                max_lat = max(max_lat, lat_ms)
                if result.status == Readiness.WAITING_FOR_DATA.value:
                    waiting += 1
                    st.readiness = Readiness.WAITING_FOR_DATA
                elif result.bars_processed > 0:
                    processed += 1
                    for row in result.signal_rows:
                        log.write(row)
                    catchup_bars += result.catchup_bars
                    duplicate_bars += result.duplicate_bars
                    missed_bars += result.missed_bars
                    out_of_order += result.out_of_order
                    allowed += result.allowed
                    blocked += result.blocked
                    floor_blocked += result.floor_blocked
                    if result.bar_close:
                        wave_bar_close = result.bar_close
                symbol_rows.append(
                    {
                        "symbol": sym,
                        "bar_close": result.bar_close,
                        "first_seen_in_ch": result.first_seen_in_ch,
                        "processing_started_at": result.processing_started_at,
                        "processing_finished_at": result.processing_finished_at,
                        "status": result.status,
                        "bars_processed": result.bars_processed,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                failed += 1
                errors.append(f"{sym}:{exc}")

        elapsed = time.perf_counter() - t0
        if catchup_bars > 0 or processed > 0:
            summary = {
                "event": "wave_summary",
                "detected_at": detected_at.isoformat(),
                "bar_close": wave_bar_close,
                "symbols_total": len(symbols),
                "symbols_ready": sum(
                    1 for s in states.values() if s.readiness == Readiness.LIVE_READY
                ),
                "symbols_processed": processed,
                "symbols_waiting_for_data": waiting,
                "symbols_failed": failed,
                "catchup_bars": catchup_bars,
                "duplicate_bars": duplicate_bars,
                "missed_bars": missed_bars,
                "out_of_order": out_of_order,
                "allowed": allowed,
                "blocked": blocked,
                "floor_blocked": floor_blocked,
                "elapsed_seconds": round(elapsed, 3),
                "max_symbol_latency_ms": round(max_lat, 2),
                "errors": errors,
                "symbols": symbol_rows,
            }
            log.write_wave_summary(summary)
            print(summary, flush=True)

        short_registry.track_open(load_1m_bars, detected_at)
        short_registry.flush_snapshots()

        if once and catchup_bars == 0:
            break
        time.sleep(config.poll_seconds)


def cmd_readiness() -> None:
    meta = universe_meta()
    print(log_universe_header(meta))
    proc = SymbolProcessor(ScannerConfig(live=True))
    print("| Symbol | Status | Fehlende Daten | Warmup OK |")
    print("|--------|--------|----------------|-----------|")
    now = datetime.now(timezone.utc)
    for sym in meta.symbols:
        st = proc.load_state(sym, now)
        if st.readiness not in (Readiness.DATA_MISSING, Readiness.WAITING_FOR_DATA):
            proc.warmup(st)
        row = data_readiness_row(st)
        print(
            f"| {row['symbol']} | {row['status']} | {row['missing']} | {row['warmup_ok']} |"
        )


def cmd_benchmark() -> None:
    import statistics

    meta = universe_meta()
    proc = SymbolProcessor(ScannerConfig(live=True))
    times: list[float] = []
    per_coin: dict[str, dict] = {}
    now = datetime.now(timezone.utc)
    for sym in meta.symbols:
        st = proc.load_state(sym, now)
        if st.readiness in (Readiness.DATA_MISSING, Readiness.WAITING_FOR_DATA):
            continue
        proc.warmup(st)
        m = proc.benchmark_one_bar(st)
        if m:
            per_coin[sym] = m
            times.append(m["total_ms"])
    if not times:
        print("no benchmark data")
        return
    total = sum(times)
    print(f"total_ms={total:.1f} median={statistics.median(times):.1f} p95={sorted(times)[int(len(times)*0.95)-1]:.1f}")
    print(f"slowest={max(times):.1f} ({max(per_coin, key=lambda k: per_coin[k]['total_ms'])})")
    for sym, m in sorted(per_coin.items()):
        print(sym, m)


def main() -> None:
    parser = argparse.ArgumentParser(description="E1R V1 dry-run scanner")
    parser.add_argument("--once", action="store_true", help="Exit after one poll (no new bars)")
    parser.add_argument("--readiness", action="store_true")
    parser.add_argument("--benchmark", action="store_true")
    args = parser.parse_args()
    if args.readiness:
        cmd_readiness()
        return
    if args.benchmark:
        cmd_benchmark()
        return
    run_dry_loop(once=args.once)


if __name__ == "__main__":
    main()
