#!/usr/bin/env python3
"""Verify liquidity_pool_case_sequence_freeze_v1; non-zero on any drift/mutation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

OA_ROOT = Path(__file__).resolve().parents[1]
if str(OA_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(OA_ROOT / "src"))

from orderbook_analyse.liquidity_pool_case_sequence_freeze_v1.freeze import (
    FreezeError,
    verify_freeze,
)

OUT = OA_ROOT / "results" / "liquidity_pool_case_sequence_freeze_v1"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", default=str(OUT))
    args = ap.parse_args(argv)
    try:
        res = verify_freeze(OA_ROOT, Path(args.out_dir))
    except FreezeError as e:
        print(json.dumps({"ok": False, "verdict": e.verdict, "detail": str(e)}, indent=2))
        return 2
    print(json.dumps({"ok": True, **res}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
