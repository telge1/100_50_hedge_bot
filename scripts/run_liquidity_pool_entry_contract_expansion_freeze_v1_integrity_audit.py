#!/usr/bin/env python3
"""Independent integrity audit of expansion freeze v1; build v2 on dedup failure."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

OA_ROOT = Path(__file__).resolve().parents[1]
if str(OA_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(OA_ROOT / "src"))

from orderbook_analyse.liquidity_pool_entry_contract_expansion_freeze_v1.integrity_audit import (
    IntegrityAuditError,
    run_integrity_audit,
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.parse_args(argv)
    try:
        res = run_integrity_audit(OA_ROOT)
    except IntegrityAuditError as e:
        print(json.dumps({"ok": False, "verdict": e.verdict, "detail": str(e)}, indent=2))
        return 2
    except Exception as e:
        from orderbook_analyse.liquidity_pool_entry_contract_expansion_freeze_v1.freeze import (
            ExpansionFreezeError,
        )

        if isinstance(e, ExpansionFreezeError):
            print(json.dumps({"ok": False, "verdict": e.verdict, "detail": str(e)}, indent=2))
            return 2
        raise
    out = {
        "ok": True,
        "verdict": res["verdict"],
        "out_dir": res["out_dir"],
        "violation_count": res["violation_count"],
        "v1_hash": res["v1_hash"],
    }
    if "v2" in res:
        out["v2"] = {
            "verdict": res["v2"]["verdict"],
            "expansion_freeze_bundle_sha256": res["v2"]["expansion_freeze_bundle_sha256"],
            "out_dir": res["v2"]["out_dir"],
            "selected_count": res["v2"]["selected_count"],
            "removed_count": len(res["v2"]["replacement"]["removed_from_v1"]),
            "added_count": len(res["v2"]["replacement"]["added_in_v2"]),
        }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
