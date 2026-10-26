"""Phase 4T timeline builder — fixes Phase 3A visible window config per chunk."""

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


def expected_5m_bars(vis_from: str, vis_to_inclusive: str) -> int:
    a = _parse_utc(vis_from)
    b = _parse_utc(vis_to_inclusive)
    return int((b - a).total_seconds() // 300) + 1


def iter_day_chunks(vis_from: str, vis_to_inclusive: str) -> list[tuple[str, str]]:
    """~24h chunks (<= 72h export limit)."""
    start = _parse_utc(vis_from)
    end = _parse_utc(vis_to_inclusive)
    chunks: list[tuple[str, str]] = []
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(hours=24) - timedelta(minutes=5), end)
        chunks.append((_iso(cur), _iso(chunk_end)))
        cur = chunk_end + timedelta(minutes=5)
    return chunks


def iter_hour_chunks(vis_from: str, vis_to_inclusive: str, max_hours: int = MAX_EXPORT_HOURS) -> list[tuple[str, str]]:
    start = _parse_utc(vis_from)
    end = _parse_utc(vis_to_inclusive)
    chunks: list[tuple[str, str]] = []
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(hours=max_hours) - timedelta(minutes=5), end)
        chunks.append((_iso(cur), _iso(chunk_end)))
        cur = chunk_end + timedelta(minutes=5)
    return chunks


def run_timeline_on_window(window: dict[str, Any], vis_from: str, vis_to_inclusive: str) -> dict[str, Any]:
    """Run landscape_timeline.js with visible_* matching the chunk (Phase 4T fix)."""
    win = window.get("window") or {}
    vis_to = win.get("to_utc") or vis_to_inclusive
    vis_f = win.get("from_utc") or vis_from
    cfg = {
        **CFG3A,
        "slot_minutes": 5,
        "visible_from_utc": vis_f,
        "visible_to_utc": vis_to,
    }
    payload = {
        "pools": window.get("pools") or [],
        "prices": window.get("prices") or [],
        "adapter_path": None,
        "config": cfg,
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
    price_n = len(window.get("prices") or [])
    data = run_timeline_on_window(window, vis_from, vis_to_inclusive)
    snaps = data["snapshots"]
    h = hashlib.sha256(
        json.dumps([s["candle_close_utc"] for s in snaps], separators=(",", ":")).encode()
    ).hexdigest()[:16]
    return {
        "vis_from": vis_from,
        "vis_to_inclusive": vis_to_inclusive,
        "expected_bars": expected_5m_bars(vis_from, vis_to_inclusive),
        "price_rows_in_window": price_n,
        "snapshot_bars": len(snaps),
        "lifecycle_event_count": len(data.get("lifecycle_events") or []),
        "gap_rows": len(data.get("gap_timeline") or []),
        "context_from_utc": (window.get("window") or {}).get("context_from_utc"),
        "snapshot_hash": h,
        "snapshots": snaps,
        "config_visible_from": vis_from,
        "config_visible_to": vis_to_inclusive,
    }


def merge_snapshots(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_t: dict[str, dict] = {}
    for ch in chunks:
        for s in ch["snapshots"]:
            by_t[s["candle_close_utc"]] = s
    return [by_t[k] for k in sorted(by_t)]
