"""HTTP API for Wall Decision V1 — config + shadow journal + live metrics. No trading."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import JSONResponse

from .config import RULE_VERSION, V1_PROVISIONAL
from .metrics import compute_live_metrics_async
from .shadow_store import append_session_event, list_recent_sessions, new_session_record


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"success": False, "error": code, "message": message})


def _parse_iso(v: Any) -> datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        n = float(v)
        if n > 1e12:
            n /= 1000.0
        return datetime.fromtimestamp(n, tz=timezone.utc)
    s = str(v).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def build_router(*, require_auth: Callable) -> APIRouter:
    router = APIRouter(tags=["wall-decision-v1"])

    @router.get("/api/wall-decision/v1/config")
    async def api_wall_decision_config(user: dict = Depends(require_auth)):
        return {
            "success": True,
            "rule_version": RULE_VERSION,
            "thresholds": dict(V1_PROVISIONAL),
            "note": "Thresholds are V1_PROVISIONAL and not proven for expectancy.",
            "execution": False,
            "wall_xray_v1": True,
            "fixture_route": False,
        }

    @router.post("/api/wall-decision/v1/live-metrics")
    async def api_wall_decision_live_metrics(
        user: dict = Depends(require_auth),
        body: dict[str, Any] = Body(default_factory=dict),
    ):
        """Bounded read-only metrics from existing OBP + public_trades loaders."""
        symbol = str(body.get("symbol") or "").strip().upper()
        if not symbol:
            return _error(400, "invalid_body", "symbol required")
        try:
            bp = float(body.get("breakpoint"))
        except (TypeError, ValueError):
            return _error(400, "invalid_breakpoint", "breakpoint must be a number")
        tw = body.get("target_wall") if isinstance(body.get("target_wall"), dict) else None
        try:
            metrics = await compute_live_metrics_async(
                symbol=symbol,
                breakpoint=bp,
                target_wall=tw,
                baseline_qty=body.get("baseline_qty"),
                baseline_notional=body.get("baseline_notional"),
                triggered_at=_parse_iso(body.get("triggered_at")),
                trigger_price=body.get("trigger_price"),
                live_price=body.get("live_price"),
                accepted_above_sec=float(body.get("accepted_above_sec") or 0),
                accepted_below_sec=float(body.get("accepted_below_sec") or 0),
                min_qty_seen=body.get("min_qty_seen"),
                avr_state=str(body["avr_state"]) if body.get("avr_state") not in (None, "") else None,
                oi_at_trigger=body.get("oi_at_trigger"),
                oi_current=body.get("oi_current"),
            )
        except Exception as exc:  # noqa: BLE001
            return _error(500, "metrics_failed", str(exc))
        return metrics

    @router.get("/api/wall-decision/v1/live-metrics")
    async def api_wall_decision_live_metrics_get(
        user: dict = Depends(require_auth),
        symbol: str = Query(...),
        breakpoint: float = Query(...),
    ):
        """Lightweight GET for smoke tests (no target → WALL_LOST path)."""
        return await compute_live_metrics_async(
            symbol=symbol,
            breakpoint=float(breakpoint),
            target_wall=None,
            baseline_qty=None,
            baseline_notional=None,
            triggered_at=None,
            trigger_price=None,
            live_price=None,
        )

    @router.post("/api/wall-decision/v1/shadow")
    async def api_wall_decision_shadow(
        user: dict = Depends(require_auth),
        body: dict[str, Any] = Body(default_factory=dict),
    ):
        session_id = str(body.get("session_id") or "").strip()
        symbol = str(body.get("symbol") or "").strip().upper()
        if not session_id or not symbol:
            return _error(400, "invalid_body", "session_id and symbol required")
        try:
            bp = float(body.get("breakpoint"))
        except (TypeError, ValueError):
            return _error(400, "invalid_breakpoint", "breakpoint must be a number")
        record = new_session_record(
            session_id=session_id,
            symbol=symbol,
            breakpoint=bp,
            target_wall=body.get("target_wall") if isinstance(body.get("target_wall"), dict) else None,
            armed_at=body.get("armed_at"),
            triggered_at=body.get("triggered_at"),
            state=str(body.get("state") or "TRIGGERED"),
            reason_codes=list(body.get("reason_codes") or []),
            metrics=body.get("metrics") if isinstance(body.get("metrics"), dict) else {},
            event_time=body.get("event_time"),
            available_at=body.get("available_at"),
            data_gap=bool(body.get("data_gap")),
        )
        path = append_session_event(record)
        return {"success": True, "path": str(path.name), "record": record}

    @router.get("/api/wall-decision/v1/shadow")
    async def api_wall_decision_shadow_list(
        user: dict = Depends(require_auth),
        limit: int = 50,
    ):
        lim = max(1, min(int(limit or 50), 200))
        return {"success": True, "sessions": list_recent_sessions(limit=lim)}

    return router
