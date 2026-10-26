"""Read-only: setups.json → BT-D10 overlay bundle for research chart position boxes."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CONTRACT_VERSION = "bt_d23_setup_scanner_overlay_v1"
BACKTESTER_SOURCE = "bt_d23_pool_first_setup_scanner_v1"
PLANNED_LABEL = "PLANNED SHORT — NOT EXECUTED"
PREVIEW_HOURS = 4


def _parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def setups_document_to_overlay_bundle(doc: dict[str, Any], *, symbol: str | None = None) -> dict[str, Any]:
    sym = str(symbol or doc.get("symbol") or "DOGEUSDT").upper()
    records: list[dict[str, Any]] = []
    for row in doc.get("setups") or []:
        decision = str(row.get("decision_utc") or "")
        start = _parse_utc(decision)
        end = start + timedelta(hours=PREVIEW_HOURS)
        sid = str(row.get("setup_id") or "")
        records.append(
            {
                "signal_id": sid,
                "setup_id": sid,
                "side": "SHORT",
                "lifecycle_state": "SIGNAL",
                "signal_time_utc": decision,
                "decision_close_utc": decision,
                "order_active_from_utc": decision,
                "pending_start_utc": decision,
                "display_end_utc": _iso(end),
                "entry_price_planned": row.get("planned_entry"),
                "stop_loss_price_planned": row.get("planned_sl"),
                "take_profit_price_planned": row.get("planned_tp"),
                "entry_upper_origin_ids": row.get("upper_origin_ids"),
                "pool_lower": row.get("pool_lower"),
                "pool_upper": row.get("pool_upper"),
                "status_label": PLANNED_LABEL,
                "metadata": {
                    "plan_only": True,
                    "not_executed": True,
                    "origin_set_key": row.get("origin_set_key"),
                    "dedup_emit_reason": row.get("dedup_emit_reason"),
                },
            }
        )
    return {
        "contract_version": CONTRACT_VERSION,
        "backtester_source": BACKTESTER_SOURCE,
        "symbol": sym,
        "run_id": doc.get("run_id"),
        "config_hash": doc.get("config_hash"),
        "records": records,
    }


def load_setups_overlay(run_dir: Path, *, symbol: str = "DOGEUSDT") -> dict[str, Any]:
    path = run_dir / "setups.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    return setups_document_to_overlay_bundle(doc, symbol=symbol)
