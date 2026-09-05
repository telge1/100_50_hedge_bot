#!/usr/bin/env python3
"""Build entry contract freeze v1."""

from __future__ import annotations

import json

from orderbook_analyse.liquidity_pool_entry_contract_freeze_v1.freeze import (
    build_entry_contract_bundle,
)


def main() -> int:
    result = build_entry_contract_bundle()
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
