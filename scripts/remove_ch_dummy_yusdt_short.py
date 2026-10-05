#!/usr/bin/env python3
"""Remove accidental test row from live_forward (single signal_id)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from bot.shadow_signal_registry.ch_sync import _get_client

SIGNAL_ID = "short|e1r_cluster3_ema200_v1|v1|YUSDT|2026-02-01T12:00:00+00:00|pool-y"
TABLE = "live_forward.shadow_signals_short"
ARCHIVE = REPO / "results" / "live_forward" / "removed_dummy_yusdt_short.json"


def main() -> int:
    client = _get_client()
    q = f"SELECT * FROM {TABLE} WHERE signal_id = {{sid:String}}"
    rows = client.query(q, parameters={"sid": SIGNAL_ID})
    cols = rows.column_names
    archived = [dict(zip(cols, r)) for r in rows.result_rows]
    ARCHIVE.parent.mkdir(parents=True, exist_ok=True)
    ARCHIVE.write_text(json.dumps(archived, indent=2, default=str), encoding="utf-8")
    print("archived", len(archived), "rows to", ARCHIVE)
    if not archived:
        print("nothing to delete")
        client.close()
        return 0
    client.command(
        f"ALTER TABLE {TABLE} DELETE WHERE signal_id = {{sid:String}}",
        parameters={"sid": SIGNAL_ID},
    )
    client.close()
    print("delete mutation submitted for", SIGNAL_ID)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
