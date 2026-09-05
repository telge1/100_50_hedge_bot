#!/usr/bin/env python3
"""CLI: Major-wall defended reclaim discovery V1."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from orderbook_analyse.l2_wall_to_wall_discovery.major_defended_reclaim.runner import (
    run_major_defended_reclaim,
)


def _parse_dt(raw: str) -> datetime:
    return datetime.fromisoformat(raw.strip().replace("Z", "+00:00")).astimezone(timezone.utc)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--raw-root", type=Path, default=Path("data/orderbook_raw_shadow/ob200_v3"))
    p.add_argument("--symbols", default="BTCUSDT,DOGEUSDT")
    p.add_argument("--event-start", default="2026-08-25T00:00:00Z")
    p.add_argument("--event-end", default="2026-08-25T07:00:00Z")
    p.add_argument("--outcome-end", default="2026-08-25T11:00:00Z")
    p.add_argument(
        "--output-dir",
        type=Path,
        default=Path("results/l2_wall_to_wall_discovery/major_wall_defended_reclaim_v1"),
    )
    p.add_argument("--sample-ms", type=int, default=250)
    p.add_argument("--smoke", action="store_true", help="1h event window 06-07Z")
    args = p.parse_args(argv)

    symbols = tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip())
    if set(symbols) - {"BTCUSDT", "DOGEUSDT"}:
        raise SystemExit("only BTCUSDT,DOGEUSDT allowed")

    event_start = _parse_dt(args.event_start)
    event_end = _parse_dt(args.event_end)
    outcome_end = _parse_dt(args.outcome_end)
    if args.smoke:
        event_start = _parse_dt("2026-08-25T06:00:00Z")
        event_end = _parse_dt("2026-08-25T07:00:00Z")

    manifest = run_major_defended_reclaim(
        raw_root=args.raw_root,
        output_dir=args.output_dir,
        event_start=event_start,
        event_end=event_end,
        outcome_end=outcome_end,
        symbols=symbols,
        sample_ms=args.sample_ms,
    )
    print(json.dumps({k: manifest.get(k) for k in ("verdict", "n_events", "n_major_candidates", "output_dir")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
