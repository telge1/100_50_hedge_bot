#!/usr/bin/env python3
"""POST_CASE_02_NEXT_POOL_CAUSAL_REACTION_AUDIT_V1 — read-only CLI."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

OA_ROOT = Path(__file__).resolve().parents[1]
if str(OA_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(OA_ROOT / "src"))

from orderbook_analyse.post_case_02_next_pool_causal_reaction_audit_v1.pipeline import (
    run_audit,
)

RAW = OA_ROOT / "data" / "orderbook_raw_shadow" / "ob200_v3"
OUT = OA_ROOT / "results" / "post_case_02_next_pool_causal_reaction_audit_v1"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", default=str(OUT))
    ap.add_argument("--raw-root", default=str(RAW))
    args = ap.parse_args(argv)
    res = run_audit(raw_root=Path(args.raw_root), out_dir=Path(args.out_dir))
    print(json.dumps({"verdict": res.get("verdict"), "first_available_ts": res.get("first_available_ts"), "insufficient_room": res.get("insufficient_room")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
