"""Dashboard adapter → pools manual_long_box order_controller (no duplicate OM)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

POOLS_ROOT = Path("/home/telgenbuescher/projects/pools+ob+delta_bot")
DRAWINGS_PATH = Path(__file__).resolve().parents[1] / "research_charts" / "user_data" / "drawings.json"


def _ensure_pools_path() -> None:
    """Pools must precede spread_recovery on sys.path (namespace `research`)."""
    root = str(POOLS_ROOT)
    while root in sys.path:
        sys.path.remove(root)
    sys.path.insert(0, root)
    mlb_prefix = "research.manual_long_box_live_sync_v1"
    for name in list(sys.modules):
        if name == mlb_prefix or name.startswith(mlb_prefix + "."):
            del sys.modules[name]
    loaded = sys.modules.get("research")
    if loaded is not None:
        mod_file = str(getattr(loaded, "__file__", "") or "")
        if "pools+ob+delta_bot" not in mod_file:
            for name in list(sys.modules):
                if name == "research" or name.startswith("research."):
                    del sys.modules[name]


def prepare_place(body: dict[str, Any]) -> dict[str, Any]:
    _ensure_pools_path()
    from research.manual_long_box_live_sync_v1.order_settings import order_controller as oc

    did = str(body.get("drawing_id") or "")
    notional = body.get("notional_usd")
    return oc.prepare_place(
        did,
        notional_usd=float(notional) if notional is not None else None,
        drawings_path=DRAWINGS_PATH,
    )


def confirm_place(body: dict[str, Any]) -> dict[str, Any]:
    _ensure_pools_path()
    from research.manual_long_box_live_sync_v1.order_settings import order_controller as oc

    return oc.confirm_place(
        str(body.get("drawing_id") or ""),
        prepare_token=str(body.get("prepare_token") or ""),
        drawings_path=DRAWINGS_PATH,
    )


def prepare_cancel(body: dict[str, Any]) -> dict[str, Any]:
    _ensure_pools_path()
    from research.manual_long_box_live_sync_v1.order_settings import order_controller as oc

    return oc.prepare_cancel(str(body.get("drawing_id") or ""))


def confirm_cancel(body: dict[str, Any]) -> dict[str, Any]:
    _ensure_pools_path()
    from research.manual_long_box_live_sync_v1.order_settings import order_controller as oc

    return oc.confirm_cancel(
        str(body.get("drawing_id") or ""),
        prepare_token=str(body.get("prepare_token") or ""),
    )


def status_bulk(body: dict[str, Any]) -> dict[str, Any]:
    _ensure_pools_path()
    from research.manual_long_box_live_sync_v1.order_settings import order_controller as oc

    ids = body.get("drawing_ids") or []
    if not isinstance(ids, list):
        ids = []
    return oc.status_bulk([str(x) for x in ids])


def drawing_drag_triggers_entry_sync(body: dict[str, Any]) -> bool:
    _ensure_pools_path()
    from research.manual_long_box_live_sync_v1.order_settings.drag_sync import (
        drawing_drag_triggers_entry_sync as _gate,
    )

    return _gate(body)


def sync_pass(
    *,
    drag_mode: str | None = None,
    overlay_id: str | None = None,
) -> dict[str, Any]:
    _ensure_pools_path()
    from research.manual_long_box_live_sync_v1.order_settings import order_controller as oc

    return {
        "ok": True,
        "results": oc.run_sync_pass(
            source="drawing_sync",
            drag_mode=drag_mode,
            overlay_id=overlay_id,
        ),
    }


def sizing_config() -> dict[str, Any]:
    """Canonical caps/steps for chart display (same source as order prepare)."""
    _ensure_pools_path()
    from decimal import Decimal

    from research.manual_long_box_live_sync_v1.order_settings.config import load_dotenv_chain, load_settings
    from research.manual_long_box_live_sync_v1.order_settings.order_sizing import (
        TEST_ORDER_NOTIONAL_USDT,
        fetch_instrument_steps_public,
    )

    load_dotenv_chain()
    settings = load_settings()
    cap = settings.fixed_test_notional_usd
    if cap is None:
        cap = float(TEST_ORDER_NOTIONAL_USDT)
    sym = (settings.symbol or "DOGEUSDT").upper()
    steps = fetch_instrument_steps_public(sym)
    qty_step = float(steps[0]) if steps else 1.0
    min_qty = float(steps[1]) if steps else 0.0
    return {
        "ok": True,
        "symbol": sym,
        "notional_cap_usdt": float(cap),
        "qty_step": qty_step,
        "min_order_qty": min_qty,
        "source": "research.manual_long_box_live_sync_v1.order_settings.order_sizing",
    }


def quote_order_display_notional(entry_price: float, symbol: str | None = None) -> dict[str, Any]:
    _ensure_pools_path()
    from decimal import Decimal

    from research.manual_long_box_live_sync_v1.order_settings.config import load_dotenv_chain, load_settings
    from research.manual_long_box_live_sync_v1.order_settings.order_sizing import (
        TEST_ORDER_NOTIONAL_USDT,
        compute_fixed_notional_qty,
        fetch_instrument_steps_public,
    )

    load_dotenv_chain()
    settings = load_settings()
    cap = settings.fixed_test_notional_usd
    cap_dec = Decimal(str(cap if cap is not None else TEST_ORDER_NOTIONAL_USDT))
    sym = (symbol or settings.symbol or "DOGEUSDT").upper()
    steps = fetch_instrument_steps_public(sym)
    if not steps:
        return {"ok": False, "error": "instrument_steps_unknown"}
    qty, meta = compute_fixed_notional_qty(
        float(entry_price),
        qty_step=steps[0],
        min_qty=steps[1],
        notional_cap=cap_dec,
    )
    if qty is None:
        return {"ok": False, **meta}
    notional = float(qty) * float(entry_price)
    return {
        "ok": True,
        "symbol": sym,
        "entry_price": float(entry_price),
        "qty_doge": float(qty),
        "notional_usdt": notional,
        "notional_cap_usdt": float(cap_dec),
        "sizing": meta,
    }


def is_mlb_test_user_position(payload: dict[str, Any]) -> bool:
    if payload.get("type") != "position":
        return False
    meta = payload.get("metadata") or {}
    did = str(meta.get("drawing_id") or "")
    if not did.startswith("d-"):
        return False
    return meta.get("drawing_type") in ("long_position", "short_position")


def patch_mlb_test_position_overlay(payload: dict[str, Any]) -> dict[str, Any]:
    """Server-side labels: user manual boxes use order_sizing notional (not TRP 100 default)."""
    if not is_mlb_test_user_position(payload):
        return payload
    entry = float(payload.get("entry_price") or 0)
    if entry <= 0:
        return payload
    sym = str(payload.get("symbol") or "DOGEUSDT")
    quote = quote_order_display_notional(entry, sym)
    if not quote.get("ok"):
        return payload
    from .trp_import import ensure_trp_on_path

    ensure_trp_on_path()
    from drawings.position import (
        compute_position,
        format_entry_label,
        format_stop_label,
        format_target_label,
    )

    side = str(payload.get("side") or "long")
    stats = compute_position(
        side,
        entry,
        float(payload.get("stop_price") or 0),
        float(payload.get("target_price") or 0),
        notional=float(quote["notional_usdt"]),
    )
    out = dict(payload)
    out["position_notional"] = float(stats.notional)
    out["entry_label"] = format_entry_label(stats)
    out["stop_label"] = format_stop_label(stats)
    out["target_label"] = format_target_label(stats)
    meta = dict(out.get("metadata") or {})
    meta["mlb_order_notional_usdt"] = float(quote["notional_usdt"])
    meta["mlb_order_qty"] = float(quote["qty_doge"])
    out["metadata"] = meta
    return out
