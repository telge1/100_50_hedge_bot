"""Snapshot → dashboard-ready CSV/JSON (no UI)."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import bot.shadow_signal_registry.store as registry_store

Side = registry_store.Side


def _runtime() -> Path:
    return registry_store.RUNTIME

DASHBOARD_COLUMNS = [
    "StartTime",
    "EndTime",
    "Coin",
    "Status",
    "Entry",
    "SL",
    "TP",
    "PnL",
    "Endprofit +/-",
]


def dashboard_paths(side: Side) -> tuple[Path, Path]:
    base = _runtime() / f"{side}_dashboard_signals"
    return base.with_suffix(".csv"), base.with_suffix(".json")


def _parse_utc(iso: str | None) -> datetime | None:
    if not iso:
        return None
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)


def format_time_utc(iso: str | None) -> str:
    dt = _parse_utc(iso)
    if dt is None:
        return ""
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def format_pnl_pct(pnl: float | None) -> str:
    if pnl is None:
        return "OPEN"
    rounded = round(float(pnl), 2)
    if rounded > 0:
        return f"+{rounded:.2f}%"
    if rounded < 0:
        return f"{rounded:.2f}%"
    return "0.00%"


def format_endprofit(pnl: float | None, *, is_open: bool) -> str:
    if is_open or pnl is None:
        return "OPEN"
    rounded = round(float(pnl), 2)
    if rounded > 0:
        return "+"
    if rounded < 0:
        return "-"
    return "0"


def snapshot_row_to_dashboard(row: dict[str, Any]) -> dict[str, Any]:
    is_open = row.get("tracking_status") == "OPEN"
    status = "ALLOWED" if row.get("allowed") else "BLOCKED"
    pnl_raw = row.get("pnl_pct")

    if is_open:
        end_time = "OPEN"
        pnl_str = "OPEN"
        endprofit = "OPEN"
    else:
        end_time = format_time_utc(row.get("exit_time"))
        pnl_str = format_pnl_pct(pnl_raw if pnl_raw is not None else 0.0)
        endprofit = format_endprofit(pnl_raw, is_open=False)

    return {
        "StartTime": format_time_utc(row.get("decision_time")),
        "EndTime": end_time,
        "Coin": row.get("symbol") or "",
        "Status": status,
        "Entry": row.get("entry_price"),
        "SL": row.get("initial_sl"),
        "TP": row.get("tp"),
        "PnL": pnl_str,
        "Endprofit +/-": endprofit,
    }


def build_dashboard_rows(snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = [snapshot_row_to_dashboard(r) for r in snapshots]
    rows.sort(key=lambda r: r.get("StartTime") or "", reverse=True)
    return rows


def _atomic_write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=DASHBOARD_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k) for k in DASHBOARD_COLUMNS})
    tmp.replace(path)


def _atomic_write_json(path: Path, rows: list[dict[str, Any]]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def write_dashboard_export(side: Side, snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = build_dashboard_rows(snapshots)
    csv_path, json_path = dashboard_paths(side)
    _runtime().mkdir(parents=True, exist_ok=True)
    _atomic_write_csv(csv_path, rows)
    _atomic_write_json(json_path, rows)
    return rows


def write_dashboard_export_from_snapshot_file(side: Side) -> list[dict[str, Any]]:
    snap_path = _runtime() / f"{side}_snapshot.json"
    if not snap_path.is_file():
        return write_dashboard_export(side, [])
    snapshots = json.loads(snap_path.read_text(encoding="utf-8"))
    return write_dashboard_export(side, snapshots)
