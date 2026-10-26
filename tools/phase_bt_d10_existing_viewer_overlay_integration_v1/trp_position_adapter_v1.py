"""Map BT-D10 overlay records → TRP short_position drawing specs (isolated; not wired to workspace)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

BACKTESTER_SOURCE = "bt_d7_short_research_overlay_v1"
DRAWING_PREFIX = "btd10-short-"
# Visual-only width when ledger has no pending_expiry (matches overlay_export_v1 pending_horizon_hours).
PLANNED_PREVIEW_HOURS = 4


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _status_label(lifecycle: str, *, plan_only: bool, exit_reason: str | None) -> str:
    if plan_only:
        if lifecycle == "CANCELLED":
            return "PLANNED SHORT / CANCELLED"
        if lifecycle == "EXPIRED":
            return "PLANNED SHORT / EXPIRED"
        return "PLANNED SHORT / UNFILLED"
    if lifecycle == "FILLED_OPEN":
        return "SHORT / OPEN"
    reason = str(exit_reason or "").strip().upper()
    if reason:
        return f"SHORT / CLOSED · {reason}"
    return "SHORT / CLOSED"


def _planned_time_window(rec: dict[str, Any]) -> tuple[datetime | None, datetime | None, bool]:
    """Return (start, end, preview_end). preview_end=True when end comes from display preview only."""
    start = _parse_dt(
        rec.get("pending_start_utc")
        or rec.get("order_active_from_utc")
        or rec.get("signal_time_utc")
    )
    end = _parse_dt(rec.get("pending_expiry_utc") or rec.get("display_end_utc"))
    preview = False
    if end is None and start is not None:
        end = start + timedelta(hours=PLANNED_PREVIEW_HOURS)
        preview = True
    if start is not None and end is not None and end <= start:
        end = start + timedelta(hours=PLANNED_PREVIEW_HOURS)
        preview = True
    return start, end, preview


def overlay_record_to_position_spec(rec: dict[str, Any]) -> dict[str, Any] | None:
    """
    Produce a stoch_backtester-compatible position spec for workspace_session.import_* pattern.

    Planned pending signals use published snapshot prices only (no fill PnL).
    Filled/closed rows use ledger fill/exit times and realized PnL when present.
    """
    lifecycle = str(rec.get("lifecycle_state") or "")
    plan_only = lifecycle in ("SIGNAL", "PENDING", "CANCELLED", "EXPIRED")

    if plan_only:
        entry = rec.get("entry_price_planned") or rec.get("entry_price")
        sl = rec.get("stop_loss_price_planned") or rec.get("sl_price")
        tp = rec.get("take_profit_price_planned") or rec.get("tp_price")
        start, end, preview_end = _planned_time_window(rec)
    else:
        if lifecycle not in ("CLOSED", "CLOSED_TP", "CLOSED_SL", "FILLED", "FILLED_OPEN"):
            return None
        entry = rec.get("fill_price") or rec.get("entry_price")
        sl = rec.get("sl_price")
        tp = rec.get("tp_price")
        start = _parse_dt(
            rec.get("fill_time_utc") or rec.get("order_active_from_utc") or rec.get("signal_time_utc")
        )
        end = _parse_dt(rec.get("exit_time_utc") or rec.get("display_end_utc"))
        preview_end = False

    if entry is None or sl is None or tp is None or start is None:
        return None
    if end is None or end <= start:
        return None

    sid = str(rec.get("signal_id") or rec.get("trade_id") or "")
    safe = sid.replace(":", "").replace("+", "").replace("|", "_")
    direction = str(rec.get("side") or "SHORT").upper()
    drawing_type = "short_position" if direction == "SHORT" else "long_position"
    exit_reason = rec.get("exit_reason")

    meta: dict[str, Any] = {
        "origin": BACKTESTER_SOURCE,
        "run_id": rec.get("run_id"),
        "strategy_config_hash": rec.get("strategy_config_hash"),
        "trade_id": rec.get("trade_id"),
        "lifecycle_state": lifecycle,
        "plan_only": plan_only,
        "preview_only": plan_only,
        "status_label": _status_label(lifecycle, plan_only=plan_only, exit_reason=exit_reason),
        "snapshot_provenance": rec.get("snapshot_provenance"),
    }
    if preview_end:
        meta["preview_time_end"] = True
        meta["preview_horizon_hours"] = PLANNED_PREVIEW_HOURS
    if not plan_only:
        realized = rec.get("realized_net_pnl_usdt")
        if realized is not None:
            meta["realized_net_pnl_usdt"] = realized
            meta["realized_pnl_pct"] = rec.get("realized_pnl_pct")
            meta["exit_reason"] = exit_reason

    return {
        "drawing_id": f"{DRAWING_PREFIX}{safe}",
        "drawing_type": drawing_type,
        "symbol": str(rec.get("symbol") or "").upper(),
        "timeframe": "5m",
        "start": start,
        "end": end,
        "entry": float(entry),
        "stop": float(sl),
        "target": float(tp),
        "signal_id": sid,
        "direction": direction,
        "source": BACKTESTER_SOURCE,
        "lifecycle_state": lifecycle,
        "exit_reason": exit_reason,
        "realized_net_pnl_usdt": rec.get("realized_net_pnl_usdt") if not plan_only else None,
        "realized_pnl_pct": rec.get("realized_pnl_pct") if not plan_only else None,
        "metadata": meta,
    }


def overlay_bundle_to_position_specs(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for rec in bundle.get("records") or []:
        spec = overlay_record_to_position_spec(rec)
        if spec:
            out.append(spec)
    return out
