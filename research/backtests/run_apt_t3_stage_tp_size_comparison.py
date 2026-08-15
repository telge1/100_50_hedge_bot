"""CLI: APTUSDT Trade-3 staged/split Second-Leg size comparison (research-only).

Example:

```bash
PYTHONPATH=. python -m research.backtests.run_apt_t3_stage_tp_size_comparison \\
  --coin APTUSDT \\
  --trade-id 3 \\
  --sizes 100:50,500:250,1000:500 \\
  --output-dir research/backtests/results/apt_t3_stage_tp_size_comparison_20260721
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

from research.backtests.apt_t3_stage_tp_size_comparison import (
    APT_TRADE3_COIN,
    APT_TRADE3_ID,
    APT_TRADE3_START_INDEX,
    DEFAULT_OUT,
    PROTECTED,
    assert_output_dir_safe,
    bounce_reachability_analysis,
    classify_cycle4_split_outcome,
    coverage_after_each_stage_rows,
    exit_after_each_stage_rows,
    parse_sizes,
    run_apt_t3_at_size,
    size_summary_row,
    stage_attempt_rows_for_cycle,
    write_code_path_map,
    write_report,
)
from research.backtests.apt_baseline_blocker_root_cause import (
    build_cycle_snapshots,
    build_event_timeline,
    check_baseline_parity,
    pnl_reconciliation_rows,
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
        status["commit"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        porcelain = subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True)
        status["dirty"] = bool(porcelain.strip())
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        status["error"] = str(exc)
    return status


def run_audit(
    *,
    coin: str,
    trade_id: int,
    sizes_spec: str,
    output_dir: Path,
    candle_limit: int = FULL_HISTORY_CANDLE_LIMIT,
    start_index: int = APT_TRADE3_START_INDEX,
) -> dict[str, Any]:
    if int(trade_id) != APT_TRADE3_ID or coin.upper() != APT_TRADE3_COIN:
        raise ValueError("This audit is scoped to APTUSDT trade 3 only")

    assert_output_dir_safe(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    sizes = parse_sizes(sizes_spec)
    candles = normalize_candles(coin.upper(), load_candles_for_symbol(coin.upper(), limit=int(candle_limit)))

    summaries: list[dict[str, Any]] = []
    split_by_variant: dict[str, dict[str, Any]] = {}
    bounce_by_variant: dict[str, dict[str, Any]] = {}
    all_c4_attempts: list[dict[str, Any]] = []
    all_c4_fills: list[dict[str, Any]] = []
    all_exit_rows: list[dict[str, Any]] = []
    all_coverage: list[dict[str, Any]] = []
    parity: dict[str, Any] = {"skipped": True}

    for label, long_n, short_n in sizes:
        print(f"[apt-t3-stage] {label} long={long_n} short={short_n}", flush=True)
        result = run_apt_t3_at_size(
            candles=candles,
            start_index=int(start_index),
            base_notional_usdt=long_n,
            coin=coin,
        )
        window = candles[int(start_index) :]
        analysis = analyze_trade(
            result,
            variant=label,
            long_add_pct=0.5,
            target_profit_usdt=0.015,
            window_candles=window,
            valid=True,
            skip_reason="ok",
        )

        attempts_all = []
        for cycle in (1, 2, 3, 4, 5, 6, 7, 8):
            attempts_all.extend(
                stage_attempt_rows_for_cycle(
                    variant=label, long_notional=long_n, result=result, cycle=cycle
                )
            )
        attempts_c4 = [a for a in attempts_all if int(a.get("cycle") or 0) == 4]
        all_c4_attempts.extend(attempts_c4)

        split_outcome = classify_cycle4_split_outcome(attempts_c4, result)
        split_by_variant[label] = split_outcome

        bounce = bounce_reachability_analysis(
            variant=label,
            long_notional=long_n,
            result=result,
            candles=candles,
            start_index=int(start_index),
            cycle=4,
        )
        bounce_by_variant[label] = bounce

        exit_rows = exit_after_each_stage_rows(
            variant=label,
            long_notional=long_n,
            result=result,
            candles=candles,
            start_index=int(start_index),
            focus_cycles=(3, 4),
        )
        all_exit_rows.extend(exit_rows)
        all_coverage.extend(
            coverage_after_each_stage_rows(
                variant=label, long_notional=long_n, result=result, focus_cycles=(3, 4)
            )
        )

        # Cycle-4 fill rows for stage_fills CSV
        for fill in result.fill_log or []:
            purpose = str(fill.get("purpose") or "")
            if purpose.startswith("CYCLE_4_"):
                all_c4_fills.append(
                    {
                        "variant": label,
                        "long_notional_usdt": long_n,
                        "purpose": purpose,
                        "timestamp": fill.get("timestamp"),
                        "local_candle": fill.get("candle_index"),
                        "qty": fill.get("qty"),
                        "fill_price": fill.get("fill_price"),
                        "closed_pnl": fill.get("closed_pnl") or fill.get("confirmed_closed_pnl"),
                        "long_qty_after": fill.get("long_qty_after"),
                        "short_qty_after": fill.get("short_qty_after"),
                    }
                )

        summaries.append(
            size_summary_row(
                variant=label,
                long_n=long_n,
                short_n=short_n,
                result=result,
                analysis=analysis,
                split_outcome=split_outcome,
                bounce=bounce,
                attempts_c4=attempts_c4,
            )
        )

        # Per-size timelines / snapshots for 100 and 1000
        if label in {"S100", "S1000"}:
            suffix = "100_50" if label == "S100" else "1000_500"
            _write_csv(
                output_dir / f"event_timeline_{suffix}.csv",
                build_event_timeline(result=result, start_index=int(start_index)),
            )
            _write_csv(
                output_dir / f"cycle_snapshots_{suffix}.csv",
                build_cycle_snapshots(result=result, candles=candles, start_index=int(start_index)),
            )

        if label == "S100":
            parity = check_baseline_parity(
                coin=coin, trade_id=trade_id, result=result, analysis=analysis
            )
            _write_csv(output_dir / "pnl_reconciliation_s100.csv", pnl_reconciliation_rows(result))

    guards = {
        "s100_parity_ok": bool(parity.get("ok")),
        "s100_parity": parity,
        "start_index": int(start_index),
        "trade_id": int(trade_id),
        "coin": coin.upper(),
        "policies_disabled": [
            "inventory_mtm_freeze",
            "safe_cycle_boundary",
            "recovery_reentry",
            "exit_rebuild_policy",
        ],
        "invalid_partial_all_zero": all(int(s.get("invalid_partial_cycle") or 0) == 0 for s in summaries),
        "undercoverage": {s["variant"]: s.get("undercoverage") for s in summaries},
        "causal_fill_model": "conservative",
        "no_same_candle_new_order_fill": True,
    }

    write_code_path_map(output_dir / "code_path_map.md")
    write_report(
        output_dir / "REPORT.md",
        summaries=summaries,
        split_by_variant=split_by_variant,
        bounce_by_variant=bounce_by_variant,
        parity=parity,
    )
    _write_csv(output_dir / "size_comparison_summary.csv", summaries)
    _write_csv(output_dir / "cycle4_stage_attempts.csv", all_c4_attempts)
    _write_csv(output_dir / "cycle4_stage_fills.csv", all_c4_fills)
    _write_csv(output_dir / "exit_after_each_stage.csv", all_exit_rows)
    _write_csv(output_dir / "coverage_after_each_stage.csv", all_coverage)
    _write_json(output_dir / "bounce_reachability_comparison.json", bounce_by_variant)
    _write_json(output_dir / "parity_and_guards.json", guards)
    _write_json(
        output_dir / "applied_params.json",
        {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "sizes": [{"label": a, "long": b, "short": c} for a, b, c in sizes],
            "start_index": start_index,
            "candle_limit": candle_limit,
            "git": _git(),
            "split_outcomes": split_by_variant,
        },
    )

    if not parity.get("skipped") and not parity.get("ok"):
        raise RuntimeError(f"S100 baseline parity failed: {parity}")

    return {"ok": True, "summaries": summaries, "guards": guards, "output_dir": str(output_dir)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coin", default=APT_TRADE3_COIN)
    parser.add_argument("--trade-id", type=int, default=APT_TRADE3_ID)
    parser.add_argument("--sizes", default="100:50,500:250,1000:500")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--candle-limit", type=int, default=FULL_HISTORY_CANDLE_LIMIT)
    parser.add_argument("--start-index", type=int, default=APT_TRADE3_START_INDEX)
    args = parser.parse_args()

    if args.output_dir.resolve() in {p.resolve() for p in PROTECTED}:
        raise SystemExit(f"Refusing protected dir: {args.output_dir}")

    payload = run_audit(
        coin=args.coin,
        trade_id=int(args.trade_id),
        sizes_spec=args.sizes,
        output_dir=args.output_dir,
        candle_limit=int(args.candle_limit),
        start_index=int(args.start_index),
    )
    print(json.dumps(payload, indent=2, default=str))


if __name__ == "__main__":
    main()
