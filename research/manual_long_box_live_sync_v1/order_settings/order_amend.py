"""Manual Setzen → AMEND path (open limit only, explicit confirm)."""

from __future__ import annotations

from typing import Any

from research.manual_long_box_live_sync_v1.config import PRICE_EPS
from research.manual_long_box_live_sync_v1.persistence import StateStore

from .config import LiveOrderSettings
from .exchange_adapter import BybitLiveExchangeAdapter
from .mlb_audit_log import audit_event
from .price_tick import (
    normalize_box_prices,
    prices_equal_tick,
    resolve_tick_size_for_symbol,
    round_price_to_tick,
)
from .reconcile import (
    discover_managed_open_order_ex,
    map_bybit_order_status,
    wait_for_amend_fields,
)

_BLOCK_PHASES = frozenset(
    {
        "CREATE_PENDING",
        "AMEND_PENDING",
        "CANCEL_PENDING",
        "RECONCILE_REQUIRED",
        "UNKNOWN",
    }
)


def classify_setzen(
    drawing_id: str,
    *,
    status: dict[str, Any],
) -> str:
    phase = str(status.get("phase") or "")
    if phase in _BLOCK_PHASES or status.get("lamp") == "loading":
        return "blocked"
    if phase == "POSITION_FILLED":
        return "filled"
    if phase == "PARTIALLY_FILLED":
        return "partial"
    if phase == "ORDER_OPEN":
        return "amend"
    if phase == "INACTIVE":
        return "create"
    return "blocked"


def _live_order_row(
    settings: LiveOrderSettings,
    st,
) -> tuple[Any | None, str | None]:
    if not settings.writes_allowed:
        return None, "live_not_armed"
    try:
        adapter = BybitLiveExchangeAdapter.from_settings(settings)
        om = adapter.order_manager
    except RuntimeError:
        return None, "credentials_missing"
    row, err = discover_managed_open_order_ex(
        om,
        st.symbol,
        st.drawing_id,
        state_order_id=st.exchange_order_id,
        state_order_link_id=st.order_link_id,
    )
    if err == "ambiguous_managed_orders":
        return None, err
    if row is None:
        return None, "order_not_on_book"
    mapped = map_bybit_order_status(row.order_status)
    if mapped.value == "PARTIALLY_FILLED":
        return None, "partial_fill_reconcile_required"
    return row, None


def build_amend_diff(
    *,
    current: dict[str, float | None],
    proposed: dict[str, float],
    tick_size: float | None = None,
) -> dict[str, bool]:
    return {
        "entry_price": not prices_equal_tick(
            current.get("entry_price"), proposed["entry_price"], tick_size
        ),
        "stop_loss": not prices_equal_tick(
            current.get("stop_loss"), proposed["stop_price"], tick_size
        ),
        "take_profit": not prices_equal_tick(
            current.get("take_profit"), proposed["target_price"], tick_size
        ),
    }


def execute_confirmed_amend(
    drawing_id: str,
    bound: dict[str, Any],
    *,
    settings: LiveOrderSettings,
    state_path,
    box: dict[str, Any],
    correlation_id: str | None = None,
) -> dict[str, Any]:
    import time as _time

    from research.manual_long_box_live_sync_v1.drawing_watch import hash_payload

    t0 = _time.time()
    corr = correlation_id or bound.get("correlation_id")
    store = StateStore(state_path)
    st = store.get(drawing_id)
    if not st or not st.exchange_order_id:
        return {"ok": False, "error": "no_mapped_order", "phase": "EXCHANGE_ERROR"}

    row, err = _live_order_row(settings, st)
    if err or row is None:
        return {"ok": False, "error": err or "order_not_on_book", "phase": "RECONCILE_REQUIRED"}

    snap = bound.get("exchange_snapshot") or {}
    if str(row.order_id) != str(bound.get("order_id") or st.exchange_order_id):
        return {"ok": False, "error": "order_id_changed", "phase": "RECONCILE_REQUIRED"}
    if bound.get("order_link_id") and row.order_link_id:
        if str(row.order_link_id) != str(bound.get("order_link_id")):
            return {"ok": False, "error": "order_link_changed", "phase": "RECONCILE_REQUIRED"}

    adapter = BybitLiveExchangeAdapter.from_settings(settings)
    tick_size = bound.get("tick_size")
    tick_source = bound.get("tick_source")
    if not tick_size:
        tick_size, tick_source = resolve_tick_size_for_symbol(
            settings,
            st.symbol,
            order_manager=adapter.order_manager,
        )
    if not tick_size:
        return {
            "ok": False,
            "error": "tick_size_unavailable",
            "phase": "RECONCILE_REQUIRED",
            "tick_source": tick_source,
        }

    targets = bound.get("normalized_targets")
    if not targets:
        return {
            "ok": False,
            "error": "prepare_snapshot_missing",
            "phase": "EXCHANGE_ERROR",
        }

    changes = build_amend_diff(
        current={
            "entry_price": row.price,
            "stop_loss": row.stop_loss,
            "take_profit": row.take_profit,
        },
        proposed={
            "entry_price": float(targets["entry_price"]),
            "stop_price": float(targets["stop_loss"]),
            "target_price": float(targets["take_profit"]),
        },
        tick_size=tick_size,
    )
    for field, key in (
        ("entry_price", "price"),
        ("stop_loss", "stop_loss"),
        ("take_profit", "take_profit"),
    ):
        if not changes.get(field):
            expected = snap.get(field)
            live_val = row.price if key == "price" else getattr(row, key, None)
            if expected is not None and not prices_equal_tick(live_val, float(expected), tick_size):
                return {
                    "ok": False,
                    "error": "exchange_changed_since_prepare",
                    "phase": "RECONCILE_REQUIRED",
                }

    if not any(changes.values()):
        return {"ok": False, "error": "no_changes", "phase": "EXCHANGE_ERROR"}

    amend_price = float(targets["entry_price"]) if changes.get("entry_price") else None
    amend_sl = float(targets["stop_loss"]) if changes.get("stop_loss") else None
    amend_tp = float(targets["take_profit"]) if changes.get("take_profit") else None

    if amend_price is not None and (amend_sl is not None or amend_tp is not None):
        ack = adapter.amend_limit_fields(
            st.exchange_order_id,
            price=amend_price,
            stop_loss=amend_sl,
            take_profit=amend_tp,
        )
        action = "AMEND_MIXED"
    elif amend_price is not None:
        ack = adapter.amend_limit(st.exchange_order_id, new_price=amend_price)
        action = "AMEND_LIMIT"
    else:
        ack = adapter.amend_limit_fields(
            st.exchange_order_id,
            stop_loss=amend_sl,
            take_profit=amend_tp,
        )
        action = "AMEND_SL_TP"

    if not ack.ok:
        out = {
            "ok": False,
            "error": ack.message or "amend_failed",
            "phase": "EXCHANGE_ERROR",
            "action": action,
        }
        audit_event(
            "execute_confirmed_amend",
            source="manual_confirm",
            drawing_id=drawing_id,
            correlation_id=corr,
            duration_ms=(_time.time() - t0) * 1000,
            operation="amend",
            action=action,
            changes=changes,
            sent_entry=amend_price,
            sent_stop_loss=amend_sl,
            sent_take_profit=amend_tp,
            exchange_ok=False,
            exchange_message=ack.message,
            ok=False,
        )
        return out

    ok_verify, confirmed, reason, verify_detail = wait_for_amend_fields(
        adapter.order_manager,
        st.symbol,
        order_id=st.exchange_order_id,
        order_link_id=st.order_link_id,
        expected_entry=amend_price,
        expected_sl=amend_sl,
        expected_tp=amend_tp,
        tick_size=tick_size,
    )
    if not ok_verify:
        rest_row = verify_detail.get("last_rest") if verify_detail else None
        out = {
            "ok": False,
            "error": reason,
            "phase": "RECONCILE_REQUIRED",
            "action": action,
        }
        audit_event(
            "execute_confirmed_amend",
            source="manual_confirm",
            drawing_id=drawing_id,
            correlation_id=corr,
            duration_ms=(_time.time() - t0) * 1000,
            operation="amend",
            action=action,
            changes=changes,
            tick_size=tick_size,
            tick_source=tick_source,
            chart_raw_entry=box["entry_price"],
            chart_raw_sl=box["stop_price"],
            chart_raw_tp=box["target_price"],
            normalized_targets=targets,
            sent_entry=amend_price,
            sent_stop_loss=amend_sl,
            sent_take_profit=amend_tp,
            rest_price=getattr(rest_row, "price", None) if rest_row else None,
            rest_sl=getattr(rest_row, "stop_loss", None) if rest_row else None,
            rest_tp=getattr(rest_row, "take_profit", None) if rest_row else None,
            verify_failures=verify_detail.get("failures") if verify_detail else None,
            rest_verify_ok=False,
            rest_verify_reason=reason,
            ok=False,
        )
        return out

    content_hash = hash_payload(
        {
            "id": drawing_id,
            "entry": targets["entry_price"],
            "stop": targets["stop_loss"],
            "target": targets["take_profit"],
        }
    )
    st.entry_price = float(targets["entry_price"])
    st.stop_price = float(targets["stop_loss"])
    st.target_price = float(targets["take_profit"])
    st.desired_sl = float(targets["stop_loss"])
    st.desired_tp = float(targets["take_profit"])
    st.content_hash = content_hash
    st.last_action = action
    if confirmed:
        st.order_status = map_bybit_order_status(confirmed.order_status).value
    store.put(st)

    out = {
        "ok": True,
        "action": action,
        "order_id": st.exchange_order_id,
        "order_link_id": st.order_link_id,
        "verified": True,
        "state": st.to_dict(),
        "phase": "ORDER_OPEN",
        "lamp": "pending",
    }
    audit_event(
        "execute_confirmed_amend",
        source="manual_confirm",
        drawing_id=drawing_id,
        correlation_id=corr,
        duration_ms=(_time.time() - t0) * 1000,
        operation="amend",
        action=action,
        order_id=st.exchange_order_id,
        order_link_id=st.order_link_id,
        changes=changes,
        sent_entry=amend_price,
        sent_stop_loss=amend_sl,
        sent_take_profit=amend_tp,
        tick_size=tick_size,
        rest_verify_ok=True,
        ok=True,
    )
    return out


def execute_confirmed_amend_dry(
    drawing_id: str,
    bound: dict[str, Any],
    *,
    engine,
    box: dict[str, Any],
) -> dict[str, Any]:
    from research.manual_long_box_live_sync_v1.drawing_watch import hash_payload

    st = engine.state.get(drawing_id)
    if not st:
        return {"ok": False, "error": "no_mapped_order"}
    changes = bound.get("changes") or {}
    oid = st.exchange_order_id
    amend_price = float(box["entry_price"]) if changes.get("entry_price") else None
    amend_sl = float(box["stop_price"]) if changes.get("stop_loss") else None
    amend_tp = float(box["target_price"]) if changes.get("take_profit") else None

    if amend_price is not None and (amend_sl is None and amend_tp is None):
        ack = engine.exchange.amend_limit(oid, new_price=amend_price)
        action = "AMEND_LIMIT"
    else:
        amend_fields = getattr(engine.exchange, "amend_limit_fields", None)
        if not amend_fields:
            return {"ok": False, "error": "amend_not_supported"}
        ack = amend_fields(
            oid,
            price=amend_price,
            stop_loss=amend_sl,
            take_profit=amend_tp,
        )
        action = "AMEND_SL_TP" if amend_price is None else "AMEND_MIXED"

    if not ack.ok:
        return {"ok": False, "error": ack.message or "amend_failed", "action": action}

    st.entry_price = float(box["entry_price"])
    st.stop_price = float(box["stop_price"])
    st.target_price = float(box["target_price"])
    st.desired_sl = float(box["stop_price"])
    st.desired_tp = float(box["target_price"])
    st.content_hash = hash_payload(
        {
            "id": drawing_id,
            "entry": box["entry_price"],
            "stop": box["stop_price"],
            "target": box["target_price"],
        }
    )
    st.last_action = action
    engine.state.put(st)
    return {
        "ok": True,
        "action": action,
        "order_id": oid,
        "order_link_id": st.order_link_id,
        "verified": True,
        "state": st.to_dict(),
    }
