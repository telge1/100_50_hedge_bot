"""CLI for frozen Phase-2 baseline evaluation (research only)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .contract import EVALUATION_CONTRACT_VERSION, RESEARCH_BANNER
from .evaluate import evaluate_baselines


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Evaluate frozen simple liquidity-destination baselines "
            f"({RESEARCH_BANNER}). Reads local Phase-1D CSV artifacts only."
        )
    )
    p.add_argument(
        "--dataset-root",
        type=Path,
        required=True,
        help="Phase-1D result root containing BTCUSDT/ and DOGEUSDT/ episodes.csv",
    )
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument(
        "--require-frozen-manifest",
        action="store_true",
        help="Fail closed unless Phase-1D artifact hashes and freeze fingerprint match.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    summary = evaluate_baselines(
        args.dataset_root,
        args.output_dir,
        require_frozen_manifest=args.require_frozen_manifest,
    )
    test_combined = [
        row
        for row in summary["baseline_metrics"]
        if row["split"] == "TEST" and row["scope"] == "COMBINED"
    ]
    print(RESEARCH_BANNER)
    print(
        json.dumps(
            {
                "research_only": RESEARCH_BANNER,
                "evaluation_contract_version": EVALUATION_CONTRACT_VERSION,
                "dataset_fingerprint_sha256": summary["dataset"][
                    "dataset_fingerprint_sha256"
                ],
                "episode_count": summary["dataset"]["episode_count"],
                "database_connection": False,
                "test_combined_accuracy": {
                    row["baseline"]: row["accuracy"] for row in test_combined
                },
                "fitted_train_labels": summary["fitted_train_labels"],
                "output_dir": str(args.output_dir),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0
