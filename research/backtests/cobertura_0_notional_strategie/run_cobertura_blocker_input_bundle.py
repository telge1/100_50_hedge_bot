"""CLI: build Cobertura blocker historical input bundle."""

from __future__ import annotations

import argparse
from pathlib import Path

from .cobertura_blocker_input_bundle import build_bundle

DEFAULT_STATE = Path(
    "research/backtests/cobertura_0_notional_strategie/results/historical_blocker_states_20260726"
)
DEFAULT_FILL = Path(
    "research/backtests/cobertura_0_notional_strategie/results/historical_blocker_fill_replay_20260726"
)
DEFAULT_OUT = Path(
    "research/backtests/cobertura_0_notional_strategie/results/cobertura_blocker_input_bundle_20260726"
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Merge historical break/market/fill-replay facts into Cobertura input bundle"
    )
    p.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    p.add_argument("--fill-replay-dir", type=Path, default=DEFAULT_FILL)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--trigger-mode", default="first_break")
    p.add_argument("--taker-fee-rate", type=float, default=0.00055)
    args = p.parse_args(argv)
    payload = build_bundle(
        state_dir=args.state_dir,
        fill_replay_dir=args.fill_replay_dir,
        output_dir=args.output_dir,
        trigger_mode=args.trigger_mode,
        taker_fee_rate=float(args.taker_fee_rate),
    )
    print(f"Wrote {payload['output_dir']}")
    print(
        f"Decision={payload['decision']} ready={payload['ready']} "
        f"unresolved={payload['unresolved']} apt={payload['apt_status']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
