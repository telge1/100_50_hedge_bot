"""Run DCOS qty-sweep post-fix 120-start backtests (backtest-only)."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

CONFIG_DIR = Path("research/backtests/configs/dcos_qty_sweep_post_fix")
RESULTS_ROOT = Path("research/backtests/results/dcos_qty_sweep_post_fix")

RUN_KWARGS = [
    "--symbol",
    "APTUSDT",
    "--direction",
    "long",
    "--config-source",
    "live",
    "--multi-start",
    "--start-step-candles",
    "250",
    "--window-candles",
    "5000",
    "--limit",
    "50000",
    "--max-starts",
    "120",
    "--fill-model",
    "conservative",
]

VARIANT_CONFIGS = sorted(CONFIG_DIR.glob("variant_*.json"))


def _run(label: str, *, config_path: Path | None) -> int:
    out_dir = RESULTS_ROOT / label
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "research.backtests.run_original_hedge_backtest",
        *RUN_KWARGS,
        "--output-dir",
        str(out_dir),
    ]
    if config_path is not None:
        cmd.extend(
            [
                "--dynamic-cycle-order-scaling-config-json",
                config_path.read_text(),
            ]
        )
    print(f"\n=== Running {label} ===", flush=True)
    print(" ".join(cmd[:6]), "...")
    proc = subprocess.run(cmd, check=False)
    return int(proc.returncode)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-baseline",
        action="store_true",
        help="Skip baseline_post_fix run",
    )
    parser.add_argument(
        "--only",
        nargs="+",
        default=None,
        help="Run only these variant labels (without .json)",
    )
    args = parser.parse_args()

    failures: list[str] = []

    if not args.skip_baseline and (args.only is None or "baseline_post_fix" in args.only):
        if _run("baseline_post_fix", config_path=None) != 0:
            failures.append("baseline_post_fix")

    for config_path in VARIANT_CONFIGS:
        label = config_path.stem
        if args.only is not None and label not in args.only:
            continue
        if _run(label, config_path=config_path) != 0:
            failures.append(label)

    if failures:
        print(f"\nFailed runs: {', '.join(failures)}", file=sys.stderr)
        sys.exit(1)
    print(f"\nAll runs completed. Results under {RESULTS_ROOT}/")


if __name__ == "__main__":
    main()
