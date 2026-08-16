#!/usr/bin/env python3
"""Backfill Frozen BE50 TRADE outcomes + No-BE50 counterfactual for BE rows.

Usage:
  .venv/bin/python scripts/backfill_signal_outcomes.py --hours 168
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from signal_generator.config import get_clickhouse_settings  # noqa: E402
from signal_generator.db.candles import CandleRepository  # noqa: E402
from signal_generator.db.outcomes import SignalOutcomeRepository  # noqa: E402
from signal_generator.db.setup import setup_clickhouse  # noqa: E402
from signal_generator.db.signals import SignalRepository  # noqa: E402
from signal_generator.pipeline.outcome_eval import (  # noqa: E402
    RESULT_BE,
    RESULT_LOSS,
    RESULT_OPEN,
    RESULT_WIN,
    OutcomeEvaluator,
    display_result_for,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_outcomes")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hours", type=int, default=168)
    ap.add_argument("--symbol", action="append", default=None)
    args = ap.parse_args()

    client = setup_clickhouse(settings=get_clickhouse_settings())
    signals = SignalRepository(client)
    candles = CandleRepository(client)
    outcomes = SignalOutcomeRepository(client)
    ev = OutcomeEvaluator(candles=candles, signals=signals, outcomes=outcomes)

    as_of = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = as_of - timedelta(hours=max(1, args.hours))
    rows, total = signals.query_signals(
        start=start,
        end=as_of + timedelta(minutes=1),
        symbols=args.symbol,
        tier_a=True,
        limit=5000,
        offset=0,
    )
    symbols = sorted({str(r["symbol"]) for r in rows})
    logger.info("tier_a signals=%s symbols=%s", total, len(symbols))

    totals = Counter()
    for sym in symbols:
        st = ev.evaluate_symbol(sym, as_of=as_of, lookback_hours=args.hours + 24)
        logger.info("[%s] %s", sym, st)
        for k, v in st.items():
            totals[k] += v

    ids = [r["signal_id"] for r in rows]
    views = outcomes.get_trade_outcomes_by_signal_ids(ids)
    rc = Counter(v.result for v in views.values())
    dc = Counter(
        (v.display_result or display_result_for(v.result, v.counterfactual_no_be_result))
        for v in views.values()
    )
    pnls = [v.pnl_pct for v in views.values() if v.pnl_pct is not None]
    durs = [
        v.duration_seconds
        for v in views.values()
        if v.result != RESULT_OPEN and v.duration_seconds is not None
    ]
    closed = rc[RESULT_WIN] + rc[RESULT_LOSS] + rc[RESULT_BE]
    win_ex_be = (
        rc[RESULT_WIN] / (rc[RESULT_WIN] + rc[RESULT_LOSS])
        if (rc[RESULT_WIN] + rc[RESULT_LOSS])
        else None
    )

    be_win = dc.get("BE / WIN", 0)
    be_loss = dc.get("BE / LOSS", 0)
    be_open = dc.get("BE / OPEN", 0)
    be_resolved = be_win + be_loss
    later_win_rate = be_win / be_resolved if be_resolved else None
    saved_loss_rate = be_loss / be_resolved if be_resolved else None

    # Descriptive PnL: Frozen ON vs Counterfactual OFF for same signals
    frozen_total = 0.0
    cf_total = 0.0
    for v in views.values():
        if v.result == RESULT_OPEN:
            continue
        fp = float(v.pnl_pct) if v.pnl_pct is not None else 0.0
        frozen_total += fp
        if v.result == RESULT_BE:
            if v.counterfactual_no_be_result in (RESULT_WIN, RESULT_LOSS):
                cf_total += float(v.counterfactual_no_be_pnl_pct or 0.0)
            # BE/OPEN contributes 0 in both until resolved
        else:
            cf_total += fp

    print("=== OUTCOME BACKFILL ===")
    print(dict(totals))
    print("results", dict(rc))
    print("display_results", dict(dc))
    print("win_rate_ex_be", win_ex_be)
    print("--- BE Breakdown ---")
    print(f"BE / WIN  {be_win}")
    print(f"BE / LOSS {be_loss}")
    print(f"BE / OPEN {be_open}")
    print("later_win_rate", later_win_rate)
    print("saved_loss_rate", saved_loss_rate)
    print("--- PnL Comparison (descriptive) ---")
    print("frozen_be50_on_total_pnl", frozen_total)
    print("counterfactual_be50_off_total_pnl", cf_total)
    print("difference_off_minus_on", cf_total - frozen_total)
    if pnls:
        print("mean_pnl", float(np.mean(pnls)), "median_pnl", float(np.median(pnls)))
    if durs:
        print("avg_duration_s", float(np.mean(durs)))
    print("closed", closed, "open", rc[RESULT_OPEN], "missing_outcome", len(rows) - len(views))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
