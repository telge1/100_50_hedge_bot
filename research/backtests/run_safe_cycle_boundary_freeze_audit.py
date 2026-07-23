"""Research-only Safe Cycle Boundary Freeze audit (S0–S3).

Runs the 27 original baseline blockers under B1 terminal-stop for variants:

* S0 — exact C1a parity (A1 @ mtm < -0.50, immediate cycle block)
* S1 — PENDING → ACTIVE safe boundary; block next first-leg opener only
* S2 — stop after complete Cycle 1
* S3 — stop after complete Cycle 2

Also evaluates impact on the 238 baseline closed trades (no full portfolio rotation).

Hard constraints: no live/config/runtime edits; never overwrite prior result dirs;
never commit; INJUSDT T8 remains a separate undercoverage marker.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.backtests.backtest_report import BacktestResult
from research.backtests.blocker_recovery_trigger_policy import terminal_recovery_config
from research.backtests.candle_loader import load_candles_for_symbol
from research.backtests.continuous_reentry_backtest import run_continuous_reentry_backtests
from research.backtests.historical_backtest import normalize_candles
from research.backtests.inventory_mtm_freeze import (
    InventoryMtmFreezeConfig,
    is_injusdt_trade8_undercoverage,
    parse_cycle_number,
)
from research.backtests.long_add_multistart_metrics import analyze_trade, normalize_trade_status, safe_float
from research.backtests.recovery_reentry_policy import (
    baseline_blocker_trade_number_by_coin,
    load_baseline_blockers,
)
from research.backtests.run_inventory_mtm_neg1_policy_audit import (
    BASELINE_DIR,
    CONFIG_SOURCE,
    CONTINUOUS_START_INDEX,
    DIRECTION,
    FILL_MODEL,
    FULL_HISTORY_CANDLE_LIMIT,
    LONG_FILL_DISTANCE_PCT,
    TARGET_PROFIT_USDT,
    TP_PROFIT_TARGET_PCT,
    load_baseline_coin_list,
)
from research.backtests.safe_cycle_boundary_freeze import detect_invalid_partial_cycle
from research.backtests.safe_cycle_boundary_policy import (
    C1A_MTM_TOLERANCE,
    C1A_REFERENCE_BLOCKERS,
    C1A_REFERENCE_CLOSED,
    C1A_REFERENCE_RECOVERED,
    C1A_REFERENCE_SERIES_MTM,
    C1A_REFERENCE_TRADES,
    SafeBoundaryVariantSpec,
    build_s0_s3_specs,
)

ROOT = Path(__file__).resolve().parents[2]
HYBRID_DIR = ROOT / "research/backtests/results/blocker_recovery_trigger_and_hybrid_audit_20260720"
PLAN_DIR = ROOT / "research/backtests/results/safe_cycle_boundary_freeze_plan_20260720"
DEFAULT_OUT = ROOT / "research/backtests/results/safe_cycle_boundary_freeze_audit_20260720"
PROTECTED_DIRS = (
    BASELINE_DIR,
    HYBRID_DIR,
    PLAN_DIR,
    ROOT / "research/backtests/results/inventory_mtm_neg1_policy_audit_20260720",
    ROOT / "research/backtests/results/inventory_mtm_neg1_recovery_reentry_audit_20260720",
    ROOT / "research/backtests/results/c1a_single_blocker_recovery_case_study_20260720",
)

PENDING_ALLOW_ACTIONS = frozenset(
    {
        "second_leg_allowed_while_pending",
        "staged_second_leg_allowed_while_pending",
        "refill_allowed_while_pending",
        "coverage_allowed_while_pending",
        "exit_rebuild_allowed_while_pending",
    }
)


def _git_status() -> dict[str, Any]:
    status: dict[str, Any] = {"commit": None, "dirty": None, "status_porcelain": ""}
    try:
        status["commit"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        porcelain = subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True)
        status["dirty"] = bool(porcelain.strip())
        status["status_porcelain"] = porcelain
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        status["error"] = str(exc)
    return status


def _write_csv(path: Path, rows: list[dict[str, Any]], *, fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(fieldnames) if fieldnames else []
    seen = set(fields)
    if not fields:
        for row in rows:
            for key in row:
                if key not in seen:
                    seen.add(key)
                    fields.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in fields})


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    return float(statistics.median(values))


def _config_to_dict(cfg: InventoryMtmFreezeConfig) -> dict[str, Any]:
    return {field: getattr(cfg, field) for field in cfg.__dataclass_fields__}  # type: ignore[attr-defined]


def load_baseline_closed_trades(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            rows.append(row)
    return rows


def build_call_kwargs(
    *,
    symbol: str,
    candles: list[Any],
    inventory_mtm_freeze_config: InventoryMtmFreezeConfig,
    target_blocker_trade_number: int,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "direction": DIRECTION,
        "candles": candles,
        "continuous_start_index": CONTINUOUS_START_INDEX,
        "config_source": CONFIG_SOURCE,
        "fill_model": FILL_MODEL,
        "tp_profit_target_pct": TP_PROFIT_TARGET_PCT,
        "long_fill_distance_pct": LONG_FILL_DISTANCE_PCT,
        "target_profit_usdt": TARGET_PROFIT_USDT,
        "inventory_mtm_freeze_config": inventory_mtm_freeze_config,
        "recovery_reentry_config": terminal_recovery_config(
            target_blocker_trade_number=target_blocker_trade_number
        ),
        "write_json": False,
        "write_csv": False,
    }


def build_trade_row(
    *,
    coin: str,
    variant: str,
    result: BacktestResult,
    candles: list[Any],
    target_blocker_trade_number: int,
    baseline_closed_by_trade: dict[tuple[str, int], dict[str, Any]],
) -> dict[str, Any]:
    start_index = int(result.start_index or 0)
    window = candles[start_index:]
    analysis = analyze_trade(
        result,
        variant=variant,
        long_add_pct=LONG_FILL_DISTANCE_PCT,
        target_profit_usdt=TARGET_PROFIT_USDT,
        window_candles=window,
        valid=True,
        skip_reason="ok",
    )
    status = normalize_trade_status(result)
    trade_number = int(result.trade_number or 0)
    excerpt = dict(result.final_strategy_state_excerpt or {})
    trigger_event = excerpt.get("inventory_mtm_trigger_event") or {}
    freeze_state = excerpt.get("inventory_mtm_freeze_state") or {}
    policy_actions = list(excerpt.get("inventory_mtm_policy_actions") or [])
    safe_boundary = dict(freeze_state.get("safe_boundary") or {})
    strategy_excerpt = dict(excerpt.get("strategy_state") or excerpt)

    invalid_partial = int(
        detect_invalid_partial_cycle(strategy_excerpt)
        if status != "closed"
        else False
    )

    baseline = baseline_closed_by_trade.get((coin, trade_number))
    baseline_status = "closed" if baseline is not None else (
        "blocker" if trade_number == target_blocker_trade_number else "other"
    )
    baseline_pnl = safe_float(baseline.get("mtm_pnl")) if baseline else None

    return {
        "coin": coin,
        "variant": variant,
        "baseline_trade_id": trade_number,
        "baseline_status": baseline_status,
        "target_blocker_trade_number": target_blocker_trade_number,
        "trade_number": trade_number,
        "direction": DIRECTION,
        "start_index": start_index,
        "end_index": result.end_index,
        "status": status,
        "is_blocker": int(status != "closed"),
        "duration_candles": int(result.candles_processed or 0),
        "realized_pnl": analysis.get("realized_pnl"),
        "unrealized_pnl": analysis.get("unrealized_pnl"),
        "mtm_pnl": analysis.get("mtm_pnl"),
        "final_pnl_or_mtm": analysis.get("mtm_pnl"),
        "max_cycle": analysis.get("max_cycle"),
        "max_cycle_seen": analysis.get("max_cycle"),
        "final_long_qty": analysis.get("final_long_qty"),
        "final_short_qty": analysis.get("final_short_qty"),
        "undercoverage": analysis.get("undercoverage"),
        "exit_reason": result.exit_reason,
        "injusdt_trade8_marker": int(
            is_injusdt_trade8_undercoverage(coin=coin, trade_number=trade_number)
        ),
        "trigger_fired": bool(trigger_event),
        "trigger_candle": trigger_event.get("trigger_candle"),
        "trigger_cycle": trigger_event.get("cycles_at_trigger", freeze_state.get("cycles_at_trigger")),
        "trigger_mtm": trigger_event.get("trigger_mtm"),
        "freeze_pending_candle": safe_boundary.get("freeze_requested_at_candle"),
        "freeze_active_candle": safe_boundary.get("freeze_activated_at_candle"),
        "freeze_active_after_cycle": safe_boundary.get("freeze_activated_after_cycle"),
        "freeze_state": safe_boundary.get("freeze_state") or (
            "active" if freeze_state.get("cycle_freeze_enabled") else ("normal" if not trigger_event else "legacy_active")
        ),
        "exit_signature_at_activation": safe_boundary.get("exit_signature_at_activation"),
        "blocked_opener_count": safe_boundary.get("blocked_opener_count") or 0,
        "blocked_opener_purposes": list(safe_boundary.get("blocked_opener_purposes") or []),
        "safe_boundary_reason": safe_boundary.get("safe_boundary_reason"),
        "recovered_flat_of_target_blocker": bool(excerpt.get("recovered_flat_of_target_blocker")),
        "first_flat_candle_absolute": excerpt.get("first_flat_candle_absolute"),
        "research_terminal_reason": excerpt.get("research_terminal_reason"),
        "baseline_closed_pnl": baseline_pnl,
        "invalid_partial_cycle": invalid_partial,
        "policy_actions": policy_actions,
        "trigger_event": trigger_event,
        "freeze_state_raw": freeze_state,
        "safe_boundary": safe_boundary,
    }


def enrich_blocker_trade_level(row: dict[str, Any]) -> dict[str, Any]:
    """Flatten trade-level export fields required by the plan."""
    actions = list(row.get("policy_actions") or [])
    blocked = [a for a in actions if a.get("action") == "next_cycle_opener_blocked"]
    first_blocked = blocked[0] if blocked else {}
    trigger_candle = row.get("trigger_candle")
    flat_candle = row.get("first_flat_candle_absolute")
    active_candle = row.get("freeze_active_candle")
    trigger_to_flat = None
    activation_to_flat = None
    if trigger_candle is not None and flat_candle is not None:
        trigger_to_flat = int(flat_candle) - int(trigger_candle)
    if active_candle is not None and flat_candle is not None:
        activation_to_flat = int(flat_candle) - int(active_candle)

    second_leg_purposes = sorted(
        {
            str(a.get("purpose"))
            for a in actions
            if str(a.get("action") or "").endswith("second_leg_allowed_while_pending")
            and a.get("purpose")
        }
    )
    return {
        **row,
        "first_leg_purpose": None,
        "first_leg_filled": None,
        "second_leg_purposes": "|".join(second_leg_purposes),
        "second_leg_complete": int(
            any(a.get("action") == "cycle_complete_confirmed" for a in actions)
        ),
        "staged_complete": int(
            any(a.get("action") == "staged_second_leg_allowed_while_pending" for a in actions)
        ),
        "refill_complete": int(
            any(a.get("action") == "refill_allowed_while_pending" for a in actions)
        ),
        "coverage_complete": int(
            any(a.get("action") == "coverage_allowed_while_pending" for a in actions)
        ),
        "exit_signature_before_activation": None,
        "blocked_opener_purpose": first_blocked.get("purpose"),
        "blocked_opener_cycle": first_blocked.get("cycle")
        or parse_cycle_number(str(first_blocked.get("purpose") or "")),
        "final_status": row.get("status"),
        "trigger_to_flat_candles": trigger_to_flat,
        "activation_to_flat_candles": activation_to_flat,
        "undercoverage": row.get("undercoverage"),
    }


def summarize_variant(
    *,
    variant: str,
    trade_rows: list[dict[str, Any]],
    outcome_rows: list[dict[str, Any]],
    policy_actions: list[dict[str, Any]],
    closed_impact_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    recovered = [r for r in outcome_rows if int(r.get("recovered") or 0) == 1]
    unrecovered = [r for r in outcome_rows if int(r.get("recovered") or 0) == 0]
    pending_actions = [a for a in policy_actions if a.get("action") in PENDING_ALLOW_ACTIONS]
    blocked = [a for a in policy_actions if a.get("action") == "next_cycle_opener_blocked"]
    activated = [a for a in policy_actions if a.get("action") == "freeze_activated"]
    requested = [a for a in policy_actions if a.get("action") == "freeze_requested"]
    pending_entered = [a for a in policy_actions if a.get("action") == "freeze_pending_entered"]

    opener_cycles = Counter()
    for a in blocked:
        cyc = a.get("cycle")
        if cyc is None:
            cyc = parse_cycle_number(str(a.get("purpose") or ""))
        if cyc is not None:
            opener_cycles[int(cyc)] += 1

    trigger_to_flat = [
        safe_float(r.get("trigger_to_flat_candles"))
        for r in recovered
        if r.get("trigger_to_flat_candles") not in (None, "")
    ]
    activation_to_flat = [
        safe_float(r.get("activation_to_flat_candles"))
        for r in recovered
        if r.get("activation_to_flat_candles") not in (None, "")
    ]
    flat_pnls = [safe_float(r.get("realized_pnl_at_recovered_flat")) for r in recovered]
    series_mtm = sum(safe_float(r.get("mtm_pnl")) for r in trade_rows)
    never_activated = sum(
        1
        for r in outcome_rows
        if int(r.get("trigger_fired") or 0) == 1 and r.get("freeze_active_candle") in (None, "")
        and variant != "S0"
    )
    # S0 has no pending/active machine — treat legacy cycle freeze as activated.
    if variant == "S0":
        never_activated = 0

    damaged = [r for r in closed_impact_rows if int(r.get("damaged") or 0) == 1]
    still_pos = [r for r in closed_impact_rows if int(r.get("baseline_closed_still_positive") or 0) == 1]
    became_open = [r for r in closed_impact_rows if int(r.get("baseline_closed_became_open") or 0) == 1]
    became_neg = [r for r in closed_impact_rows if int(r.get("baseline_closed_became_negative") or 0) == 1]

    return {
        "variant": variant,
        "original_blockers": len(outcome_rows),
        "trigger_requested_count": len(requested) if variant != "S0" else sum(
            1 for r in outcome_rows if int(r.get("trigger_fired") or 0) == 1
        ),
        "freeze_pending_count": len(pending_entered) if variant != "S0" else 0,
        "freeze_active_count": len(activated)
        if variant != "S0"
        else sum(1 for r in outcome_rows if int(r.get("trigger_fired") or 0) == 1),
        "freeze_never_activated_count": never_activated,
        "blocked_next_cycle_opener_count": len(blocked)
        if variant != "S0"
        else sum(1 for a in policy_actions if a.get("action") == "block_new_cycle"),
        "blocked_opener_cycle_distribution": dict(sorted(opener_cycles.items())),
        "second_leg_orders_allowed_after_trigger": sum(
            1 for a in pending_actions if a.get("action") == "second_leg_allowed_while_pending"
        ),
        "staged_second_leg_orders_allowed": sum(
            1 for a in pending_actions if a.get("action") == "staged_second_leg_allowed_while_pending"
        ),
        "refills_allowed_after_trigger": sum(
            1 for a in pending_actions if a.get("action") == "refill_allowed_while_pending"
        ),
        "coverage_actions_allowed_after_trigger": sum(
            1 for a in pending_actions if a.get("action") == "coverage_allowed_while_pending"
        ),
        "exit_rebuilds_after_trigger": sum(
            1
            for a in policy_actions
            if a.get("action") in {"exit_rebuild_committed", "exit_rebuild_allowed_while_pending"}
        ),
        "recovered_flat_count": len(recovered),
        "positive_recovered_count": sum(1 for p in flat_pnls if p > 1e-9),
        "negative_recovered_count": sum(1 for p in flat_pnls if p < -1e-9),
        "unrecovered_count": len(unrecovered),
        "realized_recovery_pnl": sum(flat_pnls),
        "final_open_mtm": sum(safe_float(r.get("final_mtm")) for r in unrecovered),
        "terminal_series_mtm": series_mtm,
        "median_trigger_to_flat_candles": _median(trigger_to_flat),
        "median_activation_to_flat_candles": _median(activation_to_flat),
        "maximum_cycle_reached": max(
            (int(safe_float(r.get("max_cycle")) or 0) for r in trade_rows), default=0
        ),
        "invalid_partial_cycle_count": sum(int(r.get("invalid_partial_cycle") or 0) for r in trade_rows),
        "undercoverage_count": sum(int(safe_float(r.get("undercoverage")) or 0) for r in trade_rows),
        "pending_cycle_loss_at_run_end": sum(
            max(0.0, -safe_float(r.get("final_mtm"))) for r in unrecovered
        ),
        "damaged_baseline_closed_count": len(damaged),
        "baseline_closed_still_positive": len(still_pos),
        "baseline_closed_became_open": len(became_open),
        "baseline_closed_became_negative": len(became_neg),
        "closed_pnl_delta_vs_baseline": sum(
            safe_float(r.get("closed_pnl_delta_vs_baseline")) for r in closed_impact_rows
        ),
        "trades": len(trade_rows),
        "closed": sum(1 for r in trade_rows if not r.get("is_blocker")),
        "blockers": sum(1 for r in trade_rows if r.get("is_blocker")),
        "recovery_rate": (len(recovered) / len(outcome_rows)) if outcome_rows else 0.0,
    }


def build_closed_impact(
    *,
    variant: str,
    trade_rows: list[dict[str, Any]],
    baseline_closed: list[dict[str, Any]],
    coins: list[str] | None = None,
) -> list[dict[str, Any]]:
    by_key = {(r["coin"], int(r["trade_number"])): r for r in trade_rows}
    coin_set = set(coins) if coins is not None else {r["coin"] for r in trade_rows}
    out: list[dict[str, Any]] = []
    for base in baseline_closed:
        coin = str(base.get("coin") or "")
        if coin not in coin_set:
            continue
        trade_number = int(base.get("trade_number") or 0)
        base_pnl = safe_float(base.get("mtm_pnl"))
        row = by_key.get((coin, trade_number))
        if row is None:
            # Trade never opened under variant (terminal stop earlier on prior trade).
            out.append(
                {
                    "variant": variant,
                    "coin": coin,
                    "baseline_trade_id": trade_number,
                    "baseline_status": "closed",
                    "baseline_pnl": base_pnl,
                    "variant_status": "not_opened",
                    "variant_pnl_or_mtm": None,
                    "damaged": 1 if base_pnl > 1e-9 else 0,
                    "baseline_closed_still_positive": 0,
                    "baseline_closed_became_open": 0,
                    "baseline_closed_became_negative": 0,
                    "closed_pnl_delta_vs_baseline": -base_pnl if base_pnl > 0 else 0.0,
                    "note": "missing_under_variant_series",
                }
            )
            continue
        status = str(row.get("status") or "")
        pnl = safe_float(row.get("mtm_pnl"))
        became_open = status != "closed"
        became_neg = (not became_open) and pnl < -1e-9 and base_pnl >= 0
        still_pos = (not became_open) and pnl > 1e-9
        damaged = int(became_open or became_neg or (pnl + 1e-9 < base_pnl and base_pnl > 1e-9))
        out.append(
            {
                "variant": variant,
                "coin": coin,
                "baseline_trade_id": trade_number,
                "baseline_status": "closed",
                "baseline_pnl": base_pnl,
                "variant_status": status,
                "variant_pnl_or_mtm": pnl,
                "damaged": damaged,
                "baseline_closed_still_positive": int(still_pos),
                "baseline_closed_became_open": int(became_open),
                "baseline_closed_became_negative": int(became_neg),
                "closed_pnl_delta_vs_baseline": pnl - base_pnl,
                "note": "",
            }
        )
    return out


def build_blocker_outcome(
    *,
    coin: str,
    variant: str,
    rows: list[dict[str, Any]],
    target_blocker_trade_number: int,
    baseline_mtm: float | None,
) -> dict[str, Any]:
    target_rows = [r for r in rows if int(r["trade_number"]) == int(target_blocker_trade_number)]
    target = enrich_blocker_trade_level(target_rows[0]) if target_rows else {}
    recovered = any(r.get("recovered_flat_of_target_blocker") for r in rows)
    first_flat = next((r for r in rows if r.get("recovered_flat_of_target_blocker")), None)
    trigger_candle = target.get("trigger_candle")
    flat_candle = (first_flat or {}).get("first_flat_candle_absolute")
    active_candle = target.get("freeze_active_candle")
    trigger_to_flat = None
    activation_to_flat = None
    if trigger_candle is not None and flat_candle is not None:
        trigger_to_flat = int(flat_candle) - int(trigger_candle)
    if active_candle is not None and flat_candle is not None:
        activation_to_flat = int(flat_candle) - int(active_candle)
    return {
        "variant": variant,
        "coin": coin,
        "baseline_trade_id": target_blocker_trade_number,
        "baseline_final_mtm": baseline_mtm,
        "recovered": int(recovered),
        "trigger_fired": int(bool(target.get("trigger_fired"))),
        "trigger_candle": trigger_candle,
        "trigger_cycle": target.get("trigger_cycle"),
        "freeze_pending_candle": target.get("freeze_pending_candle"),
        "freeze_active_candle": active_candle,
        "freeze_active_after_cycle": target.get("freeze_active_after_cycle"),
        "freeze_state": target.get("freeze_state"),
        "blocked_opener_purpose": target.get("blocked_opener_purpose"),
        "blocked_opener_cycle": target.get("blocked_opener_cycle"),
        "max_cycle_seen": target.get("max_cycle_seen"),
        "first_flat_candle": flat_candle,
        "trigger_to_flat_candles": trigger_to_flat,
        "activation_to_flat_candles": activation_to_flat,
        "realized_pnl_at_recovered_flat": (first_flat or {}).get("realized_pnl") if recovered else None,
        "final_status": target.get("status"),
        "final_pnl_or_mtm": target.get("mtm_pnl"),
        "invalid_partial_cycle": target.get("invalid_partial_cycle"),
        "undercoverage": target.get("undercoverage"),
        "exit_signature_at_activation": target.get("exit_signature_at_activation"),
        "second_leg_purposes": target.get("second_leg_purposes"),
        "injusdt_trade8_marker": target.get("injusdt_trade8_marker"),
        "research_terminal_reason": next(
            (r.get("research_terminal_reason") for r in reversed(rows) if r.get("research_terminal_reason")),
            None,
        ),
        "series_mtm": sum(safe_float(r.get("mtm_pnl")) for r in rows),
    }


def check_s0_parity(summary: dict[str, Any]) -> dict[str, Any]:
    checks = {
        "recovered": (
            summary["recovered_flat_count"],
            C1A_REFERENCE_RECOVERED,
            summary["recovered_flat_count"] == C1A_REFERENCE_RECOVERED,
        ),
        "series_mtm": (
            summary["terminal_series_mtm"],
            C1A_REFERENCE_SERIES_MTM,
            abs(summary["terminal_series_mtm"] - C1A_REFERENCE_SERIES_MTM) <= C1A_MTM_TOLERANCE,
        ),
        "trades": (
            summary["trades"],
            C1A_REFERENCE_TRADES,
            summary["trades"] == C1A_REFERENCE_TRADES,
        ),
        "closed": (
            summary["closed"],
            C1A_REFERENCE_CLOSED,
            summary["closed"] == C1A_REFERENCE_CLOSED,
        ),
        "blockers": (
            summary["blockers"],
            C1A_REFERENCE_BLOCKERS,
            summary["blockers"] == C1A_REFERENCE_BLOCKERS,
        ),
        "negative_recovered": (
            summary["negative_recovered_count"],
            0,
            summary["negative_recovered_count"] == 0,
        ),
        "invalid_partial": (
            summary["invalid_partial_cycle_count"],
            0,
            summary["invalid_partial_cycle_count"] == 0,
        ),
    }
    return {"ok": all(c[2] for c in checks.values()), "checks": checks}


def run_one_variant(
    *,
    spec: SafeBoundaryVariantSpec,
    coins: list[str],
    coin_candles: dict[str, list[Any]],
    target_map: dict[str, int],
    baseline_blocker_by_coin: dict[str, dict[str, Any]],
    baseline_closed: list[dict[str, Any]],
    baseline_closed_by_trade: dict[tuple[str, int], dict[str, Any]],
) -> dict[str, Any]:
    trade_rows: list[dict[str, Any]] = []
    outcome_rows: list[dict[str, Any]] = []
    policy_actions: list[dict[str, Any]] = []
    freeze_events: list[dict[str, Any]] = []
    allowed_pending: list[dict[str, Any]] = []
    blocked_openers: list[dict[str, Any]] = []
    boundary_checks: list[dict[str, Any]] = []
    invalid_partials: list[dict[str, Any]] = []
    trade_level: list[dict[str, Any]] = []

    for symbol in coins:
        candles = coin_candles[symbol]
        target = int(target_map.get(symbol, -1))
        print(f"[{spec.name}] {symbol} target_blocker={target}", flush=True)
        payload = run_continuous_reentry_backtests(
            **build_call_kwargs(
                symbol=symbol,
                candles=candles,
                inventory_mtm_freeze_config=spec.freeze_config,
                target_blocker_trade_number=target,
            )
        )
        results = list(payload["results"])
        coin_rows: list[dict[str, Any]] = []
        for result in results:
            row = build_trade_row(
                coin=symbol,
                variant=spec.name,
                result=result,
                candles=candles,
                target_blocker_trade_number=target,
                baseline_closed_by_trade=baseline_closed_by_trade,
            )
            coin_rows.append(row)
            trade_rows.append(row)
            enriched = enrich_blocker_trade_level(row)
            if int(row["trade_number"]) == target:
                trade_level.append(enriched)
            if int(row.get("invalid_partial_cycle") or 0):
                invalid_partials.append(
                    {
                        "variant": spec.name,
                        "coin": symbol,
                        "trade_number": row["trade_number"],
                        "status": row["status"],
                        "max_cycle": row.get("max_cycle"),
                        "freeze_state": row.get("freeze_state"),
                    }
                )
            for action in row.get("policy_actions") or []:
                enriched_action = {
                    "coin": symbol,
                    "variant": spec.name,
                    "trade_number": row["trade_number"],
                    **action,
                }
                policy_actions.append(enriched_action)
                act = str(action.get("action") or "")
                if act in {
                    "freeze_requested",
                    "freeze_pending_entered",
                    "freeze_activated",
                    "cycle_complete_confirmed",
                    "exit_rebuild_committed",
                    "flat_reached",
                    "terminal_coin_stop",
                    "current_cycle_first_leg_seen",
                    "next_cycle_opener_blocked",
                    "trigger_fired",
                    "block_new_cycle",
                }:
                    freeze_events.append(enriched_action)
                if act in PENDING_ALLOW_ACTIONS:
                    allowed_pending.append(enriched_action)
                if act in {"next_cycle_opener_blocked", "block_new_cycle"}:
                    blocked_openers.append(
                        {
                            **enriched_action,
                            "cycle_number": action.get("cycle")
                            or parse_cycle_number(str(action.get("purpose") or "")),
                            "freeze_state": row.get("freeze_state"),
                            "completed_cycle": row.get("freeze_active_after_cycle"),
                            "reason": action.get("reason") or act,
                        }
                    )
            sb = row.get("safe_boundary") or {}
            if sb:
                boundary_checks.append(
                    {
                        "variant": spec.name,
                        "coin": symbol,
                        "trade_number": row["trade_number"],
                        "freeze_state": sb.get("freeze_state"),
                        "freeze_requested_cycle": sb.get("freeze_requested_cycle"),
                        "freeze_activated_after_cycle": sb.get("freeze_activated_after_cycle"),
                        "safe_boundary_reason": sb.get("safe_boundary_reason"),
                        "exit_signature_at_activation": sb.get("exit_signature_at_activation"),
                        "blocked_opener_count": sb.get("blocked_opener_count"),
                        "allowed_current_cycle_action_count": sb.get(
                            "allowed_current_cycle_action_count"
                        ),
                    }
                )

        baseline_mtm = None
        if symbol in baseline_blocker_by_coin:
            baseline_mtm = safe_float(baseline_blocker_by_coin[symbol].get("mtm_pnl"))
        outcome_rows.append(
            build_blocker_outcome(
                coin=symbol,
                variant=spec.name,
                rows=coin_rows,
                target_blocker_trade_number=target,
                baseline_mtm=baseline_mtm,
            )
        )

    closed_impact = build_closed_impact(
        variant=spec.name,
        trade_rows=trade_rows,
        baseline_closed=baseline_closed,
        coins=coins,
    )
    summary = summarize_variant(
        variant=spec.name,
        trade_rows=trade_rows,
        outcome_rows=outcome_rows,
        policy_actions=policy_actions,
        closed_impact_rows=closed_impact,
    )
    return {
        "spec": spec,
        "summary": summary,
        "trade_rows": trade_rows,
        "outcome_rows": outcome_rows,
        "policy_actions": policy_actions,
        "freeze_events": freeze_events,
        "allowed_pending": allowed_pending,
        "blocked_openers": blocked_openers,
        "boundary_checks": boundary_checks,
        "invalid_partials": invalid_partials,
        "closed_impact": closed_impact,
        "trade_level": trade_level,
    }


def short_opener_mapping_rows() -> list[dict[str, Any]]:
    from research.backtests.inventory_mtm_freeze import is_new_cycle_open_purpose
    from research.backtests.safe_cycle_boundary_freeze import (
        is_direction_aware_cycle_opener,
        legacy_short_opener_bug_would_match,
    )

    cases = [
        ("long", "CYCLE_2_LONG_ADD"),
        ("long", "CYCLE_2_SHORT_REDUCE"),
        ("short", "CYCLE_2_SHORT_REDUCE"),
        ("short", "CYCLE_2_SHORT_ADD"),
        ("short", "CYCLE_2_LONG_REDUCE"),
    ]
    rows = []
    for primary, purpose in cases:
        rows.append(
            {
                "primary_side": primary,
                "purpose": purpose,
                "legacy_is_new_cycle_open_purpose": int(
                    is_new_cycle_open_purpose(purpose, primary_side=primary)
                ),
                "direction_aware_is_opener": int(
                    is_direction_aware_cycle_opener(purpose, primary_side=primary)
                ),
                "legacy_short_add_bug_match": int(legacy_short_opener_bug_would_match(purpose)),
                "correct_short_opener_is_short_reduce": int(
                    primary == "short" and purpose.endswith("SHORT_REDUCE")
                ),
            }
        )
    return rows


def write_report(
    path: Path,
    *,
    summaries: list[dict[str, Any]],
    s0_parity: dict[str, Any],
    hooks_used: list[str],
) -> None:
    by = {s["variant"]: s for s in summaries}
    s0, s1, s2, s3 = by.get("S0", {}), by.get("S1", {}), by.get("S2", {}), by.get("S3", {})
    best = max(summaries, key=lambda r: safe_float(r.get("terminal_series_mtm")))
    lines = [
        "# Safe Cycle Boundary Freeze Audit (S0–S3)",
        "",
        f"Generated: `{datetime.now(timezone.utc).isoformat()}`",
        "",
        "## Setup",
        "",
        "- Corpus: 27 original baseline blockers + guard on 238 baseline closed trades.",
        "- Terminal stop: B1 after recovered flat of the original blocker.",
        "- Research-only; no live/config/runtime changes; no commit.",
        "- INJUSDT T8 remains a separate undercoverage marker.",
        "",
        "## Hooks actually used",
        "",
    ]
    for h in hooks_used:
        lines.append(f"- `{h}`")
    lines.extend(
        [
            "",
            "## S0 ↔ C1a parity",
            "",
            f"- Result: **{'PASS' if s0_parity['ok'] else 'FAIL'}**",
            "",
            "| check | actual | expected | ok |",
            "|---|---:|---:|:---:|",
        ]
    )
    for name, (actual, expected, ok) in s0_parity["checks"].items():
        lines.append(f"| {name} | {actual} | {expected} | {ok} |")

    lines.extend(
        [
            "",
            "## Variant summary",
            "",
            "| variant | recovered/27 | series_mtm | invalid_partial | blocked_openers | "
            "damaged_closed | median trigger→flat |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in summaries:
        lines.append(
            f"| {row['variant']} | {row['recovered_flat_count']}/27 | "
            f"{safe_float(row['terminal_series_mtm']):.4f} | {row['invalid_partial_cycle_count']} | "
            f"{row['blocked_next_cycle_opener_count']} | {row['damaged_baseline_closed_count']} | "
            f"{row.get('median_trigger_to_flat_candles')} |"
        )

    lines.extend(
        [
            "",
            "## Abschlussfragen",
            "",
            f"1. **Reproduziert S0 C1a exakt?** {'Ja' if s0_parity['ok'] else 'Nein'} "
            f"(recovered={s0.get('recovered_flat_count')}, series_mtm={safe_float(s0.get('terminal_series_mtm')):.4f}).",
            f"2. **Unterscheidet sich S1 wirtschaftlich oder nur semantisch von S0?** "
            f"series_mtm S0={safe_float(s0.get('terminal_series_mtm')):.4f} vs "
            f"S1={safe_float(s1.get('terminal_series_mtm')):.4f}; "
            f"recovered S0={s0.get('recovered_flat_count')} vs S1={s1.get('recovered_flat_count')}.",
            f"3. **Wird in S1 jemals ein laufender Cycle unvollständig gelassen?** "
            f"invalid_partial_cycle_count={s1.get('invalid_partial_cycle_count')} "
            f"(Soll: 0).",
            f"4. **Welche Cycle-Opener werden tatsächlich blockiert?** "
            f"S1 distribution={s1.get('blocked_opener_cycle_distribution')}; "
            f"S2={s2.get('blocked_opener_cycle_distribution')}; "
            f"S3={s3.get('blocked_opener_cycle_distribution')}.",
            "5. **Ist die Short-Spiegelung nach DirectionConfig korrekt?** Ja im Research-Shim "
            "(Opener=`SHORT_REDUCE`; Legacy-Bug `SHORT_ADD` nur dokumentiert/regressiert).",
            f"6. **Wie viele der 27 Blocker werden flat?** "
            f"S1={s1.get('recovered_flat_count')}, S2={s2.get('recovered_flat_count')}, "
            f"S3={s3.get('recovered_flat_count')}.",
            f"7. **Beste terminal Series-MTM?** `{best.get('variant')}` = "
            f"{safe_float(best.get('terminal_series_mtm')):.4f}.",
            f"8. **Recovery-Geschwindigkeit (median trigger→flat):** "
            f"S0={s0.get('median_trigger_to_flat_candles')}, "
            f"S1={s1.get('median_trigger_to_flat_candles')}, "
            f"S2={s2.get('median_trigger_to_flat_candles')}, "
            f"S3={s3.get('median_trigger_to_flat_candles')}.",
            f"9. **Benötigen erfolgreiche Recoveries Cycle 2?** Siehe blocked opener cycles / "
            f"max_cycle in `original_blocker_outcomes.csv` (S2 recovered="
            f"{s2.get('recovered_flat_count')} isoliert Cap-nach-C1).",
            f"10. **Baseline-Gewinner beschädigt?** "
            f"S1={s1.get('damaged_baseline_closed_count')}, "
            f"S2={s2.get('damaged_baseline_closed_count')}, "
            f"S3={s3.get('damaged_baseline_closed_count')} "
            f"(von 238; became_open S1={s1.get('baseline_closed_became_open')}).",
            f"11. **MTM-Trigger besser als Cycle-Cap?** Vergleiche S1 vs S2/S3 series_mtm und recovery.",
            f"12. **Undercoverage / halbfertige Cycles?** "
            f"undercoverage S1={s1.get('undercoverage_count')}, "
            f"invalid_partial S1={s1.get('invalid_partial_cycle_count')}.",
            "13. **Keine Runtime-Empfehlung** — Research-Audit only.",
            "",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--variants", nargs="*", default=["S0", "S1", "S2", "S3"])
    parser.add_argument("--coins", nargs="*", default=None)
    parser.add_argument("--candle-limit", type=int, default=FULL_HISTORY_CANDLE_LIMIT)
    args = parser.parse_args()

    out: Path = args.out
    if out.resolve() in {p.resolve() for p in PROTECTED_DIRS}:
        raise SystemExit(f"Refusing to write into protected directory: {out}")
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"Refusing to overwrite non-empty output directory: {out}")
    out.mkdir(parents=True, exist_ok=True)

    blockers = load_baseline_blockers(BASELINE_DIR / "blocker_trades.csv")
    target_map = baseline_blocker_trade_number_by_coin(blockers)
    baseline_blocker_by_coin = {str(r["coin"]): r for r in blockers}
    coins = list(args.coins) if args.coins else load_baseline_coin_list()
    coins = [c for c in coins if c in target_map]
    baseline_closed = load_baseline_closed_trades(BASELINE_DIR / "closed_trades.csv")
    baseline_closed_by_trade = {
        (str(r["coin"]), int(r["trade_number"])): r for r in baseline_closed
    }

    print(f"[safe-boundary] loading candles for {len(coins)} coins...", flush=True)
    coin_candles: dict[str, list[Any]] = {}
    for symbol in coins:
        coin_candles[symbol] = normalize_candles(
            symbol, load_candles_for_symbol(symbol, limit=int(args.candle_limit))
        )

    specs = [s for s in build_s0_s3_specs() if s.name in set(args.variants)]
    all_summaries: list[dict[str, Any]] = []
    all_outcomes: list[dict[str, Any]] = []
    all_closed_impact: list[dict[str, Any]] = []
    all_freeze_events: list[dict[str, Any]] = []
    all_allowed: list[dict[str, Any]] = []
    all_blocked: list[dict[str, Any]] = []
    all_boundary: list[dict[str, Any]] = []
    all_invalid: list[dict[str, Any]] = []
    all_trade_level: list[dict[str, Any]] = []
    applied: dict[str, Any] = {}
    s0_parity: dict[str, Any] = {"ok": False, "checks": {}}

    for spec in specs:
        result = run_one_variant(
            spec=spec,
            coins=coins,
            coin_candles=coin_candles,
            target_map=target_map,
            baseline_blocker_by_coin=baseline_blocker_by_coin,
            baseline_closed=baseline_closed,
            baseline_closed_by_trade=baseline_closed_by_trade,
        )
        all_summaries.append(result["summary"])
        all_outcomes.extend(result["outcome_rows"])
        all_closed_impact.extend(result["closed_impact"])
        all_freeze_events.extend(result["freeze_events"])
        all_allowed.extend(result["allowed_pending"])
        all_blocked.extend(result["blocked_openers"])
        all_boundary.extend(result["boundary_checks"])
        all_invalid.extend(result["invalid_partials"])
        all_trade_level.extend(result["trade_level"])
        applied[spec.name] = _config_to_dict(spec.freeze_config)
        if spec.name == "S0":
            s0_parity = check_s0_parity(result["summary"])
            print(f"[safe-boundary] S0 parity: {s0_parity['ok']}", flush=True)

    hooks_used = [
        "inventory_mtm_freeze_shim._maybe_fire_trigger (trigger → PENDING for S1)",
        "inventory_mtm_freeze_shim._maybe_arm_stop_after_cycle (S2/S3 arm)",
        "inventory_mtm_freeze_shim._maybe_activate_safe_boundary "
        "(after process_candle + before submit_intents_to_book)",
        "sim.submit_intents_to_book wrap (activation before intent_filter)",
        "sim.intent_filter (PENDING allow-all; ACTIVE DirectionConfig first-leg only)",
        "safe_cycle_boundary_freeze.safe_boundary_ready "
        "(cycle_states[N].complete + last_exit_signature + staged/refill gates)",
        "safe_cycle_boundary_freeze.is_direction_aware_cycle_opener "
        "(LONG_ADD long-primary / SHORT_REDUCE short-primary)",
        "recovery_reentry B1 terminal stop after recovered flat",
    ]

    _write_csv(out / "variant_summary.csv", all_summaries)
    _write_csv(out / "original_blocker_outcomes.csv", all_outcomes)
    _write_csv(out / "baseline_closed_impact.csv", all_closed_impact)
    _write_csv(out / "freeze_events.csv", all_freeze_events)
    _write_csv(out / "allowed_pending_actions.csv", all_allowed)
    _write_csv(out / "blocked_opener_events.csv", all_blocked)
    _write_csv(out / "cycle_boundary_checks.csv", all_boundary)
    _write_csv(out / "invalid_partial_cycles.csv", all_invalid)
    _write_csv(out / "trade_level_export.csv", all_trade_level)
    _write_csv(out / "short_opener_mapping_regression.csv", short_opener_mapping_rows())
    _write_json(
        out / "applied_params.json",
        {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "direction": DIRECTION,
            "config_source": CONFIG_SOURCE,
            "fill_model": FILL_MODEL,
            "long_fill_distance_pct": LONG_FILL_DISTANCE_PCT,
            "target_profit_usdt": TARGET_PROFIT_USDT,
            "tp_profit_target_pct": TP_PROFIT_TARGET_PCT,
            "continuous_start_index": CONTINUOUS_START_INDEX,
            "candle_limit": args.candle_limit,
            "coins": coins,
            "variants": applied,
            "s0_parity": s0_parity,
            "git": _git_status(),
            "hooks_used": hooks_used,
        },
    )
    write_report(out / "REPORT.md", summaries=all_summaries, s0_parity=s0_parity, hooks_used=hooks_used)
    _write_json(
        out / "run_manifest.json",
        {
            "out": str(out),
            "n_coins": len(coins),
            "variants": [s.name for s in specs],
            "s0_parity_ok": s0_parity.get("ok"),
            "invalid_partial_total": sum(
                int(s.get("invalid_partial_cycle_count") or 0) for s in all_summaries
            ),
        },
    )
    print(f"[safe-boundary] wrote {out}", flush=True)


if __name__ == "__main__":
    main()
