"""Frozen entry + initial TP/SL trade plan for persisted signals.

Uses only frozen helpers:
- ``resolve_entries`` (T0 = first 1m open strictly after confirmation)
- ``tpsl_for_tf`` / ``trade_levels`` (initial TP/SL; BE50 does not alter initial SL)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping

import pandas as pd

from signal_generator.strategy.wave_fade.be50 import trade_levels
from signal_generator.strategy.wave_fade.signals import resolve_entries


PRICE_SOURCE = "resolve_entries_t0_1m_open"


def _iso_z(ts: Any) -> str | None:
    if ts is None or (isinstance(ts, float) and pd.isna(ts)):
        return None
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    return t.isoformat().replace("+00:00", "Z")


def levels_for_entry(*, side: str, timeframe: str, entry_price: float) -> dict[str, float]:
    """Initial TP/SL/BE-trigger from frozen ``trade_levels`` (BE50 does not change initial SL)."""
    tr = pd.Series(
        {
            "entry_price": float(entry_price),
            "side": str(side).upper(),
            "highest_tf_reached": str(timeframe),
        }
    )
    lv = trade_levels(tr)
    return {
        "entry_price": float(lv["entry"]),
        "tp_price": float(lv["tp"]),
        "sl_price": float(lv["sl"]),
        "be_trigger_price": float(lv["be_trigger"]),
        "break_even_price": float(lv["entry"]),  # BE50 moves SL to entry when armed
        "tp_pct": float(lv["tp_pct"]),
        "sl_pct": float(lv["sl_pct"]),
    }


def attach_resolved_entries(
    events: pd.DataFrame,
    open_times,
    opens,
) -> pd.DataFrame:
    """Run frozen ``resolve_entries`` and attach initial TP/SL columns."""
    if events is None or events.empty:
        return events
    out = resolve_entries(events, open_times, opens)
    tp_list: list[float | None] = []
    sl_list: list[float | None] = []
    tp_pct_list: list[float | None] = []
    sl_pct_list: list[float | None] = []
    be_trig_list: list[float | None] = []
    be_px_list: list[float | None] = []
    for _, row in out.iterrows():
        if not bool(row.get("entry_valid")):
            tp_list.append(None)
            sl_list.append(None)
            tp_pct_list.append(None)
            sl_pct_list.append(None)
            be_trig_list.append(None)
            be_px_list.append(None)
            continue
        side = str(row.get("side") or row.get("direction") or "").upper()
        # Annotated fade events use ``side``; persisted rows use direction synonym
        if side not in ("LONG", "SHORT"):
            side = str(row.get("side") or "").upper()
        tf = str(row.get("signal_tf") or row.get("timeframe") or "")
        lv = levels_for_entry(
            side=side,
            timeframe=tf,
            entry_price=float(row["entry_price"]),
        )
        tp_list.append(lv["tp_price"])
        sl_list.append(lv["sl_price"])
        tp_pct_list.append(lv["tp_pct"])
        sl_pct_list.append(lv["sl_pct"])
        be_trig_list.append(lv["be_trigger_price"])
        be_px_list.append(lv["break_even_price"])
    out = out.copy()
    out["tp_price"] = tp_list
    out["sl_price"] = sl_list
    out["tp_pct"] = tp_pct_list
    out["sl_pct"] = sl_pct_list
    out["be_trigger_price"] = be_trig_list
    out["break_even_price"] = be_px_list
    out["price_source"] = PRICE_SOURCE
    return out


def trade_plan_dict_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Serialize entry/TP/SL plan for Signal.metadata (JSON-safe)."""
    entry_valid = bool(row.get("entry_valid", False))
    entry_price = row.get("entry_price")
    try:
        entry_f = float(entry_price) if entry_price is not None and pd.notna(entry_price) else None
    except (TypeError, ValueError):
        entry_f = None
    if entry_f is not None and entry_f <= 0:
        entry_valid = False
        entry_f = None

    plan: dict[str, Any] = {
        "entry_valid": entry_valid,
        "price_source": PRICE_SOURCE,
        "entry_price": entry_f,
        "entry_time": _iso_z(row.get("entry_time")),
        "tp_pct": float(row["tp_pct"]) if row.get("tp_pct") is not None and pd.notna(row.get("tp_pct")) else None,
        "sl_pct": float(row["sl_pct"]) if row.get("sl_pct") is not None and pd.notna(row.get("sl_pct")) else None,
        "tp_price": float(row["tp_price"]) if row.get("tp_price") is not None and pd.notna(row.get("tp_price")) else None,
        "sl_price": float(row["sl_price"]) if row.get("sl_price") is not None and pd.notna(row.get("sl_price")) else None,
        "be_trigger_price": (
            float(row["be_trigger_price"])
            if row.get("be_trigger_price") is not None and pd.notna(row.get("be_trigger_price"))
            else None
        ),
        "break_even_price": (
            float(row["break_even_price"])
            if row.get("break_even_price") is not None and pd.notna(row.get("break_even_price"))
            else None
        ),
    }
    return plan


def merge_trade_plan_into_metadata(metadata_json: str | None, plan: Mapping[str, Any]) -> str:
    try:
        meta = json.loads(metadata_json or "{}")
        if not isinstance(meta, dict):
            meta = {}
    except json.JSONDecodeError:
        meta = {}
    meta.update(dict(plan))
    return json.dumps(meta, separators=(",", ":"))


def parse_trade_plan_from_metadata(metadata: Any) -> dict[str, Any]:
    """Extract trade-plan fields from CH metadata JSON (or empty dict)."""
    if metadata is None:
        return {}
    if isinstance(metadata, dict):
        meta = metadata
    else:
        try:
            meta = json.loads(str(metadata) or "{}")
        except json.JSONDecodeError:
            return {}
        if not isinstance(meta, dict):
            return {}
    keys = (
        "entry_valid",
        "price_source",
        "entry_price",
        "entry_time",
        "tp_pct",
        "sl_pct",
        "tp_price",
        "sl_price",
        "be_trigger_price",
        "break_even_price",
    )
    return {k: meta[k] for k in keys if k in meta}


def reconstruct_trade_plan(
    *,
    confirmation_available_at: datetime,
    side: str,
    timeframe: str,
    open_times,
    opens,
) -> dict[str, Any]:
    """Deterministic backfill helper: confirmation → resolve_entries → trade_levels."""
    conf = confirmation_available_at
    if conf.tzinfo is None:
        conf = conf.replace(tzinfo=timezone.utc)
    else:
        conf = conf.astimezone(timezone.utc)
    ev = pd.DataFrame(
        {
            "confirmation_available_at": [conf],
            "side": [str(side).upper()],
            "signal_tf": [str(timeframe)],
            "timeframe": [str(timeframe)],
        }
    )
    out = attach_resolved_entries(ev, open_times, opens)
    return trade_plan_dict_from_row(out.iloc[0])
