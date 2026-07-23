"""CLI: multi-coin price-staging profile grid @1000/500 (research-only).

Checkpoint after each completed coin. Supports ``--resume``.

Example (full grid — run manually with nohup; do not start from this agent):

```bash
PYTHONPATH=. python -m research.backtests.run_multicoin_price_staging_grid \\
  --baseline-audit-dir research/backtests/results/current_baseline_multicoin_continuous_blocker_audit_20260720 \\
  --sizes 1000:500 \\
  --profiles all \\
  --output-dir research/backtests/results/multicoin_price_staging_grid_1000_500_20260721
```
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from research.backtests.multicoin_blocker_price_staging import DEFAULT_BASELINE, FULL_HISTORY_CANDLE_LIMIT
from research.backtests.multicoin_price_staging_grid import (
    DEFAULT_OUT,
    log,
    parse_sizes,
    run_grid,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-audit-dir", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--sizes", default="1000:500")
    parser.add_argument(
        "--profiles",
        default="all",
        help="``all`` or comma-separated grid profile names",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--candle-limit", type=int, default=FULL_HISTORY_CANDLE_LIMIT)
    parser.add_argument("--coins", default="", help="Optional comma filter")
    parser.add_argument("--max-coins", type=int, default=None)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--skip-apt-gate",
        action="store_true",
        help="Research debug only: skip APT prototype parity gate",
    )
    args = parser.parse_args(argv)

    try:
        long_n, short_n = parse_sizes(args.sizes)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if abs(long_n - 1000.0) > 1e-9 or abs(short_n - 500.0) > 1e-9:
        print(f"This grid is fixed at 1000:500 (got {args.sizes})", file=sys.stderr)
        return 2

    coins_filter = [c.strip() for c in str(args.coins).split(",") if c.strip()] or None
    payload = run_grid(
        baseline_dir=args.baseline_audit_dir,
        profiles_spec=args.profiles,
        output_dir=args.output_dir,
        candle_limit=args.candle_limit,
        coins_filter=coins_filter,
        max_coins=args.max_coins,
        resume=bool(args.resume),
        skip_apt_gate=bool(args.skip_apt_gate),
    )
    if payload.get("aborted"):
        return 1
    log(f"wrote {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
