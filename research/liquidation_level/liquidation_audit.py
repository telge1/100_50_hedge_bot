"""CLI audit for causal LuxAlgo Liquidation Levels replication."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from research.liquidation_level.liquidation_levels import (
    LiquidationLevelConfig,
    candle_states_to_dataframe,
    levels_to_dataframe,
    replay_liquidation_levels,
    sweep_events_dataframe,
)

DEFAULT_FEATHER = Path(
    "/home/telgenbuescher/projects/Signal_Generator_Ralf/data/bybit/futures/"
    "APT_USDT_USDT-5m-futures.feather"
)


def _parse_leverages(raw: str) -> tuple[int, ...]:
    parts = [p.strip() for p in str(raw).split(",") if p.strip()]
    if not parts:
        raise argparse.ArgumentTypeError("leverages must be a comma-separated list of integers")
    return tuple(int(p) for p in parts)


def _find_apt_hints(feather_file: Path) -> list[str]:
    hints: list[str] = []
    search_roots = [
        feather_file.parent,
        Path("/home/telgenbuescher/projects/Signal_Generator_Ralf/data/bybit/futures"),
    ]
    seen: set[str] = set()
    for root in search_roots:
        if not root.exists():
            continue
        for path in sorted(root.glob("*APT*5m*.feather")):
            s = str(path)
            if s not in seen:
                seen.add(s)
                hints.append(s)
        for path in sorted(root.glob("*APT*.feather")):
            s = str(path)
            if s not in seen:
                seen.add(s)
                hints.append(s)
    return hints[:20]


def load_feather(path: Path) -> pd.DataFrame:
    if not path.exists():
        hints = _find_apt_hints(path)
        msg = [
            f"Feather file not found: {path}",
            "No automatic fallback to another coin is allowed.",
        ]
        if hints:
            msg.append("Possible matching APT feather files:")
            msg.extend(f"  - {h}" for h in hints)
        else:
            msg.append("No nearby APT*.feather hints were found.")
        raise FileNotFoundError("\n".join(msg))
    return pd.read_feather(path)


def _filter_window(
    df: pd.DataFrame,
    *,
    start_date: str | None,
    end_date: str | None,
    max_candles: int | None,
) -> pd.DataFrame:
    out = df.copy()
    # Prefer date/timestamp column if present for filtering before normalize.
    ts_col = None
    for candidate in ("date", "timestamp", "datetime", "time"):
        if candidate in out.columns:
            ts_col = candidate
            break
    if ts_col is not None:
        ts = pd.to_datetime(out[ts_col], utc=True, errors="coerce")
        mask = pd.Series(True, index=out.index)
        if start_date:
            mask &= ts >= pd.Timestamp(start_date, tz="UTC")
        if end_date:
            # inclusive end-of-day if date-only
            end_ts = pd.Timestamp(end_date, tz="UTC")
            if len(str(end_date)) <= 10:
                end_ts = end_ts + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)
            mask &= ts <= end_ts
        out = out.loc[mask].copy()
    if max_candles is not None and max_candles > 0:
        out = out.iloc[: int(max_candles)].copy()
    return out.reset_index(drop=True)


def write_readme_results(path: Path, summary: dict, config: LiquidationLevelConfig, feather: Path) -> None:
    mean_age = summary.get("mean_age_at_sweep")
    median_age = summary.get("median_age_at_sweep")
    mean_txt = "n/a" if mean_age is None else f"{mean_age:.2f} candles"
    median_txt = "n/a" if median_age is None else f"{median_age:.2f} candles"
    text = f"""# Liquidation Levels Audit Results

## What this indicator computes

This audit replicates the LuxAlgo **Liquidation Levels** Pine logic in Python.
On each candle it estimates price levels where leveraged positions *might* get
liquidated, based on:

- a reference price (default: open)
- volume spikes vs a 13-period volume SMA
- a volatility / wick condition
- fixed leverage distances (default 25x / 50x / 100x)

Levels stay active until a later candle's range **strictly crosses** through them
(`high > level` and `low < level`), or until the active-level cap removes the oldest ones.

## Important: these are estimates, not real exchange liquidations

The levels are **heuristic / estimated**. They are **not** taken from Bybit (or any
other exchange) liquidation feeds. Do not treat them as ground-truth liquidations.

## Run summary

- Feather: `{feather}`
- Symbol: `{summary.get("symbol")}`
- Timeframe: `{summary.get("timeframe")}`
- Start: `{summary.get("start_timestamp")}`
- End: `{summary.get("end_timestamp")}`
- Candles: `{summary.get("candle_count")}`
- Created levels: `{summary.get("created_level_count")}`
  - Upper: `{summary.get("created_upper_count")}`
  - Lower: `{summary.get("created_lower_count")}`
- Swept levels: `{summary.get("swept_level_count")}`
  - Upper: `{summary.get("swept_upper_count")}`
  - Lower: `{summary.get("swept_lower_count")}`
- Active at end: `{summary.get("active_level_count_end")}`
- Removed by max-active limit: `{summary.get("removed_by_limit_count")}`
- Sweep rate: `{summary.get("sweep_rate_percent")}`
- Mean age at sweep: {mean_txt}
- Median age at sweep: {median_txt}

## Config used

```json
{json.dumps(asdict(config), indent=2)}
```

## What this audit does **not** prove

This first step only validates the causal level lifecycle and exports research
artifacts. It does **not** prove a profitable trading strategy. Entry / TP / SL
optimization and strategy backtests are intentionally deferred to a later task.
"""
    path.write_text(text, encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Audit LuxAlgo Liquidation Levels Python replication")
    p.add_argument("--feather-file", type=Path, default=DEFAULT_FEATHER)
    p.add_argument("--symbol", type=str, default="APTUSDT")
    p.add_argument("--start-date", type=str, default=None)
    p.add_argument("--end-date", type=str, default=None)
    p.add_argument("--max-candles", type=int, default=None)
    p.add_argument("--reference-price", type=str, default="open")
    p.add_argument("--volume-threshold", type=float, default=1.7)
    p.add_argument("--volatility-threshold", type=float, default=10.0)
    p.add_argument("--leverages", type=_parse_leverages, default=(25, 50, 100))
    p.add_argument(
        "--output-dir",
        type=Path,
        default=Path("research/liquidation_level/results/APTUSDT_5m"),
    )
    p.add_argument("--progress-every", type=int, default=5000)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    feather = args.feather_file.expanduser().resolve()
    out_dir = args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"loading feather: {feather}", flush=True)
    raw = load_feather(feather)
    raw = _filter_window(
        raw,
        start_date=args.start_date,
        end_date=args.end_date,
        max_candles=args.max_candles,
    )
    if raw.empty:
        raise SystemExit("No candles left after date/max-candles filters.")

    config = LiquidationLevelConfig(
        reference_price=args.reference_price,
        volume_threshold=float(args.volume_threshold),
        volatility_threshold=float(args.volatility_threshold),
        leverages=tuple(args.leverages),
    )

    print(
        f"replaying {len(raw)} candles | ref={config.reference_price} "
        f"vol_thr={config.volume_threshold} lev={config.leverages}",
        flush=True,
    )
    result = replay_liquidation_levels(
        raw,
        config,
        progress_every=max(0, int(args.progress_every)),
    )

    levels_df = levels_to_dataframe(result)
    states_df = candle_states_to_dataframe(result)
    sweeps_df = sweep_events_dataframe(result)

    summary = {
        "symbol": args.symbol,
        "timeframe": "5m",
        "feather_file": str(feather),
        **result.summary,
    }

    data_summary = {
        "symbol": args.symbol,
        "feather_file": str(feather),
        "raw_rows_after_filter": int(len(raw)),
        "columns": list(raw.columns),
        "start_timestamp": summary.get("start_timestamp"),
        "end_timestamp": summary.get("end_timestamp"),
    }

    (out_dir / "config.json").write_text(json.dumps(asdict(config), indent=2) + "\n", encoding="utf-8")
    (out_dir / "data_summary.json").write_text(json.dumps(data_summary, indent=2) + "\n", encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    levels_df.to_csv(out_dir / "levels.csv", index=False)
    states_df.to_csv(out_dir / "candle_states.csv", index=False)
    sweeps_df.to_csv(out_dir / "sweep_events.csv", index=False)
    write_readme_results(out_dir / "README_results.md", summary, config, feather)

    print(
        f"done created={summary['created_level_count']} "
        f"swept={summary['swept_level_count']} "
        f"active_end={summary['active_level_count_end']} "
        f"output={out_dir}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
