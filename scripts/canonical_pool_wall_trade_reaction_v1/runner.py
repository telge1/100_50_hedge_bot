#!/usr/bin/env python3
"""Run Pool × Wall × Trade reaction atlas V1 (read-only).

Usage:
  PYTHONPATH=src python scripts/canonical_pool_wall_trade_reaction_v1/runner.py --smoke
  PYTHONPATH=src python scripts/canonical_pool_wall_trade_reaction_v1/runner.py
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/home/telgenbuescher/projects/orderbook_analyse")
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.canonical_pool_wall_trade_reaction_v1.analyze import analyze_all  # noqa: E402
from orderbook_analyse.canonical_pool_wall_trade_reaction_v1.contracts import (  # noqa: E402
    ANALYSIS_END,
    ANALYSIS_START,
    OUT_ROOT,
)
from orderbook_analyse.canonical_pool_wall_trade_reaction_v1.loaders import (  # noqa: E402
    load_first_seen_tags,
    load_market_window,
    load_pool_episodes,
    verify_structural_freeze,
)
from orderbook_analyse.canonical_pool_wall_trade_reaction_v1.report import write_outputs  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Pool × Wall × Trade reaction V1")
    p.add_argument("--smoke", action="store_true", help="2 days + max 150 episodes")
    p.add_argument("--max-episodes", type=int, default=0, help="cap episodes (0=all)")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    t_start = time.perf_counter()
    print("=== Pool × Wall × Trade Reaction V1 ===", flush=True)
    freeze = verify_structural_freeze()
    print("structural freeze OK", flush=True)

    episodes = load_pool_episodes()
    tags = load_first_seen_tags()
    episodes = episodes.merge(tags, on="pool_id", how="left")

    start = datetime.fromisoformat(ANALYSIS_START.replace("Z", "+00:00"))
    end = datetime.fromisoformat(ANALYSIS_END.replace("Z", "+00:00"))
    if args.smoke:
        # early window with full feature coverage
        start = datetime(2026, 8, 1, 14, 0, tzinfo=timezone.utc)
        end = datetime(2026, 8, 3, 0, 0, tzinfo=timezone.utc)
        episodes = episodes[
            (episodes["first_seen"] < end) & (episodes["last_seen"] >= start)
        ].head(150)
        print(f"SMOKE: {start} → {end}, n_episodes={len(episodes)}", flush=True)
    else:
        episodes = episodes[
            (episodes["first_seen"] < end) & (episodes["last_seen"] >= start)
        ]
        if args.max_episodes and args.max_episodes > 0:
            episodes = episodes.head(args.max_episodes)
        print(f"FULL: {start} → {end}, n_episodes={len(episodes)}", flush=True)

    print("loading market data…", flush=True)
    feat, trades, market_meta = load_market_window(start, end)
    print(
        f"market loaded: features={len(feat)} trade_seconds={len(trades)}",
        flush=True,
    )

    print("analyzing episodes…", flush=True)
    df = analyze_all(episodes, feat, trades)
    summary = write_outputs(df, market_meta, freeze, smoke=bool(args.smoke))
    elapsed = time.perf_counter() - t_start
    print(f"DONE in {elapsed:.1f}s → {OUT_ROOT}", flush=True)
    print(
        f"touched={summary['n_touched']} rejected={summary['n_rejected']} "
        f"passed={summary['n_passed']} wall_yes={summary['n_wall_in_pool_yes']}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
