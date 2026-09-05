#!/usr/bin/env python3
"""CLI for L2 Wall-to-Wall Strategy Discovery V1."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from orderbook_analyse.l2_wall_to_wall_discovery.runner import run_wall_to_wall


def _parse_dt(raw: str) -> datetime:
    return datetime.fromisoformat(raw.strip().replace("Z", "+00:00")).astimezone(timezone.utc)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--attack-dir", type=Path, default=Path("results/l2_wall_attack_discovery/btc_doge_v1"))
    p.add_argument("--raw-root", type=Path, default=Path("data/orderbook_raw_shadow/ob200_v3"))
    p.add_argument("--symbols", default="BTCUSDT,DOGEUSDT")
    p.add_argument("--event-start", default="2026-08-25T00:00:00Z")
    p.add_argument("--event-end", default="2026-08-25T07:00:00Z")
    p.add_argument("--outcome-end", default="2026-08-25T11:00:00Z")
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--smoke", action="store_true")
    p.add_argument("--sample-ms", type=int, default=250)
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args(argv)

    symbols = tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip())
    if set(symbols) - {"BTCUSDT", "DOGEUSDT"}:
        raise SystemExit("only BTCUSDT,DOGEUSDT allowed")

    event_start = _parse_dt(args.event_start)
    event_end = _parse_dt(args.event_end)
    outcome_end = _parse_dt(args.outcome_end)
    if args.smoke:
        # 1h smoke inside event window; still allow longer outcomes
        event_start = _parse_dt("2026-08-25T06:00:00Z")
        event_end = _parse_dt("2026-08-25T07:00:00Z")

    manifest = run_wall_to_wall(
        attack_dir=args.attack_dir,
        raw_root=args.raw_root,
        output_dir=args.output_dir,
        event_start=event_start,
        event_end=event_end,
        outcome_end=outcome_end,
        symbols=symbols,
        sample_ms=args.sample_ms,
        seed=args.seed,
    )
    print(json.dumps({k: manifest[k] for k in (
        "event_start_utc", "event_end_utc", "outcome_end_utc",
        "n_entries", "n_entries_reclaim", "n_entries_break", "n_with_target", "n_controls",
    )}, indent=2))
    print(f"wrote {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
