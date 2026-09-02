#!/usr/bin/env python3
"""Run BTC OB fight explanatory research audit (read-only)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from research.btc_ob_fight_explanatory_audit.config import OUT  # noqa: E402
from research.btc_ob_fight_explanatory_audit.run_audit import run_explanatory_audit  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="BTC OB fight explanatory audit")
    p.add_argument("--out", type=Path, default=OUT, help="Output directory")
    args = p.parse_args()
    result = run_explanatory_audit(out_dir=args.out)
    print(json.dumps(result["manifest"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
