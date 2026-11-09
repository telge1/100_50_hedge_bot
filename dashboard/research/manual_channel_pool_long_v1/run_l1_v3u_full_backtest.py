#!/usr/bin/env python3
"""L1 V3U — post-sync INDEPENDENT_SUPPORT full backtest (no strategy changes)."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROBE_DIR = Path(__file__).resolve().parent
RUN_ID = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
PREFIX = "l1_v3u"
OUT = PROBE_DIR
V3S_MANIFEST = OUT / "l1_v3s_run_manifest.json"

# Reuse V3S runner body with different prefix/reference.
import run_l1_v3s_full_backtest as v3s

v3s.PREFIX = PREFIX
v3s.RUN_ID = RUN_ID
v3s.OUT = OUT


def _load_v3s_ids() -> tuple[str, list[str]]:
    if not V3S_MANIFEST.is_file():
        return "", []
    data = json.loads(V3S_MANIFEST.read_text(encoding="utf-8"))
    sha = str(data.get("drawings_sha256") or "")
    ids = list(data.get("drawing_ids_all") or [])
    return sha, ids


v3s._load_previous_manifest_ids = lambda: _load_v3s_ids()  # type: ignore[method-assign]
v3s.V3P_MANIFEST = V3S_MANIFEST

if __name__ == "__main__":
    raise SystemExit(v3s.main())
