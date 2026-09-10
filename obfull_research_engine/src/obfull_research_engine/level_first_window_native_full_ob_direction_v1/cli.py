"""CLI for LEVEL_FIRST_WINDOW_NATIVE_FULL_OB_DIRECTION_V1."""

from __future__ import annotations

import argparse
import json

from .runner import run_window_native


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LEVEL_FIRST_WINDOW_NATIVE_FULL_OB_DIRECTION_V1")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--precheck-only", action="store_true")
    parser.add_argument("--assemble-only", action="store_true")
    args = parser.parse_args(argv)
    out = run_window_native(
        symbol=args.symbol,
        precheck_only=args.precheck_only,
        resume=True,
        assemble_only=args.assemble_only,
    )
    keys = (
        "verdict",
        "run_key",
        "run_dir",
        "n_directed",
        "n_replayed",
        "n_excluded_no_replay",
        "n_undirected",
        "label_counts",
        "outcomes_loaded",
        "clickhouse_writes",
        "estimated_replay_s",
        "status",
    )
    print(json.dumps({k: out[k] for k in keys if k in out}, indent=2, default=str))
    return 0
