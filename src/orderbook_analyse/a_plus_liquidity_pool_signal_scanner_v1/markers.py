"""Marker payloads for research charts (no execution)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


BACKTESTER_SOURCE = "a_plus_pool_signal_scanner_v1"
STRATEGY_ID = "a_plus_liquidity_pool_signal_scanner_v1"


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _tooltip(row: dict[str, Any]) -> str:
    parts = [
        "RESEARCH ONLY – NO ORDER",
        f"state={row.get('state')}",
        f"setup_type={row.get('setup_type')}",
        f"signal_id={row.get('signal_id') or row.get('setup_id')}",
        f"manual_reference_at={row.get('manual_reference_at') or (row.get('htf_context') or {}).get('manual_reference_at')}",
        f"armed_at={row.get('armed_at')}",
        f"hypothetical_filled_at={row.get('hypothetical_filled_at') or row.get('filled_at')}",
        f"confirmed_at={row.get('confirmed_at')}",
        f"invalidated_at={row.get('invalidated_at')}",
        f"expired_at={row.get('expired_at')}",
        f"final_lifecycle={row.get('state')}",
        f"entry={row.get('entry_price')}",
        f"stop={row.get('stop_price')}",
        f"target={row.get('target_price')}",
    ]
    ep = row.get("entry_pool") or {}
    if isinstance(ep, dict):
        parts.append(f"entry_pool_id={ep.get('pool_id')}")
        parts.append(f"entry_pool_known_at={ep.get('known_at')}")
    htf = row.get("htf_context") or {}
    if isinstance(htf, dict):
        parts.append(f"target_pool_id={htf.get('target_pool_id')}")
        parts.append(f"target_pool_known_at={htf.get('target_pool_known_at_arm')}")
        parts.append(f"target_selected_at={htf.get('target_selected_at')}")
        if htf.get("same_bar_ambiguity"):
            parts.append(f"same_bar_ambiguity={htf.get('same_bar_events')}")
    return "\n".join(str(p) for p in parts)


def dedupe_plan_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One marker group per signal_id; prefer latest lifecycle state."""
    rank = {
        "CONFIRMED": 5,
        "LIMIT_INTENT_ARMED": 4,
        "CANDIDATE": 3,
        "INVALIDATED": 2,
        "EXPIRED": 1,
    }

    def richness(row: dict[str, Any]) -> int:
        score = 0
        if row.get("entry_pool"):
            score += 2
        if row.get("target_pool"):
            score += 2
        if row.get("htf_context"):
            score += 1
        return score

    best: dict[str, dict[str, Any]] = {}
    for row in rows:
        sid = str(row.get("signal_id") or row.get("setup_id") or "")
        if not sid:
            continue
        prev = best.get(sid)
        if prev is None:
            best[sid] = row
            continue
        pr = rank.get(str(prev.get("state")), 0)
        cr = rank.get(str(row.get("state")), 0)
        if cr > pr or (cr == pr and richness(row) > richness(prev)):
            best[sid] = row
    return list(best.values())


def _plan_lines(setup_id: str, ts: datetime, row: dict[str, Any]) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for label, px, color in (
        ("ENTRY", row.get("entry_price"), "#aaaaaa"),
        ("SL", row.get("stop_price"), "#ff6b6b"),
        ("TP", row.get("target_price"), "#51cf66"),
    ):
        try:
            line_px = float(px)
        except (TypeError, ValueError):
            continue
        lines.append(
            {
                "overlay_id": f"aps-line-{label.lower()}-{setup_id}",
                "kind": "APS_LINE",
                "line_kind": "horizontal",
                "setup_id": setup_id,
                "timestamp": ts,
                "price": line_px,
                "color": color,
                "text": label,
                "signal": row,
            }
        )
    return lines


def _marker_for_row(row: dict[str, Any], *, mode: str) -> list[dict[str, Any]]:
    state = str(row.get("state") or "")
    direction = str(row.get("direction") or "").upper()
    if direction not in {"LONG", "SHORT"}:
        return []
    setup_id = str(row.get("signal_id") or row.get("setup_id") or "aps")
    is_long = direction == "LONG"
    specs: list[dict[str, Any]] = []

    if state in {"LIMIT_INTENT_ARMED", "LIMIT_ARMED"}:
        ts = _parse_ts(row.get("armed_at") or row.get("decision_at"))
        if ts is None:
            return []
        try:
            price = float(row.get("entry_price") or row.get("limit_entry_price"))
        except (TypeError, ValueError):
            return []
        specs.append(
            {
                "overlay_id": f"aps-plan-{setup_id}",
                "kind": "APS_ARMED",
                "setup_id": setup_id,
                "direction": direction,
                "timestamp": ts,
                "price": price,
                "shape": "arrow_up" if is_long else "arrow_down",
                "color": "#f0ad4e",
                "text": "ARMED",
                "position": "below" if is_long else "above",
                "state": state,
                "signal": row,
                "tooltip": _tooltip(row),
            }
        )
        # Plan lines only for live ARMED plans — never for CONFIRMED bulk history.
        if mode == "active":
            specs.extend(_plan_lines(setup_id, ts, row))
        return specs

    if state == "CONFIRMED":
        ts = _parse_ts(row.get("armed_at") or row.get("signal_at") or row.get("confirmation_at"))
        fill_ts = _parse_ts(row.get("hypothetical_filled_at") or row.get("filled_at") or row.get("confirmation_at"))
        if ts is None:
            return []
        try:
            price = float(row.get("entry_price"))
        except (TypeError, ValueError):
            return []
        label = "FILLED" if row.get("setup_type", "").startswith("A_PLUS_PULLBACK") else "A+"
        specs.append(
            {
                "overlay_id": f"aps-plan-{setup_id}",
                "kind": "APS_CONFIRMED",
                "setup_id": setup_id,
                "direction": direction,
                "timestamp": ts,
                "price": price,
                "shape": "arrow_up" if is_long else "arrow_down",
                "color": "#2ca02c" if is_long else "#d62728",
                "text": label,
                "position": "below" if is_long else "above",
                "entry_price": row.get("entry_price"),
                "stop_price": row.get("stop_price"),
                "target_price": row.get("target_price"),
                "filled_at": fill_ts.isoformat() if fill_ts else None,
                "state": state,
                "signal": row,
                "tooltip": _tooltip(row),
            }
        )
        # Never attach ENTRY/TP/SL lines to confirmed — 29×3 labels = price-axis chaos.
        return specs

    if state in {
        "INVALIDATED_UNFILLED",
        "EXPIRED_UNFILLED",
        "INVALIDATED",
        "EXPIRED",
        "NO_TRADE",
        "AMBIGUOUS_INTRABAR",
    }:
        if mode not in {"debug", "all_states", "active"} and state == "AMBIGUOUS_INTRABAR":
            # Always show ambiguity when present in confirmed/active modes
            pass
        elif mode not in {"debug", "all_states"} and state != "AMBIGUOUS_INTRABAR":
            return []
        ts = _parse_ts(
            row.get("invalidated_at")
            or row.get("expired_at")
            or row.get("armed_at")
            or row.get("signal_at")
            or row.get("confirmation_at")
        )
        if ts is None:
            return []
        is_amb = state == "AMBIGUOUS_INTRABAR"
        specs.append(
            {
                "overlay_id": f"aps-plan-{setup_id}",
                "kind": "APS_AMBIGUOUS" if is_amb else "APS_INVALID",
                "setup_id": setup_id,
                "direction": direction,
                "timestamp": ts,
                "price": row.get("entry_price"),
                "shape": "circle",
                "color": "#9e9e9e",
                "text": "⚠ AMB" if is_amb else state[:4],
                "position": "at_price",
                "state": state,
                "signal": row,
                "tooltip": _tooltip(row),
            }
        )
        if is_amb and mode == "active":
            specs.extend(_plan_lines(setup_id, ts, row))
        return specs

    if mode == "debug":
        ts = _parse_ts(row.get("signal_at") or row.get("confirmation_at") or row.get("decision_at"))
        if ts is None:
            return []
        specs.append(
            {
                "overlay_id": f"aps-debug-{setup_id}",
                "kind": "APS_DEBUG",
                "setup_id": setup_id,
                "direction": direction,
                "timestamp": ts,
                "price": row.get("entry_price"),
                "shape": "circle",
                "color": "#9e9e9e",
                "text": state[:4],
                "position": "at_price",
                "state": state,
                "signal": row,
                "tooltip": _tooltip(row),
            }
        )
    return specs


def signals_to_marker_specs(
    rows: list[dict[str, Any]],
    *,
    display_mode: str = "confirmed",
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    """display_mode: off | confirmed | active | all_states | debug"""
    if display_mode == "off":
        return []
    rows = dedupe_plan_rows(list(rows or []))
    prefix = f"aps-{run_id}-" if run_id else "aps-"
    specs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        setup_id = str(row.get("signal_id") or row.get("setup_id") or "")
        for spec in _marker_for_row(row, mode=display_mode):
            oid = str(spec.get("overlay_id") or "")
            if oid.startswith("aps-"):
                suffix = oid[4:]
                spec["overlay_id"] = f"{prefix}{suffix}"
            oid = str(spec.get("overlay_id") or "")
            if oid in seen:
                continue
            seen.add(oid)
            if setup_id:
                seen.add(setup_id)
            specs.append(spec)
    return specs
