"""Append-only, crash-safe shadow session journal (JSONL per UTC day)."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import RULE_VERSION, SHADOW_DIR_NAME, SHADOW_RETENTION_DAYS


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def shadow_root(base: Path | None = None) -> Path:
    root = base or (Path(__file__).resolve().parents[1] / "logs" / SHADOW_DIR_NAME)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _day_path(root: Path, when: datetime | None = None) -> Path:
    when = when or _utc_now()
    return root / f"sessions_{when.strftime('%Y%m%d')}.jsonl"


def new_session_record(
    *,
    session_id: str,
    symbol: str,
    breakpoint: float,
    target_wall: dict[str, Any] | None,
    armed_at: str | None = None,
    triggered_at: str | None = None,
    state: str = "TRIGGERED",
    reason_codes: list[str] | None = None,
    metrics: dict[str, Any] | None = None,
    event_time: str | None = None,
    available_at: str | None = None,
    data_gap: bool = False,
) -> dict[str, Any]:
    now = _utc_now().isoformat().replace("+00:00", "Z")
    return {
        "schema": "wall_decision_shadow_v1",
        "rule_version": RULE_VERSION,
        "session_id": session_id,
        "symbol": str(symbol).upper(),
        "breakpoint": breakpoint,
        "target_wall": target_wall,
        "armed_at": armed_at,
        "triggered_at": triggered_at or now,
        "state": state,
        "reason_codes": list(reason_codes or []),
        "metrics": metrics or {},
        "event_time": event_time or now,
        "available_at": available_at or now,
        "data_gap": bool(data_gap),
        "recorded_at": now,
    }


def append_session_event(record: dict[str, Any], *, base: Path | None = None) -> Path:
    """Atomic append: write line to temp then os.replace into daily JSONL."""
    root = shadow_root(base)
    path = _day_path(root)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=".wd_shadow_", suffix=".tmp", dir=str(root))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            if path.exists():
                tmp.write(path.read_text(encoding="utf-8"))
            tmp.write(line)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
    _prune_old(root)
    return path


def _prune_old(root: Path) -> None:
    cutoff = _utc_now().timestamp() - SHADOW_RETENTION_DAYS * 86400
    for p in root.glob("sessions_*.jsonl"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
        except OSError:
            continue


def list_recent_sessions(*, limit: int = 50, base: Path | None = None) -> list[dict[str, Any]]:
    root = shadow_root(base)
    files = sorted(root.glob("sessions_*.jsonl"), reverse=True)
    out: list[dict[str, Any]] = []
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(out) >= limit:
                return out
    return out
