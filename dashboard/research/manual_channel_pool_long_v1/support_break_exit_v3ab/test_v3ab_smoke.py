#!/usr/bin/env python3
"""Smoke: V3AB manifest and drift preflight."""

from __future__ import annotations

import json
from pathlib import Path

V3AB = Path(__file__).resolve().parent
manifest = json.loads((V3AB / "l1_v3ab_run_manifest.json").read_text(encoding="utf-8"))

assert manifest["drawing_health"]["drift"] is False
assert manifest["drawings_sha256"]
assert (V3AB / "l1_v3ab_trades_all.csv").is_file()
print("OK", manifest["run_id"])
