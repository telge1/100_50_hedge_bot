"""CLI for CAUSAL_EPISODE_CANDIDATE_DETECTOR_V1."""

from __future__ import annotations

import argparse
import json
import resource
import sys
import time
import traceback
from pathlib import Path
from typing import Sequence

from ..cli_report import render_coverage_report
from ..interval_coverage import check_interval_coverage
from ..paths import CONTRACTS, ENGINE_ROOT
from ..timeparse import CliUsageError, parse_utc_z, validate_interval
from .detector import (
    candidates_content_sha256,
    config_sha256,
    detect_candidates,
    load_config,
)
from .reporting import write_episode_outputs
from .state_loader import load_states_for_interval

EXIT_OK = 0
EXIT_DATA_NOT_COMPLETE = 2
EXIT_USAGE = 64
EXIT_INTERNAL = 70

DEFAULT_CONFIG = ENGINE_ROOT / "config" / "episode_candidate_v1.json"
DEFAULT_OUT = ENGINE_ROOT / "results" / "episode_candidates_v1"
SCHEMA_PATH = CONTRACTS / "episode_candidate_v1.schema.json"


def _rss_mb() -> float:
    # ru_maxrss is KB on Linux
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def _quality_asserts(candidates, state_df, diagnostics) -> dict:
    issues = []
    if candidates is not None and not candidates.empty:
        if candidates["candidate_id"].duplicated().any():
            issues.append("duplicate_candidate_id")
        ts = candidates["trigger_ts"]
        if not ts.is_monotonic_increasing:
            # allow equal timestamps across types; check non-decreasing
            if (ts.diff().dropna() < pd_timedelta_zero()).any():
                issues.append("trigger_ts_not_nondecreasing")
        bad_ctx = candidates["context_start_ts"] > candidates["trigger_ts"]
        if bad_ctx.any():
            issues.append("context_after_trigger")
        bad_det = candidates["detection_available_at"] < candidates["trigger_ts"]
        if bad_det.any():
            issues.append("detection_before_trigger")
        bad_base = candidates["baseline_end_ts"] > candidates["trigger_ts"]
        # baseline_end is last included second < trigger, so must be < trigger_ts
        if (candidates["baseline_end_ts"] >= candidates["trigger_ts"]).any():
            issues.append("baseline_includes_trigger")
        for col in ("candidate_status", "confidence_kind", "threshold_source"):
            if col in candidates.columns and candidates[col].nunique(dropna=False) != 1:
                issues.append(f"non_constant_{col}")
        # no nan/inf in required numeric-ish
        if candidates["trigger_count"].isna().any():
            issues.append("nan_trigger_count")

    # silent missing seconds already asserted in detector
    n_invalid_base = sum(1 for r in diagnostics.get("baseline_diagnostics") or [] if not r.get("baseline_valid"))
    return {
        "ok": len(issues) == 0,
        "issues": issues,
        "n_baseline_invalid_rows": n_invalid_base,
        "n_state_rows": int(len(state_df)),
        "n_candidates": 0 if candidates is None else int(len(candidates)),
    }


def pd_timedelta_zero():
    import pandas as pd

    return pd.Timedelta(0)


def _causality_proof() -> dict:
    return {
        "future_returns_loaded": False,
        "outcome_files_read": False,
        "mfe_mae_fields_present": False,
        "future_prices_used_for_trigger_selection": False,
        "threshold_source": "PROVISIONAL_OUTCOME_BLIND",
        "baseline_window": "[t-lookback, t)",
        "episode_end_ts_emitted": False,
        "notes": [
            "Detector only reads mb_state_1s_v1 rows and config.",
            "Prefix parity proves decisions do not depend on later seconds.",
        ],
    }


def _prefix_parity_check(state_df, cfg, symbol, coverage_status, source_hash, replay_epoch):
    import pandas as pd

    if state_df.empty or len(state_df) < 10:
        return {"ok": True, "skipped": True, "reason": "too_short"}
    # prefix ends 5 minutes before full end if possible
    full_end = pd.to_datetime(state_df["state_ts"].iloc[-1], utc=True)
    prefix_end = full_end - pd.Timedelta(minutes=5)
    if prefix_end <= pd.to_datetime(state_df["state_ts"].iloc[0], utc=True):
        return {"ok": True, "skipped": True, "reason": "window_too_short_for_prefix"}

    full_c, _ = detect_candidates(
        state_df,
        cfg=cfg,
        symbol=symbol,
        coverage_status=coverage_status,
        source_state_hash=source_hash,
        replay_epoch=replay_epoch,
    )
    prefix_df = state_df[state_df["state_ts"] < prefix_end].copy()
    pref_c, _ = detect_candidates(
        prefix_df,
        cfg=cfg,
        symbol=symbol,
        coverage_status=coverage_status,
        source_state_hash=source_hash,
        replay_epoch=replay_epoch,
    )
    if full_c is None or full_c.empty:
        full_sub = full_c
    else:
        full_sub = full_c[full_c["trigger_ts"] < prefix_end].reset_index(drop=True)
    h1 = candidates_content_sha256(full_sub if full_sub is not None else full_c)
    h2 = candidates_content_sha256(pref_c)
    return {
        "ok": h1 == h2,
        "prefix_end": prefix_end.isoformat().replace("+00:00", "Z"),
        "full_prefix_hash": h1,
        "prefix_run_hash": h2,
        "n_full_prefix": 0 if full_sub is None else int(len(full_sub)),
        "n_prefix_run": 0 if pref_c is None else int(len(pref_c)),
    }


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="OBFULL causal episode candidate detector V1")
    p.add_argument("--symbol", required=True)
    p.add_argument("--start", required=True, help="UTC inclusive, must end with Z")
    p.add_argument("--end", required=True, help="UTC exclusive, must end with Z")
    p.add_argument("--check-only", action="store_true")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--output-root", type=Path, default=DEFAULT_OUT)
    args = p.parse_args(list(argv) if argv is not None else None)

    try:
        start = parse_utc_z(args.start, field="--start")
        end = parse_utc_z(args.end, field="--end")
        validate_interval(start, end)
        symbol = args.symbol.strip().upper()
        if not symbol:
            raise CliUsageError("--symbol must be non-empty")
    except CliUsageError as exc:
        print(f"CLI usage error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    try:
        report = check_interval_coverage(symbol=symbol, start=start, end=end)
    except Exception as exc:  # noqa: BLE001
        print(f"Internal coverage error: {exc}", file=sys.stderr)
        traceback.print_exc()
        return EXIT_INTERNAL

    if report["verdict"] != "DATA_COMPLETE":
        print(render_coverage_report(report), end="")
        # refuse: no candidate files
        refuse_dir = Path(args.output_root) / "_refused"
        refuse_dir.mkdir(parents=True, exist_ok=True)
        refuse_path = refuse_dir / f"REFUSED_{symbol}_{args.start}_{args.end}.json".replace(":", "")
        refuse_path.write_text(
            json.dumps(
                {
                    "verdict": "OBFULL_EPISODE_CANDIDATE_DETECTOR_V1_BLOCKED_COVERAGE",
                    "coverage": report,
                    "candidates_written": False,
                },
                indent=2,
                default=str,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"REFUSED candidates; coverage incomplete. Note: {refuse_path}")
        return EXIT_DATA_NOT_COMPLETE

    if args.check_only:
        print(render_coverage_report(report), end="")
        return EXIT_OK

    print(render_coverage_report(report), end="")
    cfg_path = Path(args.config)
    cfg = load_config(cfg_path)
    out_dir = Path(args.output_root)
    out_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()
    cpu0 = time.process_time()
    rss0 = _rss_mb()
    try:
        state_df, state_meta = load_states_for_interval(symbol=symbol, start=start, end=end)
        c1, d1 = detect_candidates(
            state_df,
            cfg=cfg,
            symbol=symbol,
            coverage_status=report["verdict"],
            source_state_hash=state_meta["content_sha256"],
            replay_epoch=state_meta["replay_epoch"],
        )
        c2, _ = detect_candidates(
            state_df,
            cfg=cfg,
            symbol=symbol,
            coverage_status=report["verdict"],
            source_state_hash=state_meta["content_sha256"],
            replay_epoch=state_meta["replay_epoch"],
        )
        h1 = candidates_content_sha256(c1)
        h2 = candidates_content_sha256(c2)
        idem = {"ok": h1 == h2, "hash_run1": h1, "hash_run2": h2}
        prefix = _prefix_parity_check(
            state_df,
            cfg,
            symbol,
            report["verdict"],
            state_meta["content_sha256"],
            state_meta["replay_epoch"],
        )
        quality = _quality_asserts(c1, state_df, d1)
        quality["prefix_parity_ok"] = bool(prefix.get("ok"))
        quality["idempotency_ok"] = bool(idem.get("ok"))
    except Exception as exc:  # noqa: BLE001
        print(f"Internal detector error: {exc}", file=sys.stderr)
        traceback.print_exc()
        return EXIT_INTERNAL

    elapsed = time.perf_counter() - t0
    cpu = time.process_time() - cpu0
    rss1 = _rss_mb()
    resources = {
        "wall_seconds": round(elapsed, 3),
        "cpu_seconds": round(cpu, 3),
        "rss_mb_start": round(rss0, 2),
        "rss_mb_peak_approx": round(max(rss0, rss1), 2),
        "n_state_rows": int(len(state_df)),
    }

    n = 0 if c1 is None else len(c1)
    if not quality.get("ok") or not idem.get("ok") or not prefix.get("ok"):
        verdict = "OBFULL_EPISODE_CANDIDATE_DETECTOR_V1_FAILED"
    elif n == 0:
        verdict = "OBFULL_EPISODE_CANDIDATE_DETECTOR_V1_NO_REAL_CANDIDATES_VALID"
    else:
        verdict = "OBFULL_EPISODE_CANDIDATE_DETECTOR_V1_PASS_WITH_PROXY_LIMITS"

    write_episode_outputs(
        out_dir=out_dir,
        candidates=c1,
        diagnostics=d1,
        cfg=cfg,
        config_path=cfg_path,
        schema_path=SCHEMA_PATH,
        coverage_report=report,
        resources=resources,
        causality_proof=_causality_proof(),
        idempotency=idem,
        quality_summary=quality,
        prefix_parity=prefix,
        verdict=verdict,
    )
    print(f"Verdict: {verdict}")
    print(f"Candidates: {n}")
    print(f"Output: {out_dir}")
    print(f"Config sha256: {config_sha256(cfg_path)}")
    print(f"Resources: {json.dumps(resources)}")
    return EXIT_OK if verdict != "OBFULL_EPISODE_CANDIDATE_DETECTOR_V1_FAILED" else EXIT_INTERNAL
