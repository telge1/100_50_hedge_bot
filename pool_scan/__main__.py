"""Command line for a historical scan. This module does not run a scan on import.

Example, not executed by the tests::

    python -m pool_scan --symbol ADAUSDT \\
        --start 2026-01-01T00:00:00+00:00 \\
        --end 2026-09-26T00:00:00+00:00 \\
        --near 0.1,0.3,0.5 --touch-tolerance 0 --windows 1,2,4 \\
        --out results/pool_scan/ADAUSDT.jsonl

The loader reads candles and the existing pool history once. It does not
place orders and does not call the phase machine.
"""

from __future__ import annotations

import argparse
import subprocess
from datetime import datetime
from pathlib import Path

from types import SimpleNamespace

from pool_scan.clock import bar_close
from pool_scan.scanner import ScanParams, scan, to_jsonl


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


def _scan_bar(bar, timeframe: str):
    """Market bars store the open on ``ts``. The scanner reads ``open_time``."""
    if hasattr(bar, "open_time"):
        return bar
    open_time = bar.ts
    return SimpleNamespace(
        open_time=open_time,
        close_time=bar_close(open_time, timeframe),
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write a causal 4h pool-path JSONL scan.")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--start", required=True, help="UTC ISO time, inclusive bar open")
    parser.add_argument("--end", required=True, help="UTC ISO time, exclusive bar open")
    parser.add_argument("--near", default="0.1,0.3,0.5", help="Approach bands in percent")
    parser.add_argument("--touch-tolerance", type=float, default=0.0)
    parser.add_argument("--windows", default="1,2,4", help="Outcome windows in closed bars")
    parser.add_argument("--out", required=True, help="JSONL path to create")
    args = parser.parse_args(argv)
    repo = Path(__file__).resolve().parents[1]
    params = ScanParams(
        near_distances_pct=tuple(float(part) for part in args.near.split(",") if part),
        touch_tolerance_pct=args.touch_tolerance,
        outcome_windows=tuple(int(part) for part in args.windows.split(",") if part),
    )
    from pool_pattern.market import load_4h

    start = datetime.fromisoformat(args.start)
    end = datetime.fromisoformat(args.end)
    bars, pools = load_4h(args.symbol, start, end)
    result = scan(
        [_scan_bar(bar, params.timeframe) for bar in bars],
        pools,
        params,
        symbol=args.symbol,
        head=_git(repo, "rev-parse", "HEAD"),
        dirty=bool(_git(repo, "status", "--porcelain")),
        start=args.start,
        end=args.end,
    )
    destination = Path(args.out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(to_jsonl(result), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
