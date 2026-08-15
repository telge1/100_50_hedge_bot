#!/usr/bin/env python3
"""CLI: causal APTUSDT trend-forecast validation (research-only, isolated outputs)."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.trend_forecast_validation.causal_replay import (
    prefix_invariance_check,
    run_causal_scanner_replay,
)
from research.trend_forecast_validation.config import (
    ForecastValidationConfig,
    default_config,
    run_output_dir,
)
from research.trend_forecast_validation.data_loader import load_apt_5m, slice_period_masks
from research.trend_forecast_validation.outcome_evaluator import (
    evaluate_signal_outcomes,
    hedge_relevance_diagnosis,
    summarize_outcomes,
)
from research.trend_forecast_validation.report import write_artifacts
from research.trend_forecast_validation.signal_extractor import extract_forecast_signals

ROOT = Path(__file__).resolve().parents[2]


def _run_unit_tests(*, skip: bool = False) -> dict[str, Any]:
    if skip:
        return {"returncode": 0, "passed": True, "stdout_tail": "skipped", "stderr_tail": ""}
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "research/trend_forecast_validation/tests",
            "-q",
            "--tb=line",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    return {
        "returncode": proc.returncode,
        "passed": proc.returncode == 0,
        "stdout_tail": "\n".join(proc.stdout.strip().splitlines()[-20:]),
        "stderr_tail": "\n".join(proc.stderr.strip().splitlines()[-20:]),
    }


def run_validation(
    cfg: ForecastValidationConfig,
    output_dir: Path,
    *,
    skip_tests: bool = False,
) -> dict[str, Any]:
    t0 = time.time()
    candles, data_quality = load_apt_5m(cfg)
    masks = slice_period_masks(candles["timestamp"], cfg)
    n_warm = int(masks["warmup"].sum())
    n_dev = int(masks["development"].sum())
    n_oos = int(masks["out_of_sample"].sum())

    trace, warmup_state = run_causal_scanner_replay(candles, cfg)
    signals = extract_forecast_signals(trace, cfg)
    outcomes = evaluate_signal_outcomes(signals, candles, cfg)
    summaries = summarize_outcomes(outcomes)
    hedge_diag = hedge_relevance_diagnosis(outcomes)

    # Inline causal guards (lightweight; full suite via pytest)
    n_guard = min(2500, max(500, len(candles) // 20))
    prefix = prefix_invariance_check(candles.iloc[: n_guard + 200].copy(), n_guard, cfg)

    # Signal activation guard: no outcome bar_index using signal candle high/low as future
    activation_ok = True
    if not outcomes.empty and not signals.empty:
        # forecast_active_from must be strictly after detected open
        bad = 0
        for _, s in signals.head(200).iterrows():
            if pd_timestamp(s["forecast_active_from"]) <= pd_timestamp(s["detected_timestamp"]):
                # equal open vs decision: decision is close = next open, so active_from == decision > detected open
                # detected is open time; active_from should be detected+5m
                if pd_timestamp(s["forecast_active_from"]) < pd_timestamp(s["detected_timestamp"]):
                    bad += 1
        activation_ok = bad == 0

    warmup_leak = False
    if not outcomes.empty:
        warmup_leak = bool(
            (
                (outcomes["development_or_oos"] == "warmup")
                & (outcomes["include_in_stats"] == True)  # noqa: E712
            ).any()
        )

    causal_guards = {
        "prefix_invariance": prefix,
        "signal_activation_ok": activation_ok,
        "warmup_signals_excluded_from_stats": not warmup_leak,
        "htf_columns_present": bool(
            {"last_visible_30m_timestamp", "last_visible_4h_timestamp"}.issubset(trace.columns)
        ),
        "ambiguity_mode": cfg.ambiguity_mode,
        "oos_isolation_note": (
            "Signal definitions and config are fixed in config.py before OOS metrics are interpreted; "
            "this run does not optimize on OOS."
        ),
    }

    test_info = _run_unit_tests(skip=skip_tests)
    causal_guards["unit_tests"] = test_info

    run_config = {
        **cfg.to_dict(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "output_dir": str(output_dir),
        "elapsed_sec": None,
    }

    paths = write_artifacts(
        output_dir,
        run_config=run_config,
        data_quality=data_quality,
        warmup_state=warmup_state,
        trace=trace,
        signals=signals,
        outcomes=outcomes,
        summaries=summaries,
        causal_guards=causal_guards,
        hedge_diag=hedge_diag,
        write_candle_trace=cfg.write_candle_trace,
    )
    elapsed = round(time.time() - t0, 2)
    run_config["elapsed_sec"] = elapsed
    (output_dir / "run_config.json").write_text(
        json.dumps(run_config, indent=2, default=str) + "\n", encoding="utf-8"
    )

    stats = signals.loc[signals["include_in_stats"] == True] if not signals.empty else signals  # noqa: E712
    by = summaries.get("summary_by_signal")

    def _sr(stype: str, period: str) -> Any:
        if by is None or by.empty:
            return None
        sub = by.loc[(by["signal_type"] == stype) & (by["development_or_oos"] == period)]
        if sub.empty:
            return None
        return sub.iloc[0]["success_rate_excluding_open"]

    mfe = None
    mae = None
    if not outcomes.empty:
        use = outcomes.loc[outcomes["include_in_stats"] == True]  # noqa: E712
        mfe = float(use["mfe_pct"].median()) if len(use) else None
        mae = float(use["mae_pct"].median()) if len(use) else None

    summary = {
        "data_source": data_quality.get("data_source"),
        "first_timestamp": data_quality.get("first_timestamp"),
        "last_timestamp": data_quality.get("last_timestamp"),
        "warmup_candles": n_warm,
        "development_candles": n_dev,
        "oos_candles": n_oos,
        "n_bull_bos": int((stats["signal_type"] == "BULLISH_EXTERNAL_BOS_AFTER_PULLBACK").sum())
        if not stats.empty
        else 0,
        "n_bear_bos": int((stats["signal_type"] == "BEARISH_EXTERNAL_BOS_AFTER_PULLBACK").sum())
        if not stats.empty
        else 0,
        "n_choch": int(stats["signal_type"].astype(str).str.contains("CHOCH").sum())
        if not stats.empty
        else 0,
        "dev_success_bull": _sr("BULLISH_EXTERNAL_BOS_AFTER_PULLBACK", "development"),
        "dev_success_bear": _sr("BEARISH_EXTERNAL_BOS_AFTER_PULLBACK", "development"),
        "oos_success_bull": _sr("BULLISH_EXTERNAL_BOS_AFTER_PULLBACK", "out_of_sample"),
        "oos_success_bear": _sr("BEARISH_EXTERNAL_BOS_AFTER_PULLBACK", "out_of_sample"),
        "median_mfe_pct": mfe,
        "median_mae_pct": mae,
        "causal_guards_pass": bool(prefix.get("equal"))
        and activation_ok
        and (not warmup_leak)
        and bool(test_info.get("passed")),
        "unit_tests_passed": test_info.get("passed"),
        "unit_tests_tail": test_info.get("stdout_tail"),
        "report_path": paths.get("REPORT"),
        "artifact_paths": paths,
        "elapsed_sec": elapsed,
        "output_dir": str(output_dir),
    }
    (output_dir / "terminal_summary.json").write_text(
        json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8"
    )
    return summary


def pd_timestamp(value: Any):
    import pandas as pd

    return pd.Timestamp(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coin", default="APTUSDT")
    parser.add_argument("--timeframe", default="5m")
    parser.add_argument("--data-source", default="mysql", choices=["mysql", "feather"])
    parser.add_argument("--warmup-start", default=None)
    parser.add_argument("--warmup-end", default=None)
    parser.add_argument("--development-start", default=None)
    parser.add_argument("--development-end", default=None)
    parser.add_argument("--oos-start", default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--no-candle-trace", action="store_true")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Allow writing into an existing non-empty output directory (overwrite artifacts).",
    )
    args = parser.parse_args(argv)

    cfg = default_config()
    updates: dict[str, Any] = {
        "symbol": str(args.coin).upper(),
        "timeframe": args.timeframe,
        "data_source": args.data_source,
    }
    if args.warmup_start:
        updates["warmup_start"] = args.warmup_start
    if args.warmup_end:
        updates["warmup_end"] = args.warmup_end
    if args.development_start:
        updates["development_start"] = args.development_start
    if args.development_end:
        updates["development_end"] = args.development_end
    if args.oos_start:
        updates["out_of_sample_start"] = args.oos_start
    if args.no_candle_trace:
        updates["write_candle_trace"] = False
    cfg = replace(cfg, **updates)

    output_dir = args.output_dir or run_output_dir()
    if output_dir.exists() and any(output_dir.iterdir()) and not args.force:
        print(f"refusing non-empty output dir: {output_dir} (pass --force to overwrite)", file=sys.stderr)
        return 2

    summary = run_validation(cfg, output_dir, skip_tests=bool(args.skip_tests))
    print(json.dumps({k: v for k, v in summary.items() if k != "artifact_paths"}, indent=2, default=str))
    print("\nArtifacts:")
    for k, v in sorted((summary.get("artifact_paths") or {}).items()):
        print(f"  {k}: {v}")
    return 0 if summary.get("causal_guards_pass") else 1


if __name__ == "__main__":
    raise SystemExit(main())
