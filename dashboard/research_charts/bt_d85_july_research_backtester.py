"""BT-D85 July 2026 no-pause research trades → Research Chart overlay (read-only)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .bt_d12_latest_backtester import resolve_pools_ob_repo

STRATEGY_ID = "bt_d85_july_ema_v3_no_pause_research"
DISPLAY_NAME = "BT-D85 · Juli 2026 · EMA V3 · 25 Trades"
BACKTESTER_SOURCE = "bt_d85_july_chart_overlay_v1"
BOX_PREFIX = "btd85-box-"
LINK_PREFIX = "btd85-link-"
MARKER_PREFIX = "btd85-mk-"
CONTRACT_VERSION = "bt_d86a_dashboard_overlay_v1"
SYMBOL = "DOGEUSDT"
EXPECTED_FILLS = 25
EXPECTED_TP = 10
EXPECTED_SL = 15
MIN_BOX_WIDTH_SEC = 300
FILL_DISCLAIMER = "BT-D85 research replay · SIMULATED — NOT EXCHANGE CONFIRMED"


def _parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ledger_paths() -> tuple[Path, Path]:
    pools = (
        resolve_pools_ob_repo()
        / "results"
        / "phase_bt_d85_july_2026_no_pause_research_v1"
        / "trade_ledger_no_pause.json"
    )
    bundled = Path(__file__).resolve().parent / "data" / "bt_d85_july_trade_ledger_no_pause.json"
    return pools, bundled


def load_trade_ledger() -> tuple[list[dict[str, Any]], Path, list[str]]:
    pools_path, bundled_path = ledger_paths()
    path = pools_path if pools_path.is_file() else bundled_path
    errs: list[str] = []
    if not path.is_file():
        return [], path, ["ledger_not_found"]
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return [], path, ["ledger_read_error"]
    if not isinstance(rows, list):
        return [], path, ["ledger_not_array"]
    return rows, path, errs


def validate_ledger(rows: list[dict[str, Any]]) -> list[str]:
    errs: list[str] = []
    if len(rows) != EXPECTED_FILLS:
        errs.append(f"fill_count:{len(rows)}")
    tp = sum(1 for r in rows if r.get("exit_reason") == "SIMULATED_TP")
    sl = sum(1 for r in rows if r.get("exit_reason") == "SIMULATED_SL")
    amb = sum(1 for r in rows if r.get("exit_reason") == "SIMULATED_EXIT_AMBIGUOUS")
    if tp != EXPECTED_TP:
        errs.append(f"tp_count:{tp}")
    if sl != EXPECTED_SL:
        errs.append(f"sl_count:{sl}")
    if amb:
        errs.append(f"ambiguous_count:{amb}")
    pids = set()
    for r in rows:
        if not r.get("fill_time_utc") or not r.get("exit_time_utc"):
            errs.append("missing_fill_or_exit_time")
            break
        if r.get("entry_price") is None or r.get("exit_price") is None:
            errs.append("missing_prices")
            break
        pid = str(r.get("position_id") or "")
        if not pid or pid in pids:
            errs.append("duplicate_or_missing_position_id")
            break
        pids.add(pid)
    return errs


def build_summary(rows: list[dict[str, Any]], *, ledger_path: Path) -> dict[str, Any]:
    times = []
    for r in rows:
        times.append(r["fill_time_utc"])
        times.append(r["exit_time_utc"])
    tp = sum(1 for r in rows if r.get("exit_reason") == "SIMULATED_TP")
    sl = sum(1 for r in rows if r.get("exit_reason") == "SIMULATED_SL")
    return {
        "contract_version": CONTRACT_VERSION,
        "strategy_id": STRATEGY_ID,
        "symbol": SYMBOL,
        "trade_count": len(rows),
        "tp_count": tp,
        "sl_count": sl,
        "marker_entry_count": len(rows),
        "marker_exit_count": len(rows),
        "fill_disclaimer": FILL_DISCLAIMER,
        "execution_model": "BT-D83 D46 research execution (July 2026 no post-SL pause)",
        "time_span": {"start": min(times), "end": max(times)} if times else {},
        "ledger_path": str(ledger_path),
        "payload_kind": "bt_d85_july_chart_integration_v1",
    }


def trade_to_position_spec(row: dict[str, Any]) -> dict[str, Any]:
    pid = str(row["position_id"])
    entry_ts = _parse_utc(str(row["fill_time_utc"]))
    exit_ts = _parse_utc(str(row["exit_time_utc"]))
    if exit_ts < entry_ts:
        exit_ts = entry_ts + timedelta(seconds=MIN_BOX_WIDTH_SEC)
    if (exit_ts - entry_ts).total_seconds() < MIN_BOX_WIDTH_SEC:
        exit_ts = entry_ts + timedelta(seconds=MIN_BOX_WIDTH_SEC)
    reason = str(row.get("exit_reason") or "")
    status = "EXIT_TP" if reason == "SIMULATED_TP" else "EXIT_SL"
    return {
        "drawing_id": f"{BOX_PREFIX}{pid}",
        "drawing_type": "short_position",
        "start": entry_ts,
        "end": exit_ts,
        "entry": float(row["entry_price"]),
        "stop": float(row["sl_price"]),
        "target": float(row["tp_price"]),
        "metadata": {
            "origin": BACKTESTER_SOURCE,
            "strategy_id": STRATEGY_ID,
            "contract_version": CONTRACT_VERSION,
            "position_id": pid,
            "order_id": row.get("order_id"),
            "pool_key": row.get("pool_key"),
            "signal_close_utc": row.get("signal_close_utc"),
            "fill_time_utc": row.get("fill_time_utc"),
            "exit_time_utc": row.get("exit_time_utc"),
            "exit_reason": reason,
            "exit_price": row.get("exit_price"),
            "gross_return_pct_short": row.get("gross_return_pct_short"),
            "trade_status": status,
            "simulated_only": True,
            "fill_disclaimer": FILL_DISCLAIMER,
            "display_mode": "short_position_with_entry_exit_times_v1",
        },
    }


def trade_to_link_spec(row: dict[str, Any]) -> dict[str, Any]:
    pid = str(row["position_id"])
    pos = trade_to_position_spec(row)
    return {
        "drawing_id": f"{LINK_PREFIX}{pid}",
        "drawing_type": "trend",
        "start": pos["start"],
        "end": pos["end"],
        "price_a": pos["entry"],
        "price_b": float(row["exit_price"]),
        "metadata": {
            "origin": BACKTESTER_SOURCE,
            "strategy_id": STRATEGY_ID,
            "position_id": pid,
            "link_role": "entry_to_exit",
            "line_style": "dotted",
            "width": 1,
            "opacity": 0.55,
        },
    }


def build_drawing_specs(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    positions = [trade_to_position_spec(r) for r in sorted(rows, key=lambda x: x["fill_time_utc"])]
    links = [trade_to_link_spec(r) for r in rows]
    return positions, links
