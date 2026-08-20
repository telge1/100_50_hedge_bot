"""CLI for wall-transition collector."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from orderbook_analyse.wall_toxicity_audit.data_access import ensure_utc, parse_utc
from orderbook_analyse.wall_transition_collector.collector import (
    orderbook_span,
    run_catchup,
    run_live,
)
from orderbook_analyse.wall_transition_collector.pidfile import read_pid, pid_alive, cmdline_of
from orderbook_analyse.wall_transition_collector.state import load_state


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Incremental wall-transition collector.")
    p.add_argument("--symbol", default=None)
    p.add_argument("--symbols", default=None, help="Comma-separated (status/audit only)")
    p.add_argument(
        "--mode",
        choices=("audit", "backfill", "live", "catchup-live", "status"),
        required=True,
    )
    p.add_argument("--start", default=None)
    p.add_argument("--end", default=None)
    p.add_argument("--state-dir", type=Path, default=Path("data/wall_transitions/state"))
    p.add_argument("--output-dir", type=Path, default=Path("data/wall_transitions"))
    p.add_argument("--work-dir", type=Path, default=Path("data/wall_transitions/_work"))
    p.add_argument("--pid-dir", type=Path, default=Path("data/wall_transitions/pids"))
    p.add_argument("--log-dir", type=Path, default=Path("data/wall_transitions/logs"))
    p.add_argument("--poll-seconds", type=float, default=120.0)
    p.add_argument("--heartbeat-seconds", type=float, default=60.0)
    p.add_argument("--max-catchup-hours", type=float, default=None)
    p.add_argument("--chunk-hours", type=float, default=2.0)
    p.add_argument("--overwrite", action="store_true", help="Ignored for live history wipe (never deletes).")
    p.add_argument(
        "--collector-token",
        default=None,
        help="Marker for PID validation (optional; also appended in live mode).",
    )
    p.add_argument("--seed-legacy-walls-csv", type=Path, default=None)
    p.add_argument("--log-level", default="INFO")
    return p


def _symbols(args: argparse.Namespace) -> list[str]:
    if args.symbols:
        return [s.strip() for s in str(args.symbols).split(",") if s.strip()]
    if args.symbol:
        return [str(args.symbol)]
    return ["DOGEUSDT", "APTUSDT", "BTCUSDT"]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    symbols = _symbols(args)
    mode = args.mode

    if mode in {"audit", "status"}:
        rows = []
        for sym in symbols:
            st_path = args.state_dir / f"wall_transition_collector_{sym}.json"
            out_csv = args.output_dir / sym / "execution_wall_transitions.csv"
            pid_path = args.pid_dir / f"{sym}.pid"
            pid = read_pid(pid_path)
            running = bool(pid and pid_alive(pid))
            ob_min, ob_max = orderbook_span(sym)
            st = load_state(st_path) if st_path.exists() else {}
            rows.append(
                {
                    "symbol": sym,
                    "mode": mode,
                    "pid": pid,
                    "running": running,
                    "cmdline": cmdline_of(pid) if pid and running else "",
                    "state_path": str(st_path),
                    "output_csv": str(out_csv),
                    "output_exists": out_csv.exists(),
                    "output_size": out_csv.stat().st_size if out_csv.exists() else 0,
                    "last_processed_ts": st.get("last_processed_ts"),
                    "last_written_transition_ts": st.get("last_written_transition_ts"),
                    "last_error": st.get("last_error"),
                    "orderbook_min": ob_min.isoformat() if ob_min else None,
                    "orderbook_max": ob_max.isoformat() if ob_max else None,
                    "audit_ts_utc": datetime.now(timezone.utc).isoformat(),
                }
            )
        print(json.dumps(rows, indent=2, default=str))
        return 0

    if len(symbols) != 1 and mode in {"backfill", "live", "catchup-live"}:
        print("ERROR: backfill/live/catchup-live require a single --symbol", file=sys.stderr)
        return 3
    sym = symbols[0]

    if mode == "backfill":
        if not args.start or not args.end:
            print("ERROR: backfill requires --start and --end", file=sys.stderr)
            return 3
        res = run_catchup(
            symbol=sym,
            output_dir=args.output_dir,
            state_dir=args.state_dir,
            work_dir=args.work_dir,
            start=ensure_utc(parse_utc(str(args.start))),
            end=ensure_utc(parse_utc(str(args.end))),
            chunk_hours=float(args.chunk_hours),
            max_catchup_hours=args.max_catchup_hours,
        )
        print(json.dumps(res, indent=2, default=str))
        return 0 if res.get("status") == "OK" else 2

    if mode == "catchup-live":
        args.pid_dir.mkdir(parents=True, exist_ok=True)
        args.log_dir.mkdir(parents=True, exist_ok=True)
        token = args.collector_token or f"wall_transition_collector:{sym}"
        if f"--collector-token={token}" not in " ".join(sys.argv) and f"--collector-token {token}" not in " ".join(sys.argv):
            sys.argv.append(f"--collector-token={token}")
        from orderbook_analyse.wall_transition_collector.pidfile import acquire_pid_file, release_pid_file

        pid_path = args.pid_dir / f"{sym}.pid"
        acquire_pid_file(pid_path, expected_token=f"wall_transition_collector:{sym}")
        try:
            res = run_catchup(
                symbol=sym,
                output_dir=args.output_dir,
                state_dir=args.state_dir,
                work_dir=args.work_dir,
                start=ensure_utc(parse_utc(str(args.start))) if args.start else None,
                end=None,
                chunk_hours=float(args.chunk_hours),
                max_catchup_hours=args.max_catchup_hours,
                seed_from_legacy_csv=args.seed_legacy_walls_csv,
            )
            print(json.dumps({"catchup": res}, indent=2, default=str))
            if res.get("status") != "OK":
                return 2
            hb = args.state_dir / f"heartbeat_{sym}.json"
            run_live(
                symbol=sym,
                output_dir=args.output_dir,
                state_dir=args.state_dir,
                work_dir=args.work_dir,
                pid_path=pid_path,
                poll_seconds=float(args.poll_seconds),
                chunk_hours=min(float(args.chunk_hours), 1.0),
                heartbeat_path=hb,
            )
            return 0
        finally:
            release_pid_file(pid_path)

    if mode == "live":
        args.pid_dir.mkdir(parents=True, exist_ok=True)
        args.log_dir.mkdir(parents=True, exist_ok=True)
        hb = args.state_dir / f"heartbeat_{sym}.json"
        # Ensure argv identifiable for stop script: print marker
        sys.argv.append(f"--collector-token=wall_transition_collector:{sym}")
        run_live(
            symbol=sym,
            output_dir=args.output_dir,
            state_dir=args.state_dir,
            work_dir=args.work_dir,
            pid_path=args.pid_dir / f"{sym}.pid",
            poll_seconds=float(args.poll_seconds),
            chunk_hours=min(float(args.chunk_hours), 1.0),
            heartbeat_path=hb,
        )
        return 0

    return 3


if __name__ == "__main__":
    raise SystemExit(main())
