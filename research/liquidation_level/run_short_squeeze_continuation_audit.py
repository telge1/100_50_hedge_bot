"""CLI: short squeeze continuation audit after upper liquidation-level sweeps."""

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
from research.liquidation_level.short_squeeze_continuation_audit import (
    ShortSqueezeConfig,
    events_to_dataframe,
    run_short_squeeze_continuation_audit,
)


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


def _fmt_hit(summary: dict, key: str) -> str:
    row = summary.get("key_hit_rates_h12", {}).get(key)
    if not row:
        return "n/a"
    return f"hit={row.get('hit_rate_pct')} n={row.get('n')} mfe/mae in horizon table"


def write_readme(path: Path, bundle, feather: Path, symbol: str) -> None:
    full = bundle.summary_full
    oos = bundle.summary_out_of_sample
    best = full.get("best_tp_sl_first_signal_only")
    ctrl = full.get("control_rows", [])
    text = f"""# Short Squeeze Continuation Audit

## Disclaimer

Upper liquidation levels are **estimated** LuxAlgo-style levels.
They are **not** real exchange liquidation feeds.

Symbol: `{symbol}`  
Feather: `{feather}`  
Candles: `{bundle.meta.get('n_candles')}`  
Period: `{bundle.meta.get('start_timestamp')}` → `{bundle.meta.get('end_timestamp')}`  
IS cut index: `{bundle.meta.get('in_sample_cut')}`

Entry is never on the sweep candle. For reclaim trades, entry is the open after
the reclaim becomes known.

Trend models T1/T2/T3 are transparent EMA/structure filters on closed 15m/30m bars.
Trend-state-machine T4 was omitted (not reproducibly available without touching protected modules).

## 1–2. Does price fall after upper 50x / 25x sweeps?

Event counts (full): `{json.dumps(full.get('event_counts'), indent=2)}`

Key h12 hit rates (full): see `variant_comparison.csv` and `summary_full.json`.

## 3. Difference with vs without bearish reclaim

Reclaim counts: `{json.dumps(full.get('reclaim_counts'), indent=2)}`

Compare groups:
- `upper_50x__no_reclaim_within_3`
- `upper_50x__immediate_reclaim`
- `upper_50x__reclaim_within_3`

## 4–5. Stronger in downtrend? Which trend model?

Compare `__T1` / `__T2` / `__T3` variants in `variant_comparison.csv`.
Prefer the model with the most stable Full→OOS hit-rate/MFE profile, not the flashiest IS number.

## 6. OOS confirmation?

Compare `summary_out_of_sample.json` against full for the same keys.
OOS snapshot keys: `{list((oos.get('key_hit_rates_h12') or {}).keys())[:6]}...`

## 7. vs matched controls?

`control_comparison.csv` (month/hour/range matched, non-sweep). Bootstrap CIs are
descriptive only — **no significance claim**.

Control rows (full): `{json.dumps(_jsonable(ctrl), indent=2)}`

## 8. MAE before continuation

See `first_touch_outcomes.csv` (`mean_adverse_before_favorable_pct`) and horizon MAE columns.

## 9. After 0.12% costs?

Best first-signal-only TP/SL (full): `{json.dumps(_jsonable(best), indent=2)}`

If mean_net ≤ 0 or PF_net < 1, there is no clear tradable edge after costs.

## 10. 6 March 2026 events

March window summary: `{json.dumps(_jsonable(bundle.march_summary), indent=2)}`

See `march_downtrend_events.csv` (is_march_06 flag).

## Integration

No scanner / bot / live integration from this audit.
"""
    path.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Short squeeze continuation audit")
    p.add_argument("--feather-file", type=Path, default=DEFAULT_FEATHER)
    p.add_argument("--symbol", type=str, default="APTUSDT")
    p.add_argument("--start-date", type=str, default=None)
    p.add_argument("--end-date", type=str, default=None)
    p.add_argument("--max-candles", type=int, default=None)
    p.add_argument("--skip-tp-sl", action="store_true")
    p.add_argument("--skip-bootstrap", action="store_true")
    p.add_argument("--march-only", action="store_true")
    p.add_argument("--bootstrap-resamples", type=int, default=1000)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument(
        "--output-dir",
        type=Path,
        default=Path("research/liquidation_level/results/APTUSDT_5m_short_squeeze_continuation"),
    )
    p.add_argument("--progress-every", type=int, default=5000)
    args = p.parse_args(argv)

    t0 = time.perf_counter()
    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    feather = args.feather_file.expanduser().resolve()

    print(f"loading {feather}", flush=True)
    raw = load_feather(feather)
    # Warm-up for March-only: include 2 months before window
    if args.march_only:
        raw = _filter_window(raw, start_date="2026-01-01", end_date="2026-03-10", max_candles=None)
    else:
        raw = _filter_window(
            raw, start_date=args.start_date, end_date=args.end_date, max_candles=args.max_candles
        )
    print(f"candles={len(raw)}", flush=True)

    print("replaying liquidation levels...", flush=True)
    replay = replay_liquidation_levels(
        raw, LiquidationLevelConfig(), progress_every=max(0, int(args.progress_every))
    )

    cfg = ShortSqueezeConfig(
        bootstrap_resamples=int(args.bootstrap_resamples),
        seed=int(args.seed),
        skip_tp_sl=bool(args.skip_tp_sl),
        skip_bootstrap=bool(args.skip_bootstrap),
    )
    bundle = run_short_squeeze_continuation_audit(replay, raw, cfg)

    config_out = {
        "symbol": args.symbol,
        "feather_file": str(feather),
        "level_config": asdict(LiquidationLevelConfig()),
        "audit_config": asdict(cfg),
        "disclaimer": "Estimated LuxAlgo-style levels; not real exchange liquidations.",
        "t4_omitted": True,
    }
    (out / "config.json").write_text(json.dumps(_jsonable(config_out), indent=2) + "\n", encoding="utf-8")
    (out / "data_summary.json").write_text(
        json.dumps(_jsonable({"meta": bundle.meta, "n_events": len(bundle.events)}), indent=2) + "\n",
        encoding="utf-8",
    )

    print("writing exports...", flush=True)
    ev_df = events_to_dataframe(bundle.events)
    ev_df.to_csv(out / "short_squeeze_events.csv", index=False)
    # reclaim / trend context extracts
    reclaim_cols = [
        c
        for c in ev_df.columns
        if c
        in {
            "event_id",
            "timestamp",
            "candle_index",
            "leverage",
            "level_id",
            "level_price",
            "reclaim_class",
            "exclusive_reclaim_group",
            "reclaim_index",
            "reclaim_delay_candles",
            "signal_index",
            "entry_index",
            "entry_price",
            "sample",
        }
    ]
    ev_df[reclaim_cols].to_csv(out / "reclaim_events.csv", index=False)
    trend_cols = [
        c
        for c in ev_df.columns
        if c.startswith("trend_") or c in {"event_id", "timestamp", "candle_index", "leverage", "sample"}
    ]
    ev_df[trend_cols].to_csv(out / "trend_context_events.csv", index=False)

    _write_csv(out / "short_continuation_horizons.csv", bundle.horizon_summary)
    _write_csv(out / "short_continuation_thresholds.csv", bundle.threshold_summary)
    _write_csv(out / "first_touch_outcomes.csv", bundle.first_touch_summary)
    _write_csv(out / "tp_sl_summary.csv", bundle.tp_sl_summary)
    _write_csv(out / "tp_sl_trades.csv", bundle.tp_sl_trades)
    _write_csv(out / "matched_controls.csv", bundle.matched_controls)
    _write_csv(out / "control_comparison.csv", bundle.control_comparison)
    _write_csv(out / "variant_comparison.csv", bundle.variant_comparison)
    _write_csv(out / "summary_monthly.csv", bundle.monthly_summary)
    _write_csv(out / "march_downtrend_events.csv", bundle.march_events)
    (out / "march_downtrend_summary.json").write_text(
        json.dumps(_jsonable(bundle.march_summary), indent=2) + "\n", encoding="utf-8"
    )

    for name, payload in (
        ("summary_full.json", bundle.summary_full),
        ("summary_in_sample.json", bundle.summary_in_sample),
        ("summary_out_of_sample.json", bundle.summary_out_of_sample),
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
