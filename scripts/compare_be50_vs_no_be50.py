#!/usr/bin/env python3
"""Compare BE50 ON (TRADE) vs NO_BE50 (TRADE_NO_BE50) on the same Tier-A signals.

Read-only aggregation into results/be50_removal_comparison/summary.md
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from signal_generator.config import get_clickhouse_settings  # noqa: E402
from signal_generator.db.outcomes import SignalOutcomeRepository  # noqa: E402
from signal_generator.db.setup import setup_clickhouse  # noqa: E402
from signal_generator.db.signals import SignalRepository  # noqa: E402
from signal_generator.pipeline.outcome_eval import summarize_trade_views  # noqa: E402

OUT = ROOT / "results" / "be50_removal_comparison"


def _pnl_list(views) -> list[float]:
    out = []
    for v in views:
        if v is None or v.result in ("OPEN", None):
            continue
        if v.result == "BE":
            out.append(float(v.pnl_pct) if v.pnl_pct is not None else 0.0)
            continue
        if v.pnl_pct is not None:
            out.append(float(v.pnl_pct))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hours", type=int, default=168)
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    client = setup_clickhouse(settings=get_clickhouse_settings())
    signals = SignalRepository(client)
    outcomes = SignalOutcomeRepository(client)

    end = datetime.now(timezone.utc).replace(microsecond=0)
    start = end - timedelta(hours=max(1, args.hours))
    rows, total = signals.query_signals(
        start=start, end=end, tier_a=True, limit=5000, offset=0
    )
    ids = [r["signal_id"] for r in rows]
    be50_map = outcomes.get_trade_outcomes_by_signal_ids(ids)
    nobe_map = outcomes.get_no_be50_outcomes_by_signal_ids(ids)
    be_views = [be50_map.get(str(i)) for i in ids]
    nb_views = [nobe_map.get(str(i)) for i in ids]

    s_be = summarize_trade_views(be_views)
    s_nb = summarize_trade_views(nb_views)
    p_be = _pnl_list(be_views)
    p_nb = _pnl_list([v for v in nb_views if v is not None and v.result != "OPEN"])

    def mean_med(xs):
        if not xs:
            return None, None
        return float(np.mean(xs)), float(np.median(xs))

    be_mean, be_med = mean_med(p_be)
    nb_mean, nb_med = mean_med(p_nb)

    # BE50 closed = WIN+LOSS+BE; NO_BE50 closed = WIN+LOSS
    table = [
        ("Closed Trades", s_be["wins"] + s_be["losses"] + s_be["be"], s_nb["wins"] + s_nb["losses"]),
        ("Wins", s_be["wins"], s_nb["wins"]),
        ("Losses", s_be["losses"], s_nb["losses"]),
        ("BE", s_be["be"], 0),
        ("Open", s_be["open"], s_nb["open"]),
        ("Win Rate", s_be["win_rate_pct"], s_nb["win_rate_pct"]),
        ("Gross Profit", s_be["gross_profit_pct"], s_nb["gross_profit_pct"]),
        ("Gross Loss", s_be["gross_loss_pct"], s_nb["gross_loss_pct"]),
        ("Total PnL", s_be["total_pnl_pct"], s_nb["total_pnl_pct"]),
        ("Mean PnL", be_mean, nb_mean),
        ("Median PnL", be_med, nb_med),
    ]

    def fmt(v):
        if v is None:
            return "–"
        if isinstance(v, float):
            return f"{v:.4g}"
        return str(v)

    lines = [
        "# BE50 Removal Comparison",
        "",
        f"- Window: `{start.isoformat().replace('+00:00','Z')}` → `{end.isoformat().replace('+00:00','Z')}`",
        f"- Tier-A signals: **{total}**",
        f"- BE50 horizon: `TRADE`",
        f"- NO_BE50 horizon: `TRADE_NO_BE50`",
        "",
        "| Metric | BE50 ON | NO_BE50 |",
        "| ------ | ------: | ------: |",
    ]
    for name, a, b in table:
        lines.append(f"| {name} | {fmt(a)} | {fmt(b)} |")

    diff = (s_nb["total_pnl_pct"] or 0) - (s_be["total_pnl_pct"] or 0)
    lines += [
        "",
        f"**Total PnL difference (NO_BE50 − BE50):** `{diff}`",
        "",
        "_Descriptive only. No strategy auto-change beyond the intentional NO_BE50 active version._",
        "",
    ]
    (OUT / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    (OUT / "comparison.json").write_text(
        json.dumps(
            {
                "start": start.isoformat().replace("+00:00", "Z"),
                "end": end.isoformat().replace("+00:00", "Z"),
                "signals": total,
                "be50": s_be,
                "no_be50": s_nb,
                "difference_total_pnl": diff,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
