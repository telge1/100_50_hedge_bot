"""Reconstruct per-close state and V1/V3 diffs from isolated replay outputs."""

from __future__ import annotations

from typing import Any


def _gate_by_bar(gate_decisions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {r["decision_close_utc"]: r for r in gate_decisions}


def _pool_candidates_by_bar(baseline_pool_candidates: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {r["decision_close_utc"]: r for r in baseline_pool_candidates}


def _position_events(modeled_positions: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for ev in modeled_positions:
        bar = ev.get("decision_close_utc")
        if bar:
            out.setdefault(bar, []).append(ev)
    return out


def _lifecycle_by_bar(pending_lifecycle: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for ev in pending_lifecycle:
        bar = ev.get("decision_close_utc")
        if bar:
            out.setdefault(bar, []).append(ev)
    return out


def rebuild_bar_states(
    *,
    bars: list[str],
    replay: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """End-of-bar pending/position flags after processing each close."""
    pos_open = False
    pending_id: str | None = None
    pending_pool: str | None = None
    pos_by_bar = _position_events(replay.get("modeled_positions") or [])
    life_by_bar = _lifecycle_by_bar(replay.get("pending_lifecycle") or [])
    states: dict[str, dict[str, Any]] = {}

    for bar in bars:
        for ev in pos_by_bar.get(bar, []):
            if ev.get("event") == "SIMULATED_FILL":
                pos_open = True
                pending_id = None
                pending_pool = None
            elif ev.get("event") == "SIMULATED_SL":
                pos_open = False

        for ev in life_by_bar.get(bar, []):
            et = ev.get("event")
            if et == "CREATE":
                pending_id = str(ev.get("order_id"))
                pending_pool = str(ev.get("pool_key") or "")
                pos_open = False
            elif et in ("CANCEL", "FILLED"):
                pending_id = None
                pending_pool = None

        states[bar] = {
            "position_open": pos_open,
            "pending_open": pending_id is not None,
            "pending_order_id": pending_id,
            "pending_pool_key": pending_pool,
        }
    return states


def build_close_comparison_rows(
    *,
    pilot_id: str,
    pause_mode: str,
    bars: list[str],
    replay_v1: dict[str, Any],
    replay_v3: dict[str, Any],
    ema_rows_v1: dict[str, dict[str, Any]],
    ema_rows_v3: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    g1 = _gate_by_bar(replay_v1.get("gate_decisions") or [])
    g3 = _gate_by_bar(replay_v3.get("gate_decisions") or [])
    pools = _pool_candidates_by_bar(replay_v1.get("baseline_pool_candidates") or [])
    s1 = rebuild_bar_states(bars=bars, replay=replay_v1)
    s3 = rebuild_bar_states(bars=bars, replay=replay_v3)

    rows: list[dict[str, Any]] = []
    for bar in bars:
        p = pools.get(bar)
        r1 = g1.get(bar, {})
        r3 = g3.get(bar, {})
        e1 = ema_rows_v1.get(bar, {})
        e3 = ema_rows_v3.get(bar, {})
        st1 = s1.get(bar, {})
        st3 = s3.get(bar, {})
        v1_allow = bool(r1.get("allow_new_short_intent"))
        v3_allow = bool(r3.get("allow_new_short_intent"))
        rows.append(
            {
                "pilot_id": pilot_id,
                "pause_mode": pause_mode,
                "decision_close_utc": bar,
                "valid_pool_candidate": p is not None,
                "pool_key": p.get("pool_key") if p else "",
                "ema_v1_decision": e1.get("ema_structure_decision") or r1.get("ema_decision"),
                "ema_v1_reason": e1.get("decision_reason_code") or r1.get("ema_reason_code"),
                "ema_v3_decision": e3.get("ema_structure_decision") or r3.get("ema_decision"),
                "ema_v3_reason": e3.get("decision_reason_code") or r3.get("ema_reason_code"),
                "ema_only_differs": (e1.get("ema_structure_decision") != e3.get("ema_structure_decision")),
                "v1_would_authorize_pending": v1_allow,
                "v3_would_authorize_pending": v3_allow,
                "pool_conditional_differs": v1_allow != v3_allow,
                "v1_pending_open_end": st1.get("pending_open"),
                "v3_pending_open_end": st3.get("pending_open"),
                "v1_position_open_end": st1.get("position_open"),
                "v3_position_open_end": st3.get("position_open"),
                "pause_state_v1": r1.get("pause_state"),
                "pause_state_v3": r3.get("pause_state"),
                "v1_block_reasons": "|".join(r1.get("reasons") or []),
                "v3_block_reasons": "|".join(r3.get("reasons") or []),
            }
        )
    return rows


def pending_difference_rows(close_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in close_rows:
        if r.get("pause_mode") != "RESEARCH_READY":
            continue
        if not (
            r.get("pool_conditional_differs")
            or r.get("v1_pending_open_end") != r.get("v3_pending_open_end")
            or r.get("ema_only_differs")
        ):
            continue
        out.append(r)
    return out


def fill_exit_rows(replay: dict[str, Any], *, classifier: str, pilot_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ev in replay.get("modeled_positions") or []:
        rows.append(
            {
                "pilot_id": pilot_id,
                "classifier": classifier,
                "event": ev.get("event"),
                "decision_close_utc": ev.get("decision_close_utc"),
                "position_id": ev.get("position_id"),
                "order_id": ev.get("order_id"),
                "entry_price": ev.get("entry_price"),
                "exit_price": ev.get("exit_price"),
                "sl_price": ev.get("sl_price"),
                "tp_price": ev.get("tp_price"),
                "signal_close_utc": ev.get("signal_close_utc"),
                "pool_key": ev.get("pool_key"),
            }
        )
    return rows
