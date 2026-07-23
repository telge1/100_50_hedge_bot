"""Research-only blocker recovery trigger + hybrid audit (terminal-stop semantics).

Builds on ``inventory_mtm_neg1_recovery_reentry_audit_20260720`` (B1 finding):
A1 flats 12/27 original blockers with ~+3.31 realized at first flat, but unrecovered
opens keep terminal series MTM at ~-168. This audit diagnoses recovered vs unrecovered
and sweeps directed C0–C5 trigger/hybrid policies **all under B1 terminal stop**
(no post-recovery re-entry).

Hard constraints:

* No live config / runtime / strategy default changes.
* Never writes into protected prior audit directories (read-only).
* Refuses to overwrite a non-empty output directory.
* Causal fills unchanged; only freeze triggers / terminal stop policy differ.
* INJUSDT T8 remains a separate undercoverage marker.
* Never calls ``git commit``.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.backtests.backtest_report import BacktestResult
from research.backtests.blocker_recovery_trigger_policy import (
    A0_SERIES_MTM,
    C0_B1_REFERENCE_BLOCKERS,
    C0_B1_REFERENCE_CLOSED,
    C0_B1_REFERENCE_RECOVERED,
    C0_B1_REFERENCE_SERIES_MTM,
    C0_B1_REFERENCE_TRADES,
    C0_MTM_TOLERANCE,
    FEATURE_COLUMNS,
    PRIOR_B1_RECOVERED_COINS,
    PRIOR_B1_UNRECOVERED_COINS,
    HybridVariantSpec,
    build_c0_c4_specs,
    build_c5_specs,
    group_feature_stats,
    pick_best_c0_c4_candidate,
    rank_separating_features,
    terminal_recovery_config,
)
from research.backtests.candle_loader import load_candles_for_symbol
from research.backtests.continuous_reentry_backtest import run_continuous_reentry_backtests
from research.backtests.historical_backtest import normalize_candles
from research.backtests.inventory_mtm_freeze import InventoryMtmFreezeConfig, is_injusdt_trade8_undercoverage
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

ROOT = Path(__file__).resolve().parents[2]
PRIOR_RECOVERY_AUDIT_DIR = (
    ROOT / "research/backtests/results/inventory_mtm_neg1_recovery_reentry_audit_20260720"
)
PRIOR_POLICY_AUDIT_DIR = ROOT / "research/backtests/results/inventory_mtm_neg1_policy_audit_20260720"
PROTECTED_DIRS = (BASELINE_DIR, PRIOR_RECOVERY_AUDIT_DIR, PRIOR_POLICY_AUDIT_DIR)
DEFAULT_OUT = ROOT / "research/backtests/results/blocker_recovery_trigger_and_hybrid_audit_20260720"


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


def _ts(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


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


def _config_to_dict(cfg: InventoryMtmFreezeConfig) -> dict[str, Any]:
    return {field: getattr(cfg, field) for field in cfg.__dataclass_fields__}  # type: ignore[attr-defined]


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
    baseline_closed_pnl_by_trade: dict[tuple[str, int], float],
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
    is_blocker = int(status != "closed")
    trade_number = int(result.trade_number or 0)
    excerpt = dict(result.final_strategy_state_excerpt or {})
    trigger_event = excerpt.get("inventory_mtm_trigger_event") or {}
    freeze_state = excerpt.get("inventory_mtm_freeze_state") or {}
    policy_actions = excerpt.get("inventory_mtm_policy_actions") or []

    baseline_key = (coin, trade_number)
    baseline_closed_pnl = baseline_closed_pnl_by_trade.get(baseline_key)
    damaged_baseline_closed = 0
    if baseline_closed_pnl is not None and status == "closed":
        if safe_float(analysis.get("realized_pnl")) + 1e-9 < float(baseline_closed_pnl):
            damaged_baseline_closed = 1

    return {
        "coin": coin,
        "variant": variant,
        "target_blocker_trade_number": target_blocker_trade_number,
        "trade_number": trade_number,
        "start_index": start_index,
        "end_index": result.end_index,
        "status": status,
        "is_blocker": is_blocker,
        "duration_candles": int(result.candles_processed or 0),
        "realized_pnl": analysis.get("realized_pnl"),
        "unrealized_pnl": analysis.get("unrealized_pnl"),
        "mtm_pnl": analysis.get("mtm_pnl"),
        "max_cycle": analysis.get("max_cycle"),
        "final_long_qty": analysis.get("final_long_qty"),
        "final_short_qty": analysis.get("final_short_qty"),
        "undercoverage": analysis.get("undercoverage"),
        "exit_reason": result.exit_reason,
        "injusdt_trade8_marker": int(
            is_injusdt_trade8_undercoverage(coin=coin, trade_number=trade_number)
        ),
        "trigger_fired": bool(trigger_event),
        "trigger_candle": trigger_event.get("trigger_candle"),
        "trigger_mtm": trigger_event.get("trigger_mtm"),
        "policy_action_count": len(policy_actions),
        "cycle_freeze_enabled": freeze_state.get("cycle_freeze_enabled"),
        "secondary_trigger_candle": freeze_state.get("secondary_trigger_candle"),
        "secondary_trigger_reason": freeze_state.get("secondary_trigger_reason"),
        "emergency_fired": freeze_state.get("emergency_fired"),
        "emergency_candle": freeze_state.get("emergency_candle"),
        "recovered_flat_of_target_blocker": bool(excerpt.get("recovered_flat_of_target_blocker")),
        "first_flat_candle_absolute": excerpt.get("first_flat_candle_absolute"),
        "research_terminal_reason": excerpt.get("research_terminal_reason"),
        "damaged_baseline_closed_trade": damaged_baseline_closed,
        "freeze_state": freeze_state,
        "trigger_event": trigger_event,
        "policy_actions": policy_actions,
    }


def build_original_blocker_outcome(
    *,
    coin: str,
    variant: str,
    rows: list[dict[str, Any]],
    target_blocker_trade_number: int,
    baseline_mtm: float | None,
) -> dict[str, Any]:
    target_rows = [r for r in rows if int(r["trade_number"]) == int(target_blocker_trade_number)]
    target = target_rows[0] if target_rows else {}
    trigger_event = dict(target.get("trigger_event") or {})
    freeze_state = dict(target.get("freeze_state") or {})
    recovered = any(r.get("recovered_flat_of_target_blocker") for r in rows)
    first_flat = next((r for r in rows if r.get("recovered_flat_of_target_blocker")), None)
    trigger_candle = trigger_event.get("trigger_candle")
    flat_candle = (first_flat or {}).get("first_flat_candle_absolute")
    trigger_to_flat = None
    if trigger_candle is not None and flat_candle is not None:
        trigger_to_flat = int(flat_candle) - int(trigger_candle)

    return {
        "variant": variant,
        "coin": coin,
        "baseline_trade_id": target_blocker_trade_number,
        "baseline_final_mtm": baseline_mtm,
        "recovered": int(recovered),
        "trigger_fired": int(bool(trigger_event)),
        "trigger_candle": trigger_candle,
        "trigger_inventory_mtm": trigger_event.get("trigger_mtm", freeze_state.get("trigger_mtm")),
        "cycle_count_at_trigger": trigger_event.get(
            "cycles_at_trigger", freeze_state.get("cycles_at_trigger")
        ),
        "exit_increase_count_at_trigger": trigger_event.get(
            "exit_increases_at_trigger", freeze_state.get("exit_increases_at_trigger")
        ),
        "long_qty_at_trigger": trigger_event.get("long_qty", freeze_state.get("trigger_long_qty")),
        "short_qty_at_trigger": trigger_event.get("short_qty", freeze_state.get("trigger_short_qty")),
        "net_qty_at_trigger": trigger_event.get(
            "net_exposure_at_trigger", freeze_state.get("net_exposure_at_trigger")
        ),
        "gross_notional_at_trigger": trigger_event.get(
            "gross_notional_at_trigger", freeze_state.get("trigger_gross_notional")
        ),
        "net_exposure_usdt_at_trigger": trigger_event.get(
            "net_exposure_usdt_at_trigger", freeze_state.get("trigger_net_exposure_usdt")
        ),
        "active_exit_price_at_trigger": trigger_event.get(
            "active_exit_at_trigger", freeze_state.get("active_exit_at_trigger")
        ),
        "market_price_at_trigger": trigger_event.get("trigger_mark", freeze_state.get("trigger_mark")),
        "exit_distance_pct_at_trigger": trigger_event.get(
            "exit_distance_pct_at_trigger", freeze_state.get("trigger_exit_distance_pct")
        ),
        "required_recovery_move_pct_at_trigger": trigger_event.get(
            "required_recovery_move_pct_at_trigger",
            freeze_state.get("trigger_required_recovery_move_pct"),
        ),
        "realized_cycle_pnl_at_trigger": trigger_event.get(
            "realized_cycle_pnl_at_trigger", freeze_state.get("realized_pnl")
        ),
        "pending_cycle_loss_at_trigger": trigger_event.get(
            "pending_cycle_loss_at_trigger", freeze_state.get("trigger_pending_cycle_loss")
        ),
        "worst_mtm_after_trigger": freeze_state.get("worst_mtm_after_trigger"),
        "maximum_adverse_price_move_after_trigger": freeze_state.get(
            "max_adverse_price_move_after_trigger"
        ),
        "maximum_favorable_price_move_after_trigger": freeze_state.get(
            "max_favorable_price_move_after_trigger"
        ),
        "first_reclaim_candle": freeze_state.get("first_reclaim_candle"),
        "first_flat_candle": flat_candle,
        "trigger_to_flat_candles": trigger_to_flat,
        "realized_pnl_at_recovered_flat": (first_flat or {}).get("realized_pnl") if recovered else None,
        "final_status": target.get("status"),
        "final_mtm": target.get("mtm_pnl"),
        "series_mtm": sum(safe_float(r.get("mtm_pnl")) for r in rows),
        "trades": len(rows),
        "blockers_remaining": sum(1 for r in rows if r.get("is_blocker")),
        "capital_bound_at_end": int(any(r.get("is_blocker") for r in rows)),
        "prior_b1_recovered_cohort": int(coin in PRIOR_B1_RECOVERED_COINS),
        "prior_b1_unrecovered_cohort": int(coin in PRIOR_B1_UNRECOVERED_COINS),
        "secondary_trigger_reason": freeze_state.get("secondary_trigger_reason"),
        "emergency_fired": int(bool(freeze_state.get("emergency_fired"))),
        "research_terminal_reason": next(
            (r.get("research_terminal_reason") for r in reversed(rows) if r.get("research_terminal_reason")),
            None,
        ),
    }


def summarize_variant(
    *,
    variant: str,
    trade_rows: list[dict[str, Any]],
    outcome_rows: list[dict[str, Any]],
    policy_actions: list[dict[str, Any]],
) -> dict[str, Any]:
    recovered = [r for r in outcome_rows if int(r.get("recovered") or 0) == 1]
    unrecovered = [r for r in outcome_rows if int(r.get("recovered") or 0) == 0]
    triggered = [r for r in outcome_rows if int(r.get("trigger_fired") or 0) == 1]
    flat_pnls = [safe_float(r.get("realized_pnl_at_recovered_flat")) for r in recovered]
    trigger_to_flat = [
        safe_float(r.get("trigger_to_flat_candles"))
        for r in recovered
        if r.get("trigger_to_flat_candles") not in (None, "")
    ]
    worst_before_flat = [
        safe_float(r.get("worst_mtm_after_trigger"))
        for r in recovered
        if r.get("worst_mtm_after_trigger") not in (None, "")
    ]
    unrecovered_final = [safe_float(r.get("final_mtm")) for r in unrecovered]
    series_mtm = sum(safe_float(r.get("mtm_pnl")) for r in trade_rows)
    prior_unrec_now_rec = sum(
        1
        for r in recovered
        if str(r.get("coin") or "") in PRIOR_B1_UNRECOVERED_COINS
    )
    prior_rec_lost = sum(
        1
        for r in unrecovered
        if str(r.get("coin") or "") in PRIOR_B1_RECOVERED_COINS
    )
    prior_rec_worse = 0
    for r in recovered:
        if str(r.get("coin") or "") not in PRIOR_B1_RECOVERED_COINS:
            continue
        # "worsened" vs prior cohort: negative flat PnL (diagnostic).
        if safe_float(r.get("realized_pnl_at_recovered_flat")) < -1e-9:
            prior_rec_worse += 1

    exposure_freeze_count = sum(
        1 for a in policy_actions if a.get("action") == "stage1_exposure_freeze"
    ) + sum(1 for a in policy_actions if a.get("action") == "block_exposure_growth")
    cycle_freeze_events = sum(1 for a in policy_actions if a.get("action") == "stage2_cycle_freeze")
    cycle_block_events = sum(1 for a in policy_actions if a.get("action") == "block_new_cycle")
    emergency_count = sum(
        1 for a in policy_actions if a.get("action") == "emergency_partial_neutralize"
    )

    return {
        "variant": variant,
        "original_baseline_blockers": 27,
        "original_blockers_triggered": len(triggered),
        "original_blockers_recovered_flat": len(recovered),
        "recovery_rate": len(recovered) / 27.0,
        "unrecovered_original_blockers": len(unrecovered),
        "realized_pnl_at_recovered_flat": sum(flat_pnls),
        "mean_realized_pnl_at_recovered_flat": (sum(flat_pnls) / len(flat_pnls)) if flat_pnls else None,
        "median_trigger_to_flat_candles": (
            sorted(trigger_to_flat)[len(trigger_to_flat) // 2] if trigger_to_flat else None
        ),
        "worst_mtm_before_flat": min(worst_before_flat) if worst_before_flat else None,
        "final_mtm_of_unrecovered": sum(unrecovered_final),
        "series_mtm_terminal_stop": series_mtm,
        "series_mtm": series_mtm,
        "delta_vs_A0": series_mtm - A0_SERIES_MTM,
        "delta_vs_C0": None,  # filled after C0 known
        "negative_recovered_flats": sum(1 for p in flat_pnls if p < -1e-9),
        "damaged_baseline_closed_trades": sum(
            int(r.get("damaged_baseline_closed_trade") or 0) for r in trade_rows
        ),
        "total_policy_actions": len(policy_actions),
        "exposure_freeze_count": exposure_freeze_count,
        "cycle_freeze_count": cycle_freeze_events + cycle_block_events,
        "emergency_neutralization_count": emergency_count,
        "undercoverage_count": sum(int(r.get("undercoverage") or 0) for r in trade_rows),
        "prior_15_unrecovered_now_recovered": prior_unrec_now_rec,
        "prior_15_unrecovered_recovery_share": prior_unrec_now_rec / 15.0,
        "prior_12_recovered_lost": prior_rec_lost,
        "prior_12_recovered_lost_share": prior_rec_lost / 12.0,
        "prior_12_recovered_negative_flat": prior_rec_worse,
        "coins_still_binding_capital": sum(int(r.get("capital_bound_at_end") or 0) for r in outcome_rows),
        "trades": len(trade_rows),
        "closed": sum(1 for r in trade_rows if not r.get("is_blocker")),
        "blockers": sum(1 for r in trade_rows if r.get("is_blocker")),
    }


def check_c0_parity(summary: dict[str, Any]) -> dict[str, Any]:
    checks = {
        "trades": (
            summary["trades"],
            C0_B1_REFERENCE_TRADES,
            summary["trades"] == C0_B1_REFERENCE_TRADES,
        ),
        "closed": (
            summary["closed"],
            C0_B1_REFERENCE_CLOSED,
            summary["closed"] == C0_B1_REFERENCE_CLOSED,
        ),
        "blockers": (
            summary["blockers"],
            C0_B1_REFERENCE_BLOCKERS,
            summary["blockers"] == C0_B1_REFERENCE_BLOCKERS,
        ),
        "recovered": (
            summary["original_blockers_recovered_flat"],
            C0_B1_REFERENCE_RECOVERED,
            summary["original_blockers_recovered_flat"] == C0_B1_REFERENCE_RECOVERED,
        ),
        "series_mtm": (
            summary["series_mtm_terminal_stop"],
            C0_B1_REFERENCE_SERIES_MTM,
            abs(summary["series_mtm_terminal_stop"] - C0_B1_REFERENCE_SERIES_MTM) <= C0_MTM_TOLERANCE,
        ),
    }
    return {"ok": all(c[2] for c in checks.values()), "checks": checks}


def run_one_variant(
    *,
    spec: HybridVariantSpec,
    coins: list[str],
    coin_candles: dict[str, list[Any]],
    target_map: dict[str, int],
    baseline_blocker_by_coin: dict[str, dict[str, Any]],
    baseline_closed_pnl_by_trade: dict[tuple[str, int], float],
) -> dict[str, Any]:
    trade_rows: list[dict[str, Any]] = []
    outcome_rows: list[dict[str, Any]] = []
    policy_actions: list[dict[str, Any]] = []
    emergency_actions: list[dict[str, Any]] = []
    trigger_events: list[dict[str, Any]] = []

    for symbol in coins:
        candles = coin_candles[symbol]
        target = int(target_map.get(symbol, -1))
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
                baseline_closed_pnl_by_trade=baseline_closed_pnl_by_trade,
            )
            coin_rows.append(row)
            trade_rows.append(row)
            for action in row.get("policy_actions") or []:
                enriched = {
                    "coin": symbol,
                    "variant": spec.name,
                    "trade_number": row["trade_number"],
                    **action,
                }
                policy_actions.append(enriched)
                if str(action.get("action") or "").startswith("emergency"):
                    emergency_actions.append(enriched)
            te = row.get("trigger_event") or {}
            if te:
                trigger_events.append(
                    {
                        "coin": symbol,
                        "variant": spec.name,
                        "trade_number": row["trade_number"],
                        "is_original_blocker": int(row["trade_number"] == target),
                        **te,
                    }
                )

        baseline_mtm = None
        if symbol in baseline_blocker_by_coin:
            baseline_mtm = safe_float(baseline_blocker_by_coin[symbol].get("mtm_pnl"))
        outcome_rows.append(
            build_original_blocker_outcome(
                coin=symbol,
                variant=spec.name,
                rows=coin_rows,
                target_blocker_trade_number=target,
                baseline_mtm=baseline_mtm,
            )
        )

    summary = summarize_variant(
        variant=spec.name,
        trade_rows=trade_rows,
        outcome_rows=outcome_rows,
        policy_actions=policy_actions,
    )
    return {
        "spec": spec,
        "summary": summary,
        "trade_rows": trade_rows,
        "outcome_rows": outcome_rows,
        "policy_actions": policy_actions,
        "emergency_actions": emergency_actions,
        "trigger_events": trigger_events,
    }


def write_report(
    path: Path,
    *,
    variant_summaries: list[dict[str, Any]],
    feature_rank: list[dict[str, Any]],
    c0_parity: dict[str, Any],
    best_c0_c4: str,
    c5_base: str | None,
) -> None:
    by_name = {row["variant"]: row for row in variant_summaries}
    c0 = by_name.get("C0", {})
    best = by_name.get(best_c0_c4, {})
    top_feature = feature_rank[0]["feature"] if feature_rank else "n/a"
    lines = [
        "# Blocker Recovery Trigger & Hybrid Audit",
        "",
        f"Generated: `{datetime.now(timezone.utc).isoformat()}`",
        "",
        "## Setup",
        "",
        f"- Prior recovery/re-entry audit (read-only): `{PRIOR_RECOVERY_AUDIT_DIR.name}`",
        f"- Baseline corpus (read-only): `{BASELINE_DIR.name}`",
        "- All variants use **B1 terminal stop** after the first true recovered flat of the original blocker.",
        "- No live/config/runtime changes. Causal fills unchanged. INJUSDT T8 separate marker.",
        "",
        "## C0 parity vs prior B1",
        "",
        f"- Result: **{'PASS' if c0_parity['ok'] else 'FAIL'}**",
        "",
        "| check | actual | expected | ok |",
        "|---|---:|---:|:---:|",
    ]
    for name, (actual, expected, ok) in c0_parity["checks"].items():
        lines.append(f"| {name} | {actual} | {expected} | {ok} |")

    lines.extend(
        [
            "",
            "## Variant summary (terminal stop)",
            "",
            "| variant | recovered/27 | recovery_rate | series_mtm | ΔA0 | ΔC0 | "
            "prior15→rec | prior12 lost | emergency | capital_bound |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in variant_summaries:
        lines.append(
            f"| {row['variant']} | {row['original_blockers_recovered_flat']}/27 | "
            f"{100.0 * row['recovery_rate']:.1f}% | {safe_float(row['series_mtm_terminal_stop']):.2f} | "
            f"{safe_float(row['delta_vs_A0']):.2f} | {safe_float(row.get('delta_vs_C0')):.2f} | "
            f"{row['prior_15_unrecovered_now_recovered']}/15 | {row['prior_12_recovered_lost']}/12 | "
            f"{row['emergency_neutralization_count']} | {row['coins_still_binding_capital']} |"
        )

    lines.extend(
        [
            "",
            "## Recovered vs unrecovered (C0 diagnosis)",
            "",
            f"Strongest separating feature (std. mean diff): **`{top_feature}`**",
            "",
            "| rank | feature | recovered_mean | unrecovered_mean | abs_std_mean_diff |",
            "|---:|---|---:|---:|---:|",
        ]
    )
    for i, row in enumerate(feature_rank[:12], start=1):
        lines.append(
            f"| {i} | {row['feature']} | {safe_float(row['recovered_mean']):.4f} | "
            f"{safe_float(row['unrecovered_mean']):.4f} | {safe_float(row['abs_std_mean_diff']):.3f} |"
        )

    lines.extend(
        [
            "",
            "## Entscheidungsfragen",
            "",
            f"1. **Welches Merkmal trennt recovered/unrecovered am stärksten?** `{top_feature}` "
            f"(siehe `feature_group_summary.csv` / Ranking oben; rein diagnostisch).",
            f"2. **Ist `inventory_mtm < -1` für A1 zu spät?** Vergleiche C1a/C1b vs C0: "
            f"C1a recovered={by_name.get('C1a', {}).get('original_blockers_recovered_flat')}, "
            f"C1b={by_name.get('C1b', {}).get('original_blockers_recovered_flat')}, "
            f"C0={c0.get('original_blockers_recovered_flat')}; series_mtm C1a="
            f"{safe_float(by_name.get('C1a', {}).get('series_mtm_terminal_stop')):.2f}, "
            f"C0={safe_float(c0.get('series_mtm_terminal_stop')):.2f}.",
            f"3. **Erhöht früherer Freeze die Recovery-Rate deutlich?** "
            f"Beste C1-Rate vs C0: siehe Tabelle; absolute recovered counts.",
            f"4. **Ist Cycle-Count robuster als fixer USDT-MTM-Threshold?** "
            f"C2a/b/c recovered="
            f"{by_name.get('C2a', {}).get('original_blockers_recovered_flat')}/"
            f"{by_name.get('C2b', {}).get('original_blockers_recovered_flat')}/"
            f"{by_name.get('C2c', {}).get('original_blockers_recovered_flat')} "
            f"vs C0={c0.get('original_blockers_recovered_flat')}.",
            f"5. **Verbessert A2→A1 Recovery-Rate oder nur Open-MTM?** "
            f"C4 recovered counts und series_mtm vs C0; wenn recovered≈0 aber series_mtm besser → nur Open-MTM.",
            f"6. **Wie viele der bisherigen 15 unrecovered werden zusätzlich flat?** "
            f"Bester C0–C4 (`{best_c0_c4}`): "
            f"{best.get('prior_15_unrecovered_now_recovered', 'n/a')}/15.",
            f"7. **Werden die bisherigen 12 Recoveries beschädigt?** "
            f"`{best_c0_c4}` verliert {best.get('prior_12_recovered_lost', 'n/a')}/12; "
            f"negative flats unter prior cohort: {best.get('prior_12_recovered_negative_flat', 'n/a')}.",
            f"8. **Bester terminaler Series-MTM?** `{best_c0_c4}` mit "
            f"{safe_float(best.get('series_mtm_terminal_stop')):.2f} "
            f"(C5-Basis: `{c5_base or 'n/a'}`).",
            "9. **Negative / unterdeckte Flat-Recoveries?** Siehe `negative_recovered_flats` und "
            "`undercoverage_count` in `variant_summary.csv`.",
            "10. **Emergency für verbleibende unrecovered nötig?** Nur wenn C5 klar mehr flats "
            "liefert ohne die prior-12 zu zerstören — siehe C5a–d vs Basis; **kein Runtime-Kandidat**.",
            "11. **Runtime?** Noch keine Runtime-Änderung. Empfehlung erst, wenn deutlich mehr als "
            "12/27 Original-Blocker tatsächlich flat werden **und** Series-MTM klar über A0/C0 liegt.",
            "",
            "### Notes",
            f"- Best C0–C4 by terminal series_mtm (then recovery_rate): `{best_c0_c4}`",
            f"- C5 layered on: `{c5_base}`",
            "",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_baseline_closed_pnl_map() -> dict[tuple[str, int], float]:
    path = BASELINE_DIR / "all_trades.csv"
    if not path.exists():
        # fallback: empty map (damaged-closed metric stays 0)
        return {}
    out: dict[tuple[str, int], float] = {}
    with path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            coin = str(row.get("coin") or "").strip().upper()
            trade_number = int(safe_float(row.get("trade_number"), -1))
            status = str(row.get("status") or "").strip().lower()
            if status != "closed":
                continue
            out[(coin, trade_number)] = safe_float(row.get("realized_pnl"))
    return out


def run_pipeline(
    *,
    output_root: Path,
    max_coins: int | None = None,
    skip_c5: bool = False,
    only_variants: list[str] | None = None,
) -> dict[str, Any]:
    output_root_resolved = output_root.resolve()
    for protected in PROTECTED_DIRS:
        if output_root_resolved == protected.resolve():
            raise RuntimeError(f"refusing to target a protected directory: {protected}")
    if output_root.exists() and any(output_root.iterdir()):
        raise FileExistsError(f"refusing to overwrite existing output directory: {output_root}")
    output_root.mkdir(parents=True, exist_ok=True)

    coins = load_baseline_coin_list(max_coins=max_coins)
    blocker_rows = load_baseline_blockers(BASELINE_DIR / "blocker_trades.csv")
    target_map = baseline_blocker_trade_number_by_coin(blocker_rows)
    baseline_blocker_by_coin = {
        str(row.get("coin") or "").strip().upper(): row for row in blocker_rows
    }
    baseline_closed_pnl_by_trade = load_baseline_closed_pnl_map()

    coin_candles = {
        symbol: normalize_candles(
            symbol, load_candles_for_symbol(symbol, limit=FULL_HISTORY_CANDLE_LIMIT)
        )
        for symbol in coins
    }

    all_specs = build_c0_c4_specs()
    if only_variants:
        wanted = set(only_variants)
        all_specs = [s for s in all_specs if s.name in wanted]
        if "C0" not in wanted and any(n.startswith("C") for n in wanted):
            # still require C0 for parity when running full family; allow subset for smoke tests
            pass

    applied = {
        "baseline_source": str(BASELINE_DIR),
        "prior_recovery_audit": str(PRIOR_RECOVERY_AUDIT_DIR),
        "pinned_live_params": {
            "direction": DIRECTION,
            "config_source": CONFIG_SOURCE,
            "fill_model": FILL_MODEL,
            "continuous_start_index": CONTINUOUS_START_INDEX,
            "long_fill_distance_pct": LONG_FILL_DISTANCE_PCT,
            "target_profit_usdt": TARGET_PROFIT_USDT,
            "tp_profit_target_pct": TP_PROFIT_TARGET_PCT,
            "candle_limit": FULL_HISTORY_CANDLE_LIMIT,
        },
        "terminal_stop": "B1 recovered_flat_terminal after original blocker flat",
        "c0_parity_reference": {
            "trades": C0_B1_REFERENCE_TRADES,
            "closed": C0_B1_REFERENCE_CLOSED,
            "blockers": C0_B1_REFERENCE_BLOCKERS,
            "recovered": C0_B1_REFERENCE_RECOVERED,
            "series_mtm": C0_B1_REFERENCE_SERIES_MTM,
        },
        "variants": {},
        "required_recovery_move_pct_definition": (
            "long: (active_exit_price - mark) / mark * 100 using the causally active "
            "exit/TP path at the trigger candle; short mirrors (mark - active_exit) / mark * 100"
        ),
    }

    results_by_variant: dict[str, dict[str, Any]] = {}
    variant_summaries: list[dict[str, Any]] = []

    # Ensure C0 runs first when present.
    ordered = sorted(all_specs, key=lambda s: (0 if s.name == "C0" else 1, s.name))
    for spec in ordered:
        print(f"[hybrid-audit] running {spec.name}: {spec.description}", flush=True)
        applied["variants"][spec.name] = {
            "description": spec.description,
            "family": spec.family,
            "freeze_config": _config_to_dict(spec.freeze_config),
        }
        results_by_variant[spec.name] = run_one_variant(
            spec=spec,
            coins=coins,
            coin_candles=coin_candles,
            target_map=target_map,
            baseline_blocker_by_coin=baseline_blocker_by_coin,
            baseline_closed_pnl_by_trade=baseline_closed_pnl_by_trade,
        )
        variant_summaries.append(results_by_variant[spec.name]["summary"])

    if "C0" not in results_by_variant:
        raise RuntimeError("C0 must be run for B1 parity gate")

    c0_parity = check_c0_parity(results_by_variant["C0"]["summary"])
    if not c0_parity["ok"]:
        _write_json(output_root / "c0_parity_failed.json", c0_parity)
        write_report(
            output_root / "REPORT.md",
            variant_summaries=variant_summaries,
            feature_rank=[],
            c0_parity=c0_parity,
            best_c0_c4="C0",
            c5_base=None,
        )
        raise RuntimeError(f"C0 did not reproduce prior B1 within tolerance: {c0_parity}")

    c0_series = results_by_variant["C0"]["summary"]["series_mtm_terminal_stop"]
    for summary in variant_summaries:
        summary["delta_vs_C0"] = summary["series_mtm_terminal_stop"] - c0_series

    best_name = pick_best_c0_c4_candidate(variant_summaries)
    c5_base_name: str | None = None
    if not skip_c5 and only_variants is None:
        best_spec = next(s for s in build_c0_c4_specs() if s.name == best_name)
        c5_specs = build_c5_specs(best_spec)
        c5_base_name = best_name
        applied["c5_base_variant"] = best_name
        for spec in c5_specs:
            print(f"[hybrid-audit] running {spec.name}: {spec.description}", flush=True)
            applied["variants"][spec.name] = {
                "description": spec.description,
                "family": spec.family,
                "freeze_config": _config_to_dict(spec.freeze_config),
                "base_variant": best_name,
            }
            results_by_variant[spec.name] = run_one_variant(
                spec=spec,
                coins=coins,
                coin_candles=coin_candles,
                target_map=target_map,
                baseline_blocker_by_coin=baseline_blocker_by_coin,
                baseline_closed_pnl_by_trade=baseline_closed_pnl_by_trade,
            )
            summary = results_by_variant[spec.name]["summary"]
            summary["delta_vs_C0"] = summary["series_mtm_terminal_stop"] - c0_series
            variant_summaries.append(summary)

    # Part 1 diagnosis from C0 outcomes
    c0_outcomes = results_by_variant["C0"]["outcome_rows"]
    recovered_rows = [r for r in c0_outcomes if int(r.get("recovered") or 0) == 1]
    unrecovered_rows = [r for r in c0_outcomes if int(r.get("recovered") or 0) == 0]
    feature_rank = rank_separating_features(recovered_rows, unrecovered_rows)
    feature_group_rows: list[dict[str, Any]] = []
    for group_name, group_rows in (("recovered", recovered_rows), ("unrecovered", unrecovered_rows)):
        for feature in FEATURE_COLUMNS:
            stats = group_feature_stats(group_rows, feature=feature)
            feature_group_rows.append({"group": group_name, **stats})

    # Aggregate exports
    all_outcomes: list[dict[str, Any]] = []
    all_triggers: list[dict[str, Any]] = []
    all_actions: list[dict[str, Any]] = []
    all_emergencies: list[dict[str, Any]] = []
    for name, payload in results_by_variant.items():
        all_outcomes.extend(payload["outcome_rows"])
        all_triggers.extend(payload["trigger_events"])
        all_actions.extend(payload["policy_actions"])
        all_emergencies.extend(payload["emergency_actions"])

    _write_json(output_root / "applied_params.json", applied)
    _write_csv(output_root / "variant_summary.csv", variant_summaries)
    _write_csv(output_root / "original_blocker_outcomes.csv", all_outcomes)
    _write_csv(
        output_root / "recovered_vs_unrecovered_features.csv",
        [r for r in c0_outcomes],
    )
    _write_csv(output_root / "feature_group_summary.csv", feature_group_rows + [
        {"group": "rank", **row} for row in feature_rank
    ])
    _write_csv(output_root / "trigger_events.csv", all_triggers)
    _write_csv(output_root / "policy_actions.csv", all_actions)
    _write_csv(output_root / "emergency_actions.csv", all_emergencies)
    write_report(
        output_root / "REPORT.md",
        variant_summaries=variant_summaries,
        feature_rank=feature_rank,
        c0_parity=c0_parity,
        best_c0_c4=best_name,
        c5_base=c5_base_name,
    )
    manifest = {
        "git": _git_status(),
        "mode": "blocker_recovery_trigger_and_hybrid_audit",
        "output_root": str(output_root),
        "coins": coins,
        "c0_parity": c0_parity,
        "best_c0_c4": best_name,
        "c5_base": c5_base_name,
        "variants_run": [row["variant"] for row in variant_summaries],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_json(output_root / "run_manifest.json", manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--max-coins", type=int, default=None)
    parser.add_argument("--skip-c5", action="store_true")
    parser.add_argument(
        "--only-variants",
        nargs="*",
        default=None,
        help="Optional subset for smoke tests (still should include C0 for parity).",
    )
    args = parser.parse_args()
    run_pipeline(
        output_root=args.output_root,
        max_coins=args.max_coins,
        skip_c5=args.skip_c5,
        only_variants=args.only_variants,
    )


if __name__ == "__main__":
    main()
