"""CLI for 60s Full-OB silver pilot build."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import PILOT_SYMBOL, PILOT_WINDOW_END, PILOT_WINDOW_START
from .silver_builder import SilverBuildError, run_silver_build
from .silver_replay import SilverReplayError


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Full-OB silver 60s pilot build")
    p.add_argument("--symbol", default=PILOT_SYMBOL)
    p.add_argument("--window-start", default=PILOT_WINDOW_START)
    p.add_argument("--window-end", default=PILOT_WINDOW_END)
    p.add_argument("--json-out", default="")
    p.add_argument("--reference-states", default="")
    args = p.parse_args(argv)
    try:
        result = run_silver_build(
            symbol=args.symbol,
            window_start=args.window_start,
            window_end=args.window_end,
            reference_states_path=args.reference_states or None,
        )
        out = result.to_dict()
        print(json.dumps(out, indent=2, default=str))
        if args.json_out:
            Path(args.json_out).write_text(json.dumps(out, indent=2, default=str) + "\n", encoding="utf-8")
        ok = result.verdict.startswith("FULL_OB_CLICKHOUSE_SILVER_60S_") or result.skipped
        return 0 if ok else 1
    except (SilverBuildError, SilverReplayError) as exc:
        msg = str(exc)
        verdict = msg.split(":", 1)[0] if msg.startswith("STOP_") else "STOP_CLICKHOUSE_ERROR"
        err = {"verdict": verdict, "error": msg}
        print(json.dumps(err, indent=2), file=sys.stderr)
        print(json.dumps(err, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
