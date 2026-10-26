#!/usr/bin/env python3
"""Rebuild merged JSONL from on-disk chunk files (Phase 4S)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parents[1]
RESULTS = ROOT / "results/phase4s_timeline_recovery_v1"
MERGED = RESULTS / "landscape_timeline_merged.jsonl"

sys.path.insert(0, str(TOOLS))
from chunk_timeline import merge_snapshots  # noqa: E402


def main() -> int:
    chunks = []
    for path in sorted((RESULTS / "chunks").glob("chunk_*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        chunks.append({"snapshots": data["snapshots"]})
    merged = merge_snapshots(chunks)
    with MERGED.open("w", encoding="utf-8") as f:
        for s in merged:
            f.write(json.dumps(s, separators=(",", ":")) + "\n")
    print(json.dumps({"bars": len(merged), "first": merged[0]["candle_close_utc"], "last": merged[-1]["candle_close_utc"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
