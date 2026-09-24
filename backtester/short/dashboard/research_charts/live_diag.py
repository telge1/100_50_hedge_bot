"""In-memory live-price diagnostics for Research Charts.

Client posts apply outcomes; operators can poll/tail the ring buffer to see
why forming updates are skipped or fail to paint.
Also appends NDJSON to a log file for offline/tail monitoring without auth.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
_EVENTS: deque[dict[str, Any]] = deque(maxlen=400)
_STATS: dict[str, int] = {
    "posts": 0,
    "painted": 0,
    "skipped_unchanged": 0,
    "no_chart_api": 0,
    "update_false": 0,
    "setdata_fallback": 0,
    "blocked_history": 0,
    "no_forming": 0,
    "no_pending": 0,
    "auth_401": 0,
    "errors": 0,
}

_LOG_PATH = Path(__file__).resolve().parents[1] / "logs" / "research_live_diag.ndjson"
_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_log(row: dict[str, Any]) -> None:
    try:
        with _LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def record_live_diag(payload: dict[str, Any]) -> dict[str, Any]:
    """Append one client diagnostic event and update counters."""
    row = dict(payload or {})
    row["server_ts"] = _utc_iso()
    row["server_mono"] = time.monotonic()
    reason = str(row.get("reason") or row.get("result") or "unknown")
    with _LOCK:
        _EVENTS.append(row)
        _STATS["posts"] += 1
        if reason in _STATS:
            _STATS[reason] += 1
        elif row.get("painted"):
            _STATS["painted"] += 1
        elif row.get("error"):
            _STATS["errors"] += 1
    _append_log(row)
    return {"success": True, "accepted": True, "log": str(_LOG_PATH)}


def snapshot_live_diag(*, limit: int = 80) -> dict[str, Any]:
    lim = max(1, min(int(limit or 80), 400))
    with _LOCK:
        events = list(_EVENTS)[-lim:]
        stats = dict(_STATS)
    # Compact lag view: last painted close per symbol/tf
    last_by_key: dict[str, dict[str, Any]] = {}
    for ev in events:
        sym = str(ev.get("symbol") or "")
        tf = str(ev.get("tf") or "")
        if not sym:
            continue
        key = f"{sym}|{tf}"
        last_by_key[key] = ev
    return {
        "success": True,
        "stats": stats,
        "events": events,
        "last_by_pane": last_by_key,
        "log_path": str(_LOG_PATH),
        "server_ts": _utc_iso(),
    }


def clear_live_diag() -> None:
    with _LOCK:
        _EVENTS.clear()
        for k in list(_STATS.keys()):
            _STATS[k] = 0
    try:
        if _LOG_PATH.exists():
            _LOG_PATH.write_text("", encoding="utf-8")
    except Exception:
        pass
