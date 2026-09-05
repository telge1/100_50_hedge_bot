#!/usr/bin/env python3
"""Verify Entry Contract V2 freeze and Expansion binding V4."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

OA_ROOT = Path(__file__).resolve().parents[1]
if str(OA_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(OA_ROOT / "src"))

from orderbook_analyse.liquidity_pool_entry_contract_v2.freeze import (
    EntryContractV2FreezeError,
    verify_entry_contract_v2_freeze,
    verify_expansion_binding_v4,
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mutate", action="store_true")
    ap.add_argument("--v2-only", action="store_true")
    ap.add_argument("--v4-only", action="store_true")
    args = ap.parse_args(argv)
    try:
        out: dict = {"ok": True}
        if not args.v4_only:
            out["v2"] = verify_entry_contract_v2_freeze(OA_ROOT, mutate=args.mutate)
        if not args.v2_only:
            out["v4"] = verify_expansion_binding_v4(OA_ROOT, mutate=args.mutate)
    except EntryContractV2FreezeError as e:
        print(json.dumps({"ok": False, "verdict": e.verdict, "detail": str(e)}, indent=2))
        return 2
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
