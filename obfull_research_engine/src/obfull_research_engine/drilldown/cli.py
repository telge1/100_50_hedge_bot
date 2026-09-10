"""CLI for EVENT_LEVEL_CANDIDATE_DRILLDOWN_V1."""

from __future__ import annotations

import argparse
import json
import resource
import sys
import time
import traceback
from pathlib import Path
from typing import Sequence

import pandas as pd

from ..cli_report import render_coverage_report
from ..interval_coverage import check_interval_coverage
from ..paths import ENGINE_ROOT
from ..timeparse import CliUsageError, parse_utc_z, validate_interval
from .engine import config_sha256, content_hash_bundle, load_config, run_drilldown
from .reporting import write_outputs
from .selector import effective_detection_available_at

EXIT_OK = 0
EXIT_DATA_NOT_COMPLETE = 2
EXIT_USAGE = 64
EXIT_INTERNAL = 70

DEFAULT_CONFIG = ENGINE_ROOT / "config" / "event_drilldown_v1.json"
DEFAULT_OUT = ENGINE_ROOT / "results" / "event_drilldown_v1"
DEFAULT_CANDIDATES = ENGINE_ROOT / "results" / "episode_candidates_v1" / "episode_candidates_v1.parquet"


def _rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="OBFULL event-level candidate drilldown V1")
    p.add_argument("--symbol", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--max-candidates", type=int, default=30)
    p.add_argument("--candidate-type", default=None)
    p.add_argument("--candidate-id", default=None)
    p.add_argument("--check-only", action="store_true")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--output-root", type=Path, default=DEFAULT_OUT)
    p.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES)
    args = p.parse_args(list(argv) if argv is not None else None)

    try:
        start = parse_utc_z(args.start, field="--start")
        end = parse_utc_z(args.end, field="--end")
        validate_interval(start, end)
        symbol = args.symbol.strip().upper()
        if args.max_candidates < 1 or args.max_candidates > 30:
            raise CliUsageError("--max-candidates must be in 1..30 for V1")
    except CliUsageError as exc:
        print(f"CLI usage error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    cfg = load_config(Path(args.config))
    bucket = int(cfg.get("state_bucket_seconds", 1))
    lookback = int(cfg["context_lookback_seconds"])

    # Fail-closed coverage on the requested research interval first
    try:
        report = check_interval_coverage(symbol=symbol, start=start, end=end)
    except Exception as exc:  # noqa: BLE001
        print(f"Internal coverage error: {exc}", file=sys.stderr)
        traceback.print_exc()
        return EXIT_INTERNAL

    if report["verdict"] != "DATA_COMPLETE":
        print(render_coverage_report(report), end="")
        refuse = Path(args.output_root) / "_refused"
        refuse.mkdir(parents=True, exist_ok=True)
        (refuse / "REFUSED_COVERAGE.json").write_text(
            json.dumps(
                {"verdict": "OBFULL_EVENT_LEVEL_DRILLDOWN_V1_BLOCKED_COVERAGE", "coverage": report},
                indent=2,
                default=str,
            )
            + "\n",
            encoding="utf-8",
        )
        return EXIT_DATA_NOT_COMPLETE

    print(render_coverage_report(report), flush=True)
    if args.check_only:
        return EXIT_OK

    if not Path(args.candidates).exists():
        print(f"Candidates file missing: {args.candidates}", file=sys.stderr)
        return EXIT_USAGE
    cand = pd.read_parquet(args.candidates)
    cand = cand[
        (pd.to_datetime(cand["trigger_ts"], utc=True) >= start)
        & (pd.to_datetime(cand["trigger_ts"], utc=True) < end)
    ]
    if cand.empty:
        print("No candidates in interval", file=sys.stderr)
        return EXIT_USAGE

    print(f"Candidates in interval: {len(cand)}", flush=True)
    # Expand coverage to drilldown union [min(context), max(causal_end))
    trig = pd.to_datetime(cand["trigger_ts"], utc=True)
    det = [
        effective_detection_available_at(t, d, bucket_seconds=bucket)
        for t, d in zip(trig, pd.to_datetime(cand["detection_available_at"], utc=True))
    ]
    cov_start = min(trig) - pd.Timedelta(seconds=lookback)
    cov_end = max(det)
    try:
        report_union = check_interval_coverage(
            symbol=symbol,
            start=cov_start.to_pydatetime(),
            end=cov_end.to_pydatetime(),
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Internal coverage error (union): {exc}", file=sys.stderr)
        traceback.print_exc()
        return EXIT_INTERNAL
    if report_union["verdict"] != "DATA_COMPLETE":
        print(render_coverage_report(report_union), end="")
        refuse = Path(args.output_root) / "_refused"
        refuse.mkdir(parents=True, exist_ok=True)
        (refuse / "REFUSED_COVERAGE_UNION.json").write_text(
            json.dumps(
                {
                    "verdict": "OBFULL_EVENT_LEVEL_DRILLDOWN_V1_BLOCKED_COVERAGE",
                    "coverage": report_union,
                    "note": "union context window incomplete",
                },
                indent=2,
                default=str,
            )
            + "\n",
            encoding="utf-8",
        )
        return EXIT_DATA_NOT_COMPLETE

    out_dir = Path(args.output_root)
    cfg_path = Path(args.config)
    cfg_hash = config_sha256(cfg_path)
    print(f"Config sha256: {cfg_hash}", flush=True)
    print("Running drilldown pass 1…", flush=True)
    t0 = time.perf_counter()
    cpu0 = time.process_time()
    rss0 = _rss_mb()
    try:
        result = run_drilldown(
            candidates=cand,
            cfg=cfg,
            symbol=symbol,
            interval_start=start,
            interval_end=end,
            max_candidates=int(args.max_candidates),
            candidate_type=args.candidate_type,
            candidate_id=args.candidate_id,
        )
        h1 = content_hash_bundle(result)
        # Second pass uses segment cache; still full pipeline for idempotency proof
        print("Running drilldown pass 2 (idempotency)…", flush=True)
        result2 = run_drilldown(
            candidates=cand,
            cfg=cfg,
            symbol=symbol,
            interval_start=start,
            interval_end=end,
            max_candidates=int(args.max_candidates),
            candidate_type=args.candidate_type,
            candidate_id=args.candidate_id,
        )
        h2 = content_hash_bundle(result2)
        idem_ok = h1 == h2
    except Exception as exc:  # noqa: BLE001
        print(f"Internal drilldown error: {exc}", file=sys.stderr)
        traceback.print_exc()
        return EXIT_INTERNAL

    resources = {
        "wall_seconds": round(time.perf_counter() - t0, 3),
        "cpu_seconds": round(time.process_time() - cpu0, 3),
        "rss_mb_start": round(rss0, 2),
        "rss_mb_peak_approx": round(max(rss0, _rss_mb()), 2),
        **(result.get("resources") or {}),
        "idempotency_ok": idem_ok,
        "content_hash": h1,
        "content_hash_run2": h2,
    }

    parity_ok = bool((result.get("parity") or {}).get("ok"))
    quality_ok = bool((result.get("quality") or {}).get("ok"))
    blocked = result.get("blocked") or []
    if blocked and all(b.get("status") == "DRILLDOWN_BLOCKED_COVERAGE" for b in blocked) and len(blocked) == len(
        result["selected"]
    ):
        verdict = "OBFULL_EVENT_LEVEL_DRILLDOWN_V1_BLOCKED_COVERAGE"
    elif not parity_ok:
        verdict = "OBFULL_EVENT_LEVEL_DRILLDOWN_V1_BLOCKED_PARITY"
    elif not idem_ok or not quality_ok:
        verdict = "OBFULL_EVENT_LEVEL_DRILLDOWN_V1_FAILED"
    else:
        verdict = "OBFULL_EVENT_LEVEL_DRILLDOWN_V1_PASS_WITH_PROXY_LIMITS"

    write_outputs(
        out_dir=out_dir,
        result=result,
        cfg=cfg,
        config_path=cfg_path,
        config_hash=cfg_hash,
        coverage_report=report_union,
        resources=resources,
        content_hash=h1,
        verdict=verdict,
    )
    print(f"Verdict: {verdict}")
    print(f"Selected: {len(result['selected'])} groups={len(result['groups'])}")
    print(f"Parity mismatches: {(result.get('parity') or {}).get('mismatches')}")
    print(f"Output: {out_dir}")
    if verdict in {
        "OBFULL_EVENT_LEVEL_DRILLDOWN_V1_FAILED",
        "OBFULL_EVENT_LEVEL_DRILLDOWN_V1_BLOCKED_PARITY",
        "OBFULL_EVENT_LEVEL_DRILLDOWN_V1_BLOCKED_COVERAGE",
    }:
        return EXIT_DATA_NOT_COMPLETE if "BLOCKED_COVERAGE" in verdict else EXIT_INTERNAL
    return EXIT_OK
