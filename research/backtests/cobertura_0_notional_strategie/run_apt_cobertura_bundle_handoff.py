"""CLI: isolated APT Cobertura handoff from blocker input bundle."""

from __future__ import annotations

import argparse
from pathlib import Path

from .cobertura_bundle_handoff import (
    APT_TRADE_ID,
    DEFAULT_SCENARIO_ID,
    TRIGGER_MODE,
    run_apt_bundle_handoff,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Handoff one historical blocker from Cobertura input bundle "
        "(APT isolation; neutralization only)."
    )
    p.add_argument(
        "--bundle",
        type=Path,
        required=True,
        help="Path to blocker_historical_states.jsonl",
    )
    p.add_argument(
        "--scenarios",
        type=Path,
        required=True,
        help="Path to cobertura_start_scenarios.jsonl",
    )
    p.add_argument("--trade-id", default=APT_TRADE_ID)
    p.add_argument("--scenario-id", default=DEFAULT_SCENARIO_ID)
    p.add_argument("--trigger-mode", default=TRIGGER_MODE)
    p.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="New results directory (must not overwrite the bundle dir)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    out = Path(args.output_dir).resolve()
    bundle = Path(args.bundle).resolve()
    if out == bundle.parent:
        raise SystemExit("refusing to write into the bundle results directory")
    result = run_apt_bundle_handoff(
        bundle_path=Path(args.bundle),
        scenarios_path=Path(args.scenarios),
        output_dir=Path(args.output_dir),
        trade_id=args.trade_id,
        scenario_id=args.scenario_id,
        trigger_mode=args.trigger_mode,
        cli_args={
            "bundle": str(args.bundle),
            "scenarios": str(args.scenarios),
            "trade_id": args.trade_id,
            "scenario_id": args.scenario_id,
            "trigger_mode": args.trigger_mode,
            "output_dir": str(args.output_dir),
        },
    )
    print(
        f"Wrote {result['output_dir']}\n"
        f"Decision={result['decision']} "
        f"warnings={result['warnings']}"
    )
    return 0 if "PASS" in result["decision"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
