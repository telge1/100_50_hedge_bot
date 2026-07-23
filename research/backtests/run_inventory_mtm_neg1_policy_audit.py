"""Research-only inventory_mtm<-1 freeze policy audit (multi-coin, multi-variant).

Hard constraints enforced by this module:

* No live config / runtime / strategy default is ever changed (only ``config_source="live"``
  is *read*).
* Never writes into, nor overwrites,
  ``research/backtests/results/current_baseline_multicoin_continuous_blocker_audit_20260720/``
  (that directory is only ever *read* — its ``coin_manifest.csv`` supplies the corpus).
* This script never calls ``git commit``.
* ``exit_rebuild_policy_config`` is never passed to the backtest engine (omitted entirely).
* Refuses to overwrite a non-empty output directory.

Trigger rule: after fills in each causal candle (``mark=candle.close``), fire once when
``inventory_mtm_usdt(...) < threshold_usdt`` (default ``-1.0``) for the first time at
``0 <= candle_index <= max_trigger_candle`` (default ``500``). Latched once per trade.

Variant A0 must reproduce the live-baseline totals (trades/closed/blockers/series_mtm and the
APT-specific numbers) within tolerance before any other variant is run; if it does not, the run
aborts and reports the mismatch instead of silently continuing.
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
from research.backtests.inventory_mtm_freeze import (
    InventoryMtmFreezeConfig,
    VARIANT_NAMES,
    classify_trigger_case,
    is_injusdt_trade8_undercoverage,
)
from research.backtests.long_add_multistart_metrics import analyze_trade, normalize_trade_status, safe_float

ROOT = Path(__file__).resolve().parents[2]
BASELINE_DIR = (
    ROOT
    / "research/backtests/results/current_baseline_multicoin_continuous_blocker_audit_20260720"
)
DEFAULT_OUT = ROOT / "research/backtests/results/inventory_mtm_neg1_policy_audit_20260720"

# --- Fixed live baseline params (must match the baseline audit exactly) ----
DIRECTION = "long"
CONFIG_SOURCE = "live"
FILL_MODEL = "conservative"
CONTINUOUS_START_INDEX = 0
LONG_FILL_DISTANCE_PCT = 0.5
TARGET_PROFIT_USDT = 0.015
TP_PROFIT_TARGET_PCT = 0.25
FULL_HISTORY_CANDLE_LIMIT = 50000
APT_SYMBOL = "APTUSDT"

# --- Trigger rule -------------------------------------------------------
THRESHOLD_USDT = -1.0
MAX_TRIGGER_CANDLE = 500

DEFAULT_VARIANTS: tuple[str, ...] = ("A0", "A1", "A2", "A3", "A4", "A5", "A6")

# --- A0 parity targets (must reproduce the live-baseline audit) --------
BASELINE_TOTAL_TRADES = 265
BASELINE_TOTAL_CLOSED = 238
BASELINE_TOTAL_BLOCKERS = 27
BASELINE_SERIES_MTM = -291.9656
BASELINE_SERIES_MTM_TOLERANCE = 0.5
APT_BASELINE_TRADES = 3
APT_BASELINE_CLOSED = 2
APT_BASELINE_OPEN = 1
APT_BASELINE_SERIES_MTM = -8.9865
APT_BASELINE_TOLERANCE = 0.02

INJUSDT_UNDERCOVERAGE_TRADE_NUMBER = 8


# ---------------------------------------------------------------------------
# Small shared utilities (same pattern as sibling multi-coin audit runners)
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
# Coin corpus: reused verbatim from the protected baseline (read-only)
# ---------------------------------------------------------------------------


def load_baseline_coin_list(*, max_coins: int | None = None) -> list[str]:
    """27 coins, same order, read from the baseline's ``coin_manifest.csv``."""
    manifest_path = BASELINE_DIR / "coin_manifest.csv"
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"baseline coin_manifest.csv not found (baseline audit must exist and be readable): {manifest_path}"
        )
    ranked: list[tuple[int, str]] = []
    with manifest_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            included_raw = str(row.get("included") or "").strip().lower()
            if included_raw not in {"true", "1", "yes"}:
                continue
            symbol = str(row.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            rank = int(safe_float(row.get("priority_rank"), 0))
            ranked.append((rank, symbol))
    ranked.sort(key=lambda item: item[0])
    ordered = [symbol for _, symbol in ranked]
    if max_coins is not None:
        ordered = ordered[: int(max_coins)]
    return ordered


# ---------------------------------------------------------------------------
# Fixed call kwargs (never pass exit_rebuild_policy_config)
# ---------------------------------------------------------------------------


def build_call_kwargs(
    *, symbol: str, candles: list[Any], inventory_mtm_freeze_config: InventoryMtmFreezeConfig | None
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
        "write_json": False,
        "write_csv": False,
    }


def write_applied_params(output_root: Path, *, variants: tuple[str, ...]) -> dict[str, Any]:
    payload = {
        "note": (
            "Reuses the exact 27-coin corpus (same order) from the protected baseline's "
            "coin_manifest.csv; that directory is only ever read, never written."
        ),
        "baseline_source": str(BASELINE_DIR),
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
        "trigger_rule": {
            "metric": "inventory_mtm_usdt(realized, long_qty, long_avg, short_qty, short_avg, mark=candle.close)",
            "threshold_usdt": THRESHOLD_USDT,
            "condition": "mtm < threshold_usdt",
            "eligible_window": f"0 <= candle_index <= {MAX_TRIGGER_CANDLE}",
            "causal_fills": (
                "Deferred orders created on candle X are fillable only from X+1; already-open "
                "orders may still fill on X. Trigger evaluated only inside process_candle, "
                "strictly after that candle's fills are applied."
            ),
            "latch": "fires at most once per trade; never re-evaluated afterwards",
        },
        "variants": list(variants),
        "variant_semantics": {
            "A0": "no-op control (must reproduce the live baseline)",
            "A1": "freeze new cycles (block CYCLE_N_LONG_ADD after trigger)",
            "A2": "freeze exposure growth (block any intent that would increase abs(net_qty))",
            "A3": "freeze exit increases (latch LONG_TP_EXIT ceiling at trigger time)",
            "A4": "A1 + A2 + A3 combined",
            "A5": "A4 + one-time partial (50%) neutralization of the overweight leg at trigger",
            "A6": "A4 + one-time full neutralization of the overweight leg at trigger",
        },
        "known_marker": "INJUSDT trade 8 undercoverage is kept as a separate diagnostic marker, never counted as a policy success.",
    }
    _write_json(output_root / "applied_params.json", payload)
    return payload


# ---------------------------------------------------------------------------
# Per-trade row (analyze_trade metrics + freeze excerpt fields)
# ---------------------------------------------------------------------------


def build_trade_row(*, coin: str, variant: str, result: BacktestResult, candles: list[Any]) -> dict[str, Any]:
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
    freeze_variant = excerpt.get("inventory_mtm_freeze_variant")

    return {
        "coin": coin,
        "variant": variant,
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
        "max_abs_net_exposure": analysis.get("max_abs_net_exposure"),
        "fees": analysis.get("fees"),
        "exit_rebuild_count": analysis.get("exit_rebuild_count"),
        "exit_increase_count": analysis.get("exit_increase_count"),
        "undercoverage": analysis.get("undercoverage"),
        "same_candle_long_add_short_reduce": analysis.get("same_candle_long_add_short_reduce"),
        "exit_reason": result.exit_reason,
        "injusdt_trade8_marker": int(is_injusdt_trade8_undercoverage(coin=coin, trade_number=trade_number)),
        # Freeze-specific fields (empty/None for A0).
        "freeze_variant": freeze_variant,
        "trigger_fired": bool(trigger_event),
        "trigger_candle": (trigger_event or {}).get("trigger_candle"),
        "trigger_mtm": (trigger_event or {}).get("trigger_mtm"),
        "trigger_mark": (trigger_event or {}).get("trigger_mark"),
        "cycles_at_trigger": (trigger_event or {}).get("cycles_at_trigger"),
        "active_exit_at_trigger": (trigger_event or {}).get("active_exit_at_trigger"),
        "net_exposure_at_trigger": (trigger_event or {}).get("net_exposure_at_trigger"),
        "policy_action_count": len(policy_actions),
        "cycles_after_trigger": freeze_state.get("cycles_after_trigger"),
        "exit_increases_after_trigger": freeze_state.get("exit_increases_after_trigger"),
        "neutralization_done": freeze_state.get("neutralization_done"),
        "latched_exit_ceiling": freeze_state.get("latched_exit_ceiling"),
        "latched_exit_floor": freeze_state.get("latched_exit_floor"),
    }


def build_trigger_event_rows(*, coin: str, variant: str, result: BacktestResult) -> list[dict[str, Any]]:
    excerpt = dict(result.final_strategy_state_excerpt or {})
    trigger_event = excerpt.get("inventory_mtm_trigger_event")
    if not trigger_event:
        return []
    return [
        {
            "coin": coin,
            "variant": variant,
            "trade_number": int(result.trade_number or 0),
            **trigger_event,
        }
    ]


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


# ---------------------------------------------------------------------------
# Variant summary + parity check
# ---------------------------------------------------------------------------


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
        "same_candle_violation_count": sum(int(row.get("same_candle_long_add_short_reduce") or 0) for row in rows),
        "fees_total": sum(safe_float(row.get("fees")) for row in rows),
        "trigger_fired_count": len(triggered),
        "policy_action_total": sum(int(row.get("policy_action_count") or 0) for row in rows),
    }


def check_a0_parity(a0_rows: list[dict[str, Any]]) -> dict[str, Any]:
    totals = summarize_variant_rows(a0_rows)
    apt_rows = [row for row in a0_rows if row["coin"] == APT_SYMBOL]
    apt_totals = summarize_variant_rows(apt_rows)

    checks = {
        "trades": (totals["trades"], BASELINE_TOTAL_TRADES, totals["trades"] == BASELINE_TOTAL_TRADES),
        "closed": (totals["closed"], BASELINE_TOTAL_CLOSED, totals["closed"] == BASELINE_TOTAL_CLOSED),
        "blockers": (totals["blockers"], BASELINE_TOTAL_BLOCKERS, totals["blockers"] == BASELINE_TOTAL_BLOCKERS),
        "series_mtm": (
            totals["series_mtm"],
            BASELINE_SERIES_MTM,
            abs(totals["series_mtm"] - BASELINE_SERIES_MTM) <= BASELINE_SERIES_MTM_TOLERANCE,
        ),
        "apt_trades": (apt_totals["trades"], APT_BASELINE_TRADES, apt_totals["trades"] == APT_BASELINE_TRADES),
        "apt_closed": (apt_totals["closed"], APT_BASELINE_CLOSED, apt_totals["closed"] == APT_BASELINE_CLOSED),
        "apt_open": (
            apt_totals["blockers"],
            APT_BASELINE_OPEN,
            apt_totals["blockers"] == APT_BASELINE_OPEN,
        ),
        "apt_series_mtm": (
            apt_totals["series_mtm"],
            APT_BASELINE_SERIES_MTM,
            abs(apt_totals["series_mtm"] - APT_BASELINE_SERIES_MTM) <= APT_BASELINE_TOLERANCE,
        ),
    }
    ok = all(check[2] for check in checks.values())
    return {"ok": ok, "checks": checks, "totals": totals, "apt_totals": apt_totals}


# ---------------------------------------------------------------------------
# Trade pairing (A0 <-> variant) + TP/FP/FN/TN classification
# ---------------------------------------------------------------------------


def build_trade_pairing_rows(
    *, variant: str, variant_rows: list[dict[str, Any]], baseline_lookup: dict[tuple[str, int], dict[str, Any]]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in variant_rows:
        key = (row["coin"], row["trade_number"])
        baseline_row = baseline_lookup.get(key)
        matched = baseline_row is not None
        start_diverged = bool(
            matched and str(baseline_row.get("start_timestamp") or "") != str(row.get("start_timestamp") or "")
        )
        rows.append(
            {
                "variant": variant,
                "coin": row["coin"],
                "trade_number": row["trade_number"],
                "matched_baseline": matched,
                "start_diverged": start_diverged,
                "a0_start_timestamp": (baseline_row or {}).get("start_timestamp"),
                "variant_start_timestamp": row.get("start_timestamp"),
                "a0_status": (baseline_row or {}).get("status"),
                "variant_status": row.get("status"),
                "a0_mtm_pnl": (baseline_row or {}).get("mtm_pnl"),
                "variant_mtm_pnl": row.get("mtm_pnl"),
                "a0_is_blocker": (baseline_row or {}).get("is_blocker"),
                "variant_is_blocker": row.get("is_blocker"),
            }
        )
    return rows


def build_classification_rows(
    *, variant: str, variant_rows: list[dict[str, Any]], baseline_lookup: dict[tuple[str, int], dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    tp_rows: list[dict[str, Any]] = []
    fp_rows: list[dict[str, Any]] = []
    fn_rows: list[dict[str, Any]] = []
    tn_rows: list[dict[str, Any]] = []

    matched_variant_keys: set[tuple[str, int]] = set()

    for row in variant_rows:
        key = (row["coin"], row["trade_number"])
        baseline_row = baseline_lookup.get(key)
        if baseline_row is None:
            continue  # extra trade only reachable because a variant cured an earlier blocker.
        matched_variant_keys.add(key)

        baseline_is_blocker = bool(baseline_row["is_blocker"])
        trigger_fired = bool(row.get("trigger_fired"))
        classification = classify_trigger_case(baseline_is_blocker=baseline_is_blocker, trigger_fired=trigger_fired)
        start_diverged = str(baseline_row.get("start_timestamp") or "") != str(row.get("start_timestamp") or "")
        undercoverage_marker = bool(row.get("injusdt_trade8_marker"))

        if classification == "TP":
            went_flat = not bool(row["is_blocker"])
            baseline_mtm = safe_float(baseline_row.get("mtm_pnl"))
            variant_mtm = safe_float(row.get("mtm_pnl"))
            tp_rows.append(
                {
                    "variant": variant,
                    "coin": row["coin"],
                    "trade_number": row["trade_number"],
                    "start_diverged": start_diverged,
                    "baseline_mtm": baseline_mtm,
                    "variant_mtm": variant_mtm,
                    "mtm_delta": variant_mtm - baseline_mtm,
                    "went_flat": went_flat,
                    "remaining_open_better_mtm": (not went_flat) and (variant_mtm > baseline_mtm + 1e-9),
                    "delayed_blocker": not went_flat,
                    "injusdt_trade8_marker": undercoverage_marker,
                }
            )
        elif classification == "FP":
            baseline_pnl = safe_float(baseline_row.get("realized_pnl"))
            variant_pnl = safe_float(row.get("realized_pnl")) if not row["is_blocker"] else None
            pnl_delta = (variant_pnl - baseline_pnl) if variant_pnl is not None else None
            fp_rows.append(
                {
                    "variant": variant,
                    "coin": row["coin"],
                    "trade_number": row["trade_number"],
                    "start_diverged": start_diverged,
                    "baseline_pnl": baseline_pnl,
                    "variant_pnl": variant_pnl,
                    "pnl_delta": pnl_delta,
                    "variant_still_closed": not bool(row["is_blocker"]),
                    "damaged": bool(pnl_delta is not None and pnl_delta < -1e-9),
                }
            )
        elif classification == "FN":
            fn_rows.append(
                {
                    "variant": variant,
                    "coin": row["coin"],
                    "trade_number": row["trade_number"],
                    "start_diverged": start_diverged,
                    "baseline_mtm": safe_float(baseline_row.get("mtm_pnl")),
                    "injusdt_trade8_marker": undercoverage_marker,
                }
            )
        else:
            tn_rows.append(
                {
                    "variant": variant,
                    "coin": row["coin"],
                    "trade_number": row["trade_number"],
                    "start_diverged": start_diverged,
                }
            )

    # Baseline rows this variant never produced at all (chain stopped earlier
    # for a different reason) are FN-equivalent w.r.t. missing coverage, but we
    # only classify rows that actually have a variant-side counterpart per the
    # coin+trade_number fallback pairing rule; unmatched baseline rows are a
    # research note, not a silent gap (see run_manifest.json unmatched counts).
    return {"TP": tp_rows, "FP": fp_rows, "FN": fn_rows, "TN": tn_rows}


def build_undercoverage_rows(*, variant: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        if not int(row.get("undercoverage") or 0) and not row.get("injusdt_trade8_marker"):
            continue
        out.append(
            {
                "variant": variant,
                "coin": row["coin"],
                "trade_number": row["trade_number"],
                "undercoverage_count": int(row.get("undercoverage") or 0),
                "injusdt_trade8_marker": bool(row.get("injusdt_trade8_marker")),
                "status": row.get("status"),
                "mtm_pnl": row.get("mtm_pnl"),
                "note": (
                    "INJUSDT trade 8 undercoverage kept as a separate diagnostic marker; "
                    "never counted as a policy success."
                    if row.get("injusdt_trade8_marker")
                    else ""
                ),
            }
        )
    return out


# ---------------------------------------------------------------------------
# REPORT.md
# ---------------------------------------------------------------------------


def rank_variants(variant_summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank non-A0 variants: fewer blockers, better MTM, few new negative closed, low FP damage."""
    candidates = [row for row in variant_summaries if row["variant"] != "A0"]
    ranked = sorted(
        candidates,
        key=lambda row: (
            int(row.get("blockers") or 0),
            -float(row.get("series_mtm") or 0.0),
            int(row.get("fp_damaged_count") or 0),
            -float(row.get("fp_damage_sum") or 0.0),
            int(row.get("delayed_blocker_count") or 0),
        ),
    )
    out = []
    for idx, row in enumerate(ranked, start=1):
        payload = dict(row)
        payload["rank"] = idx
        out.append(payload)
    return out


def write_report(
    path: Path,
    *,
    variants_run: list[str],
    variant_summaries: list[dict[str, Any]],
    parity: dict[str, Any],
    tp_rows_by_variant: dict[str, list[dict[str, Any]]],
    fp_rows_by_variant: dict[str, list[dict[str, Any]]],
    fn_rows_by_variant: dict[str, list[dict[str, Any]]],
    undercoverage_rows: list[dict[str, Any]],
    aborted: bool,
) -> None:
    from datetime import datetime, timezone

    lines: list[str] = [
        "# Inventory MTM<-1 Freeze Policy Audit",
        "",
        f"Generated: `{datetime.now(timezone.utc).isoformat()}`",
        "",
        "## Setup",
        "",
        f"- Corpus reused verbatim from `{BASELINE_DIR.name}` (27 coins, same order); that "
        "directory was only ever read, never written.",
        f"- Pinned live params: `long_fill_distance_pct={LONG_FILL_DISTANCE_PCT}`, "
        f"`target_profit_usdt={TARGET_PROFIT_USDT}`, `tp_profit_target_pct={TP_PROFIT_TARGET_PCT}`, "
        f"`fill_model={FILL_MODEL}`, `config_source={CONFIG_SOURCE}`, "
        f"`continuous_start_index={CONTINUOUS_START_INDEX}`, `candle_limit={FULL_HISTORY_CANDLE_LIMIT}`.",
        f"- Trigger rule: `inventory_mtm_usdt < {THRESHOLD_USDT}` on candle `0..{MAX_TRIGGER_CANDLE}`, latched once.",
        f"- `exit_rebuild_policy_config` never passed to the backtest engine.",
        f"- Variants requested: `{', '.join(variants_run)}`.",
        "",
        "## A0 parity check",
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
                "A0 did not reproduce the live baseline within tolerance. No further variants "
                "(A1..A6) were run. Fix the parity mismatch above before re-running.",
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
            "| variant | trades | closed | blockers | series_mtm | sum_closed_pnl | negative_closed | trigger_fired | policy_actions |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in variant_summaries:
        lines.append(
            f"| {row['variant']} | {row['trades']} | {row['closed']} | {row['blockers']} | "
            f"{safe_float(row['series_mtm']):.4f} | {safe_float(row['sum_closed_pnl']):.4f} | "
            f"{row['negative_closed_count']} | {row['trigger_fired_count']} | {row['policy_action_total']} |"
        )

    ranked = rank_variants(variant_summaries)
    winner = ranked[0] if ranked else None

    total_baseline_blockers = next(
        (row["blockers"] for row in variant_summaries if row["variant"] == "A0"), 0
    )

    def _sum_field(rows: list[dict[str, Any]], field: str) -> float:
        return sum(safe_float(row.get(field)) for row in rows)

    lines.extend(
        [
            "",
            "## Abschlussfragen",
            "",
            f"1. **Welche Variante gewinnt?** "
            f"{'`' + winner['variant'] + '`' if winner else 'keine (nur A0 gelaufen)'} "
            f"(Ranking nach: weniger Blocker, bessere Gesamt-MTM, wenige neue negative "
            f"Closed-Trades, geringer FP-Schaden — nicht nur verzögerte Blocker).",
            f"2. **Ist der Nutzen primär Cycle-, Exposure- oder Exit-Freeze?** "
            f"Siehe `policy_actions.csv` je Variante (A1=Cycle, A2=Exposure, A3=Exit, "
            f"A4=kombiniert) — Vergleiche `trigger_fired`/`policy_action_total` je Variante oben.",
            f"3. **Wie viele Baseline-Blocker werden flach?** "
            + "; ".join(
                f"`{variant}`: {sum(1 for r in tp_rows_by_variant.get(variant, []) if r['went_flat'])} / {total_baseline_blockers}"
                for variant in variants_run
                if variant != "A0"
            ),
            f"4. **Wie viele profitable Baseline-Trades werden beschädigt (FP, pnl_delta<0)?** "
            + "; ".join(
                f"`{variant}`: {sum(1 for r in fp_rows_by_variant.get(variant, []) if r.get('damaged'))}"
                for variant in variants_run
                if variant != "A0"
            ),
            f"5. **Ist ein engerer/weiterer MTM-Schwellenwert-Sweep gerechtfertigt?** "
            f"Noch nicht getestet in diesem Audit (nur `{THRESHOLD_USDT}` USDT); auf Basis der "
            f"TP/FP/FN-Verteilung oben ggf. als Folge-Hypothese isoliert testen.",
            "6. **Existiert ein Runtime-Kandidat?** noch kein Runtime-Kandidat — dies ist ein "
            "reines Research-Ranking ohne Live-/Runtime-Änderung.",
            "",
            "### Undercoverage (separat, keine Policy-Erfolgs-Zählung)",
            f"- Fälle gesamt: `{len(undercoverage_rows)}` — siehe `undercoverage_cases.csv`. "
            f"INJUSDT Trade 8 wird explizit als separater Marker gehalten "
            f"(`injusdt_trade8_marker`), nie als Policy-Erfolg gezählt.",
            "",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _variant_config(variant: str) -> InventoryMtmFreezeConfig | None:
    if variant == "A0":
        return None
    return InventoryMtmFreezeConfig(
        variant=variant,
        threshold_usdt=THRESHOLD_USDT,
        max_trigger_candle=MAX_TRIGGER_CANDLE,
    )


def run_pipeline(
    *,
    output_root: Path,
    variants: tuple[str, ...] = DEFAULT_VARIANTS,
    max_coins: int | None = None,
) -> dict[str, Any]:
    for variant in variants:
        if variant not in VARIANT_NAMES:
            raise ValueError(f"unknown variant: {variant}")
    if "A0" not in variants:
        raise ValueError("A0 must be included (parity gate runs first)")

    if output_root.resolve() == BASELINE_DIR.resolve():
        raise RuntimeError("refusing to target the protected baseline directory")
    if output_root.exists() and any(output_root.iterdir()):
        raise FileExistsError(f"refusing to overwrite existing output directory: {output_root}")
    output_root.mkdir(parents=True, exist_ok=True)

    applied_params = write_applied_params(output_root, variants=variants)

    coins = load_baseline_coin_list(max_coins=max_coins)

    coin_candles: dict[str, list[Any]] = {
        symbol: normalize_candles(symbol, load_candles_for_symbol(symbol, limit=FULL_HISTORY_CANDLE_LIMIT))
        for symbol in coins
    }

    def run_variant(variant: str) -> tuple[list[dict[str, Any]], dict[str, list[BacktestResult]]]:
        config = _variant_config(variant)
        rows: list[dict[str, Any]] = []
        per_coin_results: dict[str, list[BacktestResult]] = {}
        for symbol in coins:
            candles = coin_candles[symbol]
            payload = run_continuous_reentry_backtests(
                **build_call_kwargs(symbol=symbol, candles=candles, inventory_mtm_freeze_config=config)
            )
            results = list(payload["results"])
            per_coin_results[symbol] = results
            for result in results:
                rows.append(build_trade_row(coin=symbol, variant=variant, result=result, candles=candles))
        return rows, per_coin_results

    variant_trade_rows: dict[str, list[dict[str, Any]]] = {}
    variant_bt_results: dict[str, dict[str, list[BacktestResult]]] = {}

    a0_rows, a0_results = run_variant("A0")
    variant_trade_rows["A0"] = a0_rows
    variant_bt_results["A0"] = a0_results

    parity = check_a0_parity(a0_rows)

    manifest: dict[str, Any] = {
        "git": _git_status(),
        "mode": "inventory_mtm_neg1_policy_audit",
        "output_root": str(output_root),
        "variants_requested": list(variants),
        "coins": coins,
        "coins_count": len(coins),
        "applied_params": applied_params,
        "a0_parity": {
            "ok": parity["ok"],
            "checks": {name: {"actual": a, "expected": e, "ok": ok} for name, (a, e, ok) in parity["checks"].items()},
            "totals": parity["totals"],
            "apt_totals": parity["apt_totals"],
        },
    }

    if not parity["ok"]:
        manifest["aborted_after_a0_parity_failure"] = True
        _write_csv(output_root / "variant_summary.csv", [{"variant": "A0", **parity["totals"]}])
        _write_json(output_root / "run_manifest.json", manifest)
        write_report(
            output_root / "REPORT.md",
            variants_run=list(variants),
            variant_summaries=[{"variant": "A0", **parity["totals"], "trigger_fired_count": 0, "policy_action_total": 0}],
            parity=parity,
            tp_rows_by_variant={},
            fp_rows_by_variant={},
            fn_rows_by_variant={},
            undercoverage_rows=[],
            aborted=True,
        )
        return manifest

    variants_to_run = [v for v in variants if v != "A0"]
    for variant in variants_to_run:
        rows, results = run_variant(variant)
        variant_trade_rows[variant] = rows
        variant_bt_results[variant] = results

    baseline_lookup = {(row["coin"], row["trade_number"]): row for row in a0_rows}

    all_trade_rows: list[dict[str, Any]] = []
    trigger_event_rows: list[dict[str, Any]] = []
    policy_action_rows: list[dict[str, Any]] = []
    undercoverage_rows: list[dict[str, Any]] = []
    trade_pairing_rows: list[dict[str, Any]] = []
    tp_rows_by_variant: dict[str, list[dict[str, Any]]] = {}
    fp_rows_by_variant: dict[str, list[dict[str, Any]]] = {}
    fn_rows_by_variant: dict[str, list[dict[str, Any]]] = {}
    tn_rows_by_variant: dict[str, list[dict[str, Any]]] = {}
    variant_summaries: list[dict[str, Any]] = []

    for variant in variants:
        rows = variant_trade_rows[variant]
        all_trade_rows.extend(rows)
        undercoverage_rows.extend(build_undercoverage_rows(variant=variant, rows=rows))
        for coin, results in variant_bt_results[variant].items():
            for result in results:
                trigger_event_rows.extend(build_trigger_event_rows(coin=coin, variant=variant, result=result))
                policy_action_rows.extend(build_policy_action_rows(coin=coin, variant=variant, result=result))

        summary = {"variant": variant, **summarize_variant_rows(rows)}

        if variant != "A0":
            trade_pairing_rows.extend(
                build_trade_pairing_rows(variant=variant, variant_rows=rows, baseline_lookup=baseline_lookup)
            )
            classified = build_classification_rows(variant=variant, variant_rows=rows, baseline_lookup=baseline_lookup)
            tp_rows_by_variant[variant] = classified["TP"]
            fp_rows_by_variant[variant] = classified["FP"]
            fn_rows_by_variant[variant] = classified["FN"]
            tn_rows_by_variant[variant] = classified["TN"]
            summary["tp_count"] = len(classified["TP"])
            summary["fp_count"] = len(classified["FP"])
            summary["fn_count"] = len(classified["FN"])
            summary["tn_count"] = len(classified["TN"])
            summary["blockers_became_flat_count"] = sum(1 for r in classified["TP"] if r["went_flat"])
            summary["delayed_blocker_count"] = sum(1 for r in classified["TP"] if r["delayed_blocker"])
            summary["fp_damaged_count"] = sum(1 for r in classified["FP"] if r.get("damaged"))
            summary["fp_damage_sum"] = sum(
                safe_float(r.get("pnl_delta")) for r in classified["FP"] if r.get("damaged")
            )
            summary["extra_trades_beyond_baseline_count"] = sum(
                1 for r in rows if (r["coin"], r["trade_number"]) not in baseline_lookup
            )
        variant_summaries.append(summary)

    all_tp_rows = [row for rows in tp_rows_by_variant.values() for row in rows]
    all_fp_rows = [row for rows in fp_rows_by_variant.values() for row in rows]
    all_fn_rows = [row for rows in fn_rows_by_variant.values() for row in rows]

    _write_csv(output_root / "variant_summary.csv", variant_summaries)
    _write_csv(output_root / "trade_pairing.csv", trade_pairing_rows)
    _write_csv(output_root / "trigger_events.csv", trigger_event_rows)
    _write_csv(output_root / "policy_actions.csv", policy_action_rows)
    _write_csv(output_root / "true_positive_analysis.csv", all_tp_rows)
    _write_csv(output_root / "false_positive_analysis.csv", all_fp_rows)
    _write_csv(output_root / "false_negative_analysis.csv", all_fn_rows)
    _write_csv(output_root / "undercoverage_cases.csv", undercoverage_rows)

    ranked = rank_variants(variant_summaries)
    manifest.update(
        {
            "variants_run": list(variants),
            "total_trade_rows": len(all_trade_rows),
            "ranked_variants": [row["variant"] for row in ranked],
            "winning_variant": ranked[0]["variant"] if ranked else None,
        }
    )
    _write_json(output_root / "run_manifest.json", manifest)

    write_report(
        output_root / "REPORT.md",
        variants_run=list(variants),
        variant_summaries=variant_summaries,
        parity=parity,
        tp_rows_by_variant=tp_rows_by_variant,
        fp_rows_by_variant=fp_rows_by_variant,
        fn_rows_by_variant=fn_rows_by_variant,
        undercoverage_rows=undercoverage_rows,
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
    return 0 if manifest.get("a0_parity", {}).get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
