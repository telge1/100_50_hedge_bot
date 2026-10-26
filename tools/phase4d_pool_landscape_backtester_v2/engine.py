"""Strict pool-landscape backtest engine V2."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from entry_order_simulator import simulate_entry
from pool_zone_tracker import LifecycleIndex
from exit_simulator import ExitResult, simulate_exit
from signal_provider import RawSignal
from strategy_rules import plan_long, plan_short


@dataclass
class OpenPosition:
    trade_id: str
    candidate_id: str
    direction: str
    signal_close_utc: str
    fill_utc: str
    entry_price: float
    sl_price: float
    tp_price: float
    exit_utc: str | None = None
    exit_reason: str = ""


def run_backtest(
    cfg: dict[str, Any],
    signals: list[RawSignal],
    prices: dict[str, dict],
    lifecycle: LifecycleIndex | None = None,
) -> dict[str, list[dict]]:
    if lifecycle is None:
        lifecycle = LifecycleIndex.load()
    raw_rows: list[dict] = []
    intents: list[dict] = []
    orders: list[dict] = []
    trades: list[dict] = []
    suppressed: list[dict] = []
    unresolved: list[dict] = []
    exit_trace: list[dict] = []
    parity_notes: list[dict] = []

    quality_gate = bool(cfg.get("tp_pool_quality_gate_enabled"))
    fees = float(cfg["roundtrip_fees_pct"])
    active: OpenPosition | None = None
    active_exit_utc: str | None = None
    trade_seq = 0

    def clear_position_if_closed(sig: RawSignal) -> None:
        nonlocal active, active_exit_utc
        if active and active_exit_utc and sig.order_active_from_utc >= active_exit_utc:
            active = None
            active_exit_utc = None

    sorted_sigs = sorted(
        signals, key=lambda s: (s.order_active_from_utc, s.signal_close_utc, s.candidate_id)
    )

    for sig in sorted_sigs:
        clear_position_if_closed(sig)
        ms_state = sig.market_state_experimental
        ms_class = "EXPERIMENTAL_DETECTOR_STATE" if ms_state in (
            "WATCH_REVERSAL",
            "LONG_CANDIDATE",
        ) else "MANUAL_MARKET_STRUCTURE_REFERENCE"

        raw_rows.append(
            {
                "candidate_id": sig.candidate_id,
                "direction": sig.direction,
                "signal_close_utc": sig.signal_close_utc,
                "order_active_from_utc": sig.order_active_from_utc,
                "signal_source": sig.signal_source,
                "entry_pool_lower": sig.pool_lower,
                "entry_pool_upper": sig.pool_upper,
                "expected_short_entry_level": sig.pool_lower if sig.direction == "SHORT" else "",
                "expected_long_entry_level": sig.pool_upper if sig.direction == "LONG" else "",
                "market_state_experimental": ms_state,
                "market_structure_class": ms_class,
                "lifecycle": "SIGNAL_CONFIRMED",
                "strategy_version": cfg["strategy_version"],
            }
        )

        if active and (active_exit_utc is None or active_exit_utc == "" or sig.order_active_from_utc < active_exit_utc):
            suppressed.append(
                {
                    "candidate_id": sig.candidate_id,
                    "signal_close_utc": sig.signal_close_utc,
                    "reason": "SUPPRESSED_POSITION_OPEN",
                    "open_trade_id": active.trade_id,
                }
            )
            continue

        if sig.direction == "SHORT":
            pre_plan = plan_short(
                sig.pool_lower,
                sig.pool_upper,
                sig.landscape_intervals,
                sig.pool_lower,
                cfg,
                quality_gate=False,
            )
            entry_level = pre_plan.entry_level
            sl_preview = pre_plan.sl_price
        else:
            pre_plan = plan_long(
                sig.pool_lower,
                sig.pool_upper,
                sig.upper_tp_rows,
                sig.pool_upper,
                cfg,
                quality_gate=False,
            )
            entry_level = pre_plan.entry_level
            sl_preview = pre_plan.sl_price

        intents.append(
            {
                "candidate_id": sig.candidate_id,
                "direction": sig.direction,
                "signal_close_utc": sig.signal_close_utc,
                "order_active_from_utc": sig.order_active_from_utc,
                "limit_price": entry_level,
                "sl_preview": sl_preview,
                "lifecycle": "ORDER_ELIGIBLE",
                "entry_rule_ids": "|".join(pre_plan.rule_ids[:1]),
            }
        )

        fill = simulate_entry(
            cfg,
            sig.direction,
            entry_level,
            sig.order_active_from_utc,
            sig.signal_close_utc,
            prices,
            pool_lower=sig.pool_lower,
            pool_upper=sig.pool_upper,
            pool_identity=sig.pool_identity,
            lifecycle=lifecycle,
        )

        orders.append(
            {
                "candidate_id": sig.candidate_id,
                "direction": sig.direction,
                "limit_price": entry_level,
                "order_status": fill.status,
                "fill_utc": fill.fill_utc or "",
                "fill_price": fill.fill_price if fill.fill_price is not None else "",
                "execution_rule_id": fill.rule_id,
                "execution_note": fill.note,
                "diagnostic_only": cfg.get("entry_execution_variant") != "USER_PENDING_LIMIT_V1",
                "cancel_utc": "",
            }
        )

        if fill.status != "ORDER_FILLED" or fill.fill_price is None:
            if "CANCELLED" in fill.status:
                orders[-1]["cancel_utc"] = orders[-1].get("fill_utc") or sig.order_active_from_utc
            unresolved.append(
                {
                    "candidate_id": sig.candidate_id,
                    "signal_close_utc": sig.signal_close_utc,
                    "reason": fill.status if fill.status != "ORDER_FILLED" else "ENTRY_EXECUTION_UNRESOLVED",
                    "detail": fill.note,
                    "rule_id": fill.rule_id,
                }
            )
            continue

        if sig.direction == "SHORT":
            plan = plan_short(
                sig.pool_lower,
                sig.pool_upper,
                sig.landscape_intervals,
                fill.fill_price,
                cfg,
                quality_gate=quality_gate,
            )
        else:
            plan = plan_long(
                sig.pool_lower,
                sig.pool_upper,
                sig.upper_tp_rows,
                fill.fill_price,
                cfg,
                quality_gate=quality_gate,
            )

        if plan.unresolved or plan.tp_price is None:
            unresolved.append(
                {
                    "candidate_id": sig.candidate_id,
                    "signal_close_utc": sig.signal_close_utc,
                    "reason": plan.unresolved or "TP_UNRESOLVED",
                    "fill_price": fill.fill_price,
                }
            )
            continue

        trade_seq += 1
        tid = f"T4D_{trade_seq:03d}"
        ex: ExitResult = simulate_exit(
            sig.direction,
            fill.fill_utc,
            fill.fill_price,
            plan.sl_price,
            plan.tp_price,
            prices,
            cfg["pilot_to_utc"],
            fees,
        )
        for tr in ex.trace:
            exit_trace.append({**tr, "trade_id": tid, "candidate_id": sig.candidate_id})

        if ex.exit_reason in ("AMBIGUOUS_INTRABAR_EXIT", "AMBIGUOUS_ENTRY_EXIT_SEQUENCE"):
            unresolved.append(
                {
                    "candidate_id": sig.candidate_id,
                    "signal_close_utc": sig.signal_close_utc,
                    "reason": ex.exit_reason,
                    "trade_id": tid,
                }
            )

        validated = (
            cfg.get("entry_execution_user_approved")
            and not quality_gate
            and ex.exit_reason not in ("AMBIGUOUS_INTRABAR_EXIT", "AMBIGUOUS_ENTRY_EXIT_SEQUENCE")
        )

        pos = OpenPosition(
            trade_id=tid,
            candidate_id=sig.candidate_id,
            direction=sig.direction,
            signal_close_utc=sig.signal_close_utc,
            fill_utc=fill.fill_utc,
            entry_price=fill.fill_price,
            sl_price=plan.sl_price,
            tp_price=plan.tp_price,
            exit_utc=ex.exit_utc,
            exit_reason=ex.exit_reason,
        )
        active = pos
        active_exit_utc = ex.exit_utc if ex.exit_utc else ""

        trades.append(
            {
                "trade_id": tid,
                "candidate_id": sig.candidate_id,
                "direction": sig.direction,
                "signal_close_utc": sig.signal_close_utc,
                "order_active_from_utc": sig.order_active_from_utc,
                "fill_utc": fill.fill_utc,
                "entry_price": fill.fill_price,
                "entry_level_rule": entry_level,
                "sl_price": plan.sl_price,
                "tp_price": plan.tp_price,
                "tp_pool_bottom": plan.tp_pool_bottom,
                "tp_pool_top": plan.tp_pool_top,
                "tp_distance_pct": plan.tp_distance_pct,
                "rule_ids": "|".join(plan.rule_ids),
                "exit_utc": ex.exit_utc or "",
                "exit_reason": ex.exit_reason,
                "exit_price": ex.exit_price if ex.exit_price is not None else "",
                "gross_pnl_pct": ex.gross_pnl_pct if ex.gross_pnl_pct is not None else "",
                "net_pnl_pct": ex.net_pnl_pct if ex.net_pnl_pct is not None else "",
                "performance_validated": validated,
                "execution_diagnostic": not cfg.get("entry_execution_user_approved"),
                "strategy_version": cfg["strategy_version"],
            }
        )

    return {
        "raw_signals": raw_rows,
        "order_intents": intents,
        "simulated_orders": orders,
        "simulated_trades": trades,
        "suppressed_signals": suppressed,
        "unresolved_strategy_decisions": unresolved,
        "exit_trace": exit_trace,
        "strategy_rule_parity": parity_notes,
    }
