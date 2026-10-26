#!/usr/bin/env python3
"""CLI: BT-D7 integration_result.json → BT-D10 overlay bundle (isolated)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

from overlay_export_v1 import export_overlay_bundle, write_overlay_bundle  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Export BT-D10 overlay contract from BT-D7 integration_result.json")
    parser.add_argument("integration_result", type=Path)
    parser.add_argument("-o", "--output", type=Path, default=None)
    parser.add_argument("--symbol", default="DOGEUSDT")
    args = parser.parse_args()

    raw = json.loads(args.integration_result.read_text(encoding="utf-8"))
    bundle = export_overlay_bundle(raw, integration_path=args.integration_result, symbol=args.symbol.upper())
    out = args.output or (
        args.integration_result.parent / "overlay_bundle_bt_d10_v1.json"
    )
    write_overlay_bundle(out, bundle)
    print(json.dumps({"written": str(out), "records": len(bundle["records"])}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
