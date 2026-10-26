"""Chunked Phase 3A-compatible timeline build (read-only SSOT)."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ssot_adapter import MAX_EXPORT_HOURS, build_landscape_window

ROOT = Path(__file__).resolve().parents[2]
TOOLS3A = ROOT / "tools/phase3a_dynamic_pool_landscape_timeline_v1"
TIMELINE_JS = TOOLS3A / "landscape_timeline.js"
CFG3A = json.loads((TOOLS3A / "research_config_v1.json").read_text(encoding="utf-8"))


def _parse_utc(s: str) -> datetime:
    s = s.strip().replace(" ", "T")
    if not s.endswith("Z"):
        s += "Z"
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iter_chunks(vis_from: str, vis_to_inclusive: str, max_hours: int = MAX_EXPORT_HOURS) -> list[tuple[str, str]]:
    start = _parse_utc(vis_from)
    end = _parse_utc(vis_to_inclusive)
    chunks: list[tuple[str, str]] = []
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(hours=max_hours) - timedelta(minutes=5), end)
        chunks.append((_iso(cur), _iso(chunk_end)))
        cur = chunk_end + timedelta(minutes=5)
    return chunks


def expected_5m_bars(vis_from: str, vis_to_inclusive: str) -> int:
    a = _parse_utc(vis_from)
    b = _parse_utc(vis_to_inclusive)
    return int((b - a).total_seconds() // 300) + 1


def run_timeline_on_window(window: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "pools": window.get("pools") or [],
        "prices": window.get("prices") or [],
        "adapter_path": None,
        "config": {**CFG3A, "slot_minutes": 5},
    }
    proc = subprocess.run(
        ["node", str(TIMELINE_JS)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=7200,
        cwd=str(TOOLS3A),
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout or "timeline build failed")
    return json.loads(proc.stdout)


def build_chunk(vis_from: str, vis_to_inclusive: str) -> dict[str, Any]:
    window = build_landscape_window(vis_from, vis_to_inclusive)
    data = run_timeline_on_window(window)
    snaps = data["snapshots"]
    h = hashlib.sha256(
        json.dumps(
            [
                {
                    "t": s["candle_close_utc"],
                    "u": len(s.get("upper_pools") or []),
                    "l": len(s.get("lower_pools") or []),
                }
                for s in snaps
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()[:16]
    return {
        "vis_from": vis_from,
        "vis_to_inclusive": vis_to_inclusive,
        "expected_bars": expected_5m_bars(vis_from, vis_to_inclusive),
        "snapshot_bars": len(snaps),
        "lifecycle_events": len(data.get("lifecycle_events") or []),
        "gap_rows": len(data.get("gap_timeline") or []),
        "context_from_utc": (window.get("window") or {}).get("context_from_utc"),
        "snapshot_hash": h,
        "snapshots": snaps,
        "lifecycle_events": data.get("lifecycle_events") or [],
    }


def merge_snapshots(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_t: dict[str, dict] = {}
    for ch in chunks:
        for s in ch["snapshots"]:
            by_t[s["candle_close_utc"]] = s
    return [by_t[k] for k in sorted(by_t)]
