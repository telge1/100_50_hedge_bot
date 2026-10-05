"""Long V1 shadow scanner loop (no orders)."""

from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone

from bot.long_v1_live_scanner.config import LOG_DIR, RUNTIME_DIR, ScannerConfig
from bot.long_v1_live_scanner.logging_store import JsonlSignalLog
from bot.long_v1_live_scanner.processing import SymbolProcessor, data_readiness_row
from bot.long_v1_live_scanner.state import (
    PersistedState,
    Readiness,
    apply_symbol_snapshot,
    load_persisted,
    save_persisted,
)
from bot.long_v1_live_scanner.universe import log_universe_header, universe_meta
from bot.shadow_signal_registry import ShadowRegistry
from bot.shadow_signal_registry.ch_config import apply_live_scanner_runtime_env


def run_dry_loop(config: ScannerConfig | None = None, once: bool = False) -> None:
    config = config or ScannerConfig(live=True)
    if config.live:
        apply_live_scanner_runtime_env()
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    meta = universe_meta()
    print(log_universe_header(meta), flush=True)
    symbols = list(meta.symbols)
    if meta.symbol_count != 10:
        print(f"WARNING expected 10 symbols, got {meta.symbol_count}", flush=True)

    persisted = load_persisted(config.state_path)
    log = JsonlSignalLog(config.log_path)
    long_registry = ShadowRegistry("long")
    proc = SymbolProcessor(config, registry=long_registry)
    states: dict[str, object] = {}

    now0 = datetime.now(timezone.utc)
    for sym in symbols:
        st = proc.load_state(sym, now0)
        snap = persisted.symbols.get(sym)
        if snap:
            apply_symbol_snapshot(st, snap)
        if st.readiness == Readiness.DATA_MISSING:
            print(f"SKIP {sym}: {st.data_missing_reason}", flush=True)
            continue
        if st.readiness == Readiness.WAITING_FOR_DATA:
            print(f"WAIT {sym}: {st.data_missing_reason}", flush=True)
            states[sym] = st
            continue
        if st.last_processed_15m_close is None:
            proc.warmup(st)
        else:
            st.readiness = Readiness.LIVE_READY
        states[sym] = st
        print(f"READY {sym} last={st.last_processed_15m_close}", flush=True)

    dry_run_start = persisted.dry_run_start_utc
    loop_started = False

    while True:
        detected_at = datetime.now(timezone.utc)
        if dry_run_start is None:
            dry_run_start = detected_at.isoformat()
            persisted.dry_run_start_utc = dry_run_start
            config.dry_run_marker_path.write_text(dry_run_start + "\n", encoding="utf-8")
            print(f"LONG_LIVE_DRY_RUN_START={dry_run_start}", flush=True)
            loop_started = True

        t0 = time.perf_counter()
        processed = waiting = failed = 0
        errors: list[str] = []
        catchup_bars = duplicate_bars = missed_bars = out_of_order = 0
        geometry = ladder_blocked = allowed = 0
        open_shadow = closed_tp = closed_sl = closed_be = 0
        new_1m = 0
        wave_bar_close = None
        symbol_rows: list[dict] = []

        for sym, st in states.items():
            if st.readiness == Readiness.DATA_MISSING:
                failed += 1
                continue
            try:
                result = proc.poll_symbol_live(st, detected_at)
                if result.status == Readiness.WAITING_FOR_DATA.value:
                    waiting += 1
                    st.readiness = Readiness.WAITING_FOR_DATA
                elif result.bars_processed > 0 or result.signal_rows:
                    processed += 1
                    for row in result.signal_rows:
                        log.write(row)
                    catchup_bars += result.catchup_bars
                    duplicate_bars += result.duplicate_bars
                    missed_bars += result.missed_bars
                    out_of_order += result.out_of_order
                    geometry += result.geometry_candidates
                    ladder_blocked += result.ladder_blocked
                    allowed += result.allowed
                    open_shadow += result.open_shadow
                    closed_tp += result.closed_tp
                    closed_sl += result.closed_sl
                    closed_be += result.closed_be
                    new_1m += result.new_1m_bars
                    if result.bar_close:
                        wave_bar_close = result.bar_close
                symbol_rows.append(
                    {
                        "symbol": sym,
                        "bar_close": result.bar_close,
                        "status": result.status,
                        "bars_processed": result.bars_processed,
                    }
                )
                errors.extend(result.errors)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                errors.append(f"{sym}:{exc}")

        save_persisted(config.state_path, persisted, states)
        for st in states.values():
            if hasattr(st, "open_trades"):
                long_registry.sync_long_from_scanner(st.open_trades)
        long_registry.flush_snapshots()

        elapsed = time.perf_counter() - t0
        if dry_run_start is not None:
            summary = {
                "event": "wave_summary",
                "scanner": "long_v1_live",
                "dry_run_start_utc": dry_run_start,
                "detected_at": detected_at.isoformat(),
                "bar_close": wave_bar_close,
                "symbols_expected": len(symbols),
                "symbols_processed": processed,
                "symbols_waiting_for_data": waiting,
                "symbols_failed": failed,
                "new_1m_bars": new_1m,
                "new_closed_15m_bars": catchup_bars,
                "geometry_candidates": geometry,
                "ladder_blocked": ladder_blocked,
                "allowed": allowed,
                "open_shadow_trades": open_shadow,
                "closed_tp": closed_tp,
                "closed_sl": closed_sl,
                "closed_be": closed_be,
                "duplicate_bars": duplicate_bars,
                "missed_closes": missed_bars,
                "out_of_order": out_of_order,
                "errors": errors,
                "elapsed_seconds": round(elapsed, 3),
                "symbols": symbol_rows,
            }
            log.write_wave_summary(summary)
            if catchup_bars > 0 or geometry or allowed or errors:
                print(summary, flush=True)

        if once:
            break
        time.sleep(config.poll_seconds)


def cmd_readiness() -> None:
    meta = universe_meta()
    print(log_universe_header(meta))
    proc = SymbolProcessor(ScannerConfig(live=True))
    now = datetime.now(timezone.utc)
    for sym in meta.symbols:
        st = proc.load_state(sym, now)
        if st.readiness not in (Readiness.DATA_MISSING, Readiness.WAITING_FOR_DATA):
            if st.last_processed_15m_close is None:
                proc.warmup(st)
        print(data_readiness_row(st))


def main() -> None:
    parser = argparse.ArgumentParser(description="Long V1 shadow dry-run scanner")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--readiness", action="store_true")
    args = parser.parse_args()
    if args.readiness:
        cmd_readiness()
        return
    run_dry_loop(once=args.once)


if __name__ == "__main__":
    main()
