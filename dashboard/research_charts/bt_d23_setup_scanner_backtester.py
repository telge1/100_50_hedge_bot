"""BT-D23 setup scanner → research chart (planned short boxes only, read-only)."""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .bt_d12_latest_backtester import resolve_pools_ob_repo

STRATEGY_ID = "pool_first_setup_scanner_bt_d23"
DISPLAY_NAME = "Pool-First Setup Scanner (PLANNED)"
BACKTESTER_SOURCE = "bt_d23_setup_scanner_chart_v1"
DRAWING_PREFIX = "btd23-planned-"
PLANNED_LABEL = "PLANNED SHORT — NOT EXECUTED"
PREVIEW_HOURS = 4

RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,120}$", re.I)
CONTRACT_VERSION = "bt_d23_setup_scanner_chart_integration_v1"


def _pools_ob_root() -> Path:
    return resolve_pools_ob_repo()


def _scanner_runs_root() -> Path:
    return _pools_ob_root() / "results" / "setup_scanner_runs"


def _ensure_import_paths() -> None:
    root = _pools_ob_root()
    for rel in (
        "tools/phase_bt_d23_pool_first_setup_scanner_v1",
        "tools/phase_bt_d10_existing_viewer_overlay_integration_v1",
    ):
        p = str(root / rel)
        if p not in sys.path:
            sys.path.insert(0, p)


def _parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def discover_setup_scanner_runs() -> list[dict[str, Any]]:
    runs_root = _scanner_runs_root()
    repo = _pools_ob_root()
    runs: list[dict[str, Any]] = []
    if not runs_root.is_dir():
        return runs
    for path in sorted(runs_root.iterdir()):
        if not path.is_dir():
            continue
        setups_path = path / "setups.json"
        if not setups_path.is_file():
            continue
        try:
            doc = json.loads(setups_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if str(doc.get("contract_version") or "") != "bt_d23_pool_first_setup_scanner_v1":
            continue
        times = [s.get("decision_utc") for s in doc.get("setups") or [] if s.get("decision_utc")]
        span = {"start": min(times), "end": max(times)} if times else {}
        runs.append(
            {
                "run_id": path.name,
                "symbol": str(doc.get("symbol") or "DOGEUSDT").upper(),
                "setup_count": int(doc.get("setup_count") or len(doc.get("setups") or [])),
                "config_hash": doc.get("config_hash"),
                "scan_start_utc": doc.get("scan_start_utc"),
                "scan_end_utc": doc.get("scan_end_utc"),
                "time_span": span,
                "setups_path": str(setups_path.relative_to(repo)),
                "payload_kind": CONTRACT_VERSION,
            }
        )
    return runs


def resolve_setups_path(run_id: str) -> Path | None:
    rid = str(run_id or "").strip()
    if not rid or not RUN_ID_RE.match(rid):
        return None
    runs_root = _scanner_runs_root()
    candidate = (runs_root / rid / "setups.json").resolve()
    try:
        candidate.relative_to(runs_root.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def load_setups_document(run_id: str) -> tuple[dict[str, Any] | None, list[str]]:
    path = resolve_setups_path(run_id)
    if path is None:
        return None, ["run_not_found"]
    doc = json.loads(path.read_text(encoding="utf-8"))
    if str(doc.get("contract_version") or "") != "bt_d23_pool_first_setup_scanner_v1":
        return None, ["contract_version_mismatch"]
    setups = doc.get("setups") or []
    if not setups:
        return None, ["empty_setups"]
    return doc, []


def build_summary(doc: dict[str, Any], run_id: str) -> dict[str, Any]:
    setups = doc.get("setups") or []
    times = [s["decision_utc"] for s in setups if s.get("decision_utc")]
    origin_keys = {s.get("origin_set_key") for s in setups if s.get("origin_set_key")}
    return {
        "run_id": run_id,
        "strategy_id": STRATEGY_ID,
        "display_name": DISPLAY_NAME,
        "payload_kind": CONTRACT_VERSION,
        "config_hash": doc.get("config_hash"),
        "symbol": str(doc.get("symbol") or "DOGEUSDT").upper(),
        "setup_count": len(setups),
        "unique_origin_sets": len(origin_keys),
        "time_span": {"start": min(times), "end": max(times)} if times else {},
        "scan_start_utc": doc.get("scan_start_utc"),
        "scan_end_utc": doc.get("scan_end_utc"),
        "tp_selection_note": doc.get("tp_selection_note"),
        "filled_trade_count": 0,
        "signal_count": len(setups),
        "plan_only": True,
    }


def setup_to_position_spec(setup: dict[str, Any], *, run_id: str, config_hash: str | None) -> dict[str, Any]:
    decision = str(setup.get("decision_utc") or "")
    start = _parse_utc(decision)
    end = start + timedelta(hours=PREVIEW_HOURS)
    sid = str(setup.get("setup_id") or "")
    safe = sid.replace(":", "").replace("|", "_").replace("+", "")
    entry = float(setup["planned_entry"])
    sl = float(setup["planned_sl"])
    tp = float(setup["planned_tp"])
    dist_sl = setup.get("distance_entry_to_sl_pct")
    dist_tp = setup.get("distance_entry_to_tp_pct")
    rr = setup.get("planned_risk_reward")
    tooltip = (
        f"{PLANNED_LABEL}\n"
        f"setup_id={sid}\n"
        f"entry={entry} sl={sl} tp={tp}\n"
        f"pool={setup.get('pool_lower')}|{setup.get('pool_upper')}\n"
        f"origins={setup.get('origin_set_key') or setup.get('upper_origin_ids')}"
    )
    if dist_sl is not None and dist_tp is not None and rr is not None:
        tooltip += f"\nΔentry→SL {dist_sl:.3f}% · Δentry→TP {dist_tp:.3f}% · R:R {rr:.3f} (planned)"
    return {
        "drawing_id": f"{DRAWING_PREFIX}{safe}",
        "drawing_type": "short_position",
        "symbol": str(setup.get("symbol") or "DOGEUSDT").upper(),
        "timeframe": "5m",
        "start": start,
        "end": end,
        "entry": entry,
        "stop": sl,
        "target": tp,
        "signal_id": sid,
        "setup_id": sid,
        "direction": "SHORT",
        "source": BACKTESTER_SOURCE,
        "lifecycle_state": "SIGNAL",
        "metadata": {
            "origin": BACKTESTER_SOURCE,
            "strategy_id": STRATEGY_ID,
            "run_id": run_id,
            "setup_id": sid,
            "plan_only": True,
            "not_executed": True,
            "preview_only": True,
            "status_label": PLANNED_LABEL,
            "origin_set_key": setup.get("origin_set_key"),
            "upper_origin_ids": setup.get("upper_origin_ids"),
            "pool_lower": setup.get("pool_lower"),
            "pool_upper": setup.get("pool_upper"),
            "decision_utc": decision,
            "distance_entry_to_sl_pct": dist_sl,
            "distance_entry_to_tp_pct": dist_tp,
            "planned_risk_reward": rr,
            "tooltip": tooltip,
            "strategy_config_hash": config_hash,
        },
    }


def filter_setups_viewport(
    setups: list[dict[str, Any]],
    *,
    view_start_unix: int,
    view_end_unix: int,
    preview_hours: int = PREVIEW_HOURS,
) -> list[dict[str, Any]]:
    pad = int(preview_hours * 3600)
    out: list[dict[str, Any]] = []
    for s in setups:
        decision = str(s.get("decision_utc") or "")
        if not decision:
            continue
        t0 = int(_parse_utc(decision).timestamp())
        t1 = t0 + pad
        if t1 >= view_start_unix and t0 <= view_end_unix:
            out.append(s)
    return out
