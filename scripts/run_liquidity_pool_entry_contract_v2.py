#!/usr/bin/env python3
"""Build Entry Contract V2 freeze + Expansion binding V4 (no market data / no smoke)."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

OA_ROOT = Path(__file__).resolve().parents[1]
if str(OA_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(OA_ROOT / "src"))

from orderbook_analyse.liquidity_pool_entry_contract_v2.freeze import (
    EntryContractV2FreezeError,
    build_entry_contract_v2_freeze,
    build_expansion_binding_v4,
    verify_entry_contract_v2_freeze,
    verify_expansion_binding_v4,
)
from orderbook_analyse.liquidity_pool_entry_contract_v2.regression import run_v1_regression


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--skip-tests", action="store_true")
    args = ap.parse_args(argv)
    try:
        # Confirm predecessors untouched
        v1 = json.loads(
            (OA_ROOT / "results/liquidity_pool_entry_contract_freeze_v1/entry_contract_v1.json").read_text()
        )
        assert (
            v1["entry_contract_freeze_sha256"]
            == "76b79cce5ceac816feade974521f0b4f876adb5ab6960e54d6e9498b93e97494"
        )
        v3 = json.loads(
            (
                OA_ROOT
                / "results/liquidity_pool_entry_contract_expansion_freeze_v3/frozen_expansion_cases_v3.json"
            ).read_text()
        )
        assert (
            v3["expansion_freeze_bundle_sha256"]
            == "48b5a69f54603e2fa55f81e887d6f45b441878c5f3493ab936b5d849e9614cd5"
        )

        reg = run_v1_regression(OA_ROOT)
        if not reg["ok"]:
            print(json.dumps({"ok": False, "verdict": reg["verdict"], "regression": reg}, indent=2))
            return 2

        if not args.skip_tests:
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "tests/test_liquidity_pool_entry_contract_v2.py",
                    "-q",
                ],
                cwd=OA_ROOT,
                capture_output=True,
                text=True,
            )
            test_blob = {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
            if proc.returncode != 0:
                print(json.dumps({"ok": False, "verdict": "ASK_BID_SYMMETRY_FAILURE", "tests": test_blob}, indent=2))
                return 2
        else:
            test_blob = {"skipped": True}

        v2 = build_entry_contract_v2_freeze(OA_ROOT)
        v2_sha = v2["entry_contract_v2_freeze_sha256"]
        (OA_ROOT / "results/liquidity_pool_entry_contract_freeze_v2/v1_regression.json").write_text(
            json.dumps(reg, indent=2) + "\n", encoding="utf-8"
        )
        (OA_ROOT / "results/liquidity_pool_entry_contract_freeze_v2/test_results.json").write_text(
            json.dumps(test_blob, indent=2) + "\n", encoding="utf-8"
        )

        v4 = build_expansion_binding_v4(OA_ROOT, entry_contract_v2_sha=v2_sha)
        verify_entry_contract_v2_freeze(OA_ROOT)
        verify_expansion_binding_v4(OA_ROOT)
        mut_v2 = verify_entry_contract_v2_freeze(OA_ROOT, mutate=True)
        mut_v4 = verify_expansion_binding_v4(OA_ROOT, mutate=True)

        out = {
            "ok": True,
            "verdict": "LP_ENTRY_CONTRACT_V2_AND_EXPANSION_BINDING_V4_FROZEN",
            "entry_contract_v2_freeze_sha256": v2_sha,
            "expansion_v4_binding_sha256": v4["expansion_v4_binding_sha256"],
            "v1_regression": reg["verdict"],
            "mutation_v2": mut_v2,
            "mutation_v4": mut_v4,
            "exp_market_queries": 0,
            "outcomes_read": 0,
        }
        print(json.dumps(out, indent=2))
        return 0
    except EntryContractV2FreezeError as e:
        print(json.dumps({"ok": False, "verdict": e.verdict, "detail": str(e)}, indent=2))
        return 2
    except AssertionError as e:
        print(json.dumps({"ok": False, "verdict": "ENTRY_CONTRACT_V2_FREEZE_INTEGRITY_FAILURE", "detail": str(e)}, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
