#!/usr/bin/env python3
"""Build CASE_03–05 comparative synthesis from stored audit artefacts."""

from __future__ import annotations

import json
import sys

from orderbook_analyse.case_03_05_entry_contract_comparative_synthesis_v1.synthesis import (
    build_synthesis,
)


def main() -> int:
    result = build_synthesis()
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
