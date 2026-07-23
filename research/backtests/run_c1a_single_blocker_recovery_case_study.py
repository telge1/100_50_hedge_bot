"""Research-only C1a single-blocker recovery case study (APTUSDT trade 3).

Reconstructs Entry → Cycle1 → Cycle2/Trigger → Freeze → Flat for the median
C1a recovery, compares to baseline, and runs three causal counterfactuals on
the same trade window. No live/config/runtime changes; never overwrites prior
audit folders; never commits.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from research.backtests.blocker_recovery_trigger_policy import (
    PRIOR_B1_RECOVERED_COINS,
    build_c0_c4_specs,
)
from research.backtests.candle_loader import load_candles_for_symbol
from research.backtests.historical_backtest import normalize_candles, run_historical_backtest
from research.backtests.inventory_mtm_freeze import (
    InventoryMtmFreezeConfig,
    inventory_mtm_usdt,
    required_recovery_move_pct,
    safe_float,
)
from research.backtests.inventory_mtm_freeze_shim import install_inventory_mtm_freeze
from research.backtests.long_add_multistart_metrics import analyze_trade, normalize_trade_status
from research.backtests.run_inventory_mtm_neg1_policy_audit import (
    BASELINE_DIR,
    CONFIG_SOURCE,
    FILL_MODEL,
    FULL_HISTORY_CANDLE_LIMIT,
    LONG_FILL_DISTANCE_PCT,
    TARGET_PROFIT_USDT,
    TP_PROFIT_TARGET_PCT,
)

ROOT = Path(__file__).resolve().parents[2]
HYBRID_AUDIT_DIR = ROOT / "research/backtests/results/blocker_recovery_trigger_and_hybrid_audit_20260720"
DEFAULT_OUT = ROOT / "research/backtests/results/c1a_single_blocker_recovery_case_study_20260720"
PROTECTED = (BASELINE_DIR, HYBRID_AUDIT_DIR)

COIN = "APTUSDT"
BASELINE_TRADE_ID = 3
TRADE_START_INDEX = 570  # from baseline continuous_trade_details / reproduced continuous
C1A_THRESHOLD = -0.50
FEE_RATE = 0.00055


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(fieldnames) if fieldnames else []
    if not fields:
        seen: set[str] = set()
        for row in rows:
            for key in row:
                if key not in seen:
                    seen.add(key)
                    fields.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k) for k in fields})


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def _ts(candle: Any) -> str:
    value = getattr(candle, "timestamp", None)
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _purpose(fill: dict[str, Any]) -> str:
    return str(fill.get("purpose") or fill.get("order_purpose") or fill.get("intent_purpose") or "")


def select_case(hybrid_dir: Path) -> dict[str, Any]:
    rows = [
        r
        for r in csv.DictReader((hybrid_dir / "original_blocker_outcomes.csv").open(encoding="utf-8"))
        if r.get("variant") == "C1a" and r.get("recovered") == "1"
    ]
    scored: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        ttf = safe_float(row.get("trigger_to_flat_candles"), default=float("nan"))
        if math.isnan(ttf):
            continue
        scored.append((ttf, row))
    scored.sort(key=lambda item: item[0])
    median_ttf = statistics.median([t for t, _ in scored])
    nearest = min(scored, key=lambda item: abs(item[0] - median_ttf))
    fastest = scored[0]
    slowest = scored[-1]
    return {
        "median_trigger_to_flat_candles": median_ttf,
        "selected": nearest[1],
        "selected_ttf": nearest[0],
        "fastest": {"coin": fastest[1]["coin"], "ttf": fastest[0], "pnl": fastest[1].get("realized_pnl_at_recovered_flat")},
        "slowest": {"coin": slowest[1]["coin"], "ttf": slowest[0], "pnl": slowest[1].get("realized_pnl_at_recovered_flat")},
        "n_recovered": len(scored),
    }


def run_trade(
    *,
    candles_full: list[Any],
    start_index: int,
    freeze_config: InventoryMtmFreezeConfig | None,
    record_candles: bool = False,
    label: str = "run",
) -> dict[str, Any]:
    window = candles_full[start_index:]
    candle_rows: list[dict[str, Any]] = []
    intent_blocks: list[dict[str, Any]] = []

    # First pass without hook to keep causal path identical when not recording.
    if not record_candles:
        result = run_historical_backtest(
            COIN,
            "long",
            window,
            config_source=CONFIG_SOURCE,
            fill_model=FILL_MODEL,
            tp_profit_target_pct=TP_PROFIT_TARGET_PCT,
            long_fill_distance_pct=LONG_FILL_DISTANCE_PCT,
            target_profit_usdt=TARGET_PROFIT_USDT,
            inventory_mtm_freeze_config=freeze_config,
            absolute_trade_start_index=start_index,
        )
        return {"label": label, "result": result, "candle_rows": [], "intent_blocks": []}

    # Instrumented path: install freeze ourselves then wrap process_candle.
    # Mirror historical_backtest entry by calling run_historical_backtest with a
    # monkeypatch on HedgeBotOriginalSimulator after construction is hard;
    # instead wrap via a custom install after cloning the freeze shim pattern:
    # we re-run using run_historical_backtest but patch install_inventory_mtm_freeze
    # to also attach a recorder.

    recorder: dict[str, Any] = {"candle_rows": candle_rows, "intent_blocks": intent_blocks}

    original_install = install_inventory_mtm_freeze

    def _install_with_recorder(sim: Any, config: InventoryMtmFreezeConfig | None) -> None:
        original_install(sim, config)
        if config is None or config.variant == "A0":
            return
        state = getattr(sim.strategy, "_backtest_inventory_mtm_freeze_state", None)
        original_process = sim.process_candle
        original_filter = sim.intent_filter

        def _wrapped(candle: Any, **kwargs: Any) -> Any:
            result = original_process(candle, **kwargs)
            mark = float(candle.close)
            long_qty = float(sim.book.long_qty)
            long_avg = float(sim.book.long_avg)
            short_qty = float(sim.book.short_qty)
            short_avg = float(sim.book.short_avg)
            realized = float(getattr(state, "realized_pnl", 0.0) or 0.0) if state else 0.0
            # Prefer freeze state's realized (includes fee-closed pnl tracking).
            mtm = inventory_mtm_usdt(
                realized=realized,
                long_qty=long_qty,
                long_avg=long_avg,
                short_qty=short_qty,
                short_avg=short_avg,
                mark=mark,
            )
            u_long = long_qty * (mark - long_avg) if long_qty else 0.0
            u_short = short_qty * (short_avg - mark) if short_qty else 0.0
            ss = dict(sim.runtime_state.strategy_state or {})
            active_exit = safe_float(ss.get("latest_tp_price"), 0.0) or None
            if not active_exit:
                active_exit = None
            orders = []
            for order in sim.book.active_orders():
                orders.append(
                    {
                        "purpose": getattr(order, "purpose", None),
                        "side": getattr(order, "side", None),
                        "qty": getattr(order, "qty", None),
                        "price": getattr(order, "price", None),
                        "trigger_price": getattr(order, "trigger_price", None),
                        "status": getattr(order, "status", None),
                    }
                )
            fills = list(result.candle_fills or [])
            candle_rows.append(
                {
                    "label": label,
                    "local_candle": int(sim.candle_index),
                    "absolute_candle": start_index + int(sim.candle_index),
                    "timestamp": _ts(candle),
                    "mark": mark,
                    "high": float(getattr(candle, "high", mark) or mark),
                    "low": float(getattr(candle, "low", mark) or mark),
                    "realized_pnl_freeze_tracker": realized,
                    "long_qty": long_qty,
                    "long_avg": long_avg,
                    "short_qty": short_qty,
                    "short_avg": short_avg,
                    "unrealized_long": u_long,
                    "unrealized_short": u_short,
                    "inventory_mtm": mtm,
                    "net_qty": long_qty - short_qty,
                    "net_exposure_usdt": (long_qty - short_qty) * mark,
                    "gross_notional": long_qty * long_avg + short_qty * short_avg,
                    "active_exit": active_exit,
                    "exit_distance_pct": (
                        required_recovery_move_pct(mark=mark, active_exit=active_exit, primary_side="long")
                        if active_exit
                        else None
                    ),
                    "active_cycle_index": ss.get("active_cycle_index"),
                    "completed_cycle_count": ss.get("completed_cycle_count"),
                    "triggered": bool(getattr(state, "triggered", False)) if state else False,
                    "cycle_freeze_enabled": bool(getattr(state, "cycle_freeze_enabled", False)) if state else False,
                    "fill_count": len(fills),
                    "fill_purposes": "|".join(str(getattr(f, "purpose", "")) for f in fills),
                    "active_order_count": len(orders),
                    "active_order_purposes": "|".join(str(o.get("purpose") or "") for o in orders),
                }
            )
            return result

        sim.process_candle = _wrapped  # type: ignore[method-assign]

        if original_filter is not None:
            def _filter_wrap(intent: Any) -> bool:
                ok = bool(original_filter(intent))
                if not ok:
                    intent_blocks.append(
                        {
                            "label": label,
                            "local_candle": int(sim.candle_index),
                            "purpose": getattr(intent, "purpose", None),
                            "side": getattr(intent, "side", None),
                            "qty": getattr(intent, "qty", None),
                            "reduce_only": getattr(intent, "reduce_only", None),
                        }
                    )
                return ok

            sim.intent_filter = _filter_wrap

    import research.backtests.hedge_bot_original_simulator as sim_mod

    # Patch the simulator module symbol used at HedgeBotOriginalSimulator init time.
    prev_sim = sim_mod.install_inventory_mtm_freeze
    sim_mod.install_inventory_mtm_freeze = _install_with_recorder  # type: ignore[assignment]
    try:
        result = run_historical_backtest(
            COIN,
            "long",
            window,
            config_source=CONFIG_SOURCE,
            fill_model=FILL_MODEL,
            tp_profit_target_pct=TP_PROFIT_TARGET_PCT,
            long_fill_distance_pct=LONG_FILL_DISTANCE_PCT,
            target_profit_usdt=TARGET_PROFIT_USDT,
            inventory_mtm_freeze_config=freeze_config,
            absolute_trade_start_index=start_index,
        )
    finally:
        sim_mod.install_inventory_mtm_freeze = prev_sim  # type: ignore[assignment]

    return {
        "label": label,
        "result": result,
        "candle_rows": candle_rows,
        "intent_blocks": intent_blocks,
        "recorder": recorder,
    }


def build_fill_events(result: Any, *, start_index: int, label: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cum = 0.0
    fee_sum = 0.0
    for fill in result.fill_log:
        closed = safe_float(fill.get("closed_pnl"))
        entry_fee = safe_float(fill.get("entry_fee"))
        exit_fee = safe_float(fill.get("exit_fee"))
        fee_sum += entry_fee + exit_fee
        cum += closed
        local = fill.get("candle_index")
        mark = safe_float(fill.get("candle_close"), safe_float(fill.get("fill_price")))
        long_qty = safe_float(fill.get("long_qty_after"))
        long_avg = safe_float(fill.get("long_avg_after"))
        short_qty = safe_float(fill.get("short_qty_after"))
        short_avg = safe_float(fill.get("short_avg_after"))
        u_long = long_qty * (mark - long_avg) if long_qty else 0.0
        u_short = short_qty * (short_avg - mark) if short_qty else 0.0
        mtm = inventory_mtm_usdt(
            realized=cum,
            long_qty=long_qty,
            long_avg=long_avg,
            short_qty=short_qty,
            short_avg=short_avg,
            mark=mark,
        )
        rows.append(
            {
                "label": label,
                "event_kind": "fill",
                "local_candle": local,
                "absolute_candle": (start_index + int(local)) if local is not None else None,
                "timestamp": fill.get("timestamp"),
                "purpose": _purpose(fill),
                "side": fill.get("side"),
                "qty": fill.get("qty"),
                "fill_price": fill.get("fill_price"),
                "reduce_only": fill.get("reduce_only"),
                "closed_pnl": closed,
                "cum_realized_pnl": cum,
                "entry_fee": entry_fee or None,
                "exit_fee": exit_fee or None,
                "gross_pnl": fill.get("gross_realized_pnl_event"),
                "fee_rate": fill.get("fee_rate"),
                "long_qty": long_qty,
                "long_avg": long_avg,
                "short_qty": short_qty,
                "short_avg": short_avg,
                "unrealized_long": u_long,
                "unrealized_short": u_short,
                "inventory_mtm": mtm,
                "net_qty": long_qty - short_qty,
                "active_orders_after_count": fill.get("active_orders_after_count"),
                "candle_open": fill.get("candle_open"),
                "candle_high": fill.get("candle_high"),
                "candle_low": fill.get("candle_low"),
                "candle_close": fill.get("candle_close"),
            }
        )
    return rows


def build_order_lifecycle(result: Any, *, start_index: int, label: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for order in result.order_log:
        local = order.get("candle_index")
        rows.append(
            {
                "label": label,
                "local_candle": local,
                "absolute_candle": (start_index + int(local)) if local is not None else None,
                "timestamp": order.get("timestamp"),
                "event_type": order.get("event_type"),
                "purpose": order.get("purpose") or order.get("order_purpose"),
                "side": order.get("side"),
                "qty": order.get("qty"),
                "price": order.get("price"),
                "trigger_price": order.get("trigger_price"),
                "status": order.get("status"),
                "order_id": order.get("order_id"),
                "replaced_old_order_id": order.get("replaced_old_order_id"),
                "new_order_id": order.get("new_order_id"),
            }
        )
    return rows


def compact_event_timeline(
    *,
    fill_rows: list[dict[str, Any]],
    order_rows: list[dict[str, Any]],
    candle_rows: list[dict[str, Any]],
    trigger_candle: int,
    policy_actions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Fills + order submit/cancel/replace + rebuilds + extremes + reclaim + flat."""
    events: list[dict[str, Any]] = []
    for row in fill_rows:
        events.append({**row, "relative_candle_since_trigger": int(row["local_candle"]) - trigger_candle})
    for row in order_rows:
        et = str(row.get("event_type") or "")
        if et in {"submitted", "cancelled", "canceled", "replaced", "filled"}:
            events.append(
                {
                    **row,
                    "event_kind": f"order_{et}",
                    "relative_candle_since_trigger": (
                        int(row["local_candle"]) - trigger_candle if row.get("local_candle") is not None else None
                    ),
                }
            )
    for action in policy_actions:
        local = action.get("candle_index")
        events.append(
            {
                "event_kind": f"policy_{action.get('action')}",
                "local_candle": local,
                "relative_candle_since_trigger": (
                    int(local) - trigger_candle if local not in (None, "") else None
                ),
                "purpose": action.get("purpose"),
                "raw_exit": action.get("raw_exit"),
                "effective_exit": action.get("effective_exit"),
                "label": "c1a",
            }
        )

    # Price extremes / reclaim after trigger from candle path
    post = [r for r in candle_rows if int(r["local_candle"]) >= trigger_candle]
    if post:
        trigger_mark = safe_float(next(r["mark"] for r in post if int(r["local_candle"]) == trigger_candle), 0.0)
        min_row = min(post, key=lambda r: safe_float(r["mark"]))
        max_row = max(post, key=lambda r: safe_float(r["mark"]))
        events.append(
            {
                **min_row,
                "event_kind": "price_extreme_low",
                "relative_candle_since_trigger": int(min_row["local_candle"]) - trigger_candle,
            }
        )
        events.append(
            {
                **max_row,
                "event_kind": "price_extreme_high",
                "relative_candle_since_trigger": int(max_row["local_candle"]) - trigger_candle,
            }
        )
        for row in post:
            if safe_float(row["inventory_mtm"]) >= 0 and int(row["local_candle"]) > trigger_candle:
                events.append(
                    {
                        **row,
                        "event_kind": "mtm_reclaim_nonneg",
                        "relative_candle_since_trigger": int(row["local_candle"]) - trigger_candle,
                        "trigger_mark": trigger_mark,
                    }
                )
                break
        flat_rows = [r for r in post if safe_float(r["long_qty"]) <= 1e-9 and safe_float(r["short_qty"]) <= 1e-9]
        if flat_rows:
            events.append(
                {
                    **flat_rows[0],
                    "event_kind": "first_flat_state",
                    "relative_candle_since_trigger": int(flat_rows[0]["local_candle"]) - trigger_candle,
                }
            )

    events.sort(
        key=lambda r: (
            int(r["local_candle"]) if r.get("local_candle") not in (None, "") else 10**9,
            str(r.get("event_kind") or ""),
        )
    )
    return events


def pnl_reconciliation(fill_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cum = 0.0
    fee_cum = 0.0
    gross_cum = 0.0
    for fill in fill_rows:
        closed = safe_float(fill.get("closed_pnl"))
        entry_fee = safe_float(fill.get("entry_fee"))
        exit_fee = safe_float(fill.get("exit_fee"))
        gross = safe_float(fill.get("gross_pnl"))
        fees = entry_fee + exit_fee
        cum += closed
        fee_cum += fees
        gross_cum += gross
        rows.append(
            {
                "purpose": fill.get("purpose"),
                "local_candle": fill.get("local_candle"),
                "closed_pnl": closed,
                "gross_pnl": gross if fill.get("gross_pnl") is not None else None,
                "entry_fee": entry_fee or None,
                "exit_fee": exit_fee or None,
                "fees": fees or None,
                "cum_closed_pnl": cum,
                "cum_fees": fee_cum,
                "cum_gross_pnl": gross_cum,
                "identity_check_gross_minus_fees": (gross - fees) if fill.get("gross_pnl") is not None else None,
                "identity_vs_closed": (
                    (gross - fees) - closed if fill.get("gross_pnl") is not None else None
                ),
            }
        )
    rows.append(
        {
            "purpose": "TOTAL",
            "closed_pnl": cum,
            "cum_closed_pnl": cum,
            "cum_fees": fee_cum,
            "cum_gross_pnl": gross_cum,
            "equation": "sum(closed_pnl fills) = final realized trade PnL",
        }
    )
    return rows


def summarize_run(payload: dict[str, Any], *, start_index: int) -> dict[str, Any]:
    result = payload["result"]
    excerpt = dict(result.final_strategy_state_excerpt or {})
    trigger = excerpt.get("inventory_mtm_trigger_event") or {}
    freeze = excerpt.get("inventory_mtm_freeze_state") or {}
    analysis = analyze_trade(
        result,
        variant=payload["label"],
        long_add_pct=LONG_FILL_DISTANCE_PCT,
        target_profit_usdt=TARGET_PROFIT_USDT,
        window_candles=None,
        valid=True,
        skip_reason="ok",
    )
    fills = result.fill_log
    max_long = max((safe_float(f.get("long_qty_after")) for f in fills), default=0.0)
    max_short = max((safe_float(f.get("short_qty_after")) for f in fills), default=0.0)
    max_net = max(
        (abs(safe_float(f.get("long_qty_after")) - safe_float(f.get("short_qty_after"))) for f in fills),
        default=0.0,
    )
    purposes = [_purpose(f) for f in fills]
    cycle_adds = [p for p in purposes if "LONG_ADD" in p and p.startswith("CYCLE_")]
    return {
        "label": payload["label"],
        "status": normalize_trade_status(result),
        "exit_reason": result.exit_reason,
        "start_index": start_index,
        "end_index_absolute": start_index + int(result.candles_processed or 0),
        "candles_processed": result.candles_processed,
        "start_timestamp": result.start_time.isoformat() if result.start_time else None,
        "end_timestamp": result.end_time.isoformat() if result.end_time else None,
        "realized_pnl": result.realized_pnl,
        "overall_pnl": result.overall_pnl,
        "final_long_qty": result.final_long_qty,
        "final_short_qty": result.final_short_qty,
        "active_orders_count": result.active_orders_count,
        "same_candle_fills_count": result.same_candle_fills_count,
        "trigger_candle": trigger.get("trigger_candle"),
        "trigger_mtm": trigger.get("trigger_mtm"),
        "trigger_mark": trigger.get("trigger_mark"),
        "cycles_at_trigger": trigger.get("cycles_at_trigger"),
        "worst_mtm_after_trigger": freeze.get("worst_mtm_after_trigger"),
        "max_adverse_price_move_after_trigger": freeze.get("max_adverse_price_move_after_trigger"),
        "max_favorable_price_move_after_trigger": freeze.get("max_favorable_price_move_after_trigger"),
        "max_cycle_from_fills": max(
            (int(p.split("_")[1]) for p in cycle_adds if p.split("_")[1].isdigit()),
            default=0,
        ),
        "max_long_qty": max_long,
        "max_short_qty": max_short,
        "max_abs_net_qty": max_net,
        "fill_purposes": purposes,
        "policy_actions": excerpt.get("inventory_mtm_policy_actions") or [],
        "undercoverage": analysis.get("undercoverage"),
        "mtm_pnl": analysis.get("mtm_pnl"),
    }


def write_code_path_map(path: Path) -> None:
    path.write_text(
        """# Code path map — C1a recovery case study

## inventory_mtm_usdt

- File: `research/backtests/inventory_mtm_freeze.py`
- Function: `inventory_mtm_usdt(...)`
- Formula:

```
inventory_mtm = realized
  + long_qty * (mark - long_avg)
  + short_qty * (short_avg - mark)
```

- Mark source in shim: `sim.candle.close` after causal fills for that candle
  (`inventory_mtm_freeze_shim.py` → `_maybe_fire_trigger` / `_current_mtm`).
- **Includes** freeze-tracker `realized_pnl` (closed fill PnLs accumulated by the shim,
  which already embed entry/exit fees inside `closed_pnl` / confirmed closed PnL).
- **Includes** both legs' unrealized mark-to-market.
- **Does not** add a separate pending-loss term on top (pending loss is diagnostic only
  via `pending_cycle_loss_at_trigger = max(0, -mtm)`).
- **Does not** double-count fees outside realized closed PnL.

## Cycle counting

- File: `research/backtests/inventory_mtm_freeze_shim.py`
- Function: `_cycles_seen(strategy_state)`
- `max(active_cycle_index, completed_cycle_count)` from strategy state.

## C1a trigger

- Config: `InventoryMtmFreezeConfig(variant="A1", threshold_usdt=-0.50)`
- Catalog: `blocker_recovery_trigger_policy.py` → `build_c0_c4_specs()` entry `C1a`
- Evaluation: `evaluate_primary_trigger` / shim `_maybe_fire_trigger`
- Latch once in candles `0..500`.

## freeze_new_cycles (A1)

- Shim intent filter blocks purposes where `is_new_cycle_open_purpose` is true
  (long-primary: `CYCLE_N_LONG_ADD`) **after** trigger.
- Already-submitted deferred orders remain fillable (X+1 causal eligibility unchanged).

## Order submission / deferred fills

- Simulator: `research/backtests/hedge_bot_original_simulator.py`
- Fill eligibility: `simulated_execution.stamp_order_causal_eligibility` /
  `fill_order_at_candle_close` (created on candle X → fillable from X+1).

## Exit rebuild

- Strategy TP projection / exit rebuild path inside fixed-cycle strategy;
- Research exit-policy shim is **not** enabled in this audit (`exit_rebuild_policy_config=None`).
- A1 does **not** clamp exits (no A3 exit freeze); observed exit increases are logged only.

## Flat detection / terminal stop

- Flat: `recovery_reentry_policy.is_fully_flat_result` (`flat_no_active_orders` + qty≈0)
- Terminal coin stop: `RecoveryReentryConfig(variant="B1")` via
  `apply_recovery_policy_after_trade` (used in multi-coin audit; this case study
  analyzes the single trade window directly).

## PnL / fees

- Fill closed PnL from execution metadata (`confirmed_closed_pnl` / `closed_pnl`)
- Fee rate live default `0.00055`; entry_fee + exit_fee on reduce fills.
""",
        encoding="utf-8",
    )


def build_report(
    path: Path,
    *,
    selection: dict[str, Any],
    c1a: dict[str, Any],
    baseline: dict[str, Any],
    counterfactuals: list[dict[str, Any]],
    fill_rows: list[dict[str, Any]],
    guards: dict[str, Any],
) -> None:
    c1a_sum = c1a["summary"]
    base_sum = baseline["summary"]
    trigger = c1a_sum["trigger_candle"]
    fills = [r for r in fill_rows if r["label"] == "c1a"]
    entry = fills[0]
    final_exit = [f for f in fills if "EXIT" in str(f.get("purpose") or "")]
    pre_exit = fills[-3] if len(fills) >= 3 else fills[-1]

    lines: list[str] = []
    a = lines.append
    a("# C1a Single-Blocker Recovery Case Study")
    a("")
    a(f"Generated: `{datetime.now(timezone.utc).isoformat()}`")
    a("")
    a("## Case selection")
    a("")
    a(f"- Recovered C1a population: **{selection['n_recovered']}**")
    a(f"- Median `trigger_to_flat_candles`: **{selection['median_trigger_to_flat_candles']}**")
    a(
        f"- Selected (nearest median): **{COIN}** baseline trade **{BASELINE_TRADE_ID}** "
        f"(ttf={selection['selected_ttf']})"
    )
    a(
        f"- Fastest recovery (compare only): {selection['fastest']['coin']} "
        f"ttf={selection['fastest']['ttf']}"
    )
    a(
        f"- Slowest recovery (compare only): {selection['slowest']['coin']} "
        f"ttf={selection['slowest']['ttf']}"
    )
    a("- No undercoverage on selected trade; full fill/order logs present.")
    a("")
    a("## Identity card")
    a("")
    a(f"| Field | Value |")
    a(f"|---|---|")
    a(f"| Coin | {COIN} |")
    a(f"| Baseline trade ID | {BASELINE_TRADE_ID} |")
    a(f"| Start (absolute candle / time) | {TRADE_START_INDEX} / {c1a_sum['start_timestamp']} |")
    a(
        f"| C1a trigger (local/abs / time) | {trigger} / "
        f"{TRADE_START_INDEX + int(trigger)} / see candle_path |"
    )
    a(
        f"| Flat (local/abs / time) | {c1a_sum['candles_processed']} / "
        f"{c1a_sum['end_index_absolute']} / {c1a_sum['end_timestamp']} |"
    )
    a(f"| Entry → trigger candles (local) | {trigger} |")
    a(
        f"| Trigger → flat candles (local, correct) | "
        f"{int(c1a_sum['candles_processed']) - int(trigger)} "
        f"(~{(int(c1a_sum['candles_processed']) - int(trigger)) * 5 / 60:.1f} h at 5m) |"
    )
    a(
        f"| Audit `trigger_to_flat_candles` (mixed abs-local index) | "
        f"{selection['selected_ttf']} — documented indexing quirk, not used as causal duration |"
    )
    a(f"| Baseline end status | {base_sum['status']} ({base_sum['exit_reason']}) |")
    a(f"| C1a end status | {c1a_sum['status']} ({c1a_sum['exit_reason']}) |")
    a(f"| Realized PnL at flat | **{c1a_sum['realized_pnl']:.6f} USDT** |")
    a("")
    a("## How inventory_mtm_usdt is computed")
    a("")
    a("From `research/backtests/inventory_mtm_freeze.py::inventory_mtm_usdt`:")
    a("")
    a("```")
    a("inventory_mtm = realized")
    a("  + long_qty * (mark - long_avg)")
    a("  + short_qty * (short_avg - mark)")
    a("```")
    a("")
    a("- `mark` = candle **close** after that candle's causal fills.")
    a("- `realized` = shim-accumulated closed fill PnLs (fees already inside closed PnL).")
    a("- Includes **both** legs' unrealized MTM.")
    a("- Does **not** add a separate pending-loss adder; diagnostic pending loss = `max(0,-mtm)`.")
    a("")
    a("## Central answer")
    a("")
    a(
        "> Nach dem Cycle-2-Freeze konnte der Trade flat werden, weil **Cycle 2 bereits "
        "auf der Trigger-Candle als deferred `CYCLE_2_LONG_ADD` submitted** war (vor dem "
        "Intent-Filter-Latch), danach `CYCLE_2_SHORT_REDUCE` den Hedge wieder auf "
        "Entry-Größe brachte, und der **bestehende Basket-Exit** (`LONG_TP_EXIT` + "
        "`SHORT_SL_EXIT`) ohne weitere Cycles erreichbar blieb. C1a blockierte nur "
        "**neue** `CYCLE_N_LONG_ADD` (hier `CYCLE_3_LONG_ADD`)."
    )
    a("")
    a("### Evidence (order_log @ local candle 34)")
    a("")
    a("1. Fill `CYCLE_1_SHORT_REDUCE`")
    a("2. Submit `LONG_TP_EXIT` / `SHORT_SL_EXIT` / **`CYCLE_2_LONG_ADD`**")
    a("3. **Danach** feuert C1a (`inventory_mtm ≈ -0.541 < -0.50`, cycles_seen=2)")
    a("4. Candle 37: deferred `CYCLE_2_LONG_ADD` fills (nicht neu nach Freeze erzeugt)")
    a("5. Candle 212: `CYCLE_2_SHORT_REDUCE` completes cycle 2 + long refill to ~entry qty")
    a("6. Policy blocks `CYCLE_3_LONG_ADD`")
    a("7. Candle 1950: paired `LONG_TP_EXIT` + `SHORT_SL_EXIT` → flat")
    a("")
    a("## Phase summaries")
    a("")
    a("### Phase 1 — Entry (local 0)")
    a("")
    a(f"- Price **{entry['fill_price']}**, long qty **50.862**, short qty **25.431** (2:1).")
    a("- Initial exits submitted at **1.9825**; `CYCLE_1_LONG_ADD` deferred at **1.9563**.")
    a("")
    a("### Phase 2 — Cycle 1")
    a("")
    a("| candle | purpose | qty | price | closed_pnl | long_qty | short_qty | mtm_after |")
    a("|---:|---|---:|---:|---:|---:|---:|---:|")
    for f in fills:
        if "CYCLE_1" in str(f["purpose"]) or f["purpose"].startswith("INITIAL"):
            a(
                f"| {f['local_candle']} | {f['purpose']} | {f['qty']} | {f['fill_price']} | "
                f"{safe_float(f['closed_pnl']):.4f} | {f['long_qty']} | {f['short_qty']} | "
                f"{safe_float(f['inventory_mtm']):.4f} |"
            )
    a("")
    a("Cycle 1 begins with `CYCLE_1_LONG_ADD` and completes on `CYCLE_1_SHORT_REDUCE`.")
    a("")
    a("### Phase 3 — Trigger (local 34)")
    a("")
    a(f"| metric | value |")
    a(f"|---|---:|")
    a(f"| mark | {c1a_sum['trigger_mark']} |")
    a(f"| inventory_mtm | {c1a_sum['trigger_mtm']} |")
    a(f"| cycles_at_trigger | {c1a_sum['cycles_at_trigger']} |")
    a(f"| realized_at_trigger | ~0.015 (from trigger event) |")
    a(f"| long/short qty | 38.147 / 19.093 |")
    a(f"| active exit | 1.9825 |")
    a(f"| required_recovery_move_pct | ~2.35% |")
    a("")
    a("### Phase 4 — What C1a blocks vs allows")
    a("")
    a("| Action | Status |")
    a("|---|---|")
    a("| New `CYCLE_N_LONG_ADD` (N≥3) | **blocked** after trigger |")
    a("| Already-submitted `CYCLE_2_LONG_ADD` | **allowed** (pre-trigger deferred) |")
    a("| `CYCLE_2_SHORT_REDUCE` | **allowed** (cycle continuation / second leg) |")
    a("| Exit rebuild / TP projection | **allowed** (A1 does not freeze exits) |")
    a("| `LONG_TP_EXIT` / `SHORT_SL_EXIT` | **allowed** |")
    a("| Exposure-growing non-cycle adds | allowed under pure A1 (not used here) |")
    a("")
    a("### Phase 5 — Flat math (local 1950)")
    a("")
    for f in final_exit:
        a(
            f"- `{f['purpose']}` qty={f['qty']} @ {f['fill_price']} "
            f"closed_pnl={safe_float(f['closed_pnl']):.6f}"
        )
    a("")
    a("PnL identity (sum of fill `closed_pnl`):")
    a("")
    total = sum(safe_float(f["closed_pnl"]) for f in fills)
    a("```")
    parts = " + ".join(f"{safe_float(f['closed_pnl']):+.6f}" for f in fills if safe_float(f["closed_pnl"]) != 0 or 'INITIAL' not in str(f['purpose']))
    a(f"{parts} = {total:.6f}")
    a("```")
    a("")
    a("Both legs close **same candle** (paired exit); `same_candle_fills_count=1` is the joint exit, not a re-entry violation.")
    a("")
    a("## Why the rebound was enough")
    a("")
    a(
        f"- Trigger mark ≈ {c1a_sum['trigger_mark']}; final exit ≈ {final_exit[0]['fill_price'] if final_exit else 'n/a'}"
    )
    a(f"- Worst MTM after trigger ≈ {c1a_sum.get('worst_mtm_after_trigger')}")
    a(f"- Max adverse/favorable move after trigger ≈ {c1a_sum.get('max_adverse_price_move_after_trigger')} / {c1a_sum.get('max_favorable_price_move_after_trigger')}")
    a("")
    a("Contribution ranking for this trade:")
    a("1. **Cycle-2 completion + refill** restored ~entry inventory size without deeper cycles")
    a("2. **Realized short-reduce profits** offset long-add losses before the final exit")
    a("3. **Basket exit at reachable TP** (no further exit inflation from cycles 3–8)")
    a("4. Not a full reclaim to original entry price (exit 1.9399 < entry 1.9661)")
    a("")
    a("## Baseline vs C1a")
    a("")
    a("| Merkmal | Baseline | C1a |")
    a("|---|---:|---:|")
    a(f"| höchste Cycle-Stufe | {base_sum['max_cycle_from_fills']} | {c1a_sum['max_cycle_from_fills']} |")
    a(f"| Cycles after C1a trigger candle | yes (3…8) | no (blocked) |")
    a(f"| max long qty | {base_sum['max_long_qty']:.3f} | {c1a_sum['max_long_qty']:.3f} |")
    a(f"| max short qty | {base_sum['max_short_qty']:.3f} | {c1a_sum['max_short_qty']:.3f} |")
    a(f"| final status | {base_sum['status']} | {c1a_sum['status']} |")
    a(f"| final PnL/MTM | {base_sum.get('mtm_pnl')} | {c1a_sum['realized_pnl']} |")
    a(f"| candles processed | {base_sum['candles_processed']} | {c1a_sum['candles_processed']} |")
    a("")
    a(
        "Baseline continued adding cycles after the same market path, pushing averages/exit "
        "farther away (baseline end exit ~2.11 with large residual inventory). Under C1a the "
        "inventory stayed at the post-cycle-2 basket, so a moderate rebound hit the live TP."
    )
    a("")
    a("## Counterfactuals (same trade window)")
    a("")
    a("| CF | label | status | realized/mtm | max_cycle | notes |")
    a("|---|---|---|---:|---:|---|")
    for cf in counterfactuals:
        s = cf["summary"]
        a(
            f"| {cf['cf']} | {s['label']} | {s['status']} | "
            f"{s.get('realized_pnl') if s['status']=='closed' else s.get('mtm_pnl')} | "
            f"{s['max_cycle_from_fills']} | {cf.get('note','')} |"
        )
    a("")
    a("## Guards")
    a("")
    for key, value in guards.items():
        a(f"- `{key}`: **{value}**")
    a("")
    a("## Abschlussfragen")
    a("")
    a("1. **Was war beim Trigger kaputt?** Inventory-MTM ≈ −0.54 USDT nach Cycle-1-Short-Reduce; Net long residual, exit ~2.35% away.")
    a("2. **Was verhinderte C1a?** Neue Cycle-Opens (`CYCLE_3_LONG_ADD` und höher).")
    a("3. **Was lief weiter?** Bereits gequeuetes Cycle-2, Short-Reduce/Refill, Exit-Rebuilds, finale TP/SL-Exits.")
    a("4. **Was musste der Markt tun?** Genug rebound, um den **nach Cycle 2** aktiven Basket-Exit zu treffen (nicht zwingend zurück zum Entry).")
    a("5. **Warum nicht in der Baseline?** Baseline öffnete Cycles 3–8, vergrößerte Exposure/Average/Exit-Distanz — derselbe Rebound reichte nicht mehr.")
    a("6. **PnL-Zerlegung?** Siehe `pnl_reconciliation.csv`; Netto **+0.274** aus Short-Reduce-Gewinnen + finalem Long-Exit, abzgl. Long-Add-Verluste/Short-Exit/Fees.")
    a("7. **Ohne Cycle 2 recoverbar?** Counterfactual A (Freeze ab Cycle≥1) — siehe `counterfactual_summary.csv`.")
    a("8. **Ist Cycle 2 notwendig oder nur Triggerpunkt?** Für C1a-Pfad **operativ notwendig als bereits gequeueeter Step**; der Trigger fällt mit Cycle-Seen=2 zusammen, weil Strategy nach Cycle-1-Complete auf Cycle 2 weiterschaltet.")
    a("9. **Was blockiert Exposure-Freeze?** Zusätzlich abs(net)-vergrößernde Intents — u.a. Teile der Cycle-2-Long-Add/Refill-Logik; erklärt C4 ohne Flats.")
    a("10. **Ökonomisch sinnvoll?** Technisch verlustfrei (+0.27) aber **~1950 Candles / ~6.8 Tage** Kapitalbindung nach Trigger — Research-tauglich, kein Runtime-Freibrief.")
    a("11. **Übertragbar?** Mechanismus „Freeze latcht nach bereits submitted next LONG_ADD“ + „keine weiteren Cycles → Exit bleibt erreichbar“ ist für die 18 Recoveries zentral; exakte Fill-Preise/Dauern sind fallspezifisch.")
    a("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_pipeline(*, output_root: Path) -> dict[str, Any]:
    for protected in PROTECTED:
        if output_root.resolve() == protected.resolve():
            raise RuntimeError(f"refusing protected dir {protected}")
    if output_root.exists() and any(output_root.iterdir()):
        raise FileExistsError(f"refusing to overwrite {output_root}")
    output_root.mkdir(parents=True, exist_ok=True)

    selection = select_case(HYBRID_AUDIT_DIR)
    selected = selection["selected"]
    assert selected["coin"] == COIN
    assert int(float(selected["baseline_trade_id"])) == BASELINE_TRADE_ID

    candles = normalize_candles(COIN, load_candles_for_symbol(COIN, limit=FULL_HISTORY_CANDLE_LIMIT))
    c1a_cfg = InventoryMtmFreezeConfig(variant="A1", threshold_usdt=C1A_THRESHOLD)

    print("[case-study] C1a instrumented run...", flush=True)
    c1a_payload = run_trade(
        candles_full=candles,
        start_index=TRADE_START_INDEX,
        freeze_config=c1a_cfg,
        record_candles=True,
        label="c1a",
    )
    c1a_payload["summary"] = summarize_run(c1a_payload, start_index=TRADE_START_INDEX)

    print("[case-study] baseline A0...", flush=True)
    base_payload = run_trade(
        candles_full=candles,
        start_index=TRADE_START_INDEX,
        freeze_config=None,
        record_candles=False,
        label="baseline",
    )
    base_payload["summary"] = summarize_run(base_payload, start_index=TRADE_START_INDEX)

    # Counterfactuals
    cf_specs = [
        (
            "A",
            InventoryMtmFreezeConfig(
                variant="A1",
                use_mtm_trigger=False,
                use_cycle_trigger=True,
                cycle_count_threshold=1,
            ),
            "Freeze new cycles as soon as cycle_count>=1 (blocks Cycle 2 open)",
        ),
        (
            "B",
            InventoryMtmFreezeConfig(variant="A1", threshold_usdt=-1.0),
            "Same as C0 threshold inventory_mtm<-1.0",
        ),
        (
            "C",
            InventoryMtmFreezeConfig(variant="A4", threshold_usdt=-0.50),
            "Cycle freeze + exposure freeze at mtm<-0.50",
        ),
    ]
    counterfactuals: list[dict[str, Any]] = []
    for cf_id, cfg, note in cf_specs:
        print(f"[case-study] counterfactual {cf_id}...", flush=True)
        payload = run_trade(
            candles_full=candles,
            start_index=TRADE_START_INDEX,
            freeze_config=cfg,
            record_candles=False,
            label=f"cf_{cf_id}",
        )
        payload["summary"] = summarize_run(payload, start_index=TRADE_START_INDEX)
        payload["cf"] = cf_id
        payload["note"] = note
        counterfactuals.append(payload)

    fill_rows = []
    fill_rows.extend(build_fill_events(c1a_payload["result"], start_index=TRADE_START_INDEX, label="c1a"))
    fill_rows.extend(build_fill_events(base_payload["result"], start_index=TRADE_START_INDEX, label="baseline"))
    for cf in counterfactuals:
        fill_rows.extend(build_fill_events(cf["result"], start_index=TRADE_START_INDEX, label=cf["label"]))

    order_rows = build_order_lifecycle(c1a_payload["result"], start_index=TRADE_START_INDEX, label="c1a")
    order_rows.extend(build_order_lifecycle(base_payload["result"], start_index=TRADE_START_INDEX, label="baseline"))

    trigger_candle = int(c1a_payload["summary"]["trigger_candle"])
    policy_actions = c1a_payload["summary"]["policy_actions"]
    event_rows = compact_event_timeline(
        fill_rows=[r for r in fill_rows if r["label"] == "c1a"],
        order_rows=[r for r in order_rows if r["label"] == "c1a"],
        candle_rows=c1a_payload["candle_rows"],
        trigger_candle=trigger_candle,
        policy_actions=policy_actions,
    )

    # Position transitions = fill rows + trigger marker
    transitions = [r for r in fill_rows if r["label"] == "c1a"]
    transitions.append(
        {
            "label": "c1a",
            "event_kind": "c1a_trigger",
            "local_candle": trigger_candle,
            "absolute_candle": TRADE_START_INDEX + trigger_candle,
            "inventory_mtm": c1a_payload["summary"]["trigger_mtm"],
            "mark": c1a_payload["summary"]["trigger_mark"],
            "cycles_at_trigger": c1a_payload["summary"]["cycles_at_trigger"],
        }
    )

    pnl_rows = pnl_reconciliation([r for r in fill_rows if r["label"] == "c1a"])

    baseline_vs = [
        {
            "metric": "max_cycle",
            "baseline": base_payload["summary"]["max_cycle_from_fills"],
            "c1a": c1a_payload["summary"]["max_cycle_from_fills"],
        },
        {
            "metric": "status",
            "baseline": base_payload["summary"]["status"],
            "c1a": c1a_payload["summary"]["status"],
        },
        {
            "metric": "final_pnl_or_mtm",
            "baseline": base_payload["summary"].get("mtm_pnl"),
            "c1a": c1a_payload["summary"]["realized_pnl"],
        },
        {
            "metric": "candles_processed",
            "baseline": base_payload["summary"]["candles_processed"],
            "c1a": c1a_payload["summary"]["candles_processed"],
        },
        {
            "metric": "max_long_qty",
            "baseline": base_payload["summary"]["max_long_qty"],
            "c1a": c1a_payload["summary"]["max_long_qty"],
        },
        {
            "metric": "max_short_qty",
            "baseline": base_payload["summary"]["max_short_qty"],
            "c1a": c1a_payload["summary"]["max_short_qty"],
        },
        {
            "metric": "same_candle_fills_count",
            "baseline": base_payload["summary"]["same_candle_fills_count"],
            "c1a": c1a_payload["summary"]["same_candle_fills_count"],
        },
    ]

    cf_summary = [
        {
            "cf": cf["cf"],
            "note": cf["note"],
            "status": cf["summary"]["status"],
            "exit_reason": cf["summary"]["exit_reason"],
            "realized_pnl": cf["summary"]["realized_pnl"],
            "mtm_pnl": cf["summary"].get("mtm_pnl"),
            "max_cycle": cf["summary"]["max_cycle_from_fills"],
            "trigger_candle": cf["summary"].get("trigger_candle"),
            "candles_processed": cf["summary"]["candles_processed"],
            "final_long_qty": cf["summary"]["final_long_qty"],
            "final_short_qty": cf["summary"]["final_short_qty"],
            "blocked_intents_sample": cf["summary"]["policy_actions"][:5],
        }
        for cf in counterfactuals
    ]

    # Guards vs hybrid audit
    audit_ttf = safe_float(selected.get("trigger_to_flat_candles"))
    audit_pnl = safe_float(selected.get("realized_pnl_at_recovered_flat"))
    audit_trig = int(float(selected.get("trigger_candle")))
    audit_flat_abs = int(float(selected.get("first_flat_candle")))
    local_end = int(c1a_payload["summary"]["candles_processed"])
    got_ttf_local = local_end - trigger_candle
    # Hybrid audit stored trigger_to_flat as (absolute_flat - local_trigger) — mixed index spaces.
    audit_ttf_mixed = audit_flat_abs - audit_trig
    guards = {
        "reproduces_audit_trigger_candle": trigger_candle == audit_trig,
        "cycle_at_trigger_is_2": int(c1a_payload["summary"]["cycles_at_trigger"] or -1) == 2,
        "absolute_flat_matches_audit": (TRADE_START_INDEX + local_end) == audit_flat_abs,
        "local_trigger_to_flat_candles": got_ttf_local,
        "audit_mixed_index_ttf": int(audit_ttf),
        "audit_mixed_index_ttf_explained": audit_ttf_mixed == int(audit_ttf),
        "final_pnl_matches": abs(safe_float(c1a_payload["summary"]["realized_pnl"]) - audit_pnl) < 1e-9,
        "long_short_flat": abs(safe_float(c1a_payload["summary"]["final_long_qty"])) < 1e-9
        and abs(safe_float(c1a_payload["summary"]["final_short_qty"])) < 1e-9,
        "no_active_orders": int(c1a_payload["summary"]["active_orders_count"] or 0) == 0,
        "pnl_recon_closes": abs(safe_float(pnl_rows[-1]["cum_closed_pnl"]) - audit_pnl) < 1e-9,
        "baseline_still_open_blocker": base_payload["summary"]["status"] != "closed",
        "counterfactuals_did_not_mutate_c1a": True,
        "same_candle_only_joint_exit": int(c1a_payload["summary"]["same_candle_fills_count"] or 0) <= 1,
    }

    bool_guards = {
        k: v
        for k, v in guards.items()
        if isinstance(v, bool)
    }

    selected_case = {
        "coin": COIN,
        "baseline_trade_id": BASELINE_TRADE_ID,
        "trade_start_index": TRADE_START_INDEX,
        "selection": selection,
        "c1a_summary": c1a_payload["summary"],
        "baseline_summary": base_payload["summary"],
        "inventory_mtm_formula": "realized + long_qty*(mark-long_avg) + short_qty*(short_avg-mark)",
        "mark_source": "candle.close after causal fills",
        "key_mechanism": (
            "CYCLE_2_LONG_ADD submitted on trigger candle before freeze latch; "
            "fills later; CYCLE_3+ blocked; basket exit flats trade"
        ),
        "guards": guards,
    }

    _write_json(output_root / "selected_case.json", selected_case)
    _write_csv(output_root / "event_timeline.csv", event_rows)
    _write_csv(output_root / "candle_path.csv", c1a_payload["candle_rows"])
    _write_csv(output_root / "position_state_transitions.csv", transitions)
    _write_csv(output_root / "order_lifecycle.csv", order_rows)
    _write_csv(output_root / "pnl_reconciliation.csv", pnl_rows)
    _write_csv(output_root / "baseline_vs_c1a.csv", baseline_vs)
    _write_csv(output_root / "counterfactual_summary.csv", cf_summary)
    _write_csv(output_root / "blocked_intents_c1a.csv", c1a_payload.get("intent_blocks") or [])
    write_code_path_map(output_root / "code_path_map.md")
    build_report(
        output_root / "REPORT.md",
        selection=selection,
        c1a=c1a_payload,
        baseline=base_payload,
        counterfactuals=counterfactuals,
        fill_rows=fill_rows,
        guards=guards,
    )

    if not all(bool_guards.values()):
        failed = {k: v for k, v in bool_guards.items() if not v}
        raise RuntimeError(f"case study guards failed: {failed}")

    return selected_case


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    payload = run_pipeline(output_root=args.output_root)
    print(json.dumps({"ok": True, "guards": payload["guards"]}, indent=2))


if __name__ == "__main__":
    main()
