#!/usr/bin/env python3
"""Idempotent backfill: resolve T0 entry + initial TP/SL for stored signals.

Uses frozen ``resolve_entries`` (1m open strictly after confirmation) and
``trade_levels``. Does not delete candidates. Re-inserts same signal_id via
ReplacingMergeTree.

Usage:
  .venv/bin/python scripts/backfill_signal_entry_tpsl.py --hours 168
  .venv/bin/python scripts/backfill_signal_entry_tpsl.py --hours 48 --tier-a-only --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from signal_generator.config import get_clickhouse_settings  # noqa: E402
from signal_generator.db.candles import CandleRepository  # noqa: E402
from signal_generator.db.setup import setup_clickhouse  # noqa: E402
from signal_generator.db.signals import Signal, SignalRepository  # noqa: E402
from signal_generator.pipeline.trade_plan import (  # noqa: E402
    merge_trade_plan_into_metadata,
    parse_trade_plan_from_metadata,
    reconstruct_trade_plan,
)
from signal_generator.strategy.wave_fade.adapter import (  # noqa: E402
    bars_to_ohlcv_df,
    one_minute_books,
)
from signal_generator.timeframes import bars_from_mappings, ensure_utc  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_entry_tpsl")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hours", type=int, default=168, help="Lookback window on candle_close_time")
    ap.add_argument("--tier-a-only", action="store_true", help="Only repair tier_a=true rows")
    ap.add_argument("--symbol", action="append", default=None, help="Limit to symbol(s)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=5000)
    args = ap.parse_args()

    client = setup_clickhouse(settings=get_clickhouse_settings())
    signals = SignalRepository(client)
    candles = CandleRepository(client)

    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=max(1, args.hours))
    rows, total = signals.query_signals(
        start=start,
        end=end,
        symbols=args.symbol,
        tier_a=True if args.tier_a_only else None,
        limit=args.limit,
        offset=0,
    )
    logger.info("loaded %s/%s signals in window", len(rows), total)

    by_symbol: dict[str, list[dict]] = {}
    for r in rows:
        by_symbol.setdefault(str(r["symbol"]), []).append(r)

    repaired = 0
    unresolved = 0
    skipped = 0
    samples: list[dict] = []

    for symbol, sym_rows in sorted(by_symbol.items()):
        # Load 1m with cushion for T0 after last confirmation
        conf_times = [ensure_utc(r["candle_close_time"]) for r in sym_rows]
        load_start = min(conf_times) - timedelta(hours=2)
        load_end = max(conf_times) + timedelta(minutes=10)
        raw = candles.get_candles(symbol, load_start, load_end, exchange="bybit", interval="1m")
        bars = bars_from_mappings(raw)
        if not bars:
            logger.warning("[%s] no 1m candles — %s unresolved", symbol, len(sym_rows))
            unresolved += len(sym_rows)
            continue
        ohlcv = bars_to_ohlcv_df(bars)
        open_times, opens = one_minute_books(ohlcv)

        batch: list[Signal] = []
        for r in sym_rows:
            try:
                px = float(r.get("signal_price") or 0)
            except (TypeError, ValueError):
                px = 0.0
            plan0 = parse_trade_plan_from_metadata(r.get("metadata"))
            if px > 0 and plan0.get("entry_valid") is True and plan0.get("tp_price"):
                skipped += 1
                continue
            conf = ensure_utc(r["candle_close_time"])
            plan = reconstruct_trade_plan(
                confirmation_available_at=conf,
                side=str(r["direction"]),
                timeframe=str(r["timeframe"]),
                open_times=open_times,
                opens=opens,
            )
            if not plan.get("entry_valid") or not plan.get("entry_price"):
                unresolved += 1
                continue
            entry = float(plan["entry_price"])
            sig = Signal(
                symbol=str(r["symbol"]),
                timeframe=str(r["timeframe"]),
                direction=str(r["direction"]),
                signal_type=str(r["signal_type"]),
                signal_price=Decimal(str(entry)),
                candle_open_time=ensure_utc(r["candle_open_time"]),
                candle_close_time=conf,
                generator_version=str(r["generator_version"]),
                strategy_version=str(r["strategy_version"]),
                signal_id=r["signal_id"],
                generated_at=ensure_utc(r["generated_at"]),
                stoch_k=r.get("stoch_k"),
                stoch_d=r.get("stoch_d"),
                wave_state=r.get("wave_state"),
                tier_a=bool(r.get("tier_a")),
                tier_a_context=str(r.get("tier_a_context") or ""),
                rank_score=r.get("rank_score"),
                selected=bool(r.get("selected")),
                selection_reason=str(r.get("selection_reason") or ""),
                trend_15m=r.get("trend_15m"),
                trend_30m=r.get("trend_30m"),
                trend_1h=r.get("trend_1h"),
                trend_4h=r.get("trend_4h"),
                signal_bias=r.get("signal_bias"),
                traded=bool(r.get("traded")),
                trade_id=r.get("trade_id"),
                metadata=merge_trade_plan_into_metadata(r.get("metadata") or "{}", plan),
            )
            batch.append(sig)
            if len(samples) < 15:
                samples.append(
                    {
                        "symbol": sig.symbol,
                        "tf": sig.timeframe,
                        "dir": sig.direction,
                        "tier_a": sig.tier_a,
                        "entry": entry,
                        "tp": plan["tp_price"],
                        "sl": plan["sl_price"],
                        "signal_time": conf.isoformat(),
                        "signal_id": str(sig.signal_id),
                    }
                )

        if batch and not args.dry_run:
            signals.insert_signals(batch)
        repaired += len(batch)
        logger.info("[%s] repaired=%s", symbol, len(batch))

    print("=== BACKFILL SUMMARY ===")
    print(f"repaired={repaired} skipped_ok={skipped} unresolved={unresolved} dry_run={args.dry_run}")
    print("samples:")
    for s in samples:
        print(
            f"  {s['symbol']} {s['tf']} {s['dir']} tier_a={s['tier_a']} "
            f"entry={s['entry']} tp={s['tp']} sl={s['sl']} time={s['signal_time']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
