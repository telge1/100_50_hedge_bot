"""CLI: short-squeeze path / excursion audit (no classical SL scoring)."""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pandas as pd

from research.liquidation_level.liquidation_audit import DEFAULT_FEATHER, _filter_window, load_feather
from research.liquidation_level.liquidation_levels import LiquidationLevelConfig, replay_liquidation_levels
from research.liquidation_level.short_squeeze_path_audit import (
    DEFAULT_PATH_HORIZONS,
    PathAuditConfig,
    run_short_squeeze_path_audit,
)


def _parse_ints(raw: str) -> tuple[int, ...]:
    return tuple(int(x.strip()) for x in str(raw).split(",") if x.strip())


def _jsonable(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, int, bool)):
        return obj
    if isinstance(obj, float):
        if obj != obj or obj in (float("inf"), float("-inf")):
            return None
        return obj
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(x) for x in obj]
    return str(obj)


def _write_csv(path: Path, rows: list[dict[str, Any]] | pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(rows, pd.DataFrame):
        rows.to_csv(path, index=False)
        return
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    pd.DataFrame(rows).to_csv(path, index=False)


def _ans(row: dict[str, Any] | None, *keys: str) -> str:
    if not row:
        return "n/a"
    parts = [f"{k}={row.get(k)}" for k in keys]
    return " | ".join(parts)


def write_readme(path: Path, bundle, feather: Path, symbol: str) -> None:
    f = bundle.summary_full
    oos = bundle.summary_out_of_sample
    r50 = f.get("upper_50x_immediate_reclaim_h50") or {}
    r25 = f.get("upper_25x_immediate_reclaim_h50") or {}
    t50 = f.get("upper_50x_reclaim_T3_h50") or {}
    t25 = f.get("upper_25x_reclaim_T3_h50") or {}
    text = f"""# Short Squeeze Path / Excursion Audit

## Disclaimer

Estimated LuxAlgo-style upper liquidation levels — **not** real exchange liquidations.
No classical stop-loss evaluation as the main conclusion (hedge bot has no classic SL).

Symbol: `{symbol}`  
Feather: `{feather}`  
Candles: `{bundle.meta.get('n_candles')}`  
Period: `{bundle.meta.get('start_timestamp')}` → `{bundle.meta.get('end_timestamp')}`  
IS cut: `{bundle.meta.get('in_sample_cut')}`

## How far does APT still rise after entry?

### Upper 50x immediate reclaim (h=50)
{_ans(r50, 'adverse_median', 'adverse_p75', 'adverse_p90', 'adverse_p95', 'n')}

### Upper 25x immediate reclaim (h=50)
{_ans(r25, 'adverse_median', 'adverse_p75', 'adverse_p90', 'adverse_p95', 'n')}

## How far does it fall afterwards?

Favorable from entry (h=50):
- 50x: {_ans(r50, 'favorable_median', 'favorable_p75', 'favorable_p90', 'favorable_p95')}
- 25x: {_ans(r25, 'favorable_median', 'favorable_p75', 'favorable_p90', 'favorable_p95')}

Drop from the intermediate peak (often larger than entry-based fall):
- 50x: {_ans(r50, 'drop_from_peak_median', 'drop_from_peak_p75', 'drop_from_peak_p90', 'median_drop_over_adverse_ratio')}
- 25x: {_ans(r25, 'drop_from_peak_median', 'drop_from_peak_p75', 'drop_from_peak_p90', 'median_drop_over_adverse_ratio')}

## Timing

- 50x median minutes to peak / to trough: {_ans(r50, 'median_minutes_to_peak', 'median_minutes_to_trough', 'median_minutes_peak_to_trough')}
- 25x: {_ans(r25, 'median_minutes_to_peak', 'median_minutes_to_trough', 'median_minutes_peak_to_trough')}
- Share peak-then-trough 50x/25x: {_ans(r50, 'share_peak_then_trough_pct')} / {_ans(r25, 'share_peak_then_trough_pct')}

## Strong downtrend (T3)

- 50x reclaim+T3: {_ans(t50, 'adverse_median', 'favorable_median', 'drop_from_peak_median', 'share_peak_then_trough_pct', 'n')}
- 25x reclaim+T3: {_ans(t25, 'adverse_median', 'favorable_median', 'drop_from_peak_median', 'share_peak_then_trough_pct', 'n')}

Compare with `trend_comparison.csv` (T3 vs no_T3).

## OOS

OOS 50x immediate reclaim h50: `{_jsonable(oos.get('upper_50x_immediate_reclaim_h50'))}`

## Controls

See `control_comparison.csv`. Empirical only — no significance claim.

## March / 6 March

- March window: `{_jsonable(bundle.summary_march)}`
- 6 March only: `{_jsonable(bundle.summary_march_06)}`

## Hedge-bot usefulness

Useful as **path context** (how far a squeeze may still run, when the peak tends to arrive,
how large the subsequent drop-from-peak often is) — **not** as a standalone entry edge.
No scanner/bot integration from this audit.
"""
    path.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Short squeeze path/excursion audit")
    p.add_argument("--feather-file", type=Path, default=DEFAULT_FEATHER)
    p.add_argument("--symbol", type=str, default="APTUSDT")
    p.add_argument("--start-date", type=str, default=None)
    p.add_argument("--end-date", type=str, default=None)
    p.add_argument("--max-candles", type=int, default=None)
    p.add_argument("--horizons", type=_parse_ints, default=DEFAULT_PATH_HORIZONS)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--skip-controls", action="store_true")
    p.add_argument("--march-only", action="store_true")
    p.add_argument(
        "--output-dir",
        type=Path,
        default=Path("research/liquidation_level/results/APTUSDT_5m_short_squeeze_path"),
    )
    p.add_argument("--progress-every", type=int, default=5000)
    args = p.parse_args(argv)

    t0 = time.perf_counter()
    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    feather = args.feather_file.expanduser().resolve()

    print(f"loading {feather}", flush=True)
    raw = load_feather(feather)
    if args.march_only:
        raw = _filter_window(raw, start_date="2026-01-01", end_date="2026-03-10", max_candles=None)
    else:
        raw = _filter_window(
            raw, start_date=args.start_date, end_date=args.end_date, max_candles=args.max_candles
        )
    print(f"candles={len(raw)}", flush=True)

    print("replaying levels...", flush=True)
    replay = replay_liquidation_levels(
        raw, LiquidationLevelConfig(), progress_every=max(0, int(args.progress_every))
    )

    cfg = PathAuditConfig(
        horizons=tuple(args.horizons),
        seed=int(args.seed),
        skip_controls=bool(args.skip_controls),
    )
    bundle = run_short_squeeze_path_audit(replay, raw, cfg)

    (out / "config.json").write_text(
        json.dumps(
            _jsonable(
                {
                    "symbol": args.symbol,
                    "feather_file": str(feather),
                    "path_config": asdict(cfg),
                    "disclaimer": "Estimated LuxAlgo-style levels; not real exchange liquidations.",
                }
            ),
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print("writing exports...", flush=True)
    _write_csv(out / "path_events.csv", bundle.path_events)
    _write_csv(out / "path_horizon_metrics.csv", bundle.path_horizon_metrics)
    _write_csv(out / "peak_then_trough_events.csv", bundle.peak_then_trough_events)
    _write_csv(out / "path_category_events.csv", bundle.path_category_events)
    _write_csv(out / "path_category_summary.csv", bundle.path_category_summary)
    _write_csv(out / "path_profile_mean.csv", bundle.path_profile_mean)
    _write_csv(out / "path_profile_quantiles.csv", bundle.path_profile_quantiles)
    _write_csv(out / "leverage_comparison.csv", bundle.leverage_comparison)
    _write_csv(out / "trend_comparison.csv", bundle.trend_comparison)
    _write_csv(out / "control_comparison.csv", bundle.control_comparison)

    for name, payload in (
        ("summary_full.json", bundle.summary_full),
        ("summary_in_sample.json", bundle.summary_in_sample),
        ("summary_out_of_sample.json", bundle.summary_out_of_sample),
        ("summary_march.json", bundle.summary_march),
        ("summary_march_06.json", bundle.summary_march_06),
    ):
        (out / name).write_text(
            json.dumps(_jsonable({**payload, "meta": bundle.meta, "elapsed": time.perf_counter() - t0}), indent=2)
            + "\n",
            encoding="utf-8",
        )

    write_readme(out / "README_results.md", bundle, feather, args.symbol)
    print(f"done elapsed={time.perf_counter()-t0:.1f}s output={out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
