"""OBFULL RESEARCH CLI entry (fail-closed coverage gate + analyze orchestrator)."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Sequence

from .cli_report import render_coverage_report
from .hour_builder import build_one_hour
from .interval_coverage import check_interval_coverage
from .paths import COVERAGE_CHECKS
from .timeparse import CliUsageError, parse_utc_z, validate_interval

EXIT_OK = 0
EXIT_DATA_NOT_COMPLETE = 2
EXIT_USAGE = 64
EXIT_INTERNAL = 70


def write_coverage_json(report: dict) -> Path:
    COVERAGE_CHECKS.mkdir(parents=True, exist_ok=True)
    sym = report["symbol"]
    start = report["start"].replace(":", "").replace("-", "")
    end = report["end"].replace(":", "").replace("-", "")
    path = COVERAGE_CHECKS / f"{sym}_{start}_{end}_{report['verdict']}.json"
    slim = {
        "symbol": report["symbol"],
        "start": report["start"],
        "end": report["end"],
        "checked_at": report["checked_at"],
        "source_status": report["source_status"],
        "missing_intervals": report["missing_intervals"],
        "checkpoint_identity": report["checkpoint_identity"],
        "replay_epoch": report["replay_epoch"],
        "gap_count": report["gap_count"],
        "resync_count": report["resync_count"],
        "verdict": report["verdict"],
        "analysis_started": report.get("analysis_started", False),
    }
    path.write_text(json.dumps(slim, indent=2) + "\n", encoding="utf-8")
    return path


def build_intersecting_hours(report: dict) -> dict:
    built = []
    for detail in report.get("hour_details") or []:
        if detail.get("status") in {"SOURCE_NOT_CLOSED", "NO_SEGMENT"}:
            continue
        seg = detail.get("segment_path")
        if not seg:
            continue
        hour = datetime.fromisoformat(detail["hour"].replace("Z", "+00:00"))
        man_path = Path(str(seg) + ".manifest.json")
        man_sha = hashlib.sha256(man_path.read_bytes()).hexdigest()
        man = json.loads(man_path.read_text(encoding="utf-8"))
        out = build_one_hour(
            symbol=report["symbol"],
            hour_start=hour,
            segment_path=Path(seg),
            manifest_sha256=man_sha,
            segment_sha256=man.get("segment_sha256"),
            coverage_status="FULL_JOIN",
            force=False,
        )
        if not out.get("ok"):
            return {"ok": False, "error": out, "built": built}
        built.append(
            {
                "hour": detail["hour"],
                "skipped": out.get("skipped"),
                "content_sha256": (out.get("manifest") or {}).get("content_sha256"),
            }
        )
    return {"ok": True, "built": built}


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="OBFULL research CLI — coverage gate, state build, or --analyze orchestrator"
    )
    p.add_argument("--symbol", required=True, help="e.g. BTCUSDT")
    p.add_argument("--start", required=True, help="UTC start inclusive, must end with Z")
    p.add_argument("--end", required=True, help="UTC end exclusive, must end with Z")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--check-only", action="store_true", help="Coverage only; never build states")
    mode.add_argument(
        "--build-states",
        action="store_true",
        help="After DATA_COMPLETE, build intersecting hour state partitions",
    )
    mode.add_argument(
        "--analyze",
        action="store_true",
        help="Run OBFULL_RESEARCH_ANALYZE_ORCHESTRATOR_V1 end-to-end (offline)",
    )
    p.add_argument(
        "--with-avr",
        action="store_true",
        help="With --analyze: add AVR_CONTEXT stage (Footprint AVR episode context adapter)",
    )
    p.add_argument(
        "--with-avr-multiscale",
        action="store_true",
        help="With --analyze --with-avr: add AVR_MULTISCALE_CONTEXT (multi-window footprint)",
    )
    p.add_argument(
        "--coverage-policy",
        choices=["strict", "localized"],
        default="strict",
        help="strict=STRICT_WHOLE_WINDOW (default, legacy); localized=LOCALIZED_EXCLUSION_V1",
    )
    p.add_argument(
        "--focus-ts",
        default=None,
        help="Optional UTC-Z focus timestamp inside [start,end) for eligibility check",
    )
    args = p.parse_args(list(argv) if argv is not None else None)

    # Default mode when none selected: build-states (historical behavior)
    if not args.check_only and not args.build_states and not args.analyze:
        args.build_states = True

    if args.with_avr and not args.analyze:
        print("CLI usage error: --with-avr requires --analyze", file=sys.stderr)
        return EXIT_USAGE
    if args.with_avr_multiscale and not args.analyze:
        print("CLI usage error: --with-avr-multiscale requires --analyze", file=sys.stderr)
        return EXIT_USAGE
    if args.with_avr_multiscale and not args.with_avr:
        print(
            "CLI usage error: --with-avr-multiscale requires --with-avr",
            file=sys.stderr,
        )
        return EXIT_USAGE
    if args.focus_ts and args.coverage_policy != "localized" and not args.analyze:
        # focus meaningful with localized; allow with analyze+localized
        pass

    try:
        start = parse_utc_z(args.start, field="--start")
        end = parse_utc_z(args.end, field="--end")
        validate_interval(start, end)
        symbol = args.symbol.strip().upper()
        if not symbol:
            raise CliUsageError("--symbol must be non-empty")
        focus_ts = None
        if args.focus_ts:
            focus_ts = parse_utc_z(args.focus_ts, field="--focus-ts")
            if not (start <= focus_ts < end):
                raise CliUsageError("--focus-ts must satisfy start <= focus-ts < end")
        coverage_policy = (
            "LOCALIZED_EXCLUSION_V1"
            if args.coverage_policy == "localized"
            else "STRICT_WHOLE_WINDOW"
        )
        if focus_ts is not None and coverage_policy != "LOCALIZED_EXCLUSION_V1":
            raise CliUsageError("--focus-ts requires --coverage-policy localized")
    except CliUsageError as exc:
        print(f"CLI usage error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    if args.analyze:
        from .analyze.orchestrator import run_analyze

        try:
            code, _verdict, _run_dir = run_analyze(
                symbol=symbol,
                start=start,
                end=end,
                with_avr=bool(args.with_avr),
                with_avr_multiscale=bool(args.with_avr_multiscale),
                coverage_policy=coverage_policy,
                focus_ts=focus_ts,
            )
            return code
        except Exception as exc:  # noqa: BLE001
            print(f"Internal analyze error: {exc}", file=sys.stderr)
            traceback.print_exc()
            return EXIT_INTERNAL

    try:
        if coverage_policy == "LOCALIZED_EXCLUSION_V1":
            from .localized_coverage.check import check_localized_coverage
            from .localized_coverage.report import render_localized_coverage

            report = check_localized_coverage(
                symbol=symbol, start=start, end=end, focus_ts=focus_ts
            )
            print(render_localized_coverage(report), end="")
            # Write JSON under coverage_checks for check-only
            from .paths import COVERAGE_CHECKS
            import json as _json

            COVERAGE_CHECKS.mkdir(parents=True, exist_ok=True)
            outp = (
                COVERAGE_CHECKS
                / f"{symbol}_{start.strftime('%Y%m%dT%H%M%SZ')}_{end.strftime('%Y%m%dT%H%M%SZ')}_localized.json"
            )
            outp.write_text(_json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
            if report.get("verdict") == "DATA_NOT_COMPLETE" or (
                focus_ts is not None and not report.get("analysis_ok")
            ):
                return EXIT_DATA_NOT_COMPLETE
            if args.check_only:
                return EXIT_OK
            # build-states with localized: only if usable spans exist
            if report.get("verdict") == "DATA_NOT_COMPLETE":
                return EXIT_DATA_NOT_COMPLETE
            # For build-states under localized, require at least one usable span;
            # reuse intersecting hours from strict hour_details when possible via shadow
            print(
                "NOTE: --build-states with localized currently builds intersecting hours "
                "only when STRICT shadow hours exist; prefer --analyze --coverage-policy localized.",
                flush=True,
            )
            # Fall through to strict build path using interval coverage for hour list
            report = check_interval_coverage(symbol=symbol, start=start, end=end)
        else:
            report = check_interval_coverage(symbol=symbol, start=start, end=end)
    except Exception as exc:  # noqa: BLE001
        print(f"Internal coverage error: {exc}", file=sys.stderr)
        traceback.print_exc()
        return EXIT_INTERNAL

    if report["verdict"] != "DATA_COMPLETE":
        report["analysis_started"] = False
        print(render_coverage_report(report), end="")
        write_coverage_json(report)
        return EXIT_DATA_NOT_COMPLETE

    if args.check_only:
        report["analysis_started"] = False
        print(render_coverage_report(report), end="")
        write_coverage_json(report)
        return EXIT_OK

    report["analysis_started"] = True
    print(render_coverage_report(report), end="")
    write_coverage_json(report)
    try:
        result = build_intersecting_hours(report)
    except Exception as exc:  # noqa: BLE001
        print(f"Internal build/replay error: {exc}", file=sys.stderr)
        traceback.print_exc()
        return EXIT_INTERNAL
    if not result.get("ok"):
        print(f"State build failed: {result}", file=sys.stderr)
        return EXIT_INTERNAL
    print(f"State partitions: {json.dumps(result.get('built'), default=str)}")
    return EXIT_OK
