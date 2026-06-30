"""Root-cause trace: C6 DROP after CYCLE_5_SHORT_REDUCE fill (baseline vs mild DCOS). Analysis only."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from fixed_cycle_hedge_bot.models import StrategyIntent

from .candle_loader import load_candles_for_symbol
from .dynamic_cycle_order_scaling import DynamicCycleOrderScalingConfig, config_from_json_string
from .hedge_bot_original_simulator import HedgeBotOriginalSimulator
from .historical_backtest import normalize_candles, resolve_backtest_config
from .purpose_utils import preserve_bot_purpose
from .simulated_order_book import SyntheticCandle

MILD_A_CONFIG = Path("research/backtests/configs/dcos_mild_qty/variant_a.json")
TARGET_STARTS = [250, 4750]
C5_SR_PURPOSE = "CYCLE_5_SHORT_REDUCE"
C6_LA_PURPOSE = "CYCLE_6_LONG_ADD"


def _state_snapshot(strategy: Any, runtime_state: Any) -> dict[str, Any]:
    state = runtime_state.strategy_state
    cycle_state = strategy._ensure_cycle_state(runtime_state)
    active = [
        preserve_bot_purpose(getattr(o, "purpose", "") or "")
        for o in runtime_state.active_orders.values()
    ]
    pending = [
        preserve_bot_purpose(getattr(o, "purpose", "") or "")
        for o in runtime_state.active_orders.values()
        if getattr(o, "status", "") not in {"FILLED", "CANCELED", "CANCELLED"}
    ]
    c5_entry = strategy._get_cycle_sequence_entry(runtime_state, 5)
    c6_entry = strategy._get_cycle_sequence_entry(runtime_state, 6)
    return {
        "cycle_index": int(state.get("active_cycle_index") or 0),
        "cycle_completed_count": int(state.get("cycle_completed_count") or 0),
        "cycle_pair_count": int(state.get("cycle_pair_count") or 0),
        "current_long_cycle_index": int(state.get("current_long_cycle_index") or 0),
        "current_short_cycle_index": int(state.get("current_short_cycle_index") or 0),
        "current_effective_cycle": int(state.get("current_effective_cycle") or 0),
        "next_required_purpose": state.get("next_required_purpose"),
        "next_cycle_purpose": state.get("next_cycle_purpose"),
        "cycle_step": state.get("cycle_step"),
        "cycles_since_refill": int(state.get("cycle_completed_count") or 0)
        - int(state.get("last_refill_completed_cycle_index") or 0),
        "refill_pending": bool(state.get("refill_pending")),
        "refill_required": bool(state.get("refill_required")),
        "cycle_long_add_filled": bool(state.get("cycle_long_add_filled")),
        "cycle_short_tp_filled": bool(state.get("cycle_short_tp_filled")),
        "long_add_rebuild_allowed": bool(state.get("long_add_rebuild_allowed", True)),
        "cycle_waiting_for_short_tp": bool(strategy._get_second_leg_waiting(state, cycle_state)),
        "short_tp_pending_cycle": int(strategy._get_second_leg_pending_cycle(state, cycle_state) or 0),
        "pending_short_cycle_index": int(state.get("pending_short_cycle_index") or 0),
        "processed_cycle_purposes": list(state.get("processed_cycle_purposes") or []),
        "active_order_purposes": active,
        "pending_order_purposes": pending,
        "last_fill_purpose": (state.get("last_fill_info") or {}).get("purpose"),
        "trade_block_id": state.get("trade_block_id"),
        "c5_complete": bool(c5_entry.get("complete")),
        "c5_long_add_status": c5_entry.get("long_add_status"),
        "c5_short_tp_status": c5_entry.get("short_tp_status"),
        "c6_long_add_status": c6_entry.get("long_add_status"),
        "normal_split_stage_count": dict(state.get("normal_cycle_second_leg_split_stage_count") or {}),
    }


def _intent_summary(intents: list[StrategyIntent]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for intent in intents:
        md = dict(intent.metadata or {})
        out.append(
            {
                "purpose": preserve_bot_purpose(intent.purpose),
                "qty": float(intent.qty),
                "trigger_price": float(intent.trigger_price) if intent.trigger_price else None,
                "cycle_index": md.get("cycle_index"),
                "cycle_role": md.get("cycle_role"),
                "original_purpose": md.get("original_purpose") or md.get("parent_purpose"),
                "qty_factor": md.get("cycle_qty_factor") or md.get("qty_factor"),
                "dynamic_cycle_order_scaling": {
                    k: md.get(k)
                    for k in sorted(md)
                    if "dynamic" in k.lower() or "scaling" in k.lower() or "qty_factor" in k
                },
            }
        )
    return out


def _make_sim(
    candles: list[SyntheticCandle],
    *,
    scaling: DynamicCycleOrderScalingConfig | None,
) -> HedgeBotOriginalSimulator:
    config_load = resolve_backtest_config(
        config_source="live",
        signal="long",
        symbol="APTUSDT",
    )
    return HedgeBotOriginalSimulator(
        signal="long",
        symbol="APTUSDT",
        candle_close=float(candles[0].close),
        config_load=config_load,
        dynamic_cycle_scaling_config=scaling,
    )


def _install_hooks(sim: HedgeBotOriginalSimulator, trace: dict[str, Any]) -> None:
    strategy = sim.strategy
    runtime_state = sim.runtime_state
    trace["c5_sr_active"] = False

    orig_try_complete = strategy._try_complete_cycle_pair_after_confirmed_pnl
    orig_force_commit = strategy._force_commit_short_reduce_completion_even_if_duplicate
    orig_build_downside = strategy._build_downside_cycle_intents
    orig_process_candle = sim.process_candle

    def _is_c5_trace(trigger_purpose: str | None, cycle_index: int) -> bool:
        return cycle_index == 5 or (
            trigger_purpose is not None and preserve_bot_purpose(trigger_purpose) == C5_SR_PURPOSE
        )

    def wrapped_try_complete(rs: Any, cycle_index: int, trigger_purpose: str | None) -> None:
        if trace.get("c5_sr_active") or _is_c5_trace(trigger_purpose, cycle_index):
            trace.setdefault("try_complete_calls", []).append(
                {
                    "phase": "before",
                    "cycle_index": cycle_index,
                    "trigger_purpose": trigger_purpose,
                    "state": _state_snapshot(strategy, rs),
                }
            )
        result = orig_try_complete(rs, cycle_index, trigger_purpose)
        if trace.get("c5_sr_active") or _is_c5_trace(trigger_purpose, cycle_index):
            trace.setdefault("try_complete_calls", []).append(
                {
                    "phase": "after",
                    "cycle_index": cycle_index,
                    "trigger_purpose": trigger_purpose,
                    "state": _state_snapshot(strategy, rs),
                }
            )
        return result

    def wrapped_force_commit(*args: Any, **kwargs: Any) -> None:
        cycle_index = int(args[1]) if len(args) > 1 else 0
        trigger = kwargs.get("trigger_purpose")
        if trace.get("c5_sr_active") or cycle_index == 5:
            trace.setdefault("force_commit_calls", []).append(
                {
                    "phase": "before",
                    "cycle_index": cycle_index,
                    "trigger_purpose": trigger,
                    "kwargs_reason": kwargs.get("reason"),
                    "state": _state_snapshot(strategy, runtime_state),
                }
            )
        orig_force_commit(*args, **kwargs)
        if trace.get("c5_sr_active") or cycle_index == 5:
            trace.setdefault("force_commit_calls", []).append(
                {
                    "phase": "after",
                    "cycle_index": cycle_index,
                    "trigger_purpose": trigger,
                    "state": _state_snapshot(strategy, runtime_state),
                }
            )

    def wrapped_build_downside(snapshot: Any, rs: Any, context: Any) -> list[StrategyIntent]:
        state = rs.strategy_state
        next_req = str(state.get("next_required_purpose") or "").upper()
        if trace.get("c5_sr_active") or next_req == C6_LA_PURPOSE:
            trace.setdefault("build_downside_calls", []).append(
                {
                    "phase": "before",
                    "state": _state_snapshot(strategy, rs),
                }
            )
        intents = orig_build_downside(snapshot, rs, context)
        if trace.get("c5_sr_active") or next_req == C6_LA_PURPOSE:
            trace.setdefault("build_downside_calls", []).append(
                {
                    "phase": "after",
                    "intents": _intent_summary(intents),
                    "state": _state_snapshot(strategy, rs),
                }
            )
        return intents

    def wrapped_process_candle(candle: SyntheticCandle, **kwargs: Any) -> Any:
        trace["before_candle"] = _state_snapshot(strategy, runtime_state)
        result = orig_process_candle(candle, **kwargs)
        for fill in result.candle_fills:
            if preserve_bot_purpose(fill.purpose) == C5_SR_PURPOSE:
                trace["c5_sr_active"] = True
                trace["c5_sr_fill"] = {
                    "purpose": C5_SR_PURPOSE,
                    "exec_price": float(fill.exec_price or 0),
                }
                trace["on_fill_intents"] = _intent_summary(result.on_fill_intents)
                trace["tick_intents"] = _intent_summary(result.tick_intents)
                trace["after_candle"] = _state_snapshot(strategy, runtime_state)
                break
        return result

    strategy._try_complete_cycle_pair_after_confirmed_pnl = wrapped_try_complete
    strategy._force_commit_short_reduce_completion_even_if_duplicate = wrapped_force_commit
    strategy._build_downside_cycle_intents = wrapped_build_downside
    sim.process_candle = wrapped_process_candle  # type: ignore[method-assign]


def _run_start(
    candles: list[SyntheticCandle],
    start_index: int,
    *,
    variant: str,
    scaling: DynamicCycleOrderScalingConfig | None,
) -> dict[str, Any]:
    window = candles[start_index : start_index + 5000]
    sim = _make_sim(window, scaling=scaling)
    trace: dict[str, Any] = {"variant": variant, "start_index": start_index, "c5_sr_candle": None}
    _install_hooks(sim, trace)

    sim.candle = window[0]
    sim.candle_index = start_index
    entry = sim.run_entry_smoke()
    sim.submit_intents_to_book(entry.entry_intents, event_source="initial")

    for offset, candle in enumerate(window[1:], start=1):
        abs_index = start_index + offset
        result = sim.process_candle(candle, fill_model="conservative")
        if trace.get("c5_sr_active") and trace.get("c5_sr_candle") is None:
            trace["c5_sr_candle"] = abs_index
            for look in range(1, 6):
                if offset + look >= len(window):
                    break
                candle2 = window[offset + look]
                sim.candle_index = start_index + offset + look
                r2 = sim.process_candle(candle2, fill_model="conservative")
                purposes = [
                    preserve_bot_purpose(i.purpose)
                    for i in (r2.on_fill_intents + r2.tick_intents)
                ]
                if C6_LA_PURPOSE in purposes:
                    trace["c6_la_submit_candle"] = start_index + offset + look
                    break
            break

    trace["has_c6_la_in_window"] = any(
        preserve_bot_purpose(entry.get("purpose") or "") == C6_LA_PURPOSE
        for entry in sim.intent_log
    )
    return trace


def main() -> None:
    raw = load_candles_for_symbol("APTUSDT", timeframe="5m", limit=50000)
    candles = normalize_candles("APTUSDT", raw)
    mild_scaling = config_from_json_string(MILD_A_CONFIG.read_text())

    report: dict[str, Any] = {"starts": {}}
    for start in TARGET_STARTS:
        report["starts"][start] = {
            "baseline": _run_start(candles, start, variant="baseline", scaling=None),
            "mild_a": _run_start(candles, start, variant="mild_a", scaling=mild_scaling),
        }

    out_path = Path("research/backtests/results/c6_drop_intent_build_trace.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, default=str))
    print(out_path)
    for start, variants in report["starts"].items():
        print(f"\n=== start={start} ===")
        for name, trace in variants.items():
            print(
                f"  {name}: c5_sr_candle={trace.get('c5_sr_candle')} "
                f"c6_submit={trace.get('c6_la_submit_candle')} "
                f"has_c6={trace.get('has_c6_la_in_window')}"
            )
            after = trace.get("after_candle") or {}
            print(
                f"    after: next={after.get('next_required_purpose')} "
                f"long_flag={after.get('cycle_long_add_filled')} "
                f"short_flag={after.get('cycle_short_tp_filled')} "
                f"rebuild_allowed={after.get('long_add_rebuild_allowed')} "
                f"short_pending={after.get('short_tp_pending_cycle')} "
                f"completed={after.get('cycle_completed_count')}"
            )
            tick = [i["purpose"] for i in (trace.get("tick_intents") or [])]
            print(f"    tick_intents={tick}")
            fc = trace.get("force_commit_calls") or []
            print(f"    force_commit_calls={len(fc)}")
            tc = trace.get("try_complete_calls") or []
            print(f"    try_complete_calls={len(tc)}")


if __name__ == "__main__":
    main()
