"""BT-D10 — export BT-D7 integration_result to viewer overlay contract (read-only)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CONTRACT_VERSION = "bt_d10_overlay_contract_v1_1"
BACKTESTER_SOURCE = "bt_d7_short_research_overlay_v1"
DRAWING_PREFIX = "btd10-short-"

CANCEL_STATUSES = frozenset(
    {
        "CANCELLED_POOL_INACTIVE",
        "CANCELLED",
        "NO_TP",
        "AMBIGUOUS_INTRABAR",
        "OPEN_AT_END",
    }
)


def _parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_id_from_path(path: Path) -> str:
    return path.parent.name if path.name == "integration_result.json" else path.stem


def _ledger_by_trade_id(pnl: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in pnl.get("trade_ledger") or []:
        tid = str(row.get("trade_id") or "")
        if tid:
            out[tid] = row
    return out


def _orders_for_signal(orders: list[dict[str, Any]], signal_close: str) -> list[dict[str, Any]]:
    return [o for o in orders if str(o.get("signal_close") or "") == signal_close]


def _trade_for_signal(trades: list[dict[str, Any]], signal_close: str) -> dict[str, Any] | None:
    for t in trades:
        if str(t.get("signal_close_utc") or "") == signal_close:
            return t
    return None


def _lifecycle_state(
    signal: dict[str, Any],
    orders: list[dict[str, Any]],
    trade: dict[str, Any] | None,
) -> tuple[str, str]:
    """Return (lifecycle_state, pending_order_status)."""
    if trade and trade.get("exit_utc") and trade.get("exit_price") is not None:
        reason = str(trade.get("exit_reason") or "").upper()
        if reason == "TP":
            return "CLOSED_TP", "FILLED_CLOSED"
        if reason == "SL" or reason.startswith("SL_"):
            return "CLOSED_SL", "FILLED_CLOSED"
        return "CLOSED", "FILLED_CLOSED"
    if trade and trade.get("fill_utc"):
        return "FILLED_OPEN", "FILLED_OPEN"
    for o in orders:
        st = str(o.get("status") or "")
        if st == "PENDING_PLACED":
            return "PENDING", st
        if st in CANCEL_STATUSES:
            return "CANCELLED", st
    if orders:
        return "SIGNAL", str(orders[-1].get("status") or "ORDER_UNKNOWN")
    return "SIGNAL", "SIGNAL_ONLY"


def _planned_levels(sig: dict[str, Any]) -> dict[str, Any]:
    ss = sig.get("signal_snapshot") or {}
    return {
        "entry": ss.get("entry_price_planned") or sig.get("entry_level"),
        "sl": ss.get("stop_loss_price_planned"),
        "tp": ss.get("take_profit_price_planned"),
        "pending_start": ss.get("pending_start_utc") or sig.get("order_active_from_utc"),
        "pending_expiry": ss.get("pending_expiry_utc"),
        "provenance": ss.get("snapshot_provenance"),
    }


def _display_end_utc(
    lifecycle: str,
    signal: dict[str, Any],
    trade: dict[str, Any] | None,
    *,
    pending_horizon_hours: int = 4,
) -> str | None:
    if lifecycle == "CLOSED" and trade:
        return str(trade.get("exit_utc"))
    if lifecycle == "FILLED" and trade:
        return str(trade.get("exit_utc") or trade.get("fill_utc"))
    if lifecycle in ("PENDING", "SIGNAL"):
        base = signal.get("order_active_from_utc") or signal.get("decision_close_utc")
        if not base:
            return None
        return _iso(_parse_utc(str(base)) + timedelta(hours=pending_horizon_hours))
    return None


def export_overlay_bundle(
    integration_result: dict[str, Any],
    *,
    integration_path: Path | None = None,
    symbol: str | None = None,
) -> dict[str, Any]:
    """Map BT-D7 `integration_result.json` to overlay contract. No strategy re-run."""
    manifest = integration_result.get("manifest") or {}
    cfg_hash = str(integration_result.get("config_hash") or manifest.get("config_hash") or "")
    sym = (symbol or (manifest.get("symbols") or [None])[0] if isinstance(manifest.get("symbols"), list) else None)
    sym = str(sym or "DOGEUSDT").upper()
    run_id = run_id_from_path(integration_path) if integration_path else manifest.get("manifest_id", "unknown_run")

    density = manifest.get("density_contract") or {}
    density_prov = {
        "density_path": manifest.get("density_path", "ResearchDensityRunV1.collect_at_close"),
        "density_source": (integration_result.get("density") or {}).get("density_source")
        or manifest.get("density_source", "canonical_provider"),
        "validated_cache_bundle_path": (integration_result.get("density") or {}).get("validated_cache", {}).get(
            "bundle_path"
        ),
        "pipeline_line_id": None,
        "config_hash": cfg_hash or None,
    }

    pnl = integration_result.get("pnl") or {}
    ledger = _ledger_by_trade_id(pnl)
    signals = list(integration_result.get("signals") or [])
    orders = list(integration_result.get("orders") or [])
    trades = list(integration_result.get("trades") or [])

    records: list[dict[str, Any]] = []
    for i, sig in enumerate(signals):
        sig_close = str(sig.get("decision_close_utc") or "")
        signal_id = f"{run_id}:{sig_close}:{i:03d}"
        sig_orders = _orders_for_signal(orders, sig_close)
        trade = _trade_for_signal(trades, sig_close)
        lifecycle, pending_status = _lifecycle_state(sig, sig_orders, trade)

        entry_price = None
        fill_time = None
        fill_price = None
        sl_price = None
        tp_price = None
        exit_time = None
        exit_reason = None
        exit_price = None
        trade_id = None
        realized_net = None
        realized_pct = None

        if trade:
            trade_id = str(trade.get("trade_id") or "")
            fill_time = trade.get("fill_utc")
            fill_price = trade.get("entry_price")
            entry_price = trade.get("entry_price")
            sl_price = trade.get("sl_price")
            tp_price = trade.get("tp_price")
            exit_time = trade.get("exit_utc")
            exit_reason = trade.get("exit_reason")
            exit_price = trade.get("exit_price")
            leg = ledger.get(trade_id or "")
            if leg:
                realized_net = leg.get("net_pnl_usdt")
                realized_pct = leg.get("return_pct")
        else:
            planned = _planned_levels(sig)
            entry_price = planned["entry"]
            sl_price = planned["sl"]
            tp_price = planned["tp"]

        display_end = _display_end_utc(lifecycle, sig, trade)
        planned = _planned_levels(sig)
        if trade:
            planned_entry = planned.get("entry") or entry_price
        else:
            planned_entry = planned.get("entry")

        records.append(
            {
                "signal_id": signal_id,
                "run_id": run_id,
                "strategy_config_hash": cfg_hash,
                "symbol": sym,
                "signal_time_utc": sig_close,
                "order_active_from_utc": sig.get("order_active_from_utc"),
                "side": "SHORT",
                "lifecycle_state": lifecycle,
                "pending_order_status": pending_status,
                "entry_price": entry_price,
                "entry_price_planned": planned_entry,
                "stop_loss_price_planned": planned.get("sl"),
                "take_profit_price_planned": planned.get("tp"),
                "sl_price": sl_price or planned.get("sl"),
                "tp_price": tp_price or planned.get("tp"),
                "pending_start_utc": planned.get("pending_start"),
                "pending_expiry_utc": planned.get("pending_expiry"),
                "snapshot_provenance": planned.get("provenance"),
                "fill_time_utc": fill_time,
                "fill_price": fill_price,
                "exit_time_utc": exit_time,
                "exit_price": exit_price,
                "exit_reason": exit_reason,
                "realized_net_pnl_usdt": realized_net,
                "realized_pnl_pct": realized_pct,
                "unrealized_pnl_usdt": None,
                "display_end_utc": display_end,
                "density_provenance": dict(density_prov),
                "pool_lower": sig.get("pool_lower"),
                "pool_upper": sig.get("pool_upper"),
                "signal_snapshot": sig.get("signal_snapshot"),
                "trade_id": trade_id,
            }
        )

    bundle = {
        "contract_version": CONTRACT_VERSION,
        "run_id": run_id,
        "strategy_config_hash": cfg_hash,
        "symbol": sym,
        "timeframe": "5m",
        "pool_detail": 1,
        "density_method": "exact_unique_origin",
        "min_unique_origins": int(density.get("min_unique_origins") or 2),
        "strategy_id": str(manifest.get("strategy_id") or "pool_landscape_v1_candidate"),
        "strategy_freeze_status": str(
            manifest.get("strategy_freeze_status") or "FREEZE_CANDIDATE_NOT_FINAL_APPROVED"
        ),
        "tp_selection_mode": str(manifest.get("tp_selection_mode") or "next_eligible_band"),
        "density_provenance": density_prov,
        "source_paths": {
            "integration_result": str(integration_path) if integration_path else None,
        },
        "records": records,
    }
    return bundle


def bundle_content_hash(bundle: dict[str, Any]) -> str:
    payload = json.dumps(bundle, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def load_integration_result(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_overlay_bundle(path: Path, bundle: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(bundle, indent=2), encoding="utf-8")
    meta = {
        "contract_version": CONTRACT_VERSION,
        "content_hash": bundle_content_hash(bundle),
        "record_count": len(bundle.get("records") or []),
    }
    path.with_suffix(".meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return path
