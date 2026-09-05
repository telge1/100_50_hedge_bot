#!/usr/bin/env python3
"""Build liquidity_pool_case_sequence_freeze_v1 (outcome-blind). No CASE_03 audit. No commit."""

from __future__ import annotations

import json
import sys
from pathlib import Path

OA_ROOT = Path(__file__).resolve().parents[1]
if str(OA_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(OA_ROOT / "src"))

from orderbook_analyse.liquidity_pool_case_sequence_freeze_v1.freeze import (
    FreezeError,
    write_freeze,
)

OUT = OA_ROOT / "results" / "liquidity_pool_case_sequence_freeze_v1"


def main() -> int:
    try:
        res = write_freeze(OA_ROOT, OUT)
    except FreezeError as e:
        print(json.dumps({"verdict": e.verdict, "detail": str(e)}, indent=2))
        return 2
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
