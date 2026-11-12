"""Chart button backend — reuses BybitLiveExchangeAdapter + sync engine (no duplicate OM)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from research.manual_long_box_live_sync_v1.activation import BoxActivation, load_activations
from research.manual_long_box_live_sync_v1.config import (
    DEFAULT_ACTIVATIONS_PATH,
    DEFAULT_DRAWINGS_PATH,
    DEFAULT_EVENT_LOG_PATH,
    DEFAULT_STATE_PATH,
)
from research.manual_long_box_live_sync_v1.drawing_watch import hash_payload, observe_drawings
from research.manual_long_box_live_sync_v1.exchange_dry_run import OrderStatus
from research.manual_long_box_live_sync_v1.persistence import BoxSyncState, EventLog, StateStore
from research.manual_long_box_live_sync_v1.sync_engine import ManualLongBoxSyncEngine

from research.manual_long_box_research_v1.recognition import (
    is_manual_long_box,
    is_manual_short_box,
)
from research.manual_long_box_research_v1.validation import (
    validate_long_geometry,
    validate_short_geometry,
)

from decimal import Decimal

from .account_checks import verify_available_margin, verify_leverage_one_x
from .activation_store import disable_activation, get_activation_row, upsert_activation
from .config import LiveOrderSettings, load_dotenv_chain, load_settings
from .credentials import build_order_manager
from .exchange_adapter import BybitLiveExchangeAdapter
from .order_sizing import (
    MARGIN_FEE_BUFFER_USDT,
    TEST_ORDER_NOTIONAL_USDT,
    compute_fixed_notional_qty,
    fetch_instrument_steps,
    fetch_instrument_steps_public,
)
from .order_amend import (
    build_amend_diff,
    classify_setzen,
    execute_confirmed_amend,
    execute_confirmed_amend_dry,
    _live_order_row,
)
from .order_sync import build_sync_engine
from .prepare_tokens import PrepareTokenStore
from research.manual_long_box_live_sync_v1.order_links import allocate_order_link_id
from .mlb_audit_log import audit_event, new_correlation_id
from .reconcile import (
    discover_managed_open_order_ex,
    find_limit_order,
    map_bybit_order_status,
    wait_for_order_rest,
)

_PREPARE_PATH = Path(__file__).resolve().parent.parent / "data" / "order_prepare_tokens.json"
_LOCKS: dict[str, threading.Lock] = {}
_STORE = PrepareTokenStore(_PREPARE_PATH)


def _lock(drawing_id: str) -> threading.Lock:
    if drawing_id not in _LOCKS:
        _LOCKS[drawing_id] = threading.Lock()
    return _LOCKS[drawing_id]


def _box_content_hash(drawing_id: str, entry: float, stop: float, target: float) -> str:
    return hash_payload(
        {"id": drawing_id, "entry": entry, "stop": stop, "target": target}
    )


def _position_idx_for(side: str) -> int:
    return 2 if side == "short" else 1


def _expected_order_side(side: str) -> str:
    return "Sell" if side == "short" else "Buy"


def _load_manual_box(
    drawing_id: str,
    *,
    drawings_path: Path | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    path = drawings_path or DEFAULT_DRAWINGS_PATH
    obs = observe_drawings(path)
    if not obs:
        return None, "drawings_missing"
    for row in obs.payload.get("drawings") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("drawing_id")) != str(drawing_id):
            continue
        dtype = row.get("drawing_type")
        if dtype == "long_position":
            if not is_manual_long_box(row):
                return None, "not_manual_long"
            side = "long"
        elif dtype == "short_position":
            if not is_manual_short_box(row):
                return None, "not_manual_short"
            side = "short"
        else:
            return None, "not_position_box"
        try:
            entry = float(row["entry_price"])
            stop = float(row["stop_price"])
            target = float(row["target_price"])
        except (KeyError, TypeError, ValueError):
            return None, "invalid_prices"
        if side == "long":
            ok, geom_err = validate_long_geometry(stop, entry, target)
        else:
            ok, geom_err = validate_short_geometry(stop, entry, target)
        if not ok:
            return None, geom_err or "invalid_geometry"
        sym = str(row.get("symbol") or "DOGEUSDT").upper()
        return {
            "drawing_id": drawing_id,
            "symbol": sym,
            "side": side,
            "position_idx": _position_idx_for(side),
            "entry_price": entry,
            "stop_price": stop,
            "target_price": target,
            "content_hash": _box_content_hash(drawing_id, entry, stop, target),
            "raw": row,
        }, None
    return None, "drawing_not_found"


def _load_long_box(
    drawing_id: str,
    *,
    drawings_path: Path | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    box, err = _load_manual_box(drawing_id, drawings_path=drawings_path)
    if box and box.get("side") != "long":
        return None, "not_long_position"
    return box, err


def _settings() -> LiveOrderSettings:
    load_dotenv_chain()
    return load_settings()


def _order_manager_readonly(settings: LiveOrderSettings):
    if not settings.api_key or not settings.api_secret:
        return None
    try:
        return build_order_manager(settings, require_armed=False)
    except Exception:
        return None


def _instrument_steps(
    symbol: str, settings: LiveOrderSettings
) -> tuple[tuple[Decimal, Decimal] | None, Any]:
    om = _order_manager_readonly(settings)
    if om is not None:
        steps = fetch_instrument_steps(om, symbol)
        if steps:
            return steps, om
    pub = fetch_instrument_steps_public(symbol)
    return pub, om


def _resolve_qty(
    box: dict[str, Any],
    settings: LiveOrderSettings,
    *,
    notional_usd: float | None,
) -> tuple[float | None, float, dict[str, Any]]:
    """Return (qty_doge, notional_usd, meta/extra gates)."""
    entry = float(box["entry_price"])
    blockers: dict[str, Any] = {}

    if settings.fixed_test_notional_usd is not None:
        cap = Decimal(str(settings.fixed_test_notional_usd))
        steps, om = _instrument_steps(box["symbol"], settings)
        if not steps:
            return None, 0.0, {"error": "instrument_steps_unknown"}
        qty_dec, sizing = compute_fixed_notional_qty(
            entry,
            qty_step=steps[0],
            min_qty=steps[1],
            notional_cap=cap,
        )
        if qty_dec is None:
            return None, 0.0, sizing
        notional = float(qty_dec * Decimal(str(entry)))
        extra: dict[str, Any] = {
            "sizing": sizing,
            "notional_cap_usdt": float(cap),
            "qty_step": str(steps[0]),
        }
        if om is not None:
            lev = verify_leverage_one_x(om, box["symbol"])
            extra["leverage_check"] = lev
            if not lev.get("ok"):
                blockers["leverage"] = lev.get("error", "leverage_blocked")
            margin = verify_available_margin(
                om,
                required_usdt=notional + float(MARGIN_FEE_BUFFER_USDT),
            )
            extra["margin_check"] = margin
            if not margin.get("ok"):
                blockers["margin"] = margin.get("error", "margin_blocked")
        else:
            blockers["account"] = "credentials_missing_for_gates"
        if blockers:
            extra["blockers"] = blockers
        return float(qty_dec), notional, extra

    if settings.order_qty and settings.order_qty > 0:
        qty = float(settings.order_qty)
        return qty, qty * entry, {}
    notional = float(notional_usd or 0.0)
    if settings.use_activation_notional and entry > 0 and notional > 0:
        return notional / entry, notional, {}
    return None, 0.0, {"error": "qty_not_configured"}


def _heal_sync_state_from_exchange(
    store: StateStore,
    drawing_id: str,
    row,
    st,
    *,
    box: dict[str, Any] | None = None,
) -> BoxSyncState:
    if st is None:
        st = BoxSyncState(
            drawing_id=drawing_id,
            symbol=str((box or {}).get("symbol") or "DOGEUSDT").upper(),
            exchange_order_id=str(row.order_id),
            order_link_id=str(row.order_link_id or f"mlb-sync-{drawing_id}"),
            entry_price=float(row.price or (box or {}).get("entry_price") or 0),
            stop_price=float(row.stop_loss or (box or {}).get("stop_price") or 0),
            target_price=float(row.take_profit or (box or {}).get("target_price") or 0),
            order_status=OrderStatus.OPEN.value,
            content_hash=str((box or {}).get("content_hash") or ""),
            last_action="RECONCILE_OPEN_ORDER",
        )
    else:
        st.exchange_order_id = str(row.order_id)
        if row.order_link_id:
            st.order_link_id = str(row.order_link_id)
        st.order_status = OrderStatus.OPEN.value
        if row.price is not None:
            st.entry_price = float(row.price)
        if row.stop_loss is not None:
            st.stop_price = float(row.stop_loss)
        if row.take_profit is not None:
            st.target_price = float(row.take_profit)
        st.last_action = "RECONCILE_OPEN_ORDER"
    store.put(st)
    return st


def _discover_open_row(
    settings: LiveOrderSettings,
    drawing_id: str,
    symbol: str,
    st,
    *,
    box: dict[str, Any] | None = None,
) -> tuple[Any | None, str | None]:
    if not getattr(settings, "api_key", None) or not getattr(settings, "api_secret", None):
        return None, None
    try:
        om = build_order_manager(settings, require_armed=False)
    except Exception:
        return None, None
    expected_side = None
    expected_position_idx = None
    if box:
        expected_side = _expected_order_side(str(box.get("side") or "long"))
        expected_position_idx = int(box.get("position_idx") or _position_idx_for(str(box.get("side") or "long")))
    return discover_managed_open_order_ex(
        om,
        symbol,
        drawing_id,
        state_order_id=st.exchange_order_id if st else None,
        state_order_link_id=st.order_link_id if st else None,
        expected_side=expected_side,
        expected_position_idx=expected_position_idx,
    )


def _prepare_amend(drawing_id: str, box: dict[str, Any], settings: LiveOrderSettings) -> dict[str, Any]:
    store = StateStore(DEFAULT_STATE_PATH)
    st = store.get(drawing_id)
    discovered, discover_err = _discover_open_row(
        settings, drawing_id, box["symbol"], st, box=box
    )
    if discover_err == "ambiguous_managed_orders":
        return {
            "ok": False,
            "error": discover_err,
            "operation": "blocked",
            "phase": "RECONCILE_REQUIRED",
            "lamp": "warning",
            "lamp_tooltip": "Mehrere passende offene Orders — manuell bereinigen",
        }
    if discovered:
        st = _heal_sync_state_from_exchange(store, drawing_id, discovered, st, box=box)
    elif not st or not st.exchange_order_id:
        return {"ok": False, "error": "no_mapped_order", "operation": "amend", "phase": "INACTIVE"}

    row, err = _live_order_row(settings, st)
    if err or row is None:
        return {
            "ok": False,
            "error": err or "order_not_on_book",
            "operation": "amend",
            "phase": "RECONCILE_REQUIRED",
        }

    current = {
        "entry_price": row.price,
        "stop_loss": row.stop_loss,
        "take_profit": row.take_profit,
        "qty": row.qty,
    }
    from .price_tick import build_amend_price_snapshot, resolve_tick_size_for_symbol

    tick_size, tick_source = resolve_tick_size_for_symbol(settings, box["symbol"])
    if not tick_size:
        return {
            "ok": False,
            "error": "tick_size_unavailable",
            "operation": "amend",
            "phase": "RECONCILE_REQUIRED",
            "tick_source": tick_source,
        }

    chart_raw = {
        "entry_price": float(box["entry_price"]),
        "stop_price": float(box["stop_price"]),
        "target_price": float(box["target_price"]),
    }
    snapshot = build_amend_price_snapshot(
        chart_raw=chart_raw,
        exchange_current=current,
        tick_size=tick_size,
        tick_source=tick_source,
    )
    changes = snapshot["changes"]
    targets = snapshot["normalized_targets"]
    if not any(changes.values()):
        return {
            "ok": False,
            "error": "no_changes",
            "operation": "amend",
            "phase": "ORDER_OPEN",
            "tick_size": tick_size,
            "tick_source": tick_source,
            "changes": changes,
            "current_bybit": current,
            "proposed_box": dict(targets),
            "chart_raw_box": snapshot["chart_raw"],
        }

    token = _STORE.issue(
        {
            "action": "amend",
            "drawing_id": drawing_id,
            "content_hash": box["content_hash"],
            "order_id": row.order_id,
            "order_link_id": row.order_link_id or st.order_link_id,
            **snapshot,
        }
    )
    return {
        "ok": True,
        "operation": "amend",
        "prepare_token": token,
        "drawing_id": drawing_id,
        "content_hash": box["content_hash"],
        "symbol": box["symbol"],
        "side": box.get("side") or "long",
        "order_type": "LIMIT",
        "environment": settings.environment,
        "order_id": row.order_id,
        "order_link_id": row.order_link_id or st.order_link_id,
        "order_status": "OFFEN",
        "tick_size": tick_size,
        "tick_source": tick_source,
        "current_bybit": current,
        "proposed_box": dict(targets),
        "chart_raw_box": snapshot["chart_raw"],
        "changes": changes,
        "normalized_targets": dict(targets),
        "snapshot_version": snapshot.get("snapshot_version"),
        "qty_doge": round(float(row.qty or st.entry_price or 0), 8),
        "writes_allowed": settings.writes_allowed,
        "can_amend": bool(settings.writes_allowed),
        "can_place": False,
        "live_armed": settings.live_armed,
    }


def _prepare_create(
    drawing_id: str,
    box: dict[str, Any],
    settings: LiveOrderSettings,
    *,
    notional_usd: float | None = None,
) -> dict[str, Any]:
    if settings.fixed_test_notional_usd is not None and notional_usd is not None:
        notional_usd = None

    qty, notional, extra = _resolve_qty(box, settings, notional_usd=notional_usd)
    if qty is None or qty <= 0:
        return {"ok": False, "error": extra.get("error", "qty_not_configured"), **extra}

    blockers = extra.get("blockers") or {}
    lev_check = extra.get("leverage_check") or {}
    margin_check = extra.get("margin_check") or {}
    leverage = lev_check.get("leverage")
    available_margin = margin_check.get("available_margin_usdt")
    order_valid = not blockers
    can_place = bool(settings.writes_allowed and order_valid)

    token = _STORE.issue(
        {
            "action": "create",
            "drawing_id": drawing_id,
            "content_hash": box["content_hash"],
            "symbol": box["symbol"],
            "entry_price": box["entry_price"],
            "stop_price": box["stop_price"],
            "target_price": box["target_price"],
            "notional_usd": notional,
            "qty": qty,
        }
    )
    return {
        "ok": True,
        "operation": "create",
        "prepare_token": token,
        "drawing_id": drawing_id,
        "content_hash": box["content_hash"],
        "symbol": box["symbol"],
        "side": box.get("side") or "long",
        "position_idx": int(box.get("position_idx") or _position_idx_for(str(box.get("side") or "long"))),
        "order_type": "LIMIT",
        "environment": settings.environment,
        "entry_price": box["entry_price"],
        "stop_loss": box["stop_price"],
        "take_profit": box["target_price"],
        "qty_doge": round(qty, 8),
        "notional_usd": round(notional, 8),
        "notional_cap_usdt": float(settings.fixed_test_notional_usd or TEST_ORDER_NOTIONAL_USDT),
        "live_armed": settings.live_armed,
        "writes_allowed": settings.writes_allowed,
        "can_place": can_place,
        "order_valid": order_valid,
        "blockers": blockers,
        "leverage": leverage,
        "required_leverage": 1,
        "available_margin_usdt": available_margin,
        "margin_required_usdt": margin_check.get("required_usdt"),
        **{k: v for k, v in extra.items() if k not in ("blockers", "leverage_check", "margin_check")},
    }


def prepare_place(
    drawing_id: str,
    *,
    notional_usd: float | None = None,
    drawings_path: Path | None = None,
) -> dict[str, Any]:
    t0 = time.time()
    corr = new_correlation_id()
    settings = _settings()
    box, err = _load_manual_box(drawing_id, drawings_path=drawings_path)
    if not box:
        out = {"ok": False, "error": err}
        audit_event(
            "prepare_place",
            source="manual_confirm",
            drawing_id=drawing_id,
            correlation_id=corr,
            duration_ms=(time.time() - t0) * 1000,
            ok=False,
            error=err,
        )
        return out

    store = StateStore(DEFAULT_STATE_PATH)
    st = store.get(drawing_id)
    if settings.writes_allowed:
        discovered, discover_err = _discover_open_row(
            settings, drawing_id, box["symbol"], st, box=box
        )
        if discover_err == "ambiguous_managed_orders":
            out = {
                "ok": False,
                "error": discover_err,
                "operation": "blocked",
                "phase": "RECONCILE_REQUIRED",
            }
            audit_event(
                "prepare_place",
                source="manual_confirm",
                drawing_id=drawing_id,
                correlation_id=corr,
                duration_ms=(time.time() - t0) * 1000,
                operation="blocked",
                error=discover_err,
            )
            return out
        if discovered:
            _heal_sync_state_from_exchange(store, drawing_id, discovered, st, box=box)
            out = _prepare_amend(drawing_id, box, settings)
            out["correlation_id"] = corr
            audit_event(
                "prepare_place",
                source="manual_confirm",
                drawing_id=drawing_id,
                correlation_id=corr,
                duration_ms=(time.time() - t0) * 1000,
                operation=out.get("operation"),
                order_id=out.get("order_id"),
                order_link_id=out.get("order_link_id"),
                tick_size=out.get("tick_size"),
                tick_source=out.get("tick_source"),
                normalized_targets=out.get("proposed_box"),
                chart_raw=out.get("chart_raw_box"),
                changes=out.get("changes"),
                ok=bool(out.get("ok")),
                error=out.get("error"),
            )
            return out

    status = resolve_order_status(drawing_id, settings=settings)
    intent = classify_setzen(drawing_id, status=status)
    if intent == "amend":
        out = _prepare_amend(drawing_id, box, settings)
        out["correlation_id"] = corr
        audit_event(
            "prepare_place",
            source="manual_confirm",
            drawing_id=drawing_id,
            correlation_id=corr,
            duration_ms=(time.time() - t0) * 1000,
            operation=out.get("operation"),
            order_id=out.get("order_id"),
            changes=out.get("changes"),
            ok=bool(out.get("ok")),
            error=out.get("error"),
        )
        return out
    if intent == "blocked":
        out = {
            "ok": False,
            "error": status.get("phase") or "status_blocked",
            "operation": "blocked",
            **status,
        }
        audit_event(
            "prepare_place",
            source="manual_confirm",
            drawing_id=drawing_id,
            correlation_id=corr,
            duration_ms=(time.time() - t0) * 1000,
            operation="blocked",
            ok=False,
            error=out.get("error"),
        )
        return out
    if intent == "filled":
        out = {
            "ok": False,
            "error": "position_already_filled",
            "operation": "filled",
            "message": "Position bereits gefüllt — kein Entry-AMEND.",
            **status,
        }
        audit_event(
            "prepare_place",
            source="manual_confirm",
            drawing_id=drawing_id,
            correlation_id=corr,
            duration_ms=(time.time() - t0) * 1000,
            operation="filled",
            ok=False,
        )
        return out
    if intent == "partial":
        out = {
            "ok": False,
            "error": "partial_fill_reconcile_required",
            "operation": "partial",
            **status,
        }
        audit_event(
            "prepare_place",
            source="manual_confirm",
            drawing_id=drawing_id,
            correlation_id=corr,
            duration_ms=(time.time() - t0) * 1000,
            operation="partial",
            ok=False,
        )
        return out
    out = _prepare_create(
        drawing_id,
        box,
        settings,
        notional_usd=notional_usd,
    )
    out["correlation_id"] = corr
    audit_event(
        "prepare_place",
        source="manual_confirm",
        drawing_id=drawing_id,
        correlation_id=corr,
        duration_ms=(time.time() - t0) * 1000,
        operation=out.get("operation"),
        ok=bool(out.get("ok")),
        can_place=out.get("can_place"),
        blockers=out.get("blockers"),
    )
    return out


def confirm_place(
    drawing_id: str,
    *,
    prepare_token: str,
    activations_path: Path | None = None,
    drawings_path: Path | None = None,
) -> dict[str, Any]:
    settings = _settings()
    if not settings.writes_allowed:
        return {"ok": False, "error": "live_not_armed", "phase": "EXCHANGE_ERROR"}

    bound = _STORE.consume(str(prepare_token))
    if not bound or str(bound.get("drawing_id")) != str(drawing_id):
        return {"ok": False, "error": "prepare_token_invalid", "phase": "EXCHANGE_ERROR"}

    box, err = _load_manual_box(drawing_id, drawings_path=drawings_path)
    if not box or box["content_hash"] != bound.get("content_hash"):
        return {"ok": False, "error": "drawing_changed_since_prepare", "phase": "EXCHANGE_ERROR"}

    if bound.get("action") == "amend":
        t0 = time.time()
        corr = str(bound.get("correlation_id") or prepare_token or new_correlation_id())
        with _lock(drawing_id):
            if settings.writes_allowed:
                out = execute_confirmed_amend(
                    drawing_id,
                    bound,
                    settings=settings,
                    state_path=DEFAULT_STATE_PATH,
                    box=box,
                    correlation_id=corr,
                )
            else:
                eng, _mode = build_sync_engine(
                    DEFAULT_STATE_PATH,
                    DEFAULT_EVENT_LOG_PATH,
                    force_dry_run=True,
                    settings=settings,
                )
                out = execute_confirmed_amend_dry(drawing_id, bound, engine=eng, box=box)
            if out.get("ok"):
                out["message"] = "Order erfolgreich aktualisiert"
            out.update(resolve_order_status(drawing_id, settings=settings))
            audit_event(
                "confirm_place",
                source="manual_confirm",
                drawing_id=drawing_id,
                correlation_id=corr,
                duration_ms=(time.time() - t0) * 1000,
                operation="amend",
                action=out.get("action"),
                order_id=out.get("order_id"),
                order_link_id=out.get("order_link_id"),
                changes=bound.get("changes"),
                verified=bool(out.get("verified")),
                ok=bool(out.get("ok")),
                error=out.get("error"),
            )
            return out

    qty_now, notional_now, extra = _resolve_qty(box, settings, notional_usd=None)
    if qty_now is None or qty_now <= 0:
        return {
            "ok": False,
            "error": extra.get("error", "qty_not_configured"),
            "phase": "EXCHANGE_ERROR",
        }
    if extra.get("blockers"):
        return {
            "ok": False,
            "error": "order_blocked",
            "blockers": extra.get("blockers"),
            "phase": "EXCHANGE_ERROR",
        }
    bound_qty = float(bound.get("qty") or 0)
    if abs(bound_qty - float(qty_now)) > 1e-9:
        return {"ok": False, "error": "qty_changed_since_prepare", "phase": "EXCHANGE_ERROR"}

    t0_create = time.time()
    corr_create = str(bound.get("correlation_id") or prepare_token or new_correlation_id())
    with _lock(drawing_id):
        act_path = activations_path or DEFAULT_ACTIVATIONS_PATH
        cycle_link_id = allocate_order_link_id(drawing_id)
        upsert_activation(
            act_path,
            drawing_id=drawing_id,
            symbol=box["symbol"],
            notional_usd=float(notional_now),
            dry_run=False,
            enabled=True,
            qty_doge=float(qty_now),
            order_link_id=cycle_link_id,
        )
        eng, mode = build_sync_engine(
            DEFAULT_STATE_PATH,
            DEFAULT_EVENT_LOG_PATH,
            force_dry_run=False,
            settings=settings,
        )
        if mode != "bybit_live" or eng.dry_run:
            return {"ok": False, "error": "sync_not_live", "phase": "EXCHANGE_ERROR"}
        obs = observe_drawings(drawings_path or DEFAULT_DRAWINGS_PATH)
        if not obs:
            return {"ok": False, "error": "drawings_missing", "phase": "EXCHANGE_ERROR"}
        acts = load_activations(act_path)
        if drawing_id not in acts:
            return {"ok": False, "error": "activation_failed", "phase": "EXCHANGE_ERROR"}
        act = acts[drawing_id]
        results = eng.process_observation(obs, acts)
        row = next((r for r in results if r.get("drawing_id") == drawing_id), None)
        st = eng.state.get(drawing_id)
        status = resolve_order_status(drawing_id, settings=settings, phase_hint="CREATE_PENDING")
        create_actions = ("CREATE_LIMIT", "CREATE_LIMIT_RECOVER")
        sync_ok = bool(
            row
            and row.get("ok")
            and row.get("action") in create_actions
        )
        out = {
            "ok": sync_ok,
            "error": None if sync_ok else (row or {}).get("reason", "sync_failed"),
            "sync_result": row,
            "state": st.to_dict() if st else None,
            **status,
        }
        audit_event(
            "confirm_place",
            source="manual_confirm",
            drawing_id=drawing_id,
            correlation_id=corr_create,
            duration_ms=(time.time() - t0_create) * 1000,
            operation="create",
            action=(row or {}).get("action"),
            order_id=(row or {}).get("order_id"),
            ok=sync_ok,
            error=out.get("error"),
        )
        return out


def prepare_cancel(drawing_id: str) -> dict[str, Any]:
    settings = _settings()
    st = StateStore(DEFAULT_STATE_PATH).get(drawing_id)
    if not st or not st.exchange_order_id:
        return {"ok": False, "error": "no_mapped_order", "phase": "INACTIVE"}
    status = resolve_order_status(drawing_id, settings=settings)
    if status.get("phase") == "POSITION_FILLED":
        return {
            "ok": False,
            "error": "position_already_filled",
            "message": "Position bereits eröffnet — kein Order-Cancel.",
            **status,
        }
    if status.get("phase") == "PARTIALLY_FILLED":
        return {
            "ok": False,
            "error": "partial_fill_reconcile_required",
            "phase": "RECONCILE_REQUIRED",
        }
    token = _STORE.issue(
        {"drawing_id": drawing_id, "action": "cancel", "order_id": st.exchange_order_id}
    )
    return {
        "ok": True,
        "prepare_token": token,
        "drawing_id": drawing_id,
        "order_id": st.exchange_order_id,
        **status,
    }


def confirm_cancel(
    drawing_id: str,
    *,
    prepare_token: str,
    activations_path: Path | None = None,
) -> dict[str, Any]:
    settings = _settings()
    if not settings.writes_allowed:
        return {"ok": False, "error": "live_not_armed", "phase": "EXCHANGE_ERROR"}

    bound = _STORE.consume(str(prepare_token))
    if not bound or bound.get("action") != "cancel" or str(bound.get("drawing_id")) != str(drawing_id):
        return {"ok": False, "error": "prepare_token_invalid"}

    pre = resolve_order_status(drawing_id, settings=settings, phase_hint="CANCEL_PENDING")
    if pre.get("phase") == "POSITION_FILLED":
        return {"ok": False, "error": "position_already_filled", **pre}

    with _lock(drawing_id):
        store = StateStore(DEFAULT_STATE_PATH)
        st = store.get(drawing_id)
        if not st or not st.exchange_order_id:
            return {"ok": False, "error": "no_mapped_order", **pre}
        adapter = BybitLiveExchangeAdapter.from_settings(settings)
        ack = adapter.cancel_limit(st.exchange_order_id)
        if not ack.ok:
            return {"ok": False, "error": ack.message or "cancel_failed", **pre}
        ok, confirmed, _reason = wait_for_order_rest(
            adapter.order_manager,
            st.symbol,
            order_id=st.exchange_order_id,
            attempts=6,
        )
        open_row = find_limit_order(
            adapter.order_manager,
            st.symbol,
            order_id=st.exchange_order_id,
        )
        if open_row is not None:
            return {"ok": False, "error": "cancel_not_confirmed", "phase": "CANCEL_PENDING"}
        st.order_status = OrderStatus.CANCELLED.value
        st.last_action = "CANCEL_LIMIT"
        store.put(st)
        disable_activation(activations_path or DEFAULT_ACTIVATIONS_PATH, drawing_id)
        status = resolve_order_status(drawing_id, settings=settings)
        status["phase"] = "INACTIVE"
        status["lamp"] = "inactive"
        return {"ok": True, **status}


def resolve_order_status(
    drawing_id: str,
    *,
    settings: LiveOrderSettings | None = None,
    phase_hint: str | None = None,
) -> dict[str, Any]:
    """Map local state + exchange REST to UI lamp (never ACK-only)."""
    settings = settings or _settings()
    store = StateStore(DEFAULT_STATE_PATH)
    st = store.get(drawing_id)
    act_row = get_activation_row(DEFAULT_ACTIVATIONS_PATH, drawing_id)
    enabled = bool(act_row and act_row.get("enabled"))

    if phase_hint in ("CREATE_PENDING", "AMEND_PENDING", "CANCEL_PENDING"):
        return {
            "drawing_id": drawing_id,
            "phase": phase_hint,
            "lamp": "loading",
            "lamp_tooltip": phase_hint,
        }

    if not st or not st.exchange_order_id:
        if enabled:
            return {
                "drawing_id": drawing_id,
                "phase": "RECONCILE_REQUIRED",
                "lamp": "warning",
                "lamp_tooltip": "Aktivierung ohne bestätigte Order — Reconcile erforderlich",
            }
        return {
            "drawing_id": drawing_id,
            "phase": "INACTIVE",
            "lamp": "inactive",
            "lamp_tooltip": "Order nicht aktiv",
        }

    if not settings.writes_allowed:
        return {
            "drawing_id": drawing_id,
            "phase": "INACTIVE",
            "lamp": "inactive",
            "lamp_tooltip": "Live nicht armed — nur Anzeige",
            "order_id": st.exchange_order_id,
            "local_status": st.order_status,
        }

    adapter = BybitLiveExchangeAdapter.from_settings(settings)
    try:
        om = adapter.order_manager
    except RuntimeError:
        return {
            "drawing_id": drawing_id,
            "phase": "RECONCILE_REQUIRED",
            "lamp": "warning",
            "order_id": st.exchange_order_id,
        }

    status_box, _ = _load_manual_box(drawing_id)
    expected_side = None
    expected_position_idx = None
    if status_box:
        expected_side = _expected_order_side(str(status_box.get("side") or "long"))
        expected_position_idx = int(
            status_box.get("position_idx") or _position_idx_for(str(status_box.get("side") or "long"))
        )
    on_book, discover_err = discover_managed_open_order_ex(
        om,
        st.symbol,
        drawing_id,
        state_order_id=st.exchange_order_id,
        state_order_link_id=st.order_link_id,
        expected_side=expected_side,
        expected_position_idx=expected_position_idx,
    )
    if discover_err == "ambiguous_managed_orders":
        return {
            "drawing_id": drawing_id,
            "phase": "RECONCILE_REQUIRED",
            "lamp": "warning",
            "lamp_tooltip": "Mehrere passende offene Orders — blockiert",
            "order_id": st.exchange_order_id,
            "error": discover_err,
        }
    if on_book is not None and st.order_status in (OrderStatus.CANCELLED.value, "CANCELLED"):
        store = StateStore(DEFAULT_STATE_PATH)
        _heal_sync_state_from_exchange(store, drawing_id, on_book, st)
        st = store.get(drawing_id) or st

    if on_book is not None:
        mapped = map_bybit_order_status(on_book.order_status)
        if mapped == OrderStatus.PARTIALLY_FILLED:
            return {
                "drawing_id": drawing_id,
                "phase": "PARTIALLY_FILLED",
                "lamp": "warning",
                "lamp_tooltip": "Teilfüllung — manuelle Prüfung",
                "order_id": on_book.order_id,
            }
        return {
            "drawing_id": drawing_id,
            "phase": "ORDER_OPEN",
            "lamp": "pending",
            "lamp_tooltip": "Limit-Order gesetzt – wartet auf Fill",
            "order_id": on_book.order_id,
        }

    box_side = "long"
    box_loaded, _ = _load_manual_box(drawing_id)
    if box_loaded:
        box_side = str(box_loaded.get("side") or "long")
    try:
        if box_side == "short":
            pos_size, _avg = om.get_short_position(st.symbol)
        else:
            pos_size, _avg = om.get_long_position(st.symbol)
    except Exception:
        pos_size = None

    if pos_size is not None and float(pos_size) > 0:
        return {
            "drawing_id": drawing_id,
            "phase": "POSITION_FILLED",
            "lamp": "filled",
            "lamp_tooltip": "Position gefüllt",
            "order_id": st.exchange_order_id,
            "position_size": pos_size,
        }

    if st.order_status in (OrderStatus.CANCELLED.value, "CANCELLED"):
        return {
            "drawing_id": drawing_id,
            "phase": "INACTIVE",
            "lamp": "inactive",
            "order_id": st.exchange_order_id,
        }

    return {
        "drawing_id": drawing_id,
        "phase": "RECONCILE_REQUIRED",
        "lamp": "warning",
        "lamp_tooltip": "Exchange-Zustand unklar",
        "order_id": st.exchange_order_id,
    }


def status_bulk(drawing_ids: list[str]) -> dict[str, Any]:
    settings = _settings()
    items = {}
    for did in drawing_ids:
        if not did or not str(did).startswith("d-"):
            continue
        items[str(did)] = resolve_order_status(str(did), settings=settings)
    return {"ok": True, "armed": settings.live_armed, "writes_allowed": settings.writes_allowed, "statuses": items}


def run_sync_pass(
    *,
    source: str = "drawing_sync",
    drag_mode: str | None = None,
    overlay_id: str | None = None,
) -> list[dict[str, Any]]:
    """One shot after entry drag only (SL/TP drags must not call this)."""
    t0 = time.time()
    settings = _settings()
    if not settings.writes_allowed:
        return []
    eng, _mode = build_sync_engine(
        DEFAULT_STATE_PATH,
        DEFAULT_EVENT_LOG_PATH,
        force_dry_run=False,
        settings=settings,
    )
    obs = observe_drawings(DEFAULT_DRAWINGS_PATH)
    if not obs:
        return []
    acts = load_activations(DEFAULT_ACTIVATIONS_PATH)
    results = eng.process_observation(obs, acts)
    for row in results:
        audit_event(
            "drawing_sync_pass",
            source=source,
            drawing_id=str(row.get("drawing_id") or ""),
            correlation_id=new_correlation_id(),
            duration_ms=(time.time() - t0) * 1000,
            drag_mode=drag_mode,
            overlay_id=overlay_id,
            action=row.get("action"),
            ok=bool(row.get("ok")),
            reason=row.get("reason"),
            order_id=row.get("order_id"),
        )
    return results
