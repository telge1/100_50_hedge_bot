from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import (
    DEFAULT_OUT_ROOT,
    EXIT_POLICY,
    INTRABAR_POLICY,
    PORTFOLIO_POLICY,
    SIGNAL_SCOPE,
    SIGNAL_STRATEGY_VERSION,
    TF_PRIORITY,
)
from .io_util import atomic_write_json, atomic_write_jsonl, atomic_write_text


def new_run_id() -> str:
    return uuid.uuid4().hex


def write_run(
    *,
    out_root: Path,
    run_id: str,
    manifest: dict[str, Any],
    input_audit: dict[str, Any],
    summary: dict[str, Any],
    equity_curve: list[dict[str, Any]],
    accepted: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
    duplicate_audit: list[dict[str, Any]],
    slot_history: list[dict[str, Any]],
    open_at_end: list[dict[str, Any]],
    breakdowns: dict[str, Any],
    log_text: str,
) -> Path:
    dest = Path(out_root) / run_id
    if dest.exists():
        raise FileExistsError("RUN_DIR_EXISTS")
    dest.mkdir(parents=True, exist_ok=False)
    atomic_write_json(dest / "run_manifest.json", manifest)
    atomic_write_json(dest / "input_audit.json", input_audit)
    atomic_write_json(dest / "summary.json", summary)
    atomic_write_jsonl(dest / "equity_curve.jsonl", equity_curve)
    atomic_write_jsonl(dest / "accepted_trades.jsonl", accepted)
    atomic_write_jsonl(dest / "skipped_signals.jsonl", skipped)
    atomic_write_jsonl(dest / "duplicate_audit.jsonl", duplicate_audit)
    atomic_write_jsonl(dest / "slot_history.jsonl", slot_history)
    atomic_write_json(dest / "open_positions_at_end.json", open_at_end)
    atomic_write_json(dest / "per_symbol_summary.json", breakdowns["per_symbol"])
    atomic_write_json(dest / "per_timeframe_summary.json", breakdowns["per_timeframe"])
    atomic_write_json(dest / "per_slot_summary.json", breakdowns["per_slot"])
    atomic_write_text(dest / "run.log", log_text)
    return dest


def base_manifest(**kwargs: Any) -> dict[str, Any]:
    payload = {
        "source_evaluation_id": kwargs["source_evaluation_id"],
        "source_job_id": kwargs["source_job_id"],
        "signal_strategy_version": SIGNAL_STRATEGY_VERSION,
        "signal_scope": SIGNAL_SCOPE,
        "exit_policy": EXIT_POLICY,
        "intrabar_policy": INTRABAR_POLICY,
        "portfolio_policy": PORTFOLIO_POLICY,
        "initial_balance_usdt": kwargs["initial_balance"],
        "max_slots": kwargs["max_slots"],
        "notional_per_trade_usdt": kwargs["notional"],
        "compounding": False,
        "same_symbol_overlap_allowed": False,
        "slot_reuse_requires_strictly_after_exit": True,
        "dedup_key": ["symbol", "entry_time"],
        "dedup_timeframe_priority": list(TF_PRIORITY),
        "fees_applied": False,
        "net_result_status": "NOT_EVALUATED_NO_AUTHORITATIVE_FEE_RATE",
        "writes_to_clickhouse": False,
        "publish_enabled": False,
        "live_orders_enabled": False,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "same_timestamp_fill_order": (
            "entry_time asc, timeframe rank desc (4h>1h>30m>15m), symbol asc, signal_id asc; "
            "lowest free slot index first"
        ),
    }
    payload.update(kwargs.get("extra") or {})
    return payload
