"""CLI: APTUSDT Trade-3 SHORT_REDUCE multi-price staging lab (research-only).

Example:

```bash
PYTHONPATH=. python -m research.backtests.run_apt_t3_short_reduce_price_staging_lab \\
  --coin APTUSDT \\
  --trade-id 3 \\
  --sizes 100:50 \\
  --profiles legacy,linear4,conservative3,small_early4 \\
  --output-dir research/backtests/results/apt_t3_short_reduce_price_staging_lab_20260721
```
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.backtests.apt_baseline_blocker_root_cause import (
    APT_TRADE3_COIN,
    APT_TRADE3_ID,
    APT_TRADE3_START_INDEX,
    check_baseline_parity,
)
from research.backtests.apt_t3_short_reduce_price_staging_lab import (
    DEFAULT_OUT,
    annotate_fills_vs_bounce,
    assert_output_dir_safe,
    bounce_analysis,
    coverage_rows,
    exit_after_stage_rows,
    implementation_diff_scope_md,
    parse_profiles,
    parse_sizes,
    run_lab_backtest,
    stage_fill_rows,
    stage_plan_rows,
    variant_summary_row,
    write_report,
    _purpose,
)
from research.backtests.candle_loader import load_candles_for_symbol
from research.backtests.historical_backtest import normalize_candles
from research.backtests.long_add_multistart_metrics import analyze_trade
from research.backtests.run_inventory_mtm_neg1_policy_audit import FULL_HISTORY_CANDLE_LIMIT

ROOT = Path(__file__).resolve().parents[2]


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                fields.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            out = {}
            for key in fields:
                val = row.get(key)
                out[key] = json.dumps(val, default=str) if isinstance(val, (dict, list)) else val
            writer.writerow(out)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def _git() -> dict[str, Any]:
    status: dict[str, Any] = {"commit": None, "dirty": None}
    try:
        status["commit"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip()
        porcelain = subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=ROOT, text=True
        )
        status["dirty"] = bool(porcelain.strip())
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        status["error"] = str(exc)
    return status


def run_lab(
    *,
    coin: str,
    trade_id: int,
    sizes_spec: str,
    profiles_spec: str,
    output_dir: Path,
    candle_limit: int = FULL_HISTORY_CANDLE_LIMIT,
    start_index: int = APT_TRADE3_START_INDEX,
) -> dict[str, Any]:
    if int(trade_id) != APT_TRADE3_ID or coin.upper() != APT_TRADE3_COIN:
        raise ValueError("This lab is scoped to APTUSDT trade 3 only")

    assert_output_dir_safe(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    sizes = parse_sizes(sizes_spec)
    profiles = parse_profiles(profiles_spec)
    candles = normalize_candles(
        coin.upper(), load_candles_for_symbol(coin.upper(), limit=int(candle_limit))
    )

    summaries: list[dict[str, Any]] = []
    all_plans: list[dict[str, Any]] = []
    all_fills: list[dict[str, Any]] = []
    all_exits: list[dict[str, Any]] = []
    all_coverage: list[dict[str, Any]] = []
    bounce_by_key: dict[str, Any] = {}
    parity: dict[str, Any] = {"skipped": True}
    guards: dict[str, Any] = {
        "invalid_partial_by_variant": {},
        "same_candle_cascade_by_variant": {},
        "legacy_parity": None,
    }

    for cfg in profiles:
        profile = cfg.profile_name
        for size_label, long_n, _short_n in sizes:
            key = f"{profile}:{size_label}"
            print(f"[slps-lab] {key} enabled={cfg.enabled}", flush=True)
            result = run_lab_backtest(
                candles=candles,
                start_index=int(start_index),
                base_notional_usdt=long_n,
                staging_config=cfg,
                coin=coin,
            )
            window = candles[int(start_index) :]
            analysis = analyze_trade(
                result,
                variant=key,
                long_add_pct=0.5,
                target_profit_usdt=0.015,
                window_candles=window,
                valid=True,
                skip_reason="ok",
            )

            if profile == "legacy" and abs(long_n - 100.0) < 1e-9:
                parity = check_baseline_parity(
                    coin=coin, trade_id=trade_id, result=result, analysis=analysis
                )
                guards["legacy_parity"] = parity

            plans = stage_plan_rows(
                profile=profile, size_label=size_label, long_notional=long_n, result=result
            )
            fills = stage_fill_rows(
                profile=profile,
                size_label=size_label,
                long_notional=long_n,
                result=result,
                candles=candles,
                start_index=int(start_index),
            )
            long_adds = [
                f
                for f in (result.fill_log or [])
                if _purpose(f) == "CYCLE_4_LONG_ADD"
            ]
            long_local = int(long_adds[0].get("candle_index") or 0) if long_adds else None
            fills = annotate_fills_vs_bounce(
                fills, candles=candles, start_index=int(start_index), long_add_local=long_local
            )
            exits = exit_after_stage_rows(
                profile=profile,
                size_label=size_label,
                long_notional=long_n,
                result=result,
                candles=candles,
                start_index=int(start_index),
            )
            cov = coverage_rows(exits, fills)
            bounce = bounce_analysis(
                profile=profile,
                size_label=size_label,
                long_notional=long_n,
                result=result,
                candles=candles,
                start_index=int(start_index),
                fill_rows=fills,
                exit_rows=exits,
            )
            summary = variant_summary_row(
                profile=profile,
                size_label=size_label,
                long_notional=long_n,
                bounce=bounce,
                plan_rows=plans,
                fill_rows=fills,
            )

            all_plans.extend(plans)
            all_fills.extend(fills)
            all_exits.extend(exits)
            all_coverage.extend(cov)
            summaries.append(summary)
            bounce_by_key[key] = bounce
            guards["invalid_partial_by_variant"][key] = bounce.get("invalid_partial")
            guards["same_candle_cascade_by_variant"][key] = analysis.get(
                "same_candle_long_add_short_reduce"
            )

    _write_csv(output_dir / "variant_summary.csv", summaries)
    _write_csv(output_dir / "cycle4_stage_plan.csv", all_plans)
    _write_csv(output_dir / "cycle4_stage_fills.csv", all_fills)
    _write_csv(output_dir / "exit_after_each_stage.csv", all_exits)
    _write_csv(output_dir / "coverage_after_each_stage.csv", all_coverage)
    _write_json(output_dir / "bounce_reachability.json", bounce_by_key)
    parity_and_guards = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git": _git(),
        "parity": parity,
        "guards": guards,
        "notes": [
            "P0 legacy must match APT trade-3 baseline parity when size=100:50",
            "enabled=false installs no strategy wrap",
            "No live config changes; research shim only",
        ],
    }
    _write_json(output_dir / "parity_and_guards.json", parity_and_guards)
    (output_dir / "implementation_diff_scope.md").write_text(
        implementation_diff_scope_md(), encoding="utf-8"
    )
    write_report(
        output_dir / "REPORT.md",
        summaries=summaries,
        bounce_by_key=bounce_by_key,
        parity=parity,
    )

    payload = {
        "output_dir": str(output_dir),
        "summaries": summaries,
        "parity": parity,
    }
    _write_json(output_dir / "run_manifest.json", payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coin", default=APT_TRADE3_COIN)
    parser.add_argument("--trade-id", type=int, default=APT_TRADE3_ID)
    parser.add_argument("--sizes", default="100:50")
    parser.add_argument(
        "--profiles",
        default="legacy,linear4,conservative3,small_early4",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--candle-limit", type=int, default=FULL_HISTORY_CANDLE_LIMIT)
    parser.add_argument("--start-index", type=int, default=APT_TRADE3_START_INDEX)
    args = parser.parse_args(argv)
    run_lab(
        coin=args.coin,
        trade_id=args.trade_id,
        sizes_spec=args.sizes,
        profiles_spec=args.profiles,
        output_dir=args.output_dir,
        candle_limit=args.candle_limit,
        start_index=args.start_index,
    )
    print(f"wrote {args.output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
