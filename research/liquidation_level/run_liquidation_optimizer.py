"""CLI for the liquidation-level config grid optimizer."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from research.liquidation_level.liquidation_audit import DEFAULT_FEATHER, load_feather
from research.liquidation_level.liquidation_config import load_optimizer_grid
from research.liquidation_level.liquidation_optimizer import estimate_dry_run, run_optimizer


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Liquidation level path-context config optimizer")
    p.add_argument(
        "--grid-config",
        type=Path,
        default=Path("research/liquidation_level/configs/liquidation_optimizer_grid.json"),
    )
    p.add_argument("--feather-file", type=Path, default=DEFAULT_FEATHER)
    p.add_argument("--symbol", type=str, default="APTUSDT")
    p.add_argument(
        "--output-dir",
        type=Path,
        default=Path("research/liquidation_level/results/APTUSDT_5m_optimizer_v1"),
    )
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--retry-failed", action="store_true")
    p.add_argument("--max-configs", type=int, default=None)
    p.add_argument("--batch-size", type=int, default=None, help="Max NEW configs this run (server-safe)")
    p.add_argument("--max-mem-cache", type=int, default=1, help="In-memory replay cache size (default 1)")
    p.add_argument("--skip-controls", action="store_true", default=True)
    p.add_argument("--with-controls", action="store_true", help="Enable top10 matched controls")
    p.add_argument("--start-config-index", type=int, default=0)
    p.add_argument("--end-config-index", type=int, default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--baseline-only", action="store_true")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--progress-every", type=int, default=5)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    grid_cfg = load_optimizer_grid(args.grid_config)
    feather = args.feather_file.expanduser().resolve()
    out = args.output_dir

    print(f"loading {feather}", flush=True)
    raw = load_feather(feather)
    print(f"candles={len(raw)}", flush=True)

    if args.dry_run:
        est = estimate_dry_run(
            grid_cfg,
            output_dir=out,
            max_configs=args.max_configs,
            feather=feather,
            ohlcv=raw,
        )
        print(json.dumps(est, indent=2))
        out.mkdir(parents=True, exist_ok=True)
        (out / "dry_run_estimate.json").write_text(json.dumps(est, indent=2) + "\n", encoding="utf-8")
        return 0

    skip_controls = not bool(args.with_controls)
    t0 = time.perf_counter()
    summary = run_optimizer(
        grid_cfg=grid_cfg,
        ohlcv=raw,
        output_dir=out,
        max_configs=args.max_configs,
        start_config_index=int(args.start_config_index),
        end_config_index=args.end_config_index,
        resume=bool(args.resume),
        retry_failed=bool(args.retry_failed),
        workers=max(1, int(args.workers)),
        seed=int(args.seed),
        baseline_only=bool(args.baseline_only),
        progress_every=max(1, int(args.progress_every)),
        batch_size=args.batch_size,
        max_mem_cache=max(1, int(args.max_mem_cache)),
        skip_controls=skip_controls,
    )
    summary["symbol"] = args.symbol
    summary["elapsed_s"] = time.perf_counter() - t0
    print(json.dumps(summary, indent=2))
    print(f"done output={out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
