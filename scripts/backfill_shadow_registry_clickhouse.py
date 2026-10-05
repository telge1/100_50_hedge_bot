#!/usr/bin/env python3
"""One-shot backfill registry snapshots into live_forward ClickHouse."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from bot.shadow_signal_registry.ch_sync import apply_ch_schema, sync_snapshots_to_clickhouse
from bot.shadow_signal_registry.store import RUNTIME


def main() -> int:
    apply_ch_schema()
    for side in ("long", "short"):
        snap_path = RUNTIME / f"{side}_snapshot.json"
        rows = json.loads(snap_path.read_text(encoding="utf-8")) if snap_path.is_file() else []
        hashes: dict[str, str] = {}
        sync_snapshots_to_clickhouse(side, rows, hashes, force=True)
        print(side, "backfill rows", len(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
