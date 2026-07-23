"""Research-only recovery/re-entry audit for the 27 baseline inventory_mtm<-1 blockers.

Builds on the A1 finding (``inventory_mtm_neg1_policy_audit_20260720``): freezing new
cycles (A1) flats 20/27 baseline blockers, but the series MTM regresses to roughly
``-507`` because ~93 extra post-flat trades add up to roughly ``-460`` MTM on top of
the already-recovered coins. This audit isolates *what happens right after the first
recovered flat of each coin's baseline blocker trade* under six re-entry policies
(``B0``..``B5``) so the trade-off between "flatten the blocker" and "stop bleeding on
new trades" can be measured directly.

Hard constraints enforced by this module:

* No live config / runtime / strategy default is ever changed (only ``config_source="live"``
  is *read*).
* Never writes into, nor overwrites, either protected directory:
  - ``research/backtests/results/current_baseline_multicoin_continuous_blocker_audit_20260720/``
  - ``research/backtests/results/inventory_mtm_neg1_policy_audit_20260720/``
  Both are only ever *read* (coin/blocker corpus + A1 parity reference).
* Causal fills are never touched by this module or by ``recovery_reentry_policy.py``;
  only *when* a fresh backtest trade starts is ever decided here.
* INJUSDT trade 8 undercoverage remains a separate diagnostic marker (never a policy
  success), same as in the sibling freeze-policy audit.
* This script never calls ``git commit``.
* Refuses to overwrite a non-empty output directory.

B0 must reproduce the prior A1 policy audit's totals (358 trades / 331 closed / 27
blockers / series_mtm ~= -507.0096) within tolerance before B1..B5 are run; if it does
not, the run aborts and reports the mismatch instead of silently continuing.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
from pathlib import Path
from typing import Any

from research.backtests.backtest_report import BacktestResult
from research.backtests.candle_loader import load_candles_for_symbol
from research.backtests.continuous_reentry_backtest import run_continuous_reentry_backtests
from research.backtests.historical_backtest import normalize_candles
from research.backtests.inventory_mtm_freeze import InventoryMtmFreezeConfig, is_injusdt_trade8_undercoverage
from research.backtests.long_add_multistart_metrics import analyze_trade, normalize_trade_status, safe_float
from research.backtests.recovery_reentry_policy import (
    RECOVERY_VARIANTS,
    RecoveryReentryConfig,
    baseline_blocker_trade_number_by_coin,
    count_new_blockers_after_recovery,
    freeze_config_for_variant,
    load_baseline_blockers,
    post_recovery_trade_pnl,
    series_mtm_if_stopped_at_first_recovered_flat,
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
PRIOR_POLICY_AUDIT_DIR = (
    ROOT / "research/backtests/results/inventory_mtm_neg1_policy_audit_20260720"
)
PROTECTED_DIRS = (BASELINE_DIR, PRIOR_POLICY_AUDIT_DIR)
DEFAULT_OUT = ROOT / "research/backtests/results/inventory_mtm_neg1_recovery_reentry_audit_20260720"

DEFAULT_VARIANTS: tuple[str, ...] = RECOVERY_VARIANTS  # ("B0", "B1", "B2", "B3", "B4", "B5")

# --- B0 parity reference: must reproduce the prior A1 policy audit ---------
B0_REFERENCE_TRADES = 358
B0_REFERENCE_CLOSED = 331
B0_REFERENCE_BLOCKERS = 27
B0_REFERENCE_SERIES_MTM = -507.0096
B0_REFERENCE_MTM_TOLERANCE = 1.0


# ---------------------------------------------------------------------------
# Small shared utilities (same pattern as the sibling multi-coin audit runners)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Fixed call kwargs (never pass exit_rebuild_policy_config)
# ---------------------------------------------------------------------------


def build_call_kwargs(
    *,
    symbol: str,
    candles: list[Any],
    inventory_mtm_freeze_config: InventoryMtmFreezeConfig | None,
    recovery_reentry_config: RecoveryReentryConfig | None,
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
        "recovery_reentry_config": recovery_reentry_config,
        "write_json": False,
        "write_csv": False,
    }


def write_applied_params(output_root: Path, *, variants: tuple[str, ...]) -> dict[str, Any]:
    payload = {
        "note": (
            "Reuses the exact 27-coin corpus (same order) from the protected baseline's "
            "coin_manifest.csv, and the baseline blocker_trades.csv target map; both "
            "protected directories are only ever read, never written."
        ),
        "baseline_source": str(BASELINE_DIR),
        "prior_policy_audit_source": str(PRIOR_POLICY_AUDIT_DIR),
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
        "exit_rebuild_policy_config": None,
        "b0_parity_reference": {
            "source": "prior inventory_mtm_neg1_policy_audit_20260720 variant A1",
            "trades": B0_REFERENCE_TRADES,
            "closed": B0_REFERENCE_CLOSED,
            "blockers": B0_REFERENCE_BLOCKERS,
            "series_mtm": B0_REFERENCE_SERIES_MTM,
            "series_mtm_tolerance": B0_REFERENCE_MTM_TOLERANCE,
        },
        "variants": list(variants),
        "variant_semantics": {
            "B0": "continuous re-entry unchanged (control); freeze = A1",
            "B1": "stop for good after the first recovered flat of the target blocker trade",
            "B2": "cooldown window (default 500 candles) after the first recovered flat",
            "B3": "only re-enter on the first later fresh-pullback signal candle "
            "(close <= flat_mark * (1 - fresh_pullback_pct/100))",
            "B4": "only re-enter immediately if the recovered flat left a strictly clean "
            "book (flat_no_active_orders + zero qty + no active orders); else stop",
            "B5": "continuous like B0; freeze escalates in two stages (A2 exposure-only, "
            "then an A1-style cycle freeze once a secondary condition fires)",
        },
        "freeze_config_by_variant": {
            variant: (
                {
                    "variant": cfg.variant,
                    "threshold_usdt": cfg.threshold_usdt,
                    "max_trigger_candle": cfg.max_trigger_candle,
                    "staged_cycle_freeze": cfg.staged_cycle_freeze,
                    "secondary_hold_candles_below_threshold": cfg.secondary_hold_candles_below_threshold,
                    "secondary_mtm_threshold_usdt": cfg.secondary_mtm_threshold_usdt,
                    "secondary_exit_increase_count": cfg.secondary_exit_increase_count,
                }
                if (cfg := freeze_config_for_variant(variant)) is not None
                else None
            )
            for variant in variants
        },
        "known_marker": "INJUSDT trade 8 undercoverage is kept as a separate diagnostic marker, "
        "never counted as a policy success.",
        "causal_fills": "Unchanged: these policies only ever decide *when* a fresh (already "
        "causal) backtest trade starts; no fill/eligibility mechanics are altered.",
    }
    _write_json(output_root / "applied_params.json", payload)
    return payload


# ---------------------------------------------------------------------------
# Per-trade row (analyze_trade metrics + freeze/recovery excerpt fields)
# ---------------------------------------------------------------------------


def build_recovery_trade_row(
    *,
    coin: str,
    variant: str,
    result: BacktestResult,
    candles: list[Any],
    target_blocker_trade_number: int,
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
    trigger_event = excerpt.get("inventory_mtm_trigger_event") or None
    freeze_state = excerpt.get("inventory_mtm_freeze_state") or {}
    policy_actions = excerpt.get("inventory_mtm_policy_actions") or []
    reentry_event = excerpt.get("reentry_event") or None

    return {
        "coin": coin,
        "variant": variant,
        "target_blocker_trade_number": target_blocker_trade_number,
        "trade_number": trade_number,
        "start_index": start_index,
        "end_index": result.end_index,
        "start_timestamp": _ts(result.start_time),
        "end_timestamp": _ts(result.end_time),
        "status": status,
        "is_blocker": is_blocker,
        "duration_candles": int(result.candles_processed or 0),
        "realized_pnl": analysis.get("realized_pnl"),
        "unrealized_pnl": analysis.get("unrealized_pnl"),
        "mtm_pnl": analysis.get("mtm_pnl"),
        "max_cycle": analysis.get("max_cycle"),
        "final_long_qty": analysis.get("final_long_qty"),
        "final_short_qty": analysis.get("final_short_qty"),
        "final_net_qty": analysis.get("final_net_qty"),
        "fees": analysis.get("fees"),
        "undercoverage": analysis.get("undercoverage"),
        "exit_reason": result.exit_reason,
        "injusdt_trade8_marker": int(is_injusdt_trade8_undercoverage(coin=coin, trade_number=trade_number)),
        "trigger_fired": bool(trigger_event),
        "trigger_candle": (trigger_event or {}).get("trigger_candle"),
        "trigger_mtm": (trigger_event or {}).get("trigger_mtm"),
        "policy_action_count": len(policy_actions),
        "cycle_freeze_enabled": freeze_state.get("cycle_freeze_enabled"),
        "secondary_trigger_candle": freeze_state.get("secondary_trigger_candle"),
        "secondary_trigger_reason": freeze_state.get("secondary_trigger_reason"),
        "recovered_flat_of_target_blocker": bool(excerpt.get("recovered_flat_of_target_blocker")),
        "first_flat_candle_absolute": excerpt.get("first_flat_candle_absolute"),
        "flat_mark_price": excerpt.get("flat_mark_price"),
        "post_recovery_trade": bool(excerpt.get("post_recovery_trade")),
        "research_terminal_reason": excerpt.get("research_terminal_reason"),
        "reentry_event_type": (reentry_event or {}).get("type"),
    }


def build_policy_action_rows(*, coin: str, variant: str, result: BacktestResult) -> list[dict[str, Any]]:
    excerpt = dict(result.final_strategy_state_excerpt or {})
    actions = excerpt.get("inventory_mtm_policy_actions") or []
    return [
        {
            "coin": coin,
            "variant": variant,
            "trade_number": int(result.trade_number or 0),
            **action,
        }
        for action in actions
    ]


def build_reentry_event_rows(*, coin: str, variant: str, result: BacktestResult) -> list[dict[str, Any]]:
    excerpt = dict(result.final_strategy_state_excerpt or {})
    event = excerpt.get("reentry_event")
    if not event:
        return []
    return [
        {
            "coin": coin,
            "variant": variant,
            "trade_number": int(result.trade_number or 0),
            **event,
        }
    ]


# ---------------------------------------------------------------------------
# Variant / per-coin aggregation
# ---------------------------------------------------------------------------


def summarize_coin_rows(
    *, rows: list[dict[str, Any]], target_blocker_trade_number: int
) -> dict[str, Any]:
    triggered = any(
        row["trade_number"] == target_blocker_trade_number and row.get("trigger_fired") for row in rows
    )
    first_flat_row = next((row for row in rows if row.get("recovered_flat_of_target_blocker")), None)
    recovered = first_flat_row is not None
    series_mtm = sum(safe_float(row.get("mtm_pnl")) for row in rows)
    series_mtm_if_stopped = series_mtm_if_stopped_at_first_recovered_flat(
        trade_rows=rows, target_blocker_trade_number=target_blocker_trade_number, recovered=recovered
    )
    return {
        "triggered": triggered,
        "first_flat_achieved": recovered,
        "first_flat_candle_absolute": (first_flat_row or {}).get("first_flat_candle_absolute"),
        "flat_mark_price": (first_flat_row or {}).get("flat_mark_price"),
        "target_trade_mtm_pnl": next(
            (row.get("mtm_pnl") for row in rows if row["trade_number"] == target_blocker_trade_number), None
        ),
        "series_mtm": series_mtm,
        "series_mtm_if_stopped_at_first_recovered_flat": series_mtm_if_stopped,
        "post_recovery_trade_pnl": post_recovery_trade_pnl(
            series_mtm=series_mtm, series_mtm_if_stopped=series_mtm_if_stopped
        ),
        "new_blockers_after_recovery": (
            count_new_blockers_after_recovery(trade_rows=rows, target_blocker_trade_number=target_blocker_trade_number)
            if recovered
            else 0
        ),
        "terminal_reason": next(
            (row.get("research_terminal_reason") for row in reversed(rows) if row.get("research_terminal_reason")),
            None,
        ),
        "reentry_event_type": (
            next((row.get("reentry_event_type") for row in rows if row.get("reentry_event_type")), None)
        ),
        "trades": len(rows),
        "closed": sum(1 for row in rows if not row["is_blocker"]),
        "blockers": sum(1 for row in rows if row["is_blocker"]),
    }


def summarize_variant_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    closed = [row for row in rows if not row["is_blocker"]]
    blockers = [row for row in rows if row["is_blocker"]]
    negative_closed = [row for row in closed if safe_float(row.get("realized_pnl")) < 0]
    triggered = [row for row in rows if row.get("trigger_fired")]
    return {
        "trades": len(rows),
        "closed": len(closed),
        "blockers": len(blockers),
        "closed_rate": (len(closed) / len(rows)) if rows else 0.0,
        "sum_closed_pnl": sum(safe_float(row.get("realized_pnl")) for row in closed),
        "series_mtm": sum(safe_float(row.get("mtm_pnl")) for row in rows),
        "negative_closed_count": len(negative_closed),
        "undercoverage_count": sum(int(row.get("undercoverage") or 0) for row in rows),
        "trigger_fired_count": len(triggered),
        "policy_action_total": sum(int(row.get("policy_action_count") or 0) for row in rows),
        "post_recovery_trade_count": sum(1 for row in rows if row.get("post_recovery_trade")),
    }


# ---------------------------------------------------------------------------
# B0 parity check
# ---------------------------------------------------------------------------


def check_b0_parity(b0_rows: list[dict[str, Any]]) -> dict[str, Any]:
    totals = summarize_variant_rows(b0_rows)
    checks = {
        "trades": (totals["trades"], B0_REFERENCE_TRADES, totals["trades"] == B0_REFERENCE_TRADES),
        "closed": (totals["closed"], B0_REFERENCE_CLOSED, totals["closed"] == B0_REFERENCE_CLOSED),
        "blockers": (totals["blockers"], B0_REFERENCE_BLOCKERS, totals["blockers"] == B0_REFERENCE_BLOCKERS),
        "series_mtm": (
            totals["series_mtm"],
            B0_REFERENCE_SERIES_MTM,
            abs(totals["series_mtm"] - B0_REFERENCE_SERIES_MTM) <= B0_REFERENCE_MTM_TOLERANCE,
        ),
    }
    ok = all(check[2] for check in checks.values())
    return {"ok": ok, "checks": checks, "totals": totals}


# ---------------------------------------------------------------------------
# REPORT.md (Entscheidungsfragen)
# ---------------------------------------------------------------------------


def write_report(
    path: Path,
    *,
    variants_run: list[str],
    variant_summaries: list[dict[str, Any]],
    parity: dict[str, Any],
    original_blocker_rows: list[dict[str, Any]],
    original_blockers_count: int,
    aborted: bool,
) -> None:
    from datetime import datetime, timezone

    lines: list[str] = [
        "# Recovery / Re-entry Audit (Inventory MTM<-1 Blocker Follow-up)",
        "",
        f"Generated: `{datetime.now(timezone.utc).isoformat()}`",
        "",
        "## Setup",
        "",
        f"- Corpus reused verbatim from `{BASELINE_DIR.name}` (27 coins, same order) and the "
        f"baseline `blocker_trades.csv` (target blocker trade_number per coin); both protected "
        "directories were only ever read, never written.",
        f"- B0 parity reference reused from `{PRIOR_POLICY_AUDIT_DIR.name}` variant A1 "
        f"(`trades={B0_REFERENCE_TRADES}`, `closed={B0_REFERENCE_CLOSED}`, "
        f"`blockers={B0_REFERENCE_BLOCKERS}`, `series_mtm~={B0_REFERENCE_SERIES_MTM}`); that "
        "directory was only ever read, never written.",
        f"- Pinned live params: `long_fill_distance_pct={LONG_FILL_DISTANCE_PCT}`, "
        f"`target_profit_usdt={TARGET_PROFIT_USDT}`, `tp_profit_target_pct={TP_PROFIT_TARGET_PCT}`, "
        f"`fill_model={FILL_MODEL}`, `config_source={CONFIG_SOURCE}`, "
        f"`continuous_start_index={CONTINUOUS_START_INDEX}`, `candle_limit={FULL_HISTORY_CANDLE_LIMIT}`.",
        "- Causal fills unchanged: these policies only ever decide *when* a fresh (already "
        "causal) backtest trade starts.",
        f"- Variants requested: `{', '.join(variants_run)}`.",
        "",
        "## B0 parity check",
        "",
        f"- Result: **{'PASS' if parity['ok'] else 'FAIL'}**",
        "",
        "| check | actual | expected | ok |",
        "|---|---:|---:|:---:|",
    ]
    for name, (actual, expected, ok) in parity["checks"].items():
        lines.append(f"| {name} | {actual} | {expected} | {ok} |")

    if aborted:
        lines.extend(
            [
                "",
                "## Aborted",
                "",
                "B0 did not reproduce the prior A1 policy audit within tolerance. No further "
                "variants (B1..B5) were run. Fix the parity mismatch above before re-running.",
                "",
            ]
        )
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return

    lines.extend(
        [
            "",
            "## Variant summary",
            "",
            "| variant | trades | closed | blockers | series_mtm | recovered/27 | "
            "recovery_rate | post_recovery_trade_pnl | new_blockers_after_recovery |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    recovery_counts: dict[str, int] = {}
    post_recovery_pnls: dict[str, float] = {}
    new_blocker_totals: dict[str, int] = {}
    for variant in variants_run:
        variant_blocker_rows = [row for row in original_blocker_rows if row["variant"] == variant]
        recovered_count = sum(1 for row in variant_blocker_rows if row.get("first_flat_achieved"))
        recovery_counts[variant] = recovered_count
        post_recovery_pnls[variant] = sum(
            safe_float(row.get("post_recovery_trade_pnl")) for row in variant_blocker_rows
        )
        new_blocker_totals[variant] = sum(
            int(row.get("new_blockers_after_recovery") or 0) for row in variant_blocker_rows
        )

    for row in variant_summaries:
        variant = row["variant"]
        recovered_count = recovery_counts.get(variant, 0)
        recovery_rate = (recovered_count / original_blockers_count) if original_blockers_count else 0.0
        lines.append(
            f"| {variant} | {row['trades']} | {row['closed']} | {row['blockers']} | "
            f"{safe_float(row['series_mtm']):.4f} | {recovered_count}/{original_blockers_count} | "
            f"{recovery_rate * 100.0:.1f}% | {post_recovery_pnls.get(variant, 0.0):.4f} | "
            f"{new_blocker_totals.get(variant, 0)} |"
        )

    def _best_variant(metric: dict[str, float], *, maximize: bool) -> str | None:
        candidates = {v: metric[v] for v in variants_run if v != "B0" and v in metric}
        if not candidates:
            return None
        return max(candidates, key=lambda k: candidates[k]) if maximize else min(
            candidates, key=lambda k: candidates[k]
        )

    series_mtm_by_variant = {row["variant"]: safe_float(row["series_mtm"]) for row in variant_summaries}
    best_series_mtm_variant = _best_variant(series_mtm_by_variant, maximize=True)
    best_recovery_variant = _best_variant(
        {v: float(recovery_counts.get(v, 0)) for v in variants_run}, maximize=True
    )
    least_drag_variant = _best_variant(post_recovery_pnls, maximize=True)
    fewest_new_blockers_variant = _best_variant(
        {v: float(new_blocker_totals.get(v, 0)) for v in variants_run}, maximize=False
    )

    lines.extend(
        [
            "",
            "## Entscheidungsfragen",
            "",
            f"1. **Welche Variante flacht die meisten der {original_blockers_count} "
            f"Baseline-Blocker ab?** "
            + "; ".join(f"`{v}`: {recovery_counts.get(v, 0)}/{original_blockers_count}" for v in variants_run),
            f"2. **Welche Variante hat die beste series_mtm nach Recovery?** "
            f"{'`' + best_series_mtm_variant + '`' if best_series_mtm_variant else 'n/a'} "
            f"(siehe `variant_summary.csv`; B0 zeigt den ungebremsten A1-Referenzwert).",
            f"3. **Wie groß ist der Post-Recovery-Trade-PnL-Drag je Variante "
            f"(series_mtm der ~93 Extra-Trades nach dem ersten geflatteten Blocker)?** "
            + "; ".join(f"`{v}`: {post_recovery_pnls.get(v, 0.0):.4f}" for v in variants_run),
            f"4. **Wie viele neue Blocker entstehen nach der Recovery je Variante?** "
            + "; ".join(f"`{v}`: {new_blocker_totals.get(v, 0)}" for v in variants_run)
            + f" (geringster Wert: `{fewest_new_blockers_variant}`, wenn abweichend von B0).",
            "5. **Ist ein Cooldown (B2) besser als sofortiges Re-Entry (B0/B5)?** "
            "Vergleiche `post_recovery_trade_pnl` und `new_blockers_after_recovery` von `B2` "
            "gegen `B0`/`B5` in der Tabelle oben; ein klar positiverer Wert bei `B2` "
            "spricht dafür, `reentry_events.csv` (reason=`cooldown_start`) als Kandidat "
            "für einen zeitbasierten Re-Entry-Filter zu isolieren.",
            "6. **Ist ein Fresh-Pullback-Signal (B3) selektiver, und lohnt sich der Verzicht "
            "auf spätere Trades ohne dieses Signal?** Siehe `B3`-Zeile "
            "(`trades`/`series_mtm`) und `research_terminal_reason=no_fresh_pullback_signal` "
            "in `original_blocker_recovery.csv`; wenige Trades bei besserer `series_mtm` als "
            "`B0` wäre ein starkes Argument dafür.",
            "7. **Verhindert das Clean-State-Gate (B4) beschädigte Wiedereintritte?** Siehe "
            "`research_terminal_reason=not_clean_flat_state` in "
            "`original_blocker_recovery.csv` -- jeder solche Fall ist ein Wiedereintritt, "
            "den B0/B5 versucht hätten, aber B4 verweigert.",
            f"8. **Reduziert die gestaffelte Freeze-Eskalation (B5) den MTM-Schaden gegenüber "
            f"dem sofortigen A1-Cycle-Freeze (B0)?** "
            f"series_mtm `B5`={series_mtm_by_variant.get('B5', float('nan')):.4f} vs. "
            f"`B0`={series_mtm_by_variant.get('B0', float('nan')):.4f}; siehe `policy_actions.csv` "
            "für `stage1_exposure_freeze`/`stage2_cycle_freeze`-Ereignisse und deren "
            "`reason` (Hold-Candles / MTM-Schwelle / Exit-Increase-Count).",
            "9. **Existiert ein Runtime-Kandidat aus diesem Research-Audit?** noch kein "
            "Runtime-Kandidat -- dies ist ein reines Research-Ranking ohne Live-/"
            "Runtime-Änderung. Nächster Schritt wäre, die Gewinner-Variante "
            f"(`{best_recovery_variant or 'n/a'}` nach Recovery-Rate, `{least_drag_variant or 'n/a'}` "
            "nach Post-Recovery-Drag) isoliert an einem kleineren Coin-Sample erneut zu "
            "prüfen, bevor irgendeine Config berührt wird.",
            "",
            "### INJUSDT Trade 8 (separater Marker)",
            "- Wird weiterhin explizit getrennt gehalten (`injusdt_trade8_marker` in den "
            "Trade-Zeilen), nie als Policy-Erfolg gezählt.",
            "",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_pipeline(
    *,
    output_root: Path,
    variants: tuple[str, ...] = DEFAULT_VARIANTS,
    max_coins: int | None = None,
) -> dict[str, Any]:
    for variant in variants:
        if variant not in RECOVERY_VARIANTS:
            raise ValueError(f"unknown recovery re-entry variant: {variant}")
    if "B0" not in variants:
        raise ValueError("B0 must be included (parity gate runs first)")

    output_root_resolved = output_root.resolve()
    for protected in PROTECTED_DIRS:
        if output_root_resolved == protected.resolve():
            raise RuntimeError(f"refusing to target a protected directory: {protected}")
    if output_root.exists() and any(output_root.iterdir()):
        raise FileExistsError(f"refusing to overwrite existing output directory: {output_root}")
    output_root.mkdir(parents=True, exist_ok=True)

    applied_params = write_applied_params(output_root, variants=variants)

    coins = load_baseline_coin_list(max_coins=max_coins)
    blocker_rows = load_baseline_blockers(BASELINE_DIR / "blocker_trades.csv")
    target_map = baseline_blocker_trade_number_by_coin(blocker_rows)
    baseline_blocker_by_coin = {str(row.get("coin") or "").strip().upper(): row for row in blocker_rows}
    coins_with_baseline_blocker = [coin for coin in coins if coin in target_map]

    coin_candles: dict[str, list[Any]] = {
        symbol: normalize_candles(symbol, load_candles_for_symbol(symbol, limit=FULL_HISTORY_CANDLE_LIMIT))
        for symbol in coins
    }

    def run_variant(variant: str) -> tuple[list[dict[str, Any]], dict[str, list[BacktestResult]]]:
        freeze_config = freeze_config_for_variant(variant)
        rows: list[dict[str, Any]] = []
        per_coin_results: dict[str, list[BacktestResult]] = {}
        for symbol in coins:
            candles = coin_candles[symbol]
            target = int(target_map.get(symbol, -1))
            recovery_config = RecoveryReentryConfig(variant=variant, target_blocker_trade_number=target)
            payload = run_continuous_reentry_backtests(
                **build_call_kwargs(
                    symbol=symbol,
                    candles=candles,
                    inventory_mtm_freeze_config=freeze_config,
                    recovery_reentry_config=recovery_config,
                )
            )
            results = list(payload["results"])
            per_coin_results[symbol] = results
            for result in results:
                rows.append(
                    build_recovery_trade_row(
                        coin=symbol,
                        variant=variant,
                        result=result,
                        candles=candles,
                        target_blocker_trade_number=target,
                    )
                )
        return rows, per_coin_results

    variant_trade_rows: dict[str, list[dict[str, Any]]] = {}
    variant_bt_results: dict[str, dict[str, list[BacktestResult]]] = {}

    b0_rows, b0_results = run_variant("B0")
    variant_trade_rows["B0"] = b0_rows
    variant_bt_results["B0"] = b0_results

    parity = check_b0_parity(b0_rows)

    manifest: dict[str, Any] = {
        "git": _git_status(),
        "mode": "inventory_mtm_neg1_recovery_reentry_audit",
        "output_root": str(output_root),
        "variants_requested": list(variants),
        "coins": coins,
        "coins_count": len(coins),
        "coins_with_baseline_blocker_count": len(coins_with_baseline_blocker),
        "applied_params": applied_params,
        "b0_parity": {
            "ok": parity["ok"],
            "checks": {name: {"actual": a, "expected": e, "ok": ok} for name, (a, e, ok) in parity["checks"].items()},
            "totals": parity["totals"],
        },
    }

    if not parity["ok"]:
        manifest["aborted_after_b0_parity_failure"] = True
        _write_csv(output_root / "variant_summary.csv", [{"variant": "B0", **parity["totals"]}])
        _write_json(output_root / "run_manifest.json", manifest)
        write_report(
            output_root / "REPORT.md",
            variants_run=list(variants),
            variant_summaries=[{"variant": "B0", **parity["totals"]}],
            parity=parity,
            original_blocker_rows=[],
            original_blockers_count=len(coins_with_baseline_blocker),
            aborted=True,
        )
        return manifest

    variants_to_run = [v for v in variants if v != "B0"]
    for variant in variants_to_run:
        rows, results = run_variant(variant)
        variant_trade_rows[variant] = rows
        variant_bt_results[variant] = results

    all_trade_rows: list[dict[str, Any]] = []
    policy_action_rows: list[dict[str, Any]] = []
    reentry_event_rows: list[dict[str, Any]] = []
    original_blocker_rows: list[dict[str, Any]] = []
    post_recovery_trade_rows: list[dict[str, Any]] = []
    first_flat_event_rows: list[dict[str, Any]] = []
    variant_summaries: list[dict[str, Any]] = []

    for variant in variants:
        rows = variant_trade_rows[variant]
        all_trade_rows.extend(rows)
        for coin, results in variant_bt_results[variant].items():
            for result in results:
                policy_action_rows.extend(build_policy_action_rows(coin=coin, variant=variant, result=result))
                reentry_event_rows.extend(build_reentry_event_rows(coin=coin, variant=variant, result=result))

        variant_summaries.append({"variant": variant, **summarize_variant_rows(rows)})

        rows_by_coin: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            rows_by_coin.setdefault(row["coin"], []).append(row)

        for coin in coins_with_baseline_blocker:
            coin_rows = rows_by_coin.get(coin, [])
            target = int(target_map.get(coin, -1))
            coin_summary = summarize_coin_rows(rows=coin_rows, target_blocker_trade_number=target)
            baseline_row = baseline_blocker_by_coin.get(coin, {})
            original_blocker_rows.append(
                {
                    "variant": variant,
                    "coin": coin,
                    "target_blocker_trade_number": target,
                    "baseline_mtm_pnl": safe_float(baseline_row.get("mtm_pnl")),
                    **coin_summary,
                }
            )
            post_recovery_trade_rows.extend(
                row for row in coin_rows if row.get("post_recovery_trade")
            )
            first_flat_event_rows.extend(
                row for row in coin_rows if row.get("recovered_flat_of_target_blocker")
            )

    _write_csv(output_root / "variant_summary.csv", variant_summaries)
    _write_csv(output_root / "original_blocker_recovery.csv", original_blocker_rows)
    _write_csv(output_root / "post_recovery_trades.csv", post_recovery_trade_rows)
    _write_csv(output_root / "first_flat_events.csv", first_flat_event_rows)
    _write_csv(output_root / "reentry_events.csv", reentry_event_rows)
    _write_csv(output_root / "policy_actions.csv", policy_action_rows)

    manifest.update(
        {
            "variants_run": list(variants),
            "total_trade_rows": len(all_trade_rows),
            "original_blockers_count": len(coins_with_baseline_blocker),
        }
    )
    _write_json(output_root / "run_manifest.json", manifest)

    write_report(
        output_root / "REPORT.md",
        variants_run=list(variants),
        variant_summaries=variant_summaries,
        parity=parity,
        original_blocker_rows=original_blocker_rows,
        original_blockers_count=len(coins_with_baseline_blocker),
        aborted=False,
    )

    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--variants", type=str, default=",".join(DEFAULT_VARIANTS))
    parser.add_argument("--max-coins", type=int, default=None)
    args = parser.parse_args(argv)

    variants = tuple(v.strip() for v in str(args.variants).split(",") if v.strip())

    manifest = run_pipeline(output_root=args.output_dir, variants=variants, max_coins=args.max_coins)
    print(json.dumps(manifest, indent=2, default=str))
    return 0 if manifest.get("b0_parity", {}).get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
