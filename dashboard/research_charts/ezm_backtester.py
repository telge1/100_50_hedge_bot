"""EZM two-layer research markers for Research Charts (no trade/PnL)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

BACKTESTER_SOURCE = "ezm_candidate_discovery"
STRATEGY_ID = "ema_zone_microstructure_confirmation_v1"

CONFIRMATION_MODE_EMA_ONLY = "ema_only"
CONFIRMATION_MODE_EMA_PLUS_MICRO = "ema_plus_microstructure"

CONFIRMED_STATES = frozenset(
    {
        "defense_rejection_confirmed",
        "breakout_confirmed",
        "false_breakout_confirmed",
        "possible_regime_flip",
        "full_regime_flip_confirmed",
    }
)

ROLE_COLORS = {
    "resistance": "#ff7f0e",
    "support": "#1f77b4",
    "ambiguous": "#9467bd",
}


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _utc(value)
    text = str(value).strip()
    if not text or text.upper() == "MISSING":
        return None
    try:
        return _utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        return None


def _role_color(role: str) -> str:
    return ROLE_COLORS.get(str(role or "").lower(), "#888888")


def _zone_label(row: dict[str, Any]) -> str:
    zone = str(row.get("zone_name") or row.get("zone") or row.get("zone_key") or "EMA")
    role = str(row.get("zone_role") or row.get("zone_role_at_watch") or "")[:1].upper()
    if role in {"R", "S"}:
        return f"{zone}-{role}"
    return zone


def setup_rows_to_marker_specs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """EMA-only setup markers — never LONG/SHORT arrows.

    Chart markers are limited to exact_touch (+ flat blocks). Proximity-watch rows
    remain in candidates.json but are not rendered as DOM overlays — 30d runs can
    produce thousands of watches and freeze the TRP chart via layoutOverlays().
    """
    best_by_id: dict[str, dict[str, Any]] = {}
    for row in rows or []:
        if row.get("emit_setup_marker") is False:
            continue
        mode = str(row.get("confirmation_mode") or CONFIRMATION_MODE_EMA_ONLY)
        if mode != CONFIRMATION_MODE_EMA_ONLY and str(row.get("output_layer") or "") != "ema_setup":
            continue
        event = str(row.get("zone_event") or "setup")
        # Skip proximity_watch on chart (keep exact_touch / flat).
        if event == "proximity_watch":
            continue
        ts = _parse_ts(
            row.get("marker_at")
            or row.get("zone_touch_at")
            or row.get("touch_at")
            or row.get("zone_watch_started_at")
        )
        if ts is None:
            continue
        try:
            price = float(
                row.get("marker_price")
                if row.get("marker_price") is not None
                else row.get("mid")
                if row.get("mid") is not None
                else row.get("decision_price")
            )
        except (TypeError, ValueError):
            continue
        role = str(row.get("zone_role") or row.get("zone_role_at_watch") or "")
        setup_id = str(row.get("setup_id") or row.get("episode_id") or f"setup-{ts.isoformat()}")
        if str(row.get("candidate_state") or row.get("ema_setup_state") or "") == "block_flat_compression":
            text = "FLAT"
            color = "#7f7f7f"
        else:
            text = "TOUCH"
            color = _role_color(role)
        spec = {
            "overlay_id": f"ezm-setup-{setup_id}",
            "kind": "EZM_SETUP",
            "layer": "ema_setup",
            "confirmation_mode": CONFIRMATION_MODE_EMA_ONLY,
            "setup_id": setup_id,
            "candidate_id": setup_id,
            "episode_id": row.get("episode_id"),
            "direction": "NONE",
            "candidate_state": row.get("candidate_state") or row.get("ema_setup_state") or event,
            "timestamp": ts,
            "price": price,
            "shape": "diamond",
            "color": color,
            "text": f"{_zone_label(row)}·{text}",
            "position": "at_price",
            # Thin meta only — never embed full event row (multi-MB overlay payloads).
            "candidate": {
                "zone_event": event,
                "zone_name": row.get("zone_name") or row.get("zone"),
                "marker_at": row.get("marker_at"),
                "marker_price": price,
            },
        }
        prev = best_by_id.get(setup_id)
        if prev is None or event == "exact_touch":
            best_by_id[setup_id] = spec
    return list(best_by_id.values())


def micro_rows_to_marker_specs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Microstructure confirmation markers — may show direction even when marker blocked."""
    out: list[dict[str, Any]] = []
    for row in rows or []:
        mode = str(row.get("confirmation_mode") or "")
        if mode and mode != CONFIRMATION_MODE_EMA_PLUS_MICRO:
            if str(row.get("output_layer") or "") != "microstructure_confirmation":
                continue
        ts = _parse_ts(row.get("decision_at") or row.get("entry_time") or row.get("candle_close_time"))
        if ts is None:
            continue
        try:
            price = float(
                row.get("decision_price")
                if row.get("decision_price") is not None
                else row.get("entry_price")
            )
        except (TypeError, ValueError):
            continue

        reaction = str(row.get("reaction_state") or row.get("candidate_state") or "")
        direction = str(row.get("candidate_direction") or row.get("direction") or "").upper()
        setup_id = str(row.get("setup_id") or row.get("episode_id") or f"micro-{ts.isoformat()}")
        emit = row.get("emit_directional_marker") is not False and direction in {"LONG", "SHORT"}

        if emit and reaction in CONFIRMED_STATES and direction in {"LONG", "SHORT"}:
            is_long = direction == "LONG"
            out.append(
                {
                    "overlay_id": f"ezm-micro-{setup_id}",
                    "kind": "EZM_CONFIRMED",
                    "layer": "microstructure_confirmation",
                    "confirmation_mode": CONFIRMATION_MODE_EMA_PLUS_MICRO,
                    "setup_id": setup_id,
                    "candidate_id": setup_id,
                    "episode_id": row.get("episode_id"),
                    "direction": direction,
                    "candidate_state": reaction,
                    "reaction_state": reaction,
                    "timestamp": ts,
                    "price": price,
                    "shape": "arrow_up" if is_long else "arrow_down",
                    "color": "#2ca02c" if is_long else "#d62728",
                    "text": f"{'L' if is_long else 'S'}-EZM",
                    "position": "below" if is_long else "above",
                    "candidate": row,
                }
            )
            continue

        if reaction in CONFIRMED_STATES and direction in {"LONG", "SHORT"}:
            is_long = direction == "LONG"
            reason = str(row.get("clearance_status") or row.get("clearance_reason") or "blocked")
            out.append(
                {
                    "overlay_id": f"ezm-blocked-{setup_id}",
                    "kind": "EZM_MICRO_BLOCKED",
                    "layer": "microstructure_confirmation",
                    "confirmation_mode": CONFIRMATION_MODE_EMA_PLUS_MICRO,
                    "setup_id": setup_id,
                    "candidate_id": setup_id,
                    "episode_id": row.get("episode_id"),
                    "direction": direction,
                    "candidate_state": reaction,
                    "reaction_state": reaction,
                    "timestamp": ts,
                    "price": price,
                    "shape": "arrow_up" if is_long else "arrow_down",
                    "color": "#bcbd22",
                    "text": f"{'L' if is_long else 'S'}·{reason[:8]}",
                    "position": "below" if is_long else "above",
                    "candidate": row,
                }
            )
            continue

        if reaction in {
            "wait_microstructure_confirmation",
            "no_trade",
            "data_incomplete",
            "undetermined",
        } or str(row.get("candidate_state") or "") in {
            "wait_microstructure_confirmation",
            "no_trade",
            "data_incomplete",
        }:
            out.append(
                {
                    "overlay_id": f"ezm-wait-{setup_id}",
                    "kind": "EZM_MICRO_WAIT",
                    "layer": "microstructure_confirmation",
                    "confirmation_mode": CONFIRMATION_MODE_EMA_PLUS_MICRO,
                    "setup_id": setup_id,
                    "candidate_id": setup_id,
                    "episode_id": row.get("episode_id"),
                    "direction": "NONE",
                    "candidate_state": reaction or row.get("candidate_state"),
                    "reaction_state": reaction,
                    "timestamp": ts,
                    "price": price,
                    "shape": "circle",
                    "color": "#7f7f7f",
                    "text": "WAIT",
                    "position": "at_price",
                    "candidate": row,
                }
            )
    return out


def research_layers_to_marker_specs(
    *,
    ema_setup_rows: list[dict[str, Any]] | None = None,
    micro_rows: list[dict[str, Any]] | None = None,
    show_ema_setup: bool = True,
    show_microstructure: bool = True,
) -> list[dict[str, Any]]:
    """Combine EMA-setup + microstructure markers for overlay charts."""
    specs: list[dict[str, Any]] = []
    if show_ema_setup:
        specs.extend(setup_rows_to_marker_specs(list(ema_setup_rows or [])))
    if show_microstructure:
        specs.extend(micro_rows_to_marker_specs(list(micro_rows or [])))
    return specs


def signal_rows_to_marker_specs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Legacy tier-A confirmed signals → micro confirmed markers only."""
    return micro_rows_to_marker_specs(rows)


def build_overlay_markers(marker_specs: list[dict[str, Any]], *, symbol: str) -> list[Any]:
    from .trp_import import load_trp

    trp = load_trp()
    OverlayMarker = trp["OverlayMarker"]
    OverlayStyle = trp["OverlayStyle"]
    ensure_utc = trp["ensure_utc"]
    z_by_kind = {
        "EZM_SETUP": 41,
        "EZM_CONFIRMED": 43,
        "EZM_MICRO_BLOCKED": 42,
        "EZM_MICRO_WAIT": 40,
        "EZM": 43,
    }
    out = []
    for spec in marker_specs:
        kind = str(spec.get("kind") or "EZM")
        meta = {
            "origin": BACKTESTER_SOURCE,
            "strategy_id": STRATEGY_ID,
            "run_intent": "candidate_discovery",
            "kind": kind,
            "layer": spec.get("layer"),
            "confirmation_mode": spec.get("confirmation_mode"),
            "setup_id": spec.get("setup_id"),
            "candidate_id": spec.get("candidate_id"),
            "episode_id": spec.get("episode_id"),
            "direction": spec.get("direction"),
            "candidate_state": spec.get("candidate_state"),
            "reaction_state": spec.get("reaction_state"),
            "research_note": "Research Candidate – kein ausgeführter Trade",
            # Keep chart metadata thin (full event rows freeze the research chart).
            "candidate": spec.get("candidate") if isinstance(spec.get("candidate"), dict) else {},
        }
        out.append(
            OverlayMarker(
                overlay_id=str(spec["overlay_id"]),
                symbol=str(symbol).upper(),
                timestamp=ensure_utc(spec["timestamp"]),
                price=spec.get("price"),
                position=spec.get("position") or "at_price",
                shape=spec.get("shape") or "circle",
                text=str(spec.get("text") or ""),
                size=10.0 if kind == "EZM_SETUP" else 9.0,
                style=OverlayStyle(color=spec.get("color") or "#888888", width=1.0),
                timeframe_scope="all",
                visible=True,
                z_order=z_by_kind.get(kind, 42),
                metadata=meta,
            )
        )
    return out
