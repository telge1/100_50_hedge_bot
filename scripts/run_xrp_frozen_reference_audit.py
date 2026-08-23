#!/usr/bin/env python3
"""CLI: independent XRP frozen-reference audit (research-only)."""

from __future__ import annotations

import json
import sys

from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.xrp_frozen_reference_audit import (
    run_audit,
)


def main() -> int:
    result = run_audit()
    print(json.dumps({"verdict": result["verdict"], "summary": result["summary"]}, indent=2))
    print(f"wrote: {result['out_dir']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
