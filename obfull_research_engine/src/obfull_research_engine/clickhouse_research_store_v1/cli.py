"""CLI for the 60-second Full-OB ClickHouse bronze pilot."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import (
    PILOT_SEGMENT,
    PILOT_SYMBOL,
    PILOT_WINDOW_END,
    PILOT_WINDOW_START,
)
from .helpers import get_clickhouse_client
from .importer import PilotImportError, run_pilot_import
from .validation import validate_pilot_window


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Full-OB continuous 60s ClickHouse pilot import")
    p.add_argument("--segment", type=str, default=PILOT_SEGMENT)
    p.add_argument("--symbol", type=str, default=PILOT_SYMBOL)
    p.add_argument("--window-start", type=str, default=PILOT_WINDOW_START)
    p.add_argument("--window-end", type=str, default=PILOT_WINDOW_END)
    p.add_argument("--validate-only", action="store_true")
    p.add_argument("--json-out", type=str, default="")
    args = p.parse_args(argv)

    try:
        if args.validate_only:
            client = get_clickhouse_client()
            # validate-only still needs expected_rows from a count query itself
            from .helpers import datetime_to_ns, parse_iso_ns
            from . import DATABASE, EVENTS_TABLE

            start_ns = datetime_to_ns(parse_iso_ns(args.window_start))
            end_ns = datetime_to_ns(parse_iso_ns(args.window_end))
            day = parse_iso_ns(args.window_start).strftime("%Y%m%d")
            cnt = int(
                client.query(
                    f"SELECT count() FROM {DATABASE}.{EVENTS_TABLE} FINAL "
                    f"WHERE symbol={{s:String}} AND toYYYYMMDD(event_time)={{d:UInt32}} "
                    f"AND event_time_ns>={{a:UInt64}} AND event_time_ns<{{b:UInt64}}",
                    parameters={"s": args.symbol.upper(), "d": int(day), "a": start_ns, "b": end_ns},
                ).result_rows[0][0]
            )
            result = validate_pilot_window(
                client=client,
                symbol=args.symbol,
                window_start=args.window_start,
                window_end=args.window_end,
                expected_rows=cnt,
                samples=[],
                source_segment_sha256="",
            )
            payload = {"verdict": "VALIDATE_ONLY", "validation": result}
            print(json.dumps(payload, indent=2, default=str))
            return 0 if result.get("ok") else 1

        result = run_pilot_import(
            segment_path=args.segment,
            symbol=args.symbol,
            window_start=args.window_start,
            window_end=args.window_end,
        )
        out = result.to_dict()
        print(json.dumps(out, indent=2, default=str))
        if args.json_out:
            Path(args.json_out).write_text(json.dumps(out, indent=2, default=str) + "\n", encoding="utf-8")
        return 0 if result.verdict.endswith("EXACT") or result.skipped else 1
    except PilotImportError as exc:
        msg = str(exc)
        verdict = msg.split(":", 1)[0].strip() if msg.startswith("STOP_") else "STOP_CLICKHOUSE_ERROR"
        err = {"verdict": verdict, "error": msg}
        print(json.dumps(err, indent=2), file=sys.stderr)
        print(json.dumps(err, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
