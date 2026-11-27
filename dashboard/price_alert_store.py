"""Shared price-alert persistence (active alerts + fired trigger events)."""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

_project_root = Path(__file__).resolve().parent.parent
STATE_FILE = _project_root / "data" / "state" / "price_alert.json"
FIRED_FILE = _project_root / "data" / "state" / "price_alert_fired.jsonl"

_store_lock = threading.Lock()


def alert_key(symbol: str, target_price: float, trigger: str) -> str:
    return f"{str(symbol).strip().upper()}_{float(target_price):.6f}_{str(trigger).strip().lower()}"


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        result: dict[str, Any] = {}
        for k, v in data.items():
            if isinstance(v, dict) and "target_price" in v and "trigger" in v:
                sym = v.get("symbol") or k
                tp = v.get("target_price")
                tr = v.get("trigger", "above")
                new_key = (
                    f"{sym}_{tp:.6f}_{tr}"
                    if isinstance(tp, (int, float))
                    else f"{sym}_{tp}_{tr}"
                )
                result[new_key] = {**v, "symbol": str(sym).strip().upper()}
            else:
                result[k] = v
        return result
    except Exception:
        return {}


def save_state(data: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not data:
        if STATE_FILE.exists():
            try:
                STATE_FILE.unlink(missing_ok=True)
            except Exception:
                pass
        return
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def append_fired_event(
    *,
    alert_key: str,
    symbol: str,
    target_price: float,
    trigger: str,
    fired_price: float,
    source: str,
) -> dict[str, Any]:
    event_id = str(uuid.uuid4())
    row = {
        "event_id": event_id,
        "alert_key": alert_key,
        "symbol": str(symbol).strip().upper(),
        "target_price": float(target_price),
        "trigger": str(trigger),
        "fired_price": float(fired_price),
        "fired_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "fired_at_unix": int(time.time()),
        "source": source,
    }
    FIRED_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(FIRED_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def list_fired_events(*, since_unix: int = 0, limit: int = 50) -> list[dict[str, Any]]:
    if not FIRED_FILE.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with open(FIRED_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                ts = int(row.get("fired_at_unix") or 0)
                if ts < since_unix:
                    continue
                out.append(row)
    except Exception:
        return []
    out.sort(key=lambda r: int(r.get("fired_at_unix") or 0))
    if len(out) > limit:
        out = out[-limit:]
    return out


def condition_met(current: float, target: float, trigger: str) -> bool:
    if trigger == "above":
        return current >= target
    if trigger == "below":
        return current <= target
    return False


def crossed_level(
    prev: float | None,
    current: float,
    target: float,
    trigger: str,
) -> bool:
    """Detect newly satisfied crossing between prev and current."""
    if prev is None or not (isinstance(prev, (int, float))):
        return condition_met(current, target, trigger)
    p = float(prev)
    c = float(current)
    t = float(target)
    if trigger == "above":
        return c >= t and p < t
    if trigger == "below":
        return c <= t and p > t
    return False