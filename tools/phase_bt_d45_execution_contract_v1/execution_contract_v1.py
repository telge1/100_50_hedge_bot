"""Deterministic short execution contract research (BT-D45)."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

ROOT = Path(__file__).resolve().parents[2]
BT_D7 = ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1"
BT_D42 = ROOT / "tools/phase_bt_d42_fill_cancel_chronology_v1"
for p in (str(BT_D7), str(BT_D42)):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from execution_v1 import simulate_short_exit  # noqa: E402
from fill_chronology_v1 import _parse_utc, activation_contract_note  # noqa: E402
from sell_limit_fill_v1 import evaluate_short_sell_limit_fill  # noqa: E402

CONTRACT = "bt_d45_execution_contract_v1"

MODEL_1 = "model_1_touch_hypothetical_active_v1"
MODEL_2 = "model_2_conservative_open_cross_v1"

ExecutionModel = Literal["model_1_touch_hypothetical_active_v1", "model_2_conservative_open_cross_v1"]


def bar_window_keys(
    prices: dict[str, dict[str, float]],
    active_from: str,
    cancel_close: str | None,
    pilot_end: str,
) -> list[str]:
    end = cancel_close if cancel_close else pilot_end
    return sorted(t for t in prices if active_from <= t <= end)


def classify_bar_evidence(
    *,
    bar_utc: str,
    active_from: str,
    cancel_close: str | None,
    entry: float,
    ohlc: dict[str, float],
) -> dict[str, Any]:
    if bar_utc < active_from:
        return {"evidence_class": "BEFORE_ACTIVATION", "active": False}
    dec = evaluate_short_sell_limit_fill(
        fill_model="causal_sell_limit_v2",
        limit_price=entry,
        bar_utc=bar_utc,
        order_active_from_utc=active_from,
        ohlc=ohlc,
    )
    h, o = float(ohlc["h"]), float(ohlc["o"])
    lim = float(entry)
    if cancel_close and bar_utc == cancel_close and h >= lim:
        amb = "same_interval_ambiguity_with_cancel_close"
    else:
        amb = ""
    if dec.rule_id == "CAUSAL_V2_HIGH_BELOW_LIMIT":
        cls = "no_touch"
    elif dec.rule_id == "CAUSAL_V2_MARKETABLE_AT_OPEN":
        cls = "gap_through_order_level_at_open"
    elif dec.rule_id == "CAUSAL_V2_LIMIT_TOUCH_INTRABAR":
        cls = "touch_while_active"
    else:
        cls = "no_touch"
    return {
        "evidence_class": cls,
        "active": True,
        "causal_v2_rule_id": dec.rule_id,
        "causal_v2_filled": dec.filled,
        "same_interval_ambiguity": amb,
        "ohlc": {"o": o, "h": h, "l": float(ohlc["l"]), "c": float(ohlc["c"])},
    }


def model_entry_decision(
    model: ExecutionModel,
    *,
    bar_utc: str,
    active_from: str,
    entry: float,
    ohlc: dict[str, float],
) -> tuple[bool, float | None, str, str]:
    """filled, price, rule_id, resolution_note"""
    dec = evaluate_short_sell_limit_fill(
        fill_model="causal_sell_limit_v2",
        limit_price=entry,
        bar_utc=bar_utc,
        order_active_from_utc=active_from,
        ohlc=ohlc,
    )
    if model == MODEL_1:
        if dec.filled and dec.fill_price is not None:
            return True, float(dec.fill_price), dec.rule_id, "model_1_accepts_causal_v2"
        return False, None, dec.rule_id, dec.note
    # Model 2: only open-cross (marketable at open)
    if dec.rule_id == "CAUSAL_V2_MARKETABLE_AT_OPEN" and dec.fill_price is not None:
        return True, float(dec.fill_price), dec.rule_id, "model_2_open_cross_only"
    if dec.rule_id == "CAUSAL_V2_LIMIT_TOUCH_INTRABAR":
        return False, None, dec.rule_id, "UNRESOLVED_intrabar_touch_insufficient_for_conservative"
    return False, None, dec.rule_id, dec.note


def first_modeled_fill(
    tl: dict[str, Any],
    prices: dict[str, dict[str, float]],
    model: ExecutionModel,
    pilot_end: str,
) -> tuple[str | None, float | None, str, str | None]:  # unresolved_reason only when ambiguous
    active_from = tl["pending_active_from_utc"]
    cancel = tl.get("cancel_effective_decision_close_utc") or None
    entry = float(tl["planned_entry"])
    # Fills must occur on bars strictly before cancel effective close when cancel exists
    last_bar = cancel
    if cancel:
        # cancel effective at decision close — no fill on cancel bar if same as D42 EXECUTION_UNRESOLVED
        bars = [b for b in bar_window_keys(prices, active_from, cancel, pilot_end) if b < cancel]
    else:
        bars = bar_window_keys(prices, active_from, None, pilot_end)
    saw_intrabar_only_touch = False
    for bar in bars:
        ohlc = prices.get(bar)
        if not ohlc:
            continue
        filled, px, rule, note = model_entry_decision(
            model, bar_utc=bar, active_from=active_from, entry=entry, ohlc=ohlc
        )
        if filled and px is not None:
            return bar, px, rule, None
        if model == MODEL_2 and note.startswith("UNRESOLVED"):
            saw_intrabar_only_touch = True
    if cancel and tl.get("chronology_status") == "EXECUTION_UNRESOLVED":
        return None, None, "", "same_interval_ambiguity"
    if model == MODEL_2 and saw_intrabar_only_touch:
        return None, None, "CAUSAL_V2_LIMIT_TOUCH_INTRABAR", "UNRESOLVED_intrabar_only_no_open_cross"
    return None, None, "", None


@dataclass
class ModelRunStats:
    model: ExecutionModel
    pending_created: int = 0
    cancelled: int = 0
    replaced: int = 0
    modeled_entries: int = 0
    closed_positions: int = 0
    open_positions: int = 0
    unresolved_entry: int = 0
    unresolved_exit: int = 0
    blocked_scanner_emits: int = 0
    positions: list[dict[str, Any]] = field(default_factory=list)
    ambiguities: list[dict[str, Any]] = field(default_factory=list)


def run_single_position_contract(
    timelines: list[dict[str, Any]],
    *,
    model: ExecutionModel,
    config: dict[str, Any],
    prices: dict[str, dict[str, float]],
    pilot_end: str,
) -> ModelRunStats:
    stats = ModelRunStats(model=model)
    position_open = False
    sorted_tl = sorted(timelines, key=lambda x: x["decision_close_utc"])

    for tl in sorted_tl:
        stats.pending_created += 1
        end_type = tl.get("end_event_type") or ""
        if end_type == "CANCEL":
            stats.cancelled += 1
        elif end_type == "REPLACE":
            stats.replaced += 1

        if position_open:
            stats.blocked_scanner_emits += 1
            continue

        fill_bar, fill_px, rule, unresolved = first_modeled_fill(tl, prices, model, pilot_end)
        if unresolved:
            stats.unresolved_entry += 1
            stats.ambiguities.append(
                {
                    "model": model,
                    "order_id": tl["order_id"],
                    "reason": unresolved,
                    "decision_close_utc": tl["decision_close_utc"],
                    "phase": "entry",
                }
            )
            continue
        if fill_bar is None or fill_px is None:
            continue

        stats.modeled_entries += 1
        position_open = True
        sl = float(tl["planned_sl"])
        tp = float(tl["planned_tp"]) if tl.get("planned_tp") else None
        if tp is None:
            stats.unresolved_exit += 1
            position_open = False
            continue

        ex_t, ex_p, ex_reason, amb, bars_held = simulate_short_exit(
            config,
            entry_utc=fill_bar,
            entry_price=fill_px,
            sl_price=sl,
            tp_price=tp,
            prices=prices,
            end_utc=pilot_end,
        )
        pos = {
            "model": model,
            "order_id": tl["order_id"],
            "signal_close_utc": tl["decision_close_utc"],
            "pending_active_from_utc": tl["pending_active_from_utc"],
            "entry_bar": fill_bar,
            "entry_price": fill_px,
            "entry_rule_id": rule,
            "planned_sl": sl,
            "planned_tp": tp,
            "exit_bar": ex_t,
            "exit_price": ex_p,
            "exit_reason": ex_reason,
            "exit_ambiguous": amb,
            "bars_held": bars_held,
        }
        stats.positions.append(pos)

        if ex_reason == "OPEN_AT_END":
            stats.open_positions += 1
        elif amb or ex_p is None:
            stats.unresolved_exit += 1
            stats.ambiguities.append(
                {
                    "model": model,
                    "order_id": tl["order_id"],
                    "reason": ex_reason,
                    "phase": "exit",
                }
            )
        else:
            stats.closed_positions += 1
        position_open = ex_reason == "OPEN_AT_END"

    return stats


def build_execution_evidence_rows(
    timelines: list[dict[str, Any]],
    prices: dict[str, dict[str, float]],
    pilot_end: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for tl in timelines:
        oid = tl["order_id"]
        active_from = tl["pending_active_from_utc"]
        cancel = tl.get("cancel_effective_decision_close_utc") or None
        entry = float(tl["planned_entry"])
        for bar in bar_window_keys(prices, active_from, cancel or pilot_end, pilot_end):
            ohlc = prices.get(bar)
            if not ohlc:
                continue
            ev = classify_bar_evidence(
                bar_utc=bar,
                active_from=active_from,
                cancel_close=cancel,
                entry=entry,
                ohlc=ohlc,
            )
            chron = ""
            if cancel:
                if bar < cancel and ev["evidence_class"] == "touch_while_active":
                    chron = "touch_before_cancel"
                elif bar == cancel:
                    chron = "cancel_effective_close"
            rows.append(
                {
                    "order_id": oid,
                    "signal_close_utc": tl["decision_close_utc"],
                    "bar_utc": bar,
                    "pending_active_from_utc": active_from,
                    "cancel_effective_close_utc": cancel,
                    "planned_entry": entry,
                    **ev,
                    "chronology_note": chron,
                }
            )
    return rows


def comparison_row(m1: ModelRunStats, m2: ModelRunStats) -> list[dict[str, Any]]:
    def pack(s: ModelRunStats) -> dict[str, Any]:
        return {
            "execution_model": s.model,
            "pending_orders_created": s.pending_created,
            "cancelled_pending": s.cancelled,
            "replaced_pending": s.replaced,
            "modeled_entries": s.modeled_entries,
            "modeled_closed_positions": s.closed_positions,
            "still_open_positions": s.open_positions,
            "unresolved_entry_cases": s.unresolved_entry,
            "unresolved_exit_cases": s.unresolved_exit,
            "blocked_scanner_emits_while_position_open": s.blocked_scanner_emits,
        }

    rows = [pack(m1), pack(m2)]
    rows.append(
        {
            "execution_model": "delta_model_2_minus_model_1",
            "pending_orders_created": 0,
            "cancelled_pending": 0,
            "replaced_pending": 0,
            "modeled_entries": m2.modeled_entries - m1.modeled_entries,
            "modeled_closed_positions": m2.closed_positions - m1.closed_positions,
            "still_open_positions": m2.open_positions - m1.open_positions,
            "unresolved_entry_cases": m2.unresolved_entry - m1.unresolved_entry,
            "unresolved_exit_cases": m2.unresolved_exit - m1.unresolved_exit,
            "blocked_scanner_emits_while_position_open": m2.blocked_scanner_emits - m1.blocked_scanner_emits,
            "explanation": (
                "Model 2 counts only CAUSAL_V2_MARKETABLE_AT_OPEN entries; "
                "Model 1 also accepts CAUSAL_V2_LIMIT_TOUCH_INTRABAR. "
                "Fewer entries on M2; blocking counts differ when entry timing shifts."
            ),
        }
    )
    return rows
