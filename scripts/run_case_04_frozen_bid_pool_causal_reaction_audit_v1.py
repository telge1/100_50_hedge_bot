#!/usr/bin/env python3
"""CASE_04 frozen BID pool causal reaction audit — reuses CASE_03 engine (parametrized)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

OA_ROOT = Path(__file__).resolve().parents[1]
if str(OA_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(OA_ROOT / "src"))

from orderbook_analyse.case_03_frozen_bid_pool_causal_reaction_audit_v1.pipeline import (
    CASE_04_SPEC,
    run_audit,
)

RAW = OA_ROOT / "data" / "orderbook_raw_shadow" / "ob200_v3"
OUT = OA_ROOT / "results" / "case_04_frozen_bid_pool_causal_reaction_audit_v1"


def main() -> int:
    res = run_audit(repo_root=OA_ROOT, raw_root=RAW, out_dir=OUT, spec=CASE_04_SPEC)
    print(json.dumps(res, indent=2, default=str))
    v = str(res.get("verdict") or "")
    if "FAILURE" in v or v.endswith("DATA_BLOCKED") or v.endswith("PREVIOUSLY_EXPOSED"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
