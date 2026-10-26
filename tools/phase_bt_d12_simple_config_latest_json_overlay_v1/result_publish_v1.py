"""Build and atomically publish backtester result.json + latest_result.json."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RESULT_FORMAT = "bt_d12_backtest_result_v1"
RUNS_ROOT = Path(__file__).resolve().parents[2] / "results" / "backtester_runs"


class ResultPublishError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def integration_from_engine(config: dict[str, Any], engine_out: dict[str, Any]) -> dict[str, Any]:
    return {
        "manifest": engine_out.get("manifest") or {},
        "config_hash": engine_out.get("config_hash"),
        "signals": engine_out.get("signals") or [],
        "orders": engine_out.get("orders") or [],
        "trades": engine_out.get("trades") or [],
        "suppressed": engine_out.get("suppressed") or [],
        "pnl": engine_out.get("pnl") or {},
        "snapshots_collected": engine_out.get("snapshots_collected"),
    }


def build_result_document(
    *,
    run_id: str,
    config: dict[str, Any],
    integration: dict[str, Any],
    density_provenance: dict[str, Any],
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    bt = config["backtest"]
    pnl = integration.get("pnl") or {}
    manifest = integration.get("manifest") or {}
    cfg_hash = str(integration.get("config_hash") or manifest.get("config_hash") or "")
    return {
        "result_format": RESULT_FORMAT,
        "run_id": run_id,
        "created_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "config_snapshot": config,
        "config_hash": cfg_hash,
        "symbol": bt["symbol"],
        "start_utc": bt["start_utc"],
        "end_utc": bt["end_utc"],
        "end_utc_exclusive": bool(bt.get("end_utc_exclusive")),
        "density_contract": manifest.get("density_contract") or config.get("density"),
        "density_provenance": density_provenance,
        "strategy_id": manifest.get("strategy_id") or config.get("strategy_identity", {}).get("strategy_id"),
        "strategy_version": manifest.get("strategy_version"),
        "strategy_status": "EXPERIMENTAL_NOT_FINAL_APPROVED",
        "tp_selection_mode": manifest.get("tp_selection_mode") or config.get("take_profit", {}).get("selection_mode"),
        "tp_selection_note": manifest.get("tp_selection_note"),
        "signals": integration.get("signals") or [],
        "orders": integration.get("orders") or [],
        "trades": integration.get("trades") or [],
        "suppressed": integration.get("suppressed") or [],
        "pnl": {
            "initial_equity_usdt": pnl.get("initial_equity_usdt"),
            "ending_equity_usdt": pnl.get("ending_equity_usdt"),
            "total_net_pnl_usdt": pnl.get("net_pnl_usdt"),
            "total_return_pct": pnl.get("total_return_pct"),
            "gross_pnl_usdt": pnl.get("gross_pnl_usdt"),
            "trading_fees_usdt": pnl.get("trading_fees_usdt"),
            "slippage_cost_usdt": pnl.get("slippage_cost_usdt"),
            "funding_fees_usdt": pnl.get("funding_fees_usdt"),
            "total_trades": pnl.get("total_trades"),
            "winning_trades": pnl.get("winning_trades"),
            "losing_trades": pnl.get("losing_trades"),
            "win_rate_pct": pnl.get("win_rate"),
            "profit_factor": pnl.get("profit_factor"),
            "max_drawdown_pct": pnl.get("max_drawdown_pct"),
            "equity_curve": pnl.get("equity_curve"),
            "trade_ledger": pnl.get("trade_ledger"),
        },
        "integration": integration,
        "meta": meta or {},
    }


def validate_result(doc: dict[str, Any]) -> list[str]:
    errs: list[str] = []
    if doc.get("result_format") != RESULT_FORMAT:
        errs.append("result_format")
    if not doc.get("run_id"):
        errs.append("run_id")
    if not doc.get("config_hash"):
        errs.append("config_hash")
    if doc.get("symbol") != "DOGEUSDT" and doc.get("symbol"):
        pass  # future symbols
    if "signals" not in doc:
        errs.append("signals")
    return errs


def publish_result(
    run_id: str,
    doc: dict[str, Any],
    *,
    runs_root: Path | None = None,
    publish_latest: bool = True,
) -> Path:
    doc = dict(doc)
    doc["run_id"] = run_id
    errs = validate_result(doc)
    if errs:
        raise ResultPublishError("RESULT_INVALID", f"missing/invalid: {errs}")
    root = runs_root or RUNS_ROOT
    run_dir = root / run_id
    if run_dir.exists():
        raise ResultPublishError("RUN_ID_EXISTS", str(run_dir))
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise ResultPublishError("RUN_ID_EXISTS", str(run_dir)) from exc
    result_path = run_dir / "result.json"
    tmp = result_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    os.replace(tmp, result_path)
    if publish_latest:
        latest = root / "latest_result.json"
        latest_tmp = root / ".latest_result.json.tmp"
        latest_tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        os.replace(latest_tmp, latest)
    return result_path


def content_hash(doc: dict[str, Any]) -> str:
    payload = json.dumps({k: v for k, v in doc.items() if k != "created_at_utc"}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()
