"""CLI for mp_edge_event_batch_v1."""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

from .params import (
    COST_SCENARIOS_BPS,
    EPISODE_COOLDOWNS_S,
    MIN_EPOCH_DURATION_S,
    SILVER_DATABASE_DEFAULT,
    build_batch_params,
)
from .report import choose_verdict, write_batch_report
from .runner import run_batch


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mp_edge_event_batch_v1")
    p.add_argument("--manifest", default="", help="optional prebuilt batch_windows.csv")
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--output-dir", required=True)
    p.add_argument("--check-only", action="store_true")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--window-id", default=None)
    p.add_argument("--max-windows", type=int, default=None)
    p.add_argument(
        "--cost-scenarios-bps",
        default=",".join(str(int(x)) for x in COST_SCENARIOS_BPS),
    )
    p.add_argument(
        "--episode-cooldowns-s",
        default=",".join(str(x) for x in EPISODE_COOLDOWNS_S),
    )
    p.add_argument("--runtime-limit-s", type=float, default=3600.0)
    p.add_argument("--silver-database", default=SILVER_DATABASE_DEFAULT)
    p.add_argument("--min-epoch-duration-s", type=float, default=MIN_EPOCH_DURATION_S)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    params = build_batch_params(
        symbol=args.symbol,
        output_dir=args.output_dir,
        silver_database=args.silver_database,
        cost_scenarios_bps=args.cost_scenarios_bps,
        episode_cooldowns_s=args.episode_cooldowns_s,
        runtime_limit_s=args.runtime_limit_s,
        check_only=bool(args.check_only),
        resume=bool(args.resume),
        window_id=args.window_id,
        max_windows=args.max_windows,
        min_epoch_duration_s=args.min_epoch_duration_s,
    )
    try:
        result = run_batch(params)
    except Exception as exc:  # noqa: BLE001
        Path(params.output_dir).mkdir(parents=True, exist_ok=True)
        (Path(params.output_dir) / "batch.log").write_text(
            traceback.format_exc(), encoding="utf-8"
        )
        print(f"VERDICT=MP_BATCH_BLOCKED_IMPLEMENTATION")
        print(f"ERROR={exc}")
        return 2

    if params.check_only:
        print("CHECK_ONLY_OK")
        print(f"WINDOWS_INCLUDED={result['windows_included']}")
        print(f"SEMANTICS_HASH={result['semantics_hash']}")
        for wid in result.get("included_ids") or []:
            print(f"WINDOW={wid}")
        return 0

    n_events = len(result.get("events") or [])
    verdict = choose_verdict(
        complete=int(result.get("windows_complete") or 0),
        failed=int(result.get("windows_failed") or 0),
        included=int(result.get("windows_included") or 0),
        n_events=n_events,
        aborted=bool(result.get("aborted")),
    )
    write_batch_report(Path(params.output_dir) / "BATCH_REPORT.md", result=result, verdict=verdict)
    print(f"VERDICT={verdict}")
    print(f"OUTPUT={params.output_dir}")
    print(f"EVENTS={n_events}")
    print(f"COMPLETE={result.get('windows_complete')}")
    print(f"FAILED={result.get('windows_failed')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
