"""CLI: causal liquidation-level event backtest (research-only)."""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pandas as pd

from research.liquidation_level.liquidation_audit import load_feather, _filter_window, DEFAULT_FEATHER
from research.liquidation_level.liquidation_backtest import (
    BacktestConfig,
    run_backtest,
)
from research.liquidation_level.liquidation_features import (
    FeatureConfig,
    build_feature_bundle,
    candle_events_to_dataframe,
    cluster_events_to_dataframe,
    cluster_snapshots_to_dataframe,
    level_events_to_dataframe,
    signals_to_dataframe,
)
from research.liquidation_level.liquidation_levels import (
    LiquidationLevelConfig,
    replay_liquidation_levels,
)


def _write_csv(path: Path, rows: list[dict[str, Any]] | pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(rows, pd.DataFrame):
        rows.to_csv(path, index=False)
        return
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    pd.DataFrame(rows).to_csv(path, index=False)


def _jsonable(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        if isinstance(obj, float) and (obj != obj or obj in (float("inf"), float("-inf"))):
            return None
        return obj
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(x) for x in obj]
    return str(obj)


def _pick_best(summary: dict[str, Any], direction: str) -> dict[str, Any] | None:
    key = "best_long_horizon12_net" if direction == "long" else "best_short_horizon12_net"
    return summary.get(key)


def write_readme(
    path: Path,
    *,
    feather: Path,
    feature_summary: dict[str, Any],
    bt_meta: dict[str, Any],
    summary_full: dict[str, Any],
    summary_is: dict[str, Any],
    summary_oos: dict[str, Any],
    control_rows: list[dict[str, Any]],
    cost: float,
) -> None:
    best_long = _pick_best(summary_full, "long")
    best_short = _pick_best(summary_full, "short")
    best_long_oos = _pick_best(summary_oos, "long")
    best_short_oos = _pick_best(summary_oos, "short")

    def _fmt(b: dict[str, Any] | None) -> str:
        if not b:
            return "n/a"
        return (
            f"{b.get('variant')} | n={b.get('event_count')} | "
            f"mean_gross={b.get('mean_gross_return_pct')} | "
            f"mean_net={b.get('mean_net_return_pct')}"
        )

    # Control edge for best full variants at h=12
    def ctrl_note(variant: str | None, direction: str) -> str:
        if not variant:
            return "n/a"
        hits = [
            r
            for r in control_rows
            if r.get("variant") == variant
            and r.get("direction") == direction
            and int(r.get("horizon", -1)) == 12
            and r.get("sample") == "full"
        ]
        if not hits:
            return "no control row"
        r = hits[0]
        return (
            f"event-control={r.get('event_minus_control')} | "
            f"frac_controls_better={r.get('fraction_controls_better')} "
            f"(empirical share only; not a formal significance claim)"
        )

    long_net = None if not best_long else best_long.get("mean_net_return_pct")
    short_net = None if not best_short else best_short.get("mean_net_return_pct")
    long_oos_net = None if not best_long_oos else best_long_oos.get("mean_net_return_pct")
    short_oos_net = None if not best_short_oos else best_short_oos.get("mean_net_return_pct")

    integration = (
        "No scanner/live integration recommended from this run alone: "
        "treat results as research diagnostics until a variant is robust "
        "out-of-sample and clearly above its random-entry control."
    )
    if (long_oos_net is not None and long_oos_net > 0) or (short_oos_net is not None and short_oos_net > 0):
        # still cautious
        pass

    text = f"""# Liquidation Level Event Backtest Results

## What was tested

Causal event backtest on LuxAlgo-style **estimated** liquidation levels
(not real exchange liquidation feeds).

Pipeline:

1. Replay levels on APTUSDT 5m candles
2. Build sweep events (per level, per candle-side, per cluster)
3. Generate variants L1–L7, S1–S7, F_LONG, F_SHORT
4. Enter at the **open of the next candle** after the sweep candle closes
5. Evaluate fixed horizons and optional TP/SL grids
6. Compare against deterministic random-entry controls (seeded)

## Entry timing

A sweep is only known after the sweep candle **closes**.
Therefore entries are never on the sweep candle itself; the earliest entry is
the **next candle open**.

## Single level vs cluster

- **Candle sweep event**: all upper (or lower) levels swept on the same candle,
  aggregated once so many levels do not spawn duplicate identical trades.
- **Cluster**: research-only grouping of nearby active levels (default gap
  0.10%) before the sweep candle. A cluster sweep needs ≥2 swept members or
  swept strength ≥3.

## Data

- Feather: `{feather}`
- Candles: `{feature_summary.get("candle_count")}`
- Level sweep events: `{feature_summary.get("level_sweep_events")}`
- Candle sweep events: `{feature_summary.get("candle_sweep_events")}`
- Cluster sweeps: `{feature_summary.get("cluster_sweep_events")}`
- Signals total: `{feature_summary.get("signals_total")}`
- Signals by variant: `{json.dumps(feature_summary.get("signals_by_variant", {}), indent=2)}`
- In-sample cut index: `{bt_meta.get("in_sample_cut")}`
- Round-trip cost: `{cost}` %

## Highest signal quality (horizon 12, mean net after costs)

- Best long (full): {_fmt(best_long)}
- Best short (full): {_fmt(best_short)}
- Best long (out-of-sample): {_fmt(best_long_oos)}
- Best short (out-of-sample): {_fmt(best_short_oos)}

## Costs ({cost}%)

- Best long mean net (full): `{long_net}`
- Best short mean net (full): `{short_net}`

Positive **gross** with negative **net** means the edge does not clear costs.

## Control comparison (horizon 12, full)

- Long best variant control: {ctrl_note(None if not best_long else best_long.get("variant"), "long")}
- Short best variant control: {ctrl_note(None if not best_short else best_short.get("variant"), "short")}

The control share is an empirical bootstrap-style fraction only.
This audit does **not** claim classical statistical significance.

## Honest reading

- Interesting ≠ tradeable.
- No overlap filtering was applied (each event tested independently).
- No parameter selection was done on out-of-sample data in this runner.
- {integration}

## Not claimed

This does **not** prove a profitable live strategy and does **not** authorize
integration into the regime scanner, trend state machine, or bots.
"""
    path.write_text(text, encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Liquidation level causal event backtest")
    p.add_argument("--feather-file", type=Path, default=DEFAULT_FEATHER)
    p.add_argument("--symbol", type=str, default="APTUSDT")
    p.add_argument("--start-date", type=str, default=None)
    p.add_argument("--end-date", type=str, default=None)
    p.add_argument("--max-candles", type=int, default=None)
    p.add_argument("--cluster-max-gap-pct", type=float, default=0.10)
    p.add_argument("--roundtrip-cost-pct", type=float, default=0.12)
    p.add_argument("--skip-tp-sl", action="store_true")
    p.add_argument("--control-runs", type=int, default=100)
    p.add_argument("--random-seed", type=int, default=42)
    p.add_argument(
        "--output-dir",
        type=Path,
        default=Path("research/liquidation_level/results/APTUSDT_5m_backtest"),
    )
    p.add_argument("--progress-every", type=int, default=5000)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    t0 = time.perf_counter()
    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    feather = args.feather_file.expanduser().resolve()

    print(f"loading feather: {feather}", flush=True)
    raw = load_feather(feather)
    raw = _filter_window(
        raw,
        start_date=args.start_date,
        end_date=args.end_date,
        max_candles=args.max_candles,
    )
    print(f"loaded candles={len(raw)}", flush=True)

    level_cfg = LiquidationLevelConfig()
    print("replaying liquidation levels...", flush=True)
    replay = replay_liquidation_levels(
        raw,
        level_cfg,
        progress_every=max(0, int(args.progress_every)),
    )
    print(
        f"replay done created={replay.summary['created_level_count']} "
        f"swept={replay.summary['swept_level_count']}",
        flush=True,
    )

    feat_cfg = FeatureConfig(cluster_max_gap_pct=float(args.cluster_max_gap_pct))
    print("building causal features/events/signals...", flush=True)
    features = build_feature_bundle(replay, raw, feat_cfg)
    print(
        f"sweep_candle_events={features.summary['candle_sweep_events']} "
        f"cluster_sweeps={features.summary['cluster_sweep_events']} "
        f"signals={features.summary['signals_total']}",
        flush=True,
    )

    bt_cfg = BacktestConfig(
        roundtrip_cost_pct=float(args.roundtrip_cost_pct),
        control_runs=int(args.control_runs),
        random_seed=int(args.random_seed),
        skip_tp_sl=bool(args.skip_tp_sl),
    )
    print("running horizon/TP-SL/control backtest...", flush=True)
    bundle = run_backtest(features, bt_cfg, tp_sl_csv_path=out / "tp_sl_trades.csv")

    # exports
    config_out = {
        "symbol": args.symbol,
        "feather_file": str(feather),
        "level_config": asdict(level_cfg),
        "feature_config": asdict(feat_cfg),
        "backtest_config": asdict(bt_cfg),
        "start_date": args.start_date,
        "end_date": args.end_date,
        "max_candles": args.max_candles,
    }
    (out / "config.json").write_text(json.dumps(_jsonable(config_out), indent=2) + "\n", encoding="utf-8")

    print("writing CSVs...", flush=True)
    level_events_to_dataframe(features.level_events).to_csv(out / "sweep_level_events.csv", index=False)
    candle_events_to_dataframe(features.candle_events).to_csv(out / "sweep_candle_events.csv", index=False)
    cluster_snapshots_to_dataframe(features.cluster_snapshots).to_csv(out / "cluster_snapshots.csv", index=False)
    cluster_events_to_dataframe(features.cluster_events).to_csv(out / "cluster_sweep_events.csv", index=False)
    signals_to_dataframe(features.signals).to_csv(out / "signals.csv", index=False)
    _write_csv(out / "horizon_trades.csv", [asdict(t) for t in bundle.horizon_trades])
    # tp_sl_trades.csv already streamed during run_backtest
    _write_csv(out / "horizon_summary.csv", bundle.horizon_summary)
    _write_csv(out / "tp_sl_summary.csv", bundle.tp_sl_summary)
    _write_csv(out / "variant_comparison.csv", bundle.variant_comparison)
    _write_csv(out / "control_comparison.csv", bundle.control_comparison)
    _write_csv(out / "monthly_summary.csv", bundle.monthly_summary)

    for name, payload in (
        ("summary_full.json", bundle.summary_full),
        ("summary_in_sample.json", bundle.summary_in_sample),
        ("summary_out_of_sample.json", bundle.summary_out_of_sample),
    ):
        enriched = {
            **payload,
            "symbol": args.symbol,
            "timeframe": "5m",
            "start_timestamp": features.ohlcv.iloc[0]["timestamp"] if len(features.ohlcv) else None,
            "end_timestamp": features.ohlcv.iloc[-1]["timestamp"] if len(features.ohlcv) else None,
            "meta": bundle.meta,
            "elapsed_seconds": time.perf_counter() - t0,
        }
        # stringify timestamps
        enriched = _jsonable(enriched)
        (out / name).write_text(json.dumps(enriched, indent=2) + "\n", encoding="utf-8")

    write_readme(
        out / "README_results.md",
        feather=feather,
        feature_summary=features.summary,
        bt_meta=bundle.meta,
        summary_full=bundle.summary_full,
        summary_is=bundle.summary_in_sample,
        summary_oos=bundle.summary_out_of_sample,
        control_rows=bundle.control_comparison,
        cost=float(args.roundtrip_cost_pct),
    )

    elapsed = time.perf_counter() - t0
    print(
        f"done elapsed={elapsed:.1f}s output={out} "
        f"horizon_trades={len(bundle.horizon_trades)} "
        f"tp_sl_trades={bundle.meta.get('tp_sl_trade_count', 0)}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
