"""Target pool causality audit helpers."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .models import _utc_naive
from .pools import pool_known_at_or_before, pool_valid_at


def target_causality_row(signal: dict[str, Any]) -> dict[str, Any]:
    ep = signal.get("entry_pool") or {}
    tp = signal.get("target_pool") or {}
    htf = signal.get("htf_context") or {}
    armed_raw = signal.get("armed_at") or signal.get("decision_at")
    armed_at = _utc_naive(armed_raw) if armed_raw else None
    target_known_raw = htf.get("target_pool_known_at_arm") or tp.get("known_at")
    target_known_at = _utc_naive(target_known_raw) if target_known_raw else None
    target_inv_raw = tp.get("invalidated_at")
    target_inv_at = _utc_naive(target_inv_raw) if target_inv_raw else None
    max_feat_raw = signal.get("max_feature_timestamp")
    max_feat = _utc_naive(max_feat_raw) if max_feat_raw else armed_at

    causality_pass = False
    if armed_at and target_known_at:
        known_ok = target_known_at <= armed_at
        feat_ok = max_feat is None or max_feat <= armed_at
        valid_ok = target_inv_at is None or target_inv_at > armed_at
        causality_pass = known_ok and feat_ok and valid_ok

    edges = htf.get("target_pool_edges_at_arm") or {}
    return {
        "signal_id": signal.get("signal_id") or signal.get("setup_id"),
        "episode_id": signal.get("episode_id"),
        "setup_type": signal.get("setup_type"),
        "direction": signal.get("direction"),
        "state": signal.get("state"),
        "armed_at": signal.get("armed_at"),
        "signal_at": signal.get("signal_at"),
        "entry_price": signal.get("entry_price"),
        "stop_loss": signal.get("stop_price"),
        "take_profit": signal.get("target_price"),
        "entry_pool_id": ep.get("pool_id"),
        "entry_pool_known_at": ep.get("known_at"),
        "target_pool_id": htf.get("target_pool_id") or tp.get("pool_id"),
        "target_pool_timeframe": htf.get("target_pool_timeframe") or tp.get("timeframe"),
        "target_pool_side": htf.get("target_pool_side") or tp.get("side"),
        "target_pool_lower_edge": edges.get("lower", tp.get("lower_edge")),
        "target_pool_upper_edge": edges.get("upper", tp.get("upper_edge")),
        "target_pool_known_at": target_known_raw or tp.get("known_at"),
        "target_pool_invalidated_at": target_inv_raw,
        "max_feature_timestamp": signal.get("max_feature_timestamp"),
        "target_selected_at": htf.get("target_selected_at"),
        "tp_policy": htf.get("tp_policy"),
        "causality_pass": causality_pass,
        "marker_overlay_id": f"aps-plan-{signal.get('signal_id') or signal.get('setup_id')}",
    }


def signal_pool_timeline_row(signal: dict[str, Any]) -> dict[str, Any]:
    base = target_causality_row(signal)
    armed = base.get("armed_at")
    target_known = base.get("target_pool_known_at")
    base.update(
        {
            "hypothetical_filled_at": signal.get("hypothetical_filled_at") or signal.get("filled_at"),
            "confirmed_at": signal.get("confirmed_at"),
            "target_rectangle_start": target_known,
            "entry_rectangle_start": base.get("entry_pool_known_at"),
        }
    )
    return base


def audit_signals(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [target_causality_row(s) for s in signals]
