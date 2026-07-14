"""CLI: matched-control validation for frozen liquidation winner config."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from research.liquidation_level.liquidation_audit import DEFAULT_FEATHER, load_feather
from research.liquidation_level.liquidation_control_validation import (
    ControlValidationConfig,
    frozen_winner_config,
    run_control_validation,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Control validation for frozen liquidation winner")
    p.add_argument("--feather-file", type=Path, default=DEFAULT_FEATHER)
    p.add_argument(
        "--optimizer-dir",
        type=Path,
        default=Path("research/liquidation_level/results/APTUSDT_5m_optimizer_v1"),
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=Path("research/liquidation_level/results/APTUSDT_5m_control_validation_v1"),
    )
    p.add_argument("--control-runs", type=int, default=1000)
    p.add_argument("--random-seed", type=int, default=42)
    p.add_argument("--max-events", type=int, default=None)
    p.add_argument("--skip-seed-sensitivity", action="store_true")
    p.add_argument("--skip-matching-sensitivity", action="store_true")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--matching-mode", type=str, default="medium", choices=("strict", "medium", "loose"))
    p.add_argument("--progress-every", type=int, default=25)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    log_path = out / "run.log"

    class _Tee:
        def __init__(self, *files):
            self.files = files

        def write(self, data):
            for f in self.files:
                f.write(data)
                f.flush()

        def flush(self):
            for f in self.files:
                f.flush()

    log_f = log_path.open("a", encoding="utf-8")
    sys.stdout = _Tee(sys.__stdout__, log_f)
    sys.stderr = _Tee(sys.__stderr__, log_f)

    print(f"loading {args.feather_file}", flush=True)
    raw = load_feather(Path(args.feather_file).expanduser().resolve())
    print(f"candles={len(raw)} optimizer_dir={args.optimizer_dir}", flush=True)

    cfg = ControlValidationConfig(
        control_runs=int(args.control_runs),
        random_seed=int(args.random_seed),
        progress_every=max(1, int(args.progress_every)),
    )
    t0 = time.perf_counter()
    try:
        summary = run_control_validation(
            raw,
            output_dir=out,
            cfg=cfg,
            level_config=frozen_winner_config(),
            skip_seed_sensitivity=bool(args.skip_seed_sensitivity),
            skip_matching_sensitivity=bool(args.skip_matching_sensitivity),
            max_events=args.max_events,
            resume=bool(args.resume),
            matching_mode=str(args.matching_mode),
        )
    except RuntimeError as exc:
        print(f"ABORT: {exc}", flush=True)
        return 2
    print(f"done elapsed={time.perf_counter()-t0:.1f}s status={summary.get('oos_decision', {}).get('status')}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
