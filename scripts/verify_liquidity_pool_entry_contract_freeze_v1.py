#!/usr/bin/env python3
"""Verify entry contract freeze v1 (+ optional mutation test)."""

from __future__ import annotations

import argparse
import json
import sys

from orderbook_analyse.liquidity_pool_entry_contract_freeze_v1.freeze import (
    EntryContractFreezeError,
    verify_entry_contract_freeze,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mutate", action="store_true", help="Run mutation detection test")
    args = parser.parse_args()
    try:
        result = verify_entry_contract_freeze(mutate=args.mutate)
    except EntryContractFreezeError as exc:
        print(json.dumps({"ok": False, "verdict": exc.verdict, "detail": str(exc)}, indent=2))
        return 1
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
