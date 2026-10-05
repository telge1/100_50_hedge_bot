#!/usr/bin/env python3
"""Re-insert shadow CH rows with UTC-correct datetimes and higher version (no DELETE/UPDATE)."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from bot.shadow_signal_registry.ch_sync import (
    LONG_TABLE,
    SHORT_TABLE,
    _get_client,
    _insert_batch,
    snapshot_to_ch_row,
)
from bot.shadow_signal_registry.store import RUNTIME


def _max_versions(client, table: str) -> dict[str, int]:
    sql = f"SELECT signal_id, max(version) AS mv FROM {table} GROUP BY signal_id"
    result = client.query(sql)
    out: dict[str, int] = {}
    for sid, mv in result.result_rows:
        out[str(sid)] = int(mv or 0)
    return out


def repair_side(side: str) -> int:
    snap_path = RUNTIME / f"{side}_snapshot.json"
    rows = json.loads(snap_path.read_text(encoding="utf-8")) if snap_path.is_file() else []
    if not rows:
        print(side, "no snapshots")
        return 0
    client = _get_client()
    table = LONG_TABLE if side == "long" else SHORT_TABLE
    versions = _max_versions(client, table)
    repair_rows: list[dict] = []
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    for row in rows:
        sid = row.get("signal_id")
        if not sid:
            continue
        ch = snapshot_to_ch_row(side, row)
        base_v = int(ch.get("version") or 0)
        ch["version"] = max(versions.get(sid, 0) + 1, base_v + 1, now_ms)
        repair_rows.append(ch)
    _insert_batch(client, side, repair_rows)
    client.close()
    print(side, "repaired rows", len(repair_rows))
    return len(repair_rows)


def main() -> int:
    total = repair_side("long") + repair_side("short")
    print("total repaired", total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
