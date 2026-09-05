#!/usr/bin/env python3
"""CASE_02_POOL_EDGE_AGGRESSOR_EFFICIENCY_TIMELINE_V1 — diagnostic CLI."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

OA_ROOT = Path(__file__).resolve().parents[1]
if str(OA_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(OA_ROOT / "src"))

from orderbook_analyse.case_02_pool_edge_aggressor_efficiency_timeline_v1.pipeline import (
    run_case_02,
)

RAW = OA_ROOT / "data" / "orderbook_raw_shadow" / "ob200_v3"
OUT = OA_ROOT / "results" / "case_02_pool_edge_aggressor_efficiency_timeline_v1"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", default=str(OUT))
    ap.add_argument("--raw-root", default=str(RAW))
    args = ap.parse_args(argv)
    res = run_case_02(raw_root=Path(args.raw_root), out_dir=Path(args.out_dir))
    print(
        json.dumps(
            {
                "verdict": "CAUSALITY_FAILURE"
                if res["causality_failure"]
                else "CASE_02_POOL_EDGE_AGGRESSOR_EFFICIENCY_TIMELINE_V1_COMPLETE",
                "n_timeline": res["n_timeline"],
                "elapsed_s": res["manifest"]["elapsed_s"],
            },
            indent=2,
        )
    )
    return 2 if res["causality_failure"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
