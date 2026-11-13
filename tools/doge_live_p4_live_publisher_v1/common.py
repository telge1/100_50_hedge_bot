"""Shared DOGE 5m PD1 live publisher utilities (read/write guarded)."""

from __future__ import annotations

import fcntl
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import clickhouse_connect

REPO = Path(__file__).resolve().parents[2]
DASH = REPO / "backtester" / "short" / "dashboard"
WORK_ROOT = REPO / "results" / "doge_live_p4_live_publisher_v1"

LOCK_FILE = WORK_ROOT / "publisher.lock"
PID_FILE = WORK_ROOT / "publisher.pid"
STATE_FILE = WORK_ROOT / "publisher_state.json"
CHECKPOINT_FILE = WORK_ROOT / "resume_checkpoint_v1.json"

SYMBOL = "DOGEUSDT"
TIMEFRAME = "5m"
POOL_DETAIL = 1
TABLE_PUBLISH = "research_lmv2_active_snapshot_publish_v1"
COLLECTOR_STATUS_URL = os.environ.get(
    "STOCH_COLLECTOR_API_BASE", "http://127.0.0.1:8787"
).rstrip("/") + "/api/collector/status"


def write_state(payload: dict[str, Any]) -> None:
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    payload["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def read_state() -> dict[str, Any]:
    if not STATE_FILE.is_file():
        return {"status": "IDLE"}
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def acquire_lock() -> int:
    """Return open lock file fd; caller must keep it for process lifetime."""
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    lf = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise RuntimeError("another DOGE P4-LIVE publisher holds publisher.lock") from exc
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    lf.write(str(os.getpid()))
    lf.flush()
    return lf


def collector_status() -> dict[str, Any]:
    out: dict[str, Any] = {"api_url": COLLECTOR_STATUS_URL}
    try:
        with urllib.request.urlopen(COLLECTOR_STATUS_URL, timeout=3) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        out["reachable"] = True
        out["collector_state"] = body.get("collector_state") or body.get("state")
        out["websocket_connected"] = body.get("websocket_connected")
        out["desired_state"] = body.get("desired_state")
        syms = body.get("configured_symbols") or []
        out["doge_configured"] = SYMBOL in syms
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        out["reachable"] = False
        out["error"] = repr(exc)
    if PID_FILE.parent.parent.joinpath("live_collector/collector.lock").is_file():
        out["collector_lock_present"] = True
    return out


def pending_closes(
    published: list[datetime],
    last_complete_5m: datetime,
) -> list[datetime]:
    if not published:
        raise ValueError("no published closes on line")
    pub_max = max(c.astimezone(timezone.utc) for c in published)
    return pending_closes_after(pub_max, last_complete_5m)


def pending_closes_after(
    pub_max: datetime,
    last_complete_5m: datetime,
) -> list[datetime]:
    """Closes strictly after published high water through last complete 5m (sequential tail)."""
    from research_charts.mc3a_historical_runner_v1 import enumerate_decision_closes_between

    pub_max = pub_max.astimezone(timezone.utc)
    last_complete_5m = last_complete_5m.astimezone(timezone.utc)
    if last_complete_5m <= pub_max:
        return []
    first_new = pub_max + timedelta(minutes=5)
    return enumerate_decision_closes_between(first_new, last_complete_5m, TIMEFRAME)


def strict_5m_bucket_complete(client, db: str, decision_close: datetime) -> bool:
    dc = decision_close.astimezone(timezone.utc)
    step = timedelta(minutes=5)
    n = client.query(
        f"""
        SELECT count() AS n FROM {db}.candles_1m FINAL
        WHERE symbol = {{s:String}} AND is_closed = 1
          AND open_time >= {{b:DateTime64(3, 'UTC')}}
          AND open_time < {{e:DateTime64(3, 'UTC')}}
        """,
        parameters={"s": SYMBOL, "b": dc, "e": dc + step},
    ).first_item["n"]
    return int(n) >= 5


def load_checkpoint() -> dict[str, Any]:
    if CHECKPOINT_FILE.is_file():
        return json.loads(CHECKPOINT_FILE.read_text(encoding="utf-8"))
    return {}


def save_checkpoint(payload: dict[str, Any]) -> None:
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def publish_lag_seconds(last_publish: datetime | None, last_5m: datetime) -> float | None:
    if last_publish is None:
        return None
    gap = last_5m - last_publish.astimezone(timezone.utc)
    return max(0.0, gap.total_seconds() - 300.0)
