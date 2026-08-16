#!/usr/bin/env python3
"""Backfill TRADE_NO_BE50 outcomes for Tier-A signals (does NOT touch BE50 TRADE).

Usage:
  .venv/bin/python scripts/backfill_no_be50_outcomes.py --hours 168
  .venv/bin/python scripts/backfill_no_be50_outcomes.py --all
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from signal_generator.config import get_clickhouse_settings  # noqa: E402
from signal_generator.db.candles import CandleRepository  # noqa: E402
from signal_generator.db.outcomes import SignalOutcomeRepository  # noqa: E402
from signal_generator.db.setup import setup_clickhouse  # noqa: E402
from signal_generator.db.signals import SignalRepository  # noqa: E402
from signal_generator.pipeline.outcome_eval import (  # noqa: E402
    OutcomeEvaluator,
    summarize_trade_views,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_no_be50")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hours", type=int, default=168)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--symbol", action="append", default=None)
    args = ap.parse_args()

    client = setup_clickhouse(settings=get_clickhouse_settings())
    signals = SignalRepository(client)
    candles = CandleRepository(client)
    outcomes = SignalOutcomeRepository(client)
    ev = OutcomeEvaluator(candles=candles, signals=signals, outcomes=outcomes)

    as_of = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    if args.all:
        r = client.query(
            f"""
            SELECT min(candle_close_time) AS mn
            FROM {client.database}.signals FINAL WHERE tier_a = 1
            """
        )
        mn = r.result_rows[0][0]
        if mn is None:
            logger.error("no tier_a signals")
            return 1
        start = (mn.replace(tzinfo=timezone.utc) if mn.tzinfo is None else mn) - timedelta(seconds=1)
        lookback = int((as_of - start).total_seconds() // 3600) + 48
    else:
        start = as_of - timedelta(hours=max(1, args.hours))
        lookback = args.hours + 24

    rows, total = signals.query_signals(
        start=start,
        end=as_of + timedelta(minutes=1),
        symbols=args.symbol,
        tier_a=True,
        limit=5000,
        offset=0,
    )
    symbols = sorted({str(r["symbol"]) for r in rows})
    logger.info("tier_a signals=%s symbols=%s lookback_h=%s", total, len(symbols), lookback)

    totals: Counter = Counter()
    for sym in symbols:
        st = ev.evaluate_symbol(sym, as_of=as_of, lookback_hours=lookback)
        logger.info("[%s] %s", sym, st)
        for k, v in st.items():
            totals[k] += v

    ids = [r["signal_id"] for r in rows]
    be50 = outcomes.get_trade_outcomes_by_signal_ids(ids)
    nobe = outcomes.get_no_be50_outcomes_by_signal_ids(ids)
    s_be = summarize_trade_views([be50.get(str(i)) for i in ids])
    s_nb = summarize_trade_views([nobe.get(str(i)) for i in ids])

    print("=== NO_BE50 BACKFILL ===")
    print(dict(totals))
    print("BE50 TRADE summary", s_be)
    print("NO_BE50 TRADE_NO_BE50 summary", s_nb)
    print("missing_no_be50", sum(1 for i in ids if str(i) not in nobe))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
