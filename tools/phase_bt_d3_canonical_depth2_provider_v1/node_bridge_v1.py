"""Subprocess bridge to canonical JS density adapter (Phase 1B analyzer)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
ANALYZER_JS = ROOT / "tools/phase1b_depth2_landscape_pilot_v1/analyzer_depth2_landscape.js"


class DensityNodeBridgeError(Exception):
    pass


def run_chart_path_density(
    window_payload: dict[str, Any],
    *,
    as_of_utc: str,
    slot_minutes: int = 5,
    adapter_path: str | None = None,
    timeout_s: int = 180,
) -> dict[str, Any]:
    if not ANALYZER_JS.is_file():
        raise DensityNodeBridgeError(f"analyzer missing: {ANALYZER_JS}")
    inp: dict[str, Any] = {
        "window_payload": window_payload,
        "as_of_utc": as_of_utc,
        "slot_minutes": slot_minutes,
    }
    if adapter_path:
        inp["adapter_path"] = adapter_path
    proc = subprocess.run(
        ["node", str(ANALYZER_JS)],
        input=json.dumps(inp),
        capture_output=True,
        text=True,
        timeout=timeout_s,
        cwd=str(ANALYZER_JS.parent),
    )
    if proc.returncode != 0:
        raise DensityNodeBridgeError(proc.stderr or proc.stdout or "node analyzer failed")
    return json.loads(proc.stdout)
