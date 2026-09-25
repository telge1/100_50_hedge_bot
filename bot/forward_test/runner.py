"""Dry-run forward-test loop: scan universe, paper-track to TP/SL, never trade live."""

from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone

from bot.forward_test.config import (
    DEFAULT_POLL_SECONDS,
    DEFAULT_SYMBOLS,
    EVENTS_LOG,
    OPEN_TRADES_FILE,
    SIGNALS_LOG,
    STATE_FILE,
    SUMMARY_FILE,
    TRADES_LOG,
)
from bot.forward_test.logger import append_jsonl
from bot.forward_test.paper_trades import PaperLedger
from bot.forward_test.paths import ensure_import_paths
from bot.forward_test.scanner import scan_symbol_short
from bot.forward_test.state_store import SeenStore


def _event_key(ev: dict) -> str:
    return "|".join(
        [
            str(ev.get("symbol") or ""),
            str(ev.get("event") or ""),
            str(ev.get("cluster_id") or ""),
            str(ev.get("reason") or ""),
            str((ev.get("detail") or {}).get("touch_ts") or ""),
            str((ev.get("detail") or {}).get("short_entry_ts") or ""),
            # bucket by minute so idle/watch does not flood forever,
            # but still refreshes periodically.
            str(ev.get("ts") or "")[:16],
        ]
    )


def _signal_key(sig: dict) -> str:
    return "|".join(
        [
            str(sig.get("symbol") or ""),
            str(sig.get("side") or ""),
            str(sig.get("cluster_id") or ""),
            str(sig.get("short_entry_ts") or ""),
            f"{float(sig.get('entry_price') or 0):.8f}",
        ]
    )


def run_once(symbols: list[str], seen: SeenStore, ledger: PaperLedger) -> dict[str, int]:
    ensure_import_paths()
    counts = {
        "symbols": 0,
        "events": 0,
        "events_logged": 0,
        "signals": 0,
        "signals_logged": 0,
        "paper_opened": 0,
        "paper_closed": 0,
        "errors": 0,
    }

    # First: update open paper trades against latest bars.
    closed = ledger.update_open_trades()
    for trade in closed:
        counts["paper_closed"] += 1
        print(
            f"  PAPER CLOSE {trade.symbol} reason={trade.exit_reason} "
            f"entry={trade.entry_price} exit={trade.exit_price} pnl={trade.pnl_pct:.3f}%"
            + (f" note={trade.exit_note}" if trade.exit_note else ""),
            flush=True,
        )
    if closed:
        summary = ledger.write_summary()
        print(
            f"  PnL summary: closed={summary['closed_trades']} "
            f"wins={summary['wins']} losses={summary['losses']} "
            f"sum_pnl={summary['sum_pnl_pct']}% open={summary['open_trades']}",
            flush=True,
        )

    for symbol in symbols:
        counts["symbols"] += 1
        print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}Z] scan {symbol} ...", flush=True)

        # One open paper trade per symbol — skip new entries while open.
        if ledger.has_open(symbol):
            open_trade = ledger.open_by_symbol[symbol]
            print(
                f"  skip_new_signal (paper open) entry={open_trade.entry_price} "
                f"sl={open_trade.stop_price} tp={open_trade.tp_price}",
                flush=True,
            )
            continue

        try:
            events, signals = scan_symbol_short(symbol)
        except Exception as exc:
            counts["errors"] += 1
            row = {
                "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "symbol": symbol,
                "event": "error",
                "reason": "scan_crash",
                "detail": {"error": str(exc)},
            }
            append_jsonl(EVENTS_LOG, row)
            print(f"  ERROR {symbol}: {exc}", flush=True)
            continue

        for ev in events:
            counts["events"] += 1
            payload = ev.to_dict()
            if ev.event == "error":
                counts["errors"] += 1
            key = _event_key(payload)
            if seen.add(key):
                append_jsonl(EVENTS_LOG, payload)
                counts["events_logged"] += 1
                print(
                    f"  event={ev.event} rank={ev.rank} reason={ev.reason} "
                    f"price={ev.last_price} ob={ev.ob_ratio} delta={ev.delta_10m}",
                    flush=True,
                )

        for sig in signals:
            counts["signals"] += 1
            payload = sig.to_dict()
            key = _signal_key(payload)
            # Only log + open once per signal key (no re-open after TP/SL).
            if not seen.add(key):
                continue
            from bot.forward_test.regime import market_regime

            try:
                regime = market_regime(sig.symbol)
            except Exception as exc:
                regime = {"regime": "unknown", "allows_short": False, "error": str(exc)}
            if not regime.get("allows_short"):
                skip = {
                    "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "symbol": sig.symbol,
                    "event": "skip",
                    "reason": "regime_bullish" if regime.get("regime") == "bullish" else "regime_unknown",
                    "detail": {
                        "regime": regime.get("regime"),
                        "h1": (regime.get("h1") or {}).get("label") if isinstance(regime.get("h1"), dict) else None,
                        "h4": (regime.get("h4") or {}).get("label") if isinstance(regime.get("h4"), dict) else None,
                        "entry_price": sig.entry_price,
                    },
                }
                append_jsonl(EVENTS_LOG, skip)
                print(
                    f"  SKIP SIGNAL short {sig.symbol} regime={regime.get('regime')} "
                    f"h1={skip['detail']['h1']} h4={skip['detail']['h4']}",
                    flush=True,
                )
                continue
            append_jsonl(SIGNALS_LOG, payload)
            counts["signals_logged"] += 1
            print(
                f"  DRY SIGNAL short {sig.symbol} entry={sig.entry_price} "
                f"sl={sig.stop_price} tp={sig.tp_price} (no order)",
                flush=True,
            )
            opened = ledger.open_from_signal(payload)
            if opened is not None:
                counts["paper_opened"] += 1
                print(
                    f"  PAPER OPEN {opened.symbol} entry={opened.entry_price} "
                    f"sl={opened.stop_price} tp={opened.tp_price}",
                    flush=True,
                )

    seen.save()
    ledger.write_summary()
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Pool bounce dry-run scanner (logs only, never places Bybit orders)."
    )
    parser.add_argument(
        "--symbols",
        default=",".join(DEFAULT_SYMBOLS),
        help="Comma-separated symbols (default: forward-test set without BTC)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one scan pass and exit",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=DEFAULT_POLL_SECONDS,
        help="Sleep between full universe scans (default 30)",
    )
    args = parser.parse_args(argv)

    symbols = [s.strip().upper() for s in str(args.symbols).split(",") if s.strip()]
    if not symbols:
        print("No symbols given.", flush=True)
        return 2

    print("=== DRY RUN FORWARD TEST ===", flush=True)
    print("place_order=False  (never sends Bybit orders)", flush=True)
    print(f"symbols={symbols}", flush=True)
    print(f"events_log={EVENTS_LOG}", flush=True)
    print(f"signals_log={SIGNALS_LOG}", flush=True)
    print(f"trades_log={TRADES_LOG}", flush=True)
    print(f"pnl_summary={SUMMARY_FILE}", flush=True)

    seen = SeenStore(STATE_FILE)
    ledger = PaperLedger(
        open_path=OPEN_TRADES_FILE,
        closed_log=TRADES_LOG,
        summary_path=SUMMARY_FILE,
    )
    boot = ledger.bootstrap_from_signals_log(SIGNALS_LOG)
    if boot:
        print(f"bootstrapped {boot} open paper trade(s) from signals log", flush=True)
        ledger.write_summary()
    else:
        n_open = len(ledger.open_by_symbol)
        if n_open:
            print(
                f"restored {n_open} open paper trade(s) from {OPEN_TRADES_FILE.name} "
                f"(no signal re-open)",
                flush=True,
            )
        ledger.write_summary()

    if args.once:
        counts = run_once(symbols, seen, ledger)
        print(f"done once: {counts}", flush=True)
        print(f"summary: {ledger.write_summary()}", flush=True)
        return 0 if counts["errors"] == 0 else 1

    print(f"polling every {args.poll_seconds:.0f}s  (Ctrl+C to stop)", flush=True)
    while True:
        counts = run_once(symbols, seen, ledger)
        summary = ledger.write_summary()
        print(f"cycle done: {counts}", flush=True)
        print(
            f"pnl: closed={summary['closed_trades']} sum={summary['sum_pnl_pct']}% "
            f"open={summary['open_trades']} {summary['open_symbols']}",
            flush=True,
        )
        time.sleep(max(1.0, float(args.poll_seconds)))


if __name__ == "__main__":
    raise SystemExit(main())
