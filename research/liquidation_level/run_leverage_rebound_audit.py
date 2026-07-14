"""CLI for leverage rebound audit after LuxAlgo-style level sweeps."""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pandas as pd

from research.liquidation_level.leverage_rebound_audit import (
    DEFAULT_CASCADE_WINDOWS,
    ReboundAuditConfig,
    events_to_dataframe,
    run_leverage_rebound_audit,
)
from research.liquidation_level.liquidation_audit import DEFAULT_FEATHER, _filter_window, load_feather
from research.liquidation_level.liquidation_levels import LiquidationLevelConfig, replay_liquidation_levels


def _parse_floats(raw: str) -> tuple[float, ...]:
    return tuple(float(x.strip()) for x in str(raw).split(",") if x.strip())


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


def _hit(summary: dict[str, Any], group: str, h: int, thr: float) -> str:
    key = f"{group}_h{h}_thr{thr}"
    row = summary.get("key_hit_rates", {}).get(key)
    if not row or row.get("hit_rate_pct") is None:
        return "n/a"
    return (
        f"hit={row['hit_rate_pct']:.1f}% | mfe={row['mean_mfe_pct']:.3f}% | "
        f"mae={row['mean_mae_pct']:.3f}% | n={row['event_count']}"
    )


def write_readme(path: Path, bundle, feather: Path, symbol: str) -> None:
    full = bundle.summary_full
    oos = bundle.summary_out_of_sample
    ctrl = {r["group"]: r for r in bundle.control_comparison if r["sample"] == "full"}

    def ans_lev(lev: int) -> str:
        lines = []
        for side, label in (
            ("lower", "LONG rebound after lower sweep"),
            ("upper", "SHORT rebound after upper sweep"),
        ):
            g = f"{side}_{lev}x"
            lines.append(f"- {label}: h3/0.25% → {_hit(full, g, 3, 0.25)}")
            c = ctrl.get(g)
            if c and c.get("event_minus_control_mfe") is not None:
                lines.append(
                    f"  vs control MFE diff≈{c['event_minus_control_mfe']:.4f}% "
                    f"(bootstrap CI [{c.get('bootstrap_mfe_diff_ci95_low')}, "
                    f"{c.get('bootstrap_mfe_diff_ci95_high')}]; empirical only, "
                    f"not a significance claim)"
                )
        return "\n".join(lines)

    cost_notes = []
    for g, note in full.get("cost_vs_mfe", {}).items():
        if isinstance(note, dict):
            cost_notes.append(
                f"- {g}: mean MFE h3={note.get('mean_mfe_h3')} vs cost {note.get('cost_pct')} "
                f"→ exceeds_cost={note.get('mean_mfe_exceeds_cost')} ({note.get('note')})"
            )

    text = f"""# Leverage Rebound Audit Results

## Disclaimer

These liquidation levels are **estimated** by a causal LuxAlgo-style model.
They are **not** real exchange liquidation feeds.

Symbol: `{symbol}`  
Feather: `{feather}`  
Candles: `{bundle.meta.get('n_candles')}`  
Period: `{bundle.meta.get('start_timestamp')}` → `{bundle.meta.get('end_timestamp')}`  
Split: first 70% in-sample / last 30% out-of-sample by candle index.

Measurement starts at the **open of the next candle** after a strict through-level sweep
(`high > level` and `low < level`). The sweep candle itself is never a trade entry.

## 1–3. Small rebound after 100x / 50x / 25x sweeps?

### 100x
{ans_lev(100)}

### 50x
{ans_lev(50)}

### 25x
{ans_lev(25)}

## 4. Stronger after deeper multi-leverage sweeps?

See `rebound_threshold_summary.csv` groups `combo_*` and `cascade_*`.
Compare mean MFE / hit rates for `100x_only` vs `100x_50x_25x` and cascades
`100x->50x->25x`.

Combination counts (full): `{json.dumps(full.get('combination_counts', {}), indent=2)}`

## 5. Sweep + reclaim better than sweep alone?

See `reclaim_summary.csv`. Compare `immediate_reclaim` / `next_candle_reclaim`
vs `no_reclaim` for horizon-3 / 0.25% hit rates and mean MFE.
Also see `rejection_vs_breakthrough.csv`.

## 6. Hit rates for 0.10% / 0.25% / 0.50% at 1/3/6/12 bars

Examples (full sample):

| Group | 0.10%@1 | 0.25%@3 | 0.50%@6 | 0.50%@12 |
|---|---|---|---|---|
| lower_100x | {_hit(full, 'lower_100x', 1, 0.10)} | {_hit(full, 'lower_100x', 3, 0.25)} | {_hit(full, 'lower_100x', 6, 0.50)} | {_hit(full, 'lower_100x', 12, 0.50)} |
| lower_50x | {_hit(full, 'lower_50x', 1, 0.10)} | {_hit(full, 'lower_50x', 3, 0.25)} | {_hit(full, 'lower_50x', 6, 0.50)} | {_hit(full, 'lower_50x', 12, 0.50)} |
| lower_25x | {_hit(full, 'lower_25x', 1, 0.10)} | {_hit(full, 'lower_25x', 3, 0.25)} | {_hit(full, 'lower_25x', 6, 0.50)} | {_hit(full, 'lower_25x', 12, 0.50)} |
| upper_100x | {_hit(full, 'upper_100x', 1, 0.10)} | {_hit(full, 'upper_100x', 3, 0.25)} | {_hit(full, 'upper_100x', 6, 0.50)} | {_hit(full, 'upper_100x', 12, 0.50)} |
| upper_50x | {_hit(full, 'upper_50x', 1, 0.10)} | {_hit(full, 'upper_50x', 3, 0.25)} | {_hit(full, 'upper_50x', 6, 0.50)} | {_hit(full, 'upper_50x', 12, 0.50)} |
| upper_25x | {_hit(full, 'upper_25x', 1, 0.10)} | {_hit(full, 'upper_25x', 3, 0.25)} | {_hit(full, 'upper_25x', 6, 0.50)} | {_hit(full, 'upper_25x', 12, 0.50)} |

## 7. Adverse move size

Mean/median MAE are in `rebound_threshold_summary.csv` alongside MFE.
Also check `rebound_before_adverse_*` columns (rebound touched before adverse).

## 8. Out-of-sample confirmation?

Compare the same keys in `summary_out_of_sample.json`.
Example lower_100x h3/0.25: full=`{_hit(full, 'lower_100x', 3, 0.25)}` OOS=`{_hit(oos, 'lower_100x', 3, 0.25)}`

## 9. Larger than matched controls?

See `control_comparison.csv` (month/hour/range-matched, non-sweep candles, fixed seed).
Bootstrap CIs are descriptive only — **not** formal statistical significance.

## 10. Enough after 0.12% round-trip costs?

Peak MFE is not a realized trade return. Even if mean MFE > 0.12%, MAE and timing
usually erase a naive threshold scalp.

{chr(10).join(cost_notes)}

## Integration

No scanner / bot / strategy integration from this audit.
"""
    path.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Leverage rebound audit for estimated liquidation levels")
    p.add_argument("--feather-file", type=Path, default=DEFAULT_FEATHER)
    p.add_argument("--symbol", type=str, default="APTUSDT")
    p.add_argument("--start-date", type=str, default=None)
    p.add_argument("--end-date", type=str, default=None)
    p.add_argument("--max-candles", type=int, default=None)
    p.add_argument("--cascade-windows", type=_parse_ints, default=DEFAULT_CASCADE_WINDOWS)
    p.add_argument("--horizons", type=_parse_ints, default=(1, 2, 3, 6, 12, 24))
    p.add_argument(
        "--rebound-thresholds",
        type=_parse_floats,
        default=(0.10, 0.20, 0.25, 0.30, 0.50, 0.75, 1.00),
    )
    p.add_argument("--bootstrap-resamples", type=int, default=1000)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument(
        "--output-dir",
        type=Path,
        default=Path("research/liquidation_level/results/APTUSDT_5m_leverage_rebound"),
    )
    p.add_argument("--progress-every", type=int, default=5000)
    args = p.parse_args(argv)

    t0 = time.perf_counter()
    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    feather = args.feather_file.expanduser().resolve()

    print(f"loading {feather}", flush=True)
    raw = load_feather(feather)
    raw = _filter_window(
        raw, start_date=args.start_date, end_date=args.end_date, max_candles=args.max_candles
    )
    print(f"candles={len(raw)}", flush=True)

    print("replaying levels...", flush=True)
    replay = replay_liquidation_levels(
        raw,
        LiquidationLevelConfig(),
        progress_every=max(0, int(args.progress_every)),
    )

    cfg = ReboundAuditConfig(
        horizons=tuple(args.horizons),
        rebound_thresholds=tuple(args.rebound_thresholds),
        cascade_windows=tuple(args.cascade_windows),
        bootstrap_resamples=int(args.bootstrap_resamples),
        seed=int(args.seed),
    )
    bundle = run_leverage_rebound_audit(replay, raw, cfg)

    config_out = {
        "symbol": args.symbol,
        "feather_file": str(feather),
        "level_config": asdict(LiquidationLevelConfig()),
        "rebound_config": asdict(cfg),
        "disclaimer": "Estimated LuxAlgo-style levels; not real exchange liquidations.",
    }
    (out / "config.json").write_text(json.dumps(_jsonable(config_out), indent=2) + "\n", encoding="utf-8")

    print("writing outputs...", flush=True)
    events_to_dataframe(bundle.level_events).to_csv(out / "rebound_events.csv", index=False)
    events_to_dataframe(bundle.combination_events).to_csv(out / "leverage_combinations.csv", index=False)
    events_to_dataframe(bundle.cascade_events).to_csv(out / "cascade_events.csv", index=False)
    _write_csv(out / "rebound_threshold_summary.csv", bundle.threshold_summary)
    _write_csv(out / "reclaim_summary.csv", bundle.reclaim_summary)
    _write_csv(out / "rejection_vs_breakthrough.csv", bundle.rejection_summary)
    _write_csv(out / "control_comparison.csv", bundle.control_comparison)
    _write_csv(out / "summary_monthly.csv", bundle.monthly_summary)

    for name, payload in (
        ("summary_full.json", bundle.summary_full),
        ("summary_in_sample.json", bundle.summary_in_sample),
        ("summary_out_of_sample.json", bundle.summary_out_of_sample),
    ):
        enriched = _jsonable({**payload, "meta": bundle.meta, "elapsed_seconds": time.perf_counter() - t0})
        (out / name).write_text(json.dumps(enriched, indent=2) + "\n", encoding="utf-8")

    write_readme(out / "README_results.md", bundle, feather, args.symbol)
    print(f"done elapsed={time.perf_counter()-t0:.1f}s output={out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
