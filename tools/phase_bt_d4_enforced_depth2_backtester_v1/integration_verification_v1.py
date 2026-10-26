"""Parity verification for research integration path."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

TOOLS_D3 = Path(__file__).resolve().parents[1] / "phase_bt_d3_canonical_depth2_provider_v1"
sys.path.insert(0, str(TOOLS_D3))

from parity_compare_v1 import compare_band_sets  # noqa: E402


def bands_from_snapshot(snap: dict[str, Any]) -> list[dict[str, Any]]:
    return list(snap.get("upper_bands") or []) + list(snap.get("lower_bands") or [])


def load_golden(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return (payload.get("upper") or []) + (payload.get("lower") or [])


def verify_sep25_golden(snap: dict[str, Any], golden_path: Path) -> dict[str, Any]:
    golden = load_golden(golden_path)
    actual = bands_from_snapshot(snap)
    cmp = compare_band_sets(golden, actual)
    return {
        "ok": cmp["ok"]
        and snap.get("upper_band_count") == 39
        and snap.get("lower_band_count") == 176
        and snap.get("total_band_count") == 215,
        "compare": cmp,
        "counts": {
            "upper": snap.get("upper_band_count"),
            "lower": snap.get("lower_band_count"),
            "total": snap.get("total_band_count"),
        },
    }
