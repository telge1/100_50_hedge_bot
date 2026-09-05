#!/usr/bin/env python3
"""Verify expansion freeze v3; non-zero on drift/mutation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

OA_ROOT = Path(__file__).resolve().parents[1]
if str(OA_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(OA_ROOT / "src"))

from orderbook_analyse.liquidity_pool_entry_contract_expansion_freeze_v1.freeze_v3 import (
    ExpansionV3Error,
    verify_expansion_freeze_v3,
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mutate", action="store_true")
    args = ap.parse_args(argv)
    try:
        res = verify_expansion_freeze_v3(OA_ROOT, mutate=args.mutate)
    except ExpansionV3Error as e:
        print(json.dumps({"ok": False, "verdict": e.verdict, "detail": str(e)}, indent=2))
        return 2
    print(json.dumps({"ok": True, **res}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
