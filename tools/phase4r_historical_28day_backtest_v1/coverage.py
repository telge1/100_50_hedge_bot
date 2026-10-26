"""Causal data coverage checks for Phase 4R windows."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
WARMUP_HOURS = 168


def _parse_utc(s: str) -> datetime:
    s = s.strip().replace(" ", "T")
    if not s.endswith("Z"):
        s += "Z"
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def snapshot_span(jsonl_path: Path) -> tuple[str | None, str | None, int]:
    if not jsonl_path.is_file():
        return None, None, 0
    first = last = None
    n = 0
    with jsonl_path.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            import json

            t = json.loads(line)["candle_close_utc"]
            if first is None:
                first = t
            last = t
            n += 1
    return first, last, n


def probe_ssot_build(visible_to: str) -> dict[str, Any]:
    """Read-only probe; does not write SSOT."""
    dash = os.environ.get("PHASE1B_DASHBOARD_ROOT")
    if not dash:
        dash = str(ROOT / "backtester" / "short" / "dashboard")
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    sys.path.insert(0, dash)
    try:
        from run_pilot import load_window_from_ssot  # type: ignore

        w = load_window_from_ssot(visible_to)
        win = w.get("window") or {}
        return {
            "ssot_probe": "OK",
            "context_from_utc": win.get("context_from_utc"),
            "visible_to_utc": win.get("to_utc"),
            "price_bars": len(w.get("prices") or []),
        }
    except Exception as exc:
        return {"ssot_probe": "FAILED", "error": str(exc)}


def assess_window(
    window_id: str,
    from_utc: str,
    to_exclusive_utc: str,
    jsonl_path: Path,
) -> dict[str, Any]:
    warm_from = _parse_utc(from_utc) - __import__("datetime").timedelta(hours=WARMUP_HOURS)
    snap_lo, snap_hi, snap_n = snapshot_span(jsonl_path)
    ssot = probe_ssot_build(to_exclusive_utc)

    timeline_ok = False
    if snap_lo and snap_hi:
        timeline_ok = snap_lo <= from_utc and snap_hi >= _iso_minus_5m(to_exclusive_utc)

    warmup_ok = False
    if snap_lo:
        warmup_ok = snap_lo <= warm_from.strftime("%Y-%m-%dT%H:%M:%SZ")

    status = "COVERAGE_OK"
    notes: list[str] = []
    pilot_cached_only = window_id == "pilot_24h" and timeline_ok
    if not timeline_ok:
        status = "COVERAGE_BLOCKED"
        notes.append("No dynamic timeline JSONL spans full window with closed 5m bars.")
    if not pilot_cached_only:
        if not warmup_ok:
            status = "COVERAGE_BLOCKED"
            notes.append(f"Missing >= {WARMUP_HOURS}h SSOT context before window start.")
        if ssot.get("ssot_probe") != "OK":
            status = "COVERAGE_BLOCKED"
            notes.append(f"SSOT live build probe failed: {ssot.get('error', '')[:200]}")
    else:
        notes.append(
            "Pilot uses pre-built Phase 3A JSONL; warmup/context from original timeline build only."
        )

    return {
        "window_id": window_id,
        "from_utc": from_utc,
        "to_exclusive_utc": to_exclusive_utc,
        "warmup_required_from_utc": warm_from.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cached_timeline_from": snap_lo or "",
        "cached_timeline_to": snap_hi or "",
        "cached_timeline_bars": snap_n,
        "timeline_covers_window": timeline_ok,
        "warmup_covers_context": warmup_ok,
        "ssot_probe": ssot.get("ssot_probe"),
        "ssot_error": ssot.get("error", ""),
        "coverage_status": status,
        "notes": " | ".join(notes),
    }


def _iso_minus_5m(to_exclusive: str) -> str:
    dt = _parse_utc(to_exclusive) - __import__("datetime").timedelta(minutes=5)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
