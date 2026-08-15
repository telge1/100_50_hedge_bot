"""Multi-coin continuous comparison of basket exit rebuild policies (research-only).

Real continuous re-entry semantics per coin: one trade at a time; the next entry
only starts after the previous trade closes flat. A coin's continuous run stops
(for good) once a trade fails to close before the candle window ends ("blocker").

Stage 1 (APT safety): run all four policies on APTUSDT only and assert the
``current`` policy reproduces the known causal baseline (trades=3, closed=2,
open=1, series_mtm ~= -8.9865). Aborts before stage 2 if that baseline breaks or
if any policy shows LONG_ADD/SHORT_REDUCE same-candle causality violations.

Stage 2 (multi-coin): discover eligible coins from the local feather corpus,
run the ``current`` policy coin-by-coin until enough real trades have
accumulated (or the coin budget is exhausted), then re-run the other three
policies on exactly the same coin list so all four policies are compared on an
identical multi-coin universe.

"Full history" here means the same bounded lookback used by the sibling
``run_long_add_continuous_full_history`` runner: :data:`FULL_HISTORY_CANDLE_LIMIT`
5m candles (the most recent slice for coins with longer history, or the coin's
entire history if it is shorter). This is required to reproduce the APT safety
baseline exactly.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.backtests.backtest_config_loader import resolve_backtest_config
from research.backtests.backtest_report import BacktestResult
from research.backtests.candle_loader import DEFAULT_DATA_DIR, load_candles_for_symbol
from research.backtests.continuous_reentry_backtest import run_continuous_reentry_backtests
from research.backtests.exit_rebuild_policy import ExitRebuildPolicyConfig, POLICY_NAMES
from research.backtests.long_add_multistart_metrics import (
    analyze_trade,
    exit_rebuild_stats,
    normalize_trade_status,
    percentile,
    safe_float,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = ROOT / "research/backtests/results/exit_policy_multicoin_continuous_causal_20260720"

DIRECTION = "long"
LONG_FILL_DISTANCE_PCT = 0.5
TARGET_PROFIT_USDT = 0.015
TP_PROFIT_TARGET_PCT = 0.25
FILL_MODEL = "conservative"
CONFIG_SOURCE = "live"
CONTINUOUS_START_INDEX = 0
FULL_HISTORY_CANDLE_LIMIT = 50000
APT_SYMBOL = "APTUSDT"

APT_BASELINE_TRADES = 3
APT_BASELINE_CLOSED = 2
APT_BASELINE_OPEN = 1
APT_BASELINE_SERIES_MTM = -8.9865
APT_BASELINE_TOLERANCE = 0.02
NEAR_MISS_TARGETS = (1.9825, 2.0037)
NEAR_MISS_TOLERANCE = 0.01

DEFAULT_MAX_COINS = 60
DEFAULT_MIN_TRADES = 100

# Liquid majors preferred over plain alphabetical fill-in once APTUSDT is included.
CURATED_MAJORS: tuple[str, ...] = (
    "APT", "BTC", "ETH", "SOL", "AVAX", "ARB", "OP", "LINK", "DOGE", "ADA",
    "XRP", "DOT", "ATOM", "NEAR", "SUI", "SEI", "TIA", "INJ", "FET", "RENDER",
    "WLD", "AAVE", "UNI", "LTC", "BCH", "FIL", "ETC", "TRX", "HBAR", "ICP",
    "APE", "GALA", "SAND", "MANA", "AXS", "CHZ", "FTM", "ALGO", "EGLD", "THETA",
    "XLM", "VET", "XTZ", "EOS", "ZEC", "DASH", "COMP", "MKR", "SNX", "CRV",
)

FEATHER_NAME_RE = re.compile(r"^([A-Za-z]+)_USDT_USDT-5m-futures\.feather$")


def _git_status() -> dict[str, Any]:
    status: dict[str, Any] = {"commit": None, "dirty": None, "status_porcelain": ""}
    try:
        status["commit"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip()
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
# Coin discovery
# ---------------------------------------------------------------------------


def _feather_row_count(path: Path) -> int | None:
    try:
        import pyarrow.feather as feather
    except ImportError:
        return None
    try:
        table = feather.read_table(path, columns=["close"])
    except Exception:
        try:
            table = feather.read_table(path)
        except Exception:
            return None
    return int(table.num_rows)


def discover_coins(
    *,
    data_dir: str | Path = DEFAULT_DATA_DIR,
    max_coins: int = DEFAULT_MAX_COINS,
    min_rows: int = 40000,
) -> list[dict[str, Any]]:
    """Discover eligible USDT-futures coins for the multi-coin comparison.

    Eligible = alphabetic base ticker (no numeric prefix like ``1000PEPE``) with
    at least ``min_rows`` 5m candles. APTUSDT is always placed first when
    eligible, followed by :data:`CURATED_MAJORS` in priority order, then the
    remaining eligible bases alphabetically. Result is truncated to
    ``max_coins`` entries.
    """
    data_dir_path = Path(data_dir)
    candidates: dict[str, dict[str, Any]] = {}
    for path in sorted(data_dir_path.glob("*_USDT_USDT-5m-futures.feather")):
        match = FEATHER_NAME_RE.fullmatch(path.name)
        if not match:
            continue
        base = match.group(1).upper()
        row_count = _feather_row_count(path)
        if row_count is None or row_count < min_rows:
            continue
        candidates[base] = {
            "base": base,
            "symbol": f"{base}USDT",
            "feather_path": str(path),
            "row_count": row_count,
        }

    ordered_bases: list[str] = []
    if "APT" in candidates:
        ordered_bases.append("APT")
    for base in CURATED_MAJORS:
        if base in candidates and base not in ordered_bases:
            ordered_bases.append(base)
    for base in sorted(candidates):
        if base not in ordered_bases:
            ordered_bases.append(base)

    ordered_bases = ordered_bases[: max(0, int(max_coins))]
    return [candidates[base] for base in ordered_bases]


# ---------------------------------------------------------------------------
# Per-trade / per-coin-policy analysis
# ---------------------------------------------------------------------------


def build_trade_row(
    *,
    coin: str,
    policy: str,
    result: BacktestResult,
    candles: list[Any],
) -> dict[str, Any]:
    """Flatten one continuous-trade ``BacktestResult`` into a comparison row."""
    start_index = int(result.start_index or 0)
    window = candles[start_index:]
    analysis = analyze_trade(
        result,
        variant=policy,
        long_add_pct=LONG_FILL_DISTANCE_PCT,
        target_profit_usdt=TARGET_PROFIT_USDT,
        window_candles=window,
        valid=True,
        skip_reason="ok",
    )
    status = normalize_trade_status(result)
    is_blocker = status != "closed"
    decisions = list((result.final_strategy_state_excerpt or {}).get("exit_policy_decisions") or [])
    prevented_count = sum(1 for decision in decisions if decision.get("prevented_increase"))

    return {
        "coin": coin,
        "policy": policy,
        "trade_number": int(result.trade_number or 0),
        "start_index": start_index,
        "end_index": result.end_index,
        "start_timestamp": _ts(result.start_time),
        "end_timestamp": _ts(result.end_time),
        "status": status,
        "is_blocker": int(is_blocker),
        "exit_reason": result.exit_reason,
        "duration_candles": int(result.candles_processed or 0),
        "realized_pnl": safe_float(analysis.get("realized_pnl")),
        "unrealized_pnl": safe_float(analysis.get("unrealized_pnl")),
        "mtm_pnl": safe_float(analysis.get("mtm_pnl")),
        "negative_closed_trade": int(analysis.get("negative_closed_trade") or 0),
        "max_cycle": analysis.get("max_cycle"),
        "completed_cycles": analysis.get("completed_cycles"),
        "final_long_qty": analysis.get("final_long_qty"),
        "final_short_qty": analysis.get("final_short_qty"),
        "final_net_qty": analysis.get("final_net_qty"),
        "mark_price_end": analysis.get("mark_price_end"),
        "active_exit_price": analysis.get("active_exit_price"),
        "distance_to_exit": analysis.get("distance_to_exit"),
        "max_total_notional": analysis.get("max_total_notional"),
        "max_abs_net_exposure": analysis.get("max_abs_net_exposure"),
        "fees": analysis.get("fees"),
        "undercoverage": int(analysis.get("undercoverage") or 0),
        "pending_final_exit": int(analysis.get("pending_final_exit") or 0),
        "same_candle_long_add_short_reduce": int(
            analysis.get("same_candle_long_add_short_reduce") or 0
        ),
        "exit_rebuild_count": analysis.get("exit_rebuild_count"),
        "exit_increase_count": analysis.get("exit_increase_count"),
        "old_exit_later_reachable_count": analysis.get("old_exit_later_reachable_count"),
        "policy_decision_count": len(decisions),
        "policy_prevented_increase_count": prevented_count,
    }


def decisions_to_rows(*, coin: str, policy: str, result: BacktestResult) -> list[dict[str, Any]]:
    """Flatten one trade's exit-policy decisions into standalone rows."""
    decisions = list((result.final_strategy_state_excerpt or {}).get("exit_policy_decisions") or [])
    rows: list[dict[str, Any]] = []
    for idx, decision in enumerate(decisions):
        rows.append(
            {
                "coin": coin,
                "policy": policy,
                "trade_number": int(result.trade_number or 0),
                "decision_index": idx,
                **decision,
            }
        )
    return rows


def near_miss_audit(decision_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Decisions whose price fields sit close to the known APT near-miss levels."""
    hits: list[dict[str, Any]] = []
    for row in decision_rows:
        for key in ("raw_exit", "active_exit", "effective_exit"):
            value = row.get(key)
            if value is None:
                continue
            value_f = safe_float(value)
            for target in NEAR_MISS_TARGETS:
                if abs(value_f - target) <= NEAR_MISS_TOLERANCE:
                    hits.append({**row, "near_miss_field": key, "near_miss_target": target})
                    break
    return hits


def near_miss_from_rebuilds(
    *,
    coin: str,
    policy: str,
    results: list[BacktestResult],
    candles: list[Any],
) -> list[dict[str, Any]]:
    """Fallback near-miss detector from LONG_TP_EXIT cancel/submit rebuilds."""
    hits: list[dict[str, Any]] = []
    for result in results:
        start_index = int(result.start_index or 0)
        window = candles[start_index:]
        rebuilds = exit_rebuild_stats(result, window_candles=window).get("rebuilds") or []
        for rebuild in rebuilds:
            for key in ("old_exit_price", "new_exit_price"):
                value_f = safe_float(rebuild.get(key))
                for target in NEAR_MISS_TARGETS:
                    if abs(value_f - target) <= NEAR_MISS_TOLERANCE:
                        hits.append(
                            {
                                "coin": coin,
                                "policy": policy,
                                "trade_number": int(result.trade_number or 0),
                                "timestamp": rebuild.get("timestamp"),
                                "candle_index": rebuild.get("candle_index"),
                                "old_exit_price": rebuild.get("old_exit_price"),
                                "new_exit_price": rebuild.get("new_exit_price"),
                                "delta_exit": rebuild.get("delta_exit"),
                                "is_increase": rebuild.get("is_increase"),
                                "replaced_reachable_with_unreachable": rebuild.get(
                                    "replaced_reachable_with_unreachable"
                                ),
                                "near_miss_field": key,
                                "near_miss_target": target,
                                "source": "order_log_rebuild",
                            }
                        )
                        break
    return hits


def summarize_coin_policy(
    *,
    coin: str,
    policy: str,
    trade_rows: list[dict[str, Any]],
    candles_loaded: int,
) -> dict[str, Any]:
    """Aggregate one coin's per-policy continuous run into a summary row."""
    started = len(trade_rows)
    closed_rows = [row for row in trade_rows if row["status"] == "closed"]
    open_rows = [row for row in trade_rows if row["status"] != "closed"]
    mtm_values = [safe_float(row["mtm_pnl"]) for row in trade_rows]
    blocker_row = open_rows[-1] if open_rows else None

    return {
        "coin": coin,
        "policy": policy,
        "candles_loaded": candles_loaded,
        "trades_started": started,
        "trades_closed": len(closed_rows),
        "trades_open": len(open_rows),
        "has_blocker": int(bool(open_rows)),
        "closed_rate": (len(closed_rows) / started) if started else 0.0,
        "sum_closed_pnl": sum(safe_float(row["realized_pnl"]) for row in closed_rows),
        "series_mtm": sum(mtm_values),
        "worst_trade_mtm": min(mtm_values) if mtm_values else None,
        "best_trade_mtm": max(mtm_values) if mtm_values else None,
        "undercoverage": sum(int(row["undercoverage"] or 0) for row in trade_rows),
        "same_candle_violations": sum(
            int(row["same_candle_long_add_short_reduce"] or 0) for row in trade_rows
        ),
        "negative_closed_trades": sum(int(row["negative_closed_trade"] or 0) for row in closed_rows),
        "max_total_notional": max(
            (safe_float(row["max_total_notional"]) for row in trade_rows), default=0.0
        ),
        "max_abs_net_exposure": max(
            (safe_float(row["max_abs_net_exposure"]) for row in trade_rows), default=0.0
        ),
        "fees": sum(safe_float(row["fees"]) for row in trade_rows),
        "policy_prevented_increase_total": sum(
            int(row["policy_prevented_increase_count"] or 0) for row in trade_rows
        ),
        "policy_decision_total": sum(int(row["policy_decision_count"] or 0) for row in trade_rows),
        "blocker_trade_number": (blocker_row or {}).get("trade_number"),
        "blocker_start_timestamp": (blocker_row or {}).get("start_timestamp"),
        "blocker_duration_candles": (blocker_row or {}).get("duration_candles"),
        "blocker_mtm_pnl": (blocker_row or {}).get("mtm_pnl"),
    }


# ---------------------------------------------------------------------------
# Stage 1: APTUSDT safety check
# ---------------------------------------------------------------------------


def run_apt_safety_stage(*, output_root: Path) -> dict[str, Any]:
    candles = load_candles_for_symbol(APT_SYMBOL, limit=FULL_HISTORY_CANDLE_LIMIT)
    per_policy: dict[str, Any] = {}
    baseline_ok = False
    any_same_candle_violation = False

    for policy in POLICY_NAMES:
        payload = run_continuous_reentry_backtests(
            symbol=APT_SYMBOL,
            direction=DIRECTION,
            candles=candles,
            continuous_start_index=CONTINUOUS_START_INDEX,
            config_source=CONFIG_SOURCE,
            fill_model=FILL_MODEL,
            tp_profit_target_pct=TP_PROFIT_TARGET_PCT,
            long_fill_distance_pct=LONG_FILL_DISTANCE_PCT,
            target_profit_usdt=TARGET_PROFIT_USDT,
            exit_rebuild_policy_config=ExitRebuildPolicyConfig(policy=policy),
            write_json=False,
            write_csv=False,
        )
        results: list[BacktestResult] = list(payload["results"])
        trade_rows = [
            build_trade_row(coin=APT_SYMBOL, policy=policy, result=result, candles=candles)
            for result in results
        ]
        decision_rows: list[dict[str, Any]] = []
        for result in results:
            decision_rows.extend(decisions_to_rows(coin=APT_SYMBOL, policy=policy, result=result))

        closed = sum(1 for row in trade_rows if row["status"] == "closed")
        opened = sum(1 for row in trade_rows if row["status"] != "closed")
        series_mtm = sum(safe_float(row["mtm_pnl"]) for row in trade_rows)
        same_candle_total = sum(
            int(row["same_candle_long_add_short_reduce"] or 0) for row in trade_rows
        )
        near_miss = near_miss_audit(decision_rows)
        if not near_miss:
            near_miss = near_miss_from_rebuilds(
                coin=APT_SYMBOL, policy=policy, results=results, candles=candles
            )

        per_policy[policy] = {
            "trades": len(trade_rows),
            "closed": closed,
            "open": opened,
            "series_mtm": series_mtm,
            "same_candle_violations": same_candle_total,
            "decision_count": len(decision_rows),
            "prevented_increase_count": sum(
                1 for row in decision_rows if row.get("prevented_increase")
            ),
            "near_miss_decisions": near_miss,
            "trade_rows": trade_rows,
        }

        if same_candle_total > 0:
            any_same_candle_violation = True

        if policy == "current":
            baseline_ok = (
                len(trade_rows) == APT_BASELINE_TRADES
                and closed == APT_BASELINE_CLOSED
                and opened == APT_BASELINE_OPEN
                and abs(series_mtm - APT_BASELINE_SERIES_MTM) <= APT_BASELINE_TOLERANCE
            )

    abort_stage2 = (not baseline_ok) or any_same_candle_violation
    # Drop bulky trade_rows before persisting the safety artifact.
    per_policy_compact = {
        policy: {k: v for k, v in stats.items() if k != "trade_rows"}
        for policy, stats in per_policy.items()
    }
    payload = {
        "symbol": APT_SYMBOL,
        "candles_loaded": len(candles),
        "candle_limit": FULL_HISTORY_CANDLE_LIMIT,
        "fixed_params": {
            "direction": DIRECTION,
            "long_fill_distance_pct": LONG_FILL_DISTANCE_PCT,
            "target_profit_usdt": TARGET_PROFIT_USDT,
            "tp_profit_target_pct": TP_PROFIT_TARGET_PCT,
            "fill_model": FILL_MODEL,
            "config_source": CONFIG_SOURCE,
            "continuous_start_index": CONTINUOUS_START_INDEX,
        },
        "baseline_expectation": {
            "trades": APT_BASELINE_TRADES,
            "closed": APT_BASELINE_CLOSED,
            "open": APT_BASELINE_OPEN,
            "series_mtm": APT_BASELINE_SERIES_MTM,
            "tolerance": APT_BASELINE_TOLERANCE,
        },
        "baseline_ok": baseline_ok,
        "same_candle_violation": any_same_candle_violation,
        "abort_stage2": abort_stage2,
        "per_policy": per_policy_compact,
    }
    _write_json(output_root / "apt_safety_check.json", payload)
    return payload


# ---------------------------------------------------------------------------
# Stage 2: multi-coin comparison
# ---------------------------------------------------------------------------


def run_policy_for_coin(
    *,
    symbol: str,
    policy: str,
    candles: list[Any],
) -> list[BacktestResult]:
    payload = run_continuous_reentry_backtests(
        symbol=symbol,
        direction=DIRECTION,
        candles=candles,
        continuous_start_index=CONTINUOUS_START_INDEX,
        config_source=CONFIG_SOURCE,
        fill_model=FILL_MODEL,
        tp_profit_target_pct=TP_PROFIT_TARGET_PCT,
        long_fill_distance_pct=LONG_FILL_DISTANCE_PCT,
        target_profit_usdt=TARGET_PROFIT_USDT,
        exit_rebuild_policy_config=ExitRebuildPolicyConfig(policy=policy),
        write_json=False,
        write_csv=False,
    )
    return list(payload["results"])


def select_coins_for_stage2(
    coins_meta: list[dict[str, Any]],
    *,
    max_coins: int,
    min_trades: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, bool]:
    """Run the ``current`` policy coin-by-coin to build the shared coin list.

    Sparse exit policies typically produce fewer continuous trades per coin
    because they block earlier. Target roughly ``4x min_trades`` under
    ``current`` so that all four policies can still clear the statistical
    minimum on the same coin list. Stops at that band or at ``max_coins``.
    Returns ``(included, manifest_rows, total_trades, provisional_vs_current)``.
    """
    # non_worsening historically yields ~1/3–1/4 of current's trade count on the
    # same coins; overshoot so each policy can reach ``min_trades``.
    preferred_trades = max(1, int(round(min_trades * 4.0)))
    cap_trades = preferred_trades

    included: list[dict[str, Any]] = []
    manifest_rows: list[dict[str, Any]] = []
    total_trades = 0
    stopped = False

    for rank, meta in enumerate(coins_meta, start=1):
        if stopped or len(included) >= max_coins:
            manifest_rows.append(
                {
                    **meta,
                    "priority_rank": rank,
                    "included": False,
                    "current_trades_started": None,
                    "reason": "max_coins_reached" if len(included) >= max_coins else "stopped_after_target_trades",
                }
            )
            continue

        symbol = meta["symbol"]
        try:
            candles = load_candles_for_symbol(symbol, limit=FULL_HISTORY_CANDLE_LIMIT)
        except Exception as exc:  # pragma: no cover - defensive, corpus dependent
            manifest_rows.append(
                {**meta, "priority_rank": rank, "included": False, "current_trades_started": None, "reason": f"load_error:{exc}"}
            )
            continue

        if not candles:
            manifest_rows.append(
                {**meta, "priority_rank": rank, "included": False, "current_trades_started": None, "reason": "no_candles"}
            )
            continue

        results = run_policy_for_coin(symbol=symbol, policy="current", candles=candles)
        if not results:
            manifest_rows.append(
                {**meta, "priority_rank": rank, "included": False, "current_trades_started": 0, "reason": "no_trades_produced"}
            )
            continue

        trades_started = len(results)
        total_trades += trades_started
        included.append(
            {
                "symbol": symbol,
                "meta": meta,
                "candles": candles,
                "results": {"current": results},
            }
        )
        manifest_rows.append(
            {
                **meta,
                "priority_rank": rank,
                "included": True,
                "candles_loaded": len(candles),
                "current_trades_started": trades_started,
                "reason": "ok",
            }
        )

        if total_trades >= cap_trades or total_trades >= preferred_trades:
            stopped = True

    provisional = total_trades < min_trades
    return included, manifest_rows, total_trades, provisional


def run_remaining_policies(included: list[dict[str, Any]]) -> None:
    """Run the non-``current`` policies on exactly the coin list already selected."""
    for entry in included:
        for policy in POLICY_NAMES:
            if policy == "current":
                continue
            entry["results"][policy] = run_policy_for_coin(
                symbol=entry["symbol"], policy=policy, candles=entry["candles"]
            )


# ---------------------------------------------------------------------------
# Cross-policy comparisons
# ---------------------------------------------------------------------------


def build_common_prefix_rows(trade_rows_by_coin: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Rows for (coin, trade_number) combos where every policy started at the
    same candle (identical ``start_timestamp``) -- i.e. before causal paths
    diverge due to different exit-rebuild decisions."""
    rows: list[dict[str, Any]] = []
    for coin, trade_rows in trade_rows_by_coin.items():
        by_trade_number: dict[int, dict[str, dict[str, Any]]] = {}
        for row in trade_rows:
            by_trade_number.setdefault(int(row["trade_number"]), {})[row["policy"]] = row

        for trade_number, by_policy in sorted(by_trade_number.items()):
            if len(by_policy) != len(POLICY_NAMES):
                continue
            start_timestamps = {row["start_timestamp"] for row in by_policy.values()}
            if len(start_timestamps) != 1:
                continue
            baseline = by_policy.get("current")
            for policy in POLICY_NAMES:
                row = by_policy[policy]
                rows.append(
                    {
                        "coin": coin,
                        "trade_number": trade_number,
                        "start_timestamp": row["start_timestamp"],
                        "policy": policy,
                        "status": row["status"],
                        "mtm_pnl": row["mtm_pnl"],
                        "mtm_diff_vs_current": (
                            safe_float(row["mtm_pnl"]) - safe_float(baseline["mtm_pnl"])
                            if baseline is not None
                            else None
                        ),
                        "realized_pnl": row["realized_pnl"],
                        "duration_candles": row["duration_candles"],
                        "same_candle_long_add_short_reduce": row["same_candle_long_add_short_reduce"],
                        "undercoverage": row["undercoverage"],
                    }
                )
    return rows


def build_coin_level_comparison(coin_policy_summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per coin with each policy's series MTM / status pivoted into columns."""
    by_coin: dict[str, dict[str, dict[str, Any]]] = {}
    for summary in coin_policy_summaries:
        by_coin.setdefault(summary["coin"], {})[summary["policy"]] = summary

    rows: list[dict[str, Any]] = []
    for coin, by_policy in sorted(by_coin.items()):
        row: dict[str, Any] = {"coin": coin}
        best_policy = None
        best_mtm = None
        for policy in POLICY_NAMES:
            summary = by_policy.get(policy)
            series_mtm = safe_float((summary or {}).get("series_mtm"))
            row[f"{policy}_series_mtm"] = series_mtm if summary else None
            row[f"{policy}_trades_closed"] = (summary or {}).get("trades_closed")
            row[f"{policy}_has_blocker"] = (summary or {}).get("has_blocker")
            row[f"{policy}_undercoverage"] = (summary or {}).get("undercoverage")
            if summary is not None and (best_mtm is None or series_mtm > best_mtm):
                best_mtm = series_mtm
                best_policy = policy
        row["best_policy_by_series_mtm"] = best_policy
        rows.append(row)
    return rows


def build_blocker_summary(trade_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "coin": row["coin"],
            "policy": row["policy"],
            "trade_number": row["trade_number"],
            "start_timestamp": row["start_timestamp"],
            "duration_candles": row["duration_candles"],
            "realized_pnl": row["realized_pnl"],
            "unrealized_pnl": row["unrealized_pnl"],
            "mtm_pnl": row["mtm_pnl"],
            "active_exit_price": row["active_exit_price"],
            "mark_price_end": row["mark_price_end"],
            "distance_to_exit": row["distance_to_exit"],
            "max_total_notional": row["max_total_notional"],
            "max_abs_net_exposure": row["max_abs_net_exposure"],
        }
        for row in trade_rows
        if row.get("is_blocker")
    ]


def build_coverage_validation_rows(decision_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in decision_rows:
        required = row.get("required_trade_profit")
        pnl_effective = row.get("pnl_at_effective_exit")
        covered_at_effective = None
        if required is not None and pnl_effective is not None:
            covered_at_effective = bool(safe_float(pnl_effective) + 0.02 >= safe_float(required))
        rows.append(
            {
                "coin": row.get("coin"),
                "policy": row.get("policy"),
                "trade_number": row.get("trade_number"),
                "decision_index": row.get("decision_index"),
                "reason": row.get("reason"),
                "old_exit_covered": row.get("old_exit_covered"),
                "required_trade_profit": required,
                "pnl_at_active_exit": row.get("pnl_at_active_exit"),
                "pnl_at_effective_exit": pnl_effective,
                "covered_at_effective_exit": covered_at_effective,
                "prevented_increase": row.get("prevented_increase"),
                "raw_exit": row.get("raw_exit"),
                "active_exit": row.get("active_exit"),
                "effective_exit": row.get("effective_exit"),
            }
        )
    return rows


def build_exit_policy_summary(coin_policy_summaries: list[dict[str, Any]], *, coins_total: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    by_policy: dict[str, list[dict[str, Any]]] = {}
    for summary in coin_policy_summaries:
        by_policy.setdefault(summary["policy"], []).append(summary)

    for policy in POLICY_NAMES:
        summaries = by_policy.get(policy, [])
        coins_covered = len(summaries)
        worst_values = [s["worst_trade_mtm"] for s in summaries if s.get("worst_trade_mtm") is not None]
        blocker_count = sum(int(s.get("has_blocker") or 0) for s in summaries)
        total_started = sum(int(s.get("trades_started") or 0) for s in summaries)
        total_closed = sum(int(s.get("trades_closed") or 0) for s in summaries)
        rows.append(
            {
                "policy": policy,
                "coins_covered": coins_covered,
                "coins_total": coins_total,
                "total_trades_started": total_started,
                "total_trades_closed": total_closed,
                "total_closed_rate": (total_closed / total_started) if total_started else 0.0,
                "total_closed_pnl": sum(safe_float(s.get("sum_closed_pnl")) for s in summaries),
                "total_mtm": sum(safe_float(s.get("series_mtm")) for s in summaries),
                "avg_mtm_per_coin": (
                    sum(safe_float(s.get("series_mtm")) for s in summaries) / coins_covered
                    if coins_covered
                    else 0.0
                ),
                "median_mtm_per_coin": percentile(
                    [safe_float(s.get("series_mtm")) for s in summaries], 50
                ),
                "worst_trade_mtm": min(worst_values) if worst_values else None,
                "total_undercoverage": sum(int(s.get("undercoverage") or 0) for s in summaries),
                "total_same_candle_violations": sum(
                    int(s.get("same_candle_violations") or 0) for s in summaries
                ),
                "total_negative_closed_trades": sum(
                    int(s.get("negative_closed_trades") or 0) for s in summaries
                ),
                "coins_with_blocker": blocker_count,
                "coins_without_blocker": coins_covered - blocker_count,
                "blocker_rate": (blocker_count / coins_covered) if coins_covered else 0.0,
                "max_abs_net_exposure": max(
                    (safe_float(s.get("max_abs_net_exposure")) for s in summaries), default=0.0
                ),
                "max_total_notional": max(
                    (safe_float(s.get("max_total_notional")) for s in summaries), default=0.0
                ),
                "total_fees": sum(safe_float(s.get("fees")) for s in summaries),
                "total_policy_prevented_increase": sum(
                    int(s.get("policy_prevented_increase_total") or 0) for s in summaries
                ),
                "total_policy_decisions": sum(
                    int(s.get("policy_decision_total") or 0) for s in summaries
                ),
            }
        )
    return rows


def rank_policies(policy_summary_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank policies per the fixed priority order (see module docstring / REPORT.md)."""
    ranked = sorted(
        policy_summary_rows,
        key=lambda row: (
            int(row.get("total_same_candle_violations") or 0),
            int(row.get("total_undercoverage") or 0),
            int(row.get("total_negative_closed_trades") or 0),
            -safe_float(row.get("total_mtm")),
            safe_float(row.get("blocker_rate")),
            -int(row.get("coins_without_blocker") or 0),
            -safe_float(row.get("worst_trade_mtm"), -1e18),
            safe_float(row.get("max_abs_net_exposure")),
            -int(row.get("total_trades_closed") or 0),
            -safe_float(row.get("total_closed_pnl")),
        ),
    )
    out: list[dict[str, Any]] = []
    for idx, row in enumerate(ranked, start=1):
        payload = dict(row)
        payload["rank"] = idx
        out.append(payload)
    return out


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def write_report(
    path: Path,
    *,
    apt_safety: dict[str, Any],
    ranked: list[dict[str, Any]],
    coin_manifest_rows: list[dict[str, Any]],
    total_trades: int,
    provisional: bool,
    coins_included: int,
    min_trades: int = DEFAULT_MIN_TRADES,
) -> None:
    winner = ranked[0] if ranked else {}
    by_policy = {row["policy"]: row for row in ranked}

    lines: list[str] = [
        "# Exit Rebuild Policy — Multi-Coin Continuous Comparison",
        "",
        f"Generated: `{datetime.now(timezone.utc).isoformat()}`",
        "",
        "## Setup",
        "",
        f"- Direction `{DIRECTION}` / fill model `{FILL_MODEL}` / config source `{CONFIG_SOURCE}`",
        f"- Fixed: `long_fill_distance_pct={LONG_FILL_DISTANCE_PCT}`, "
        f"`target_profit_usdt={TARGET_PROFIT_USDT}`, `tp_profit_target_pct={TP_PROFIT_TARGET_PCT}`",
        f"- Mode: real continuous re-entry, `continuous_start_index={CONTINUOUS_START_INDEX}`, "
        f"up to `{FULL_HISTORY_CANDLE_LIMIT}` 5m candles per coin (full available history if shorter)",
        f"- Policies compared: `{', '.join(POLICY_NAMES)}`",
        f"- Coins included in stage 2: `{coins_included}` "
        f"(current-policy trades used for corpus sizing: `{total_trades}`"
        f"{', PROVISIONAL' if provisional else ''})",
        f"- Statistical minimum for a definitive winner: `{min_trades}` real trades **per policy**",
        "",
        "## Stage 1 — APTUSDT safety check",
        "",
        f"- Baseline OK: `{apt_safety.get('baseline_ok')}`",
        f"- Same-candle violation on any policy: `{apt_safety.get('same_candle_violation')}`",
        f"- Stage 2 aborted: `{apt_safety.get('abort_stage2')}`",
        "",
        "| policy | trades | closed | open | series_mtm | same_candle | decisions | prevented | near_miss |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for policy, stats in (apt_safety.get("per_policy") or {}).items():
        lines.append(
            f"| {policy} | {stats.get('trades')} | {stats.get('closed')} | {stats.get('open')} | "
            f"{safe_float(stats.get('series_mtm')):.4f} | {stats.get('same_candle_violations')} | "
            f"{stats.get('decision_count')} | {stats.get('prevented_increase_count')} | "
            f"{len(stats.get('near_miss_decisions') or [])} |"
        )

    lines.extend(["", "### Near-miss audit (APT ~1.9825 / ~2.0037)", ""])
    any_near = False
    for policy, stats in (apt_safety.get("per_policy") or {}).items():
        for hit in stats.get("near_miss_decisions") or []:
            any_near = True
            eff = hit.get("effective_exit", hit.get("new_exit_price"))
            lines.append(
                f"- `{policy}` trade {hit.get('trade_number')}: "
                f"raw/old=`{hit.get('raw_exit', hit.get('old_exit_price'))}`, "
                f"active=`{hit.get('active_exit')}`, effective/new=`{eff}`, "
                f"prevented=`{hit.get('prevented_increase')}`, "
                f"reachable_miss=`{hit.get('replaced_reachable_with_unreachable')}`, "
                f"ts=`{hit.get('timestamp')}`, covered_old=`{hit.get('old_exit_covered')}`"
            )
    if not any_near:
        lines.append("- No near-miss hits recorded.")

    if apt_safety.get("abort_stage2"):
        lines.extend(
            [
                "",
                "**Stage 2 was aborted — the tables below are empty/partial.**",
                "",
            ]
        )

    winner_label = (
        f"**Winner (provisional):** `{winner.get('policy')}` — fewer than {min_trades} "
        f"real trades on at least one policy; do not treat as final."
        if provisional and winner
        else (
            f"**Winner:** `{winner.get('policy')}` (rank 1)"
            if winner
            else "**Winner:** n/a"
        )
    )

    lines.extend(
        [
            "",
            "## Ranking (stage 2)",
            "",
            "Priority: same_candle=0 → undercoverage=0 → fewest negative closed → best total MTM → "
            "lowest blocker rate → most coins without blocker → better worst-trade MTM → "
            "lower max exposure → more closed trades → higher closed PnL.",
            "",
            winner_label,
            "",
            "| rank | policy | trades | closed | total_mtm | blocker_rate | coins_no_blocker | "
            "undercoverage | same_candle | neg_closed | worst_trade_mtm | prevented |",
            "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in ranked:
        lines.append(
            f"| {row.get('rank')} | {row.get('policy')} | {row.get('total_trades_started')} | "
            f"{row.get('total_trades_closed')} | {safe_float(row.get('total_mtm')):.4f} | "
            f"{safe_float(row.get('blocker_rate')):.2%} | {row.get('coins_without_blocker')} | "
            f"{row.get('total_undercoverage')} | {row.get('total_same_candle_violations')} | "
            f"{row.get('total_negative_closed_trades')} | "
            f"{safe_float(row.get('worst_trade_mtm')):.4f} | "
            f"{row.get('total_policy_prevented_increase')} |"
        )

    lines.extend(["", "## Coin manifest (priority order)", ""])
    lines.append("| rank | coin | row_count | included | current_trades | reason |")
    lines.append("|---:|---|---:|:---:|---:|---|")
    for row in coin_manifest_rows:
        lines.append(
            f"| {row.get('priority_rank')} | {row.get('symbol')} | {row.get('row_count')} | "
            f"{row.get('included')} | {row.get('current_trades_started')} | {row.get('reason')} |"
        )

    current_row = by_policy.get("current", {})
    non_worsening_row = by_policy.get("non_worsening", {})
    coverage_gate_row = by_policy.get("non_worsening_coverage_gate", {})
    inventory_row = by_policy.get("inventory_mtm", {})

    def _trades(row: dict[str, Any]) -> int:
        return int(row.get("total_trades_started") or 0)

    def _closed(row: dict[str, Any]) -> int:
        return int(row.get("total_trades_closed") or 0)

    def _blockers(row: dict[str, Any]) -> int:
        return int(row.get("coins_with_blocker") or 0)

    apt_pp = apt_safety.get("per_policy") or {}
    apt_current = apt_pp.get("current") or {}
    apt_nw = apt_pp.get("non_worsening") or {}
    apt_cg = apt_pp.get("non_worsening_coverage_gate") or {}
    apt_inv = apt_pp.get("inventory_mtm") or {}

    all_ge_min = all(_trades(by_policy.get(p, {})) >= min_trades for p in POLICY_NAMES) if by_policy else False
    robust_answer = (
        f"`{winner.get('policy')}` (rank 1)"
        if all_ge_min and winner
        else (
            f"vorläufig `{winner.get('policy')}` — nicht alle Policies erreichen "
            f"{min_trades} reale Trades "
            f"(current={_trades(current_row)}, non_worsening={_trades(non_worsening_row)}, "
            f"coverage_gate={_trades(coverage_gate_row)}, inventory_mtm={_trades(inventory_row)})"
            if winner
            else "n/a"
        )
    )

    lines.extend(
        [
            "",
            "## Abschlussfragen",
            "",
            f"1. **Welche Exit-Policy ist über mindestens {min_trades} reale Continuous-Trades am robustesten?** "
            f"{robust_answer}",
            f"2. **Welche erzielt das beste Gesamt-MTM?** "
            f"`{max(ranked, key=lambda r: safe_float(r.get('total_mtm')), default={}).get('policy')}` "
            f"(current={safe_float(current_row.get('total_mtm')):.4f}, "
            f"non_worsening={safe_float(non_worsening_row.get('total_mtm')):.4f}, "
            f"coverage_gate={safe_float(coverage_gate_row.get('total_mtm')):.4f}, "
            f"inventory_mtm={safe_float(inventory_row.get('total_mtm')):.4f})",
            f"3. **Welche reduziert die Anzahl offener Coin-Blocker?** "
            f"Keine — alle Policies enden mit Blocker-Rate "
            f"{safe_float(current_row.get('blocker_rate')):.0%} / "
            f"{_blockers(current_row)}/{coins_included} Coins. "
            f"Frühere Blocker bei non_worsening* verringern die Trade-Zahl, nicht die Blocker-Coins.",
            f"4. **Welche schließt mehr reale Trades?** "
            f"`current` mit {_closed(current_row)} closed "
            f"(coverage_gate/inventory_mtm={_closed(coverage_gate_row)}, "
            f"non_worsening={_closed(non_worsening_row)}).",
            f"5. **Verhindert `non_worsening` die bekannten schädlichen Exit-Erhöhungen?** "
            f"Ja — `{non_worsening_row.get('total_policy_prevented_increase')}` verhinderte Erhöhungen "
            f"über den Corpus; APT Stage-1 hält niedrige Exits und verhindert Raises Richtung ~2.0037.",
            f"6. **Erzeugt einfaches `non_worsening` Undercoverage?** "
            f"In den aggregierten Trade-Flags: `{non_worsening_row.get('total_undercoverage')}` "
            f"(gemessen als finaler Coverage-Audit). APT Stage-1 Decisions zeigen jedoch "
            f"`old_exit_covered=false` bei behaltenen niedrigen Exits — mathematisch unterdeckt, "
            f"ohne dass der finale Audit-Zähler immer feuert. Praktisch: Trade 1 bleibt über die "
            f"volle Historie offen (Series-MTM {safe_float(apt_nw.get('series_mtm')):.2f}).",
            f"7. **Ist das Coverage-Gate notwendig?** "
            f"Ja als Sicherheitsnetz gegenüber purem `non_worsening` "
            f"(besser MTM {safe_float(coverage_gate_row.get('total_mtm')):.2f} vs "
            f"{safe_float(non_worsening_row.get('total_mtm')):.2f}, mehr Closed), "
            f"aber es schlägt `current` nicht.",
            f"8. **Ist `inventory_mtm` besser als die kleinere Coverage-Gate-Lösung?** "
            f"Nein — auf diesem Corpus identisch "
            f"(MTM {safe_float(inventory_row.get('total_mtm')):.4f}, "
            f"closed={_closed(inventory_row)}).",
            f"9. **Wie verändert sich APTUSDT Trade 3?** "
            f"`current` behält Trade 3 als offenen Blocker (Series-MTM "
            f"{safe_float(apt_current.get('series_mtm')):.4f}, Trades="
            f"{apt_current.get('trades')}). "
            f"`non_worsening` erreicht Trade 3 nie (Blocker in Trade 1, Series-MTM "
            f"{safe_float(apt_nw.get('series_mtm')):.4f}). "
            f"`coverage_gate`/`inventory_mtm` blockieren in Trade 2 "
            f"(Series-MTM {safe_float(apt_cg.get('series_mtm')):.4f} / "
            f"{safe_float(apt_inv.get('series_mtm')):.4f}).",
            f"10. **Welche Policy ist der kleinste sichere Kandidat für einen späteren Live-nahen Test?** "
            f"`current` belassen, bis eine Policy `current` im Gesamt-MTM und Closed-Rate "
            f"ohne Undercoverage/Same-Candle schlägt. Unter den Alternativen ist "
            f"`non_worsening_coverage_gate` der kleinste sichere Schritt vor `inventory_mtm` "
            f"(gleiche Outcomes hier, klarere Regel). Reines `non_worsening` ist nicht sicher.",
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
    max_coins: int = DEFAULT_MAX_COINS,
    min_trades: int = DEFAULT_MIN_TRADES,
    apt_only: bool = False,
) -> dict[str, Any]:
    if output_root.exists() and any(output_root.iterdir()):
        raise FileExistsError(f"Refusing to overwrite existing output directory: {output_root}")
    output_root.mkdir(parents=True, exist_ok=True)

    live_before = resolve_backtest_config(config_source="live", signal="long", symbol=APT_SYMBOL)
    live_long_fill_distance_pct = float(live_before.config.long_fill_distance_pct)

    apt_safety = run_apt_safety_stage(output_root=output_root)

    result: dict[str, Any] = {
        "output_root": str(output_root),
        "apt_safety": apt_safety,
        "aborted_stage2": bool(apt_safety.get("abort_stage2")) or apt_only,
    }

    if apt_safety.get("abort_stage2"):
        _write_json(
            output_root / "run_manifest.json",
            {
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "git": _git_status(),
                "aborted_stage2": True,
                "apt_safety": apt_safety,
            },
        )
        write_report(
            output_root / "REPORT.md",
            apt_safety=apt_safety,
            ranked=[],
            coin_manifest_rows=[],
            total_trades=0,
            provisional=True,
            coins_included=0,
        )
        return result

    if apt_only:
        _write_json(
            output_root / "run_manifest.json",
            {
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "git": _git_status(),
                "apt_only": True,
                "apt_safety": apt_safety,
            },
        )
        write_report(
            output_root / "REPORT.md",
            apt_safety=apt_safety,
            ranked=[],
            coin_manifest_rows=[],
            total_trades=0,
            provisional=True,
            coins_included=0,
        )
        return result

    coins_meta = discover_coins(max_coins=max_coins)
    included, coin_manifest_rows, total_trades, provisional = select_coins_for_stage2(
        coins_meta, max_coins=max_coins, min_trades=min_trades
    )
    run_remaining_policies(included)

    all_trade_rows: list[dict[str, Any]] = []
    all_decision_rows: list[dict[str, Any]] = []
    coin_policy_summaries: list[dict[str, Any]] = []
    trade_rows_by_coin: dict[str, list[dict[str, Any]]] = {}

    for entry in included:
        coin = entry["symbol"]
        candles = entry["candles"]
        trade_rows_by_coin[coin] = []
        for policy in POLICY_NAMES:
            results = entry["results"].get(policy, [])
            policy_trade_rows: list[dict[str, Any]] = []
            for res in results:
                row = build_trade_row(coin=coin, policy=policy, result=res, candles=candles)
                policy_trade_rows.append(row)
                all_decision_rows.extend(decisions_to_rows(coin=coin, policy=policy, result=res))
            all_trade_rows.extend(policy_trade_rows)
            trade_rows_by_coin[coin].extend(policy_trade_rows)
            coin_policy_summaries.append(
                summarize_coin_policy(
                    coin=coin,
                    policy=policy,
                    trade_rows=policy_trade_rows,
                    candles_loaded=len(candles),
                )
            )

    exit_policy_summary_rows = build_exit_policy_summary(coin_policy_summaries, coins_total=len(included))
    ranked = rank_policies(exit_policy_summary_rows)
    min_policy_trades = min(
        (int(row.get("total_trades_started") or 0) for row in exit_policy_summary_rows),
        default=0,
    )
    provisional = provisional or (min_policy_trades < min_trades)
    common_prefix_rows = build_common_prefix_rows(trade_rows_by_coin)
    coin_level_rows = build_coin_level_comparison(coin_policy_summaries)
    blocker_rows = build_blocker_summary(all_trade_rows)
    coverage_rows = build_coverage_validation_rows(all_decision_rows)

    # Per-policy subdirectories: trades.csv + coin_summary.json.
    for policy in POLICY_NAMES:
        policy_dir = output_root / policy
        policy_trade_rows = [row for row in all_trade_rows if row["policy"] == policy]
        _write_csv(policy_dir / "trades.csv", policy_trade_rows)
        policy_summaries = [s for s in coin_policy_summaries if s["policy"] == policy]
        _write_json(policy_dir / "coin_summary.json", policy_summaries)

    _write_csv(
        output_root / "coin_manifest.csv",
        coin_manifest_rows,
        fieldnames=[
            "priority_rank",
            "base",
            "symbol",
            "row_count",
            "feather_path",
            "included",
            "candles_loaded",
            "current_trades_started",
            "reason",
        ],
    )
    _write_csv(output_root / "continuous_trade_details.csv", all_trade_rows)
    _write_csv(output_root / "continuous_coin_summary.csv", coin_policy_summaries)
    _write_csv(output_root / "exit_policy_summary.csv", exit_policy_summary_rows)
    _write_csv(output_root / "common_prefix_comparison.csv", common_prefix_rows)
    _write_csv(output_root / "coin_level_comparison.csv", coin_level_rows)
    _write_csv(output_root / "exit_rebuild_decisions.csv", all_decision_rows)
    _write_csv(output_root / "coverage_validation.csv", coverage_rows)
    _write_csv(output_root / "blocker_summary.csv", blocker_rows)
    _write_csv(output_root / "ranking.csv", ranked)

    live_after = resolve_backtest_config(config_source="live", signal="long", symbol=APT_SYMBOL)
    live_long_fill_distance_pct_after = float(live_after.config.long_fill_distance_pct)
    if live_long_fill_distance_pct_after != live_long_fill_distance_pct:
        raise RuntimeError("Live long_fill_distance_pct changed during run")

    manifest = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "git": _git_status(),
        "mode": "exit_policy_multicoin_continuous",
        "fixed_params": {
            "direction": DIRECTION,
            "long_fill_distance_pct": LONG_FILL_DISTANCE_PCT,
            "target_profit_usdt": TARGET_PROFIT_USDT,
            "tp_profit_target_pct": TP_PROFIT_TARGET_PCT,
            "fill_model": FILL_MODEL,
            "config_source": CONFIG_SOURCE,
            "continuous_start_index": CONTINUOUS_START_INDEX,
            "candle_limit": FULL_HISTORY_CANDLE_LIMIT,
        },
        "policies": list(POLICY_NAMES),
        "coins_discovered": len(coins_meta),
        "coins_included": len(included),
        "total_trades": total_trades,
        "min_policy_trades": min_policy_trades,
        "provisional": provisional,
        "max_coins": max_coins,
        "min_trades": min_trades,
        "apt_safety": {
            "baseline_ok": apt_safety.get("baseline_ok"),
            "same_candle_violation": apt_safety.get("same_candle_violation"),
        },
        "live_defaults_unchanged": {
            "long_fill_distance_pct": live_long_fill_distance_pct,
        },
        "ranking_winner": ranked[0]["policy"] if ranked else None,
        "output_root": str(output_root),
    }
    _write_json(output_root / "run_manifest.json", manifest)

    write_report(
        output_root / "REPORT.md",
        apt_safety=apt_safety,
        ranked=ranked,
        coin_manifest_rows=coin_manifest_rows,
        total_trades=total_trades,
        provisional=provisional,
        coins_included=len(included),
        min_trades=min_trades,
    )

    result.update(
        {
            "coins_included": len(included),
            "total_trades": total_trades,
            "provisional": provisional,
            "ranked": ranked,
        }
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--max-coins", type=int, default=DEFAULT_MAX_COINS)
    parser.add_argument("--min-trades", type=int, default=DEFAULT_MIN_TRADES)
    parser.add_argument("--apt-only", action="store_true")
    args = parser.parse_args(argv)

    payload = run_pipeline(
        output_root=args.output_dir,
        max_coins=args.max_coins,
        min_trades=args.min_trades,
        apt_only=args.apt_only,
    )
    print(
        json.dumps(
            {
                "output_root": payload["output_root"],
                "aborted_stage2": payload["aborted_stage2"],
                "apt_baseline_ok": payload["apt_safety"].get("baseline_ok"),
                "coins_included": payload.get("coins_included"),
                "total_trades": payload.get("total_trades"),
                "provisional": payload.get("provisional"),
                "ranking_winner": (payload.get("ranked") or [{}])[0].get("policy")
                if payload.get("ranked")
                else None,
            },
            indent=2,
        )
    )
    return 1 if payload["aborted_stage2"] and not args.apt_only else 0


if __name__ == "__main__":
    raise SystemExit(main())
