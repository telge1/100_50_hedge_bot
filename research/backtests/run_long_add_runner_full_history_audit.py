"""Full-history audit for open la_1_2 multi-start runners (research-only).

Replays each open start from its original index through the last available
APTUSDT 5m candle. Aborts if the first 10k candles diverge from the
multi-start baseline.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.backtests.backtest_config_loader import resolve_backtest_config
from research.backtests.backtest_report import BacktestResult
from research.backtests.candle_loader import load_candles_for_symbol
from research.backtests.debug_report import calculate_unrealized_pnl
from research.backtests.historical_backtest import run_historical_backtest
from research.backtests.long_add_multistart_metrics import (
    CYCLE_LEG_RE,
    _active_exit_price,
    cycle_leg_map,
    exit_rebuild_stats,
    exposure_from_fills,
    normalize_trade_status,
    safe_float,
    same_candle_long_add_short_reduce,
)
from research.backtests.pnl_coverage_audit import build_pnl_coverage_audit

ROOT = Path(__file__).resolve().parents[2]
BASELINE_ROOT = ROOT / "research/backtests/results/long_add_multistart_causal_20260720"
DEFAULT_OUT = ROOT / "research/backtests/results/long_add_runner_full_history_causal_20260720"

OPEN_STARTS = (2750, 4000, 4750, 7000, 7500, 9000, 9750, 23000, 23250, 37500)
BASELINE_OPEN_MTM_SUM = -102.2996
LONG_ADD_PCT = 1.2
TARGET_PROFIT_USDT = 0.015
TP_PROFIT_TARGET_PCT = 0.25
SYMBOL = "APTUSDT"
DIRECTION = "long"
FILL_MODEL = "conservative"
CONFIG_SOURCE = "live"
PARITY_WINDOW = 10000
PARITY_ABS_TOL = 1e-8
PARITY_REL_TOL = 1e-9


def _git_status() -> dict[str, Any]:
    status: dict[str, Any] = {"commit": None, "dirty": None, "status_porcelain": ""}
    try:
        status["commit"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip()
        porcelain = subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=ROOT, text=True
        )
        status["dirty"] = bool(porcelain.strip())
        status["status_porcelain"] = porcelain
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        status["error"] = str(exc)
    return status


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
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
            writer.writerow({key: row.get(key) for key in fields})


def _candle_ts(candle: Any) -> str:
    ts = candle["timestamp"] if isinstance(candle, dict) else candle.timestamp
    if hasattr(ts, "isoformat"):
        return ts.isoformat()
    return str(ts)


def _candle_field(candle: Any, name: str) -> float:
    if isinstance(candle, dict):
        return float(candle[name])
    return float(getattr(candle, name))


def _float_close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=PARITY_REL_TOL, abs_tol=PARITY_ABS_TOL)


def load_baseline_open_trades(baseline_root: Path = BASELINE_ROOT) -> dict[int, dict[str, Any]]:
    path = baseline_root / "la_1_2" / "trades.csv"
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    opens = {
        int(row["start_index"]): row
        for row in rows
        if str(row.get("status") or "").lower() == "open"
    }
    missing = [idx for idx in OPEN_STARTS if idx not in opens]
    extra = sorted(set(opens) - set(OPEN_STARTS))
    if missing or extra:
        raise RuntimeError(f"Baseline open starts mismatch. missing={missing} extra={extra}")
    return opens


def run_window(
    candles: list[Any],
    *,
    start_index: int,
    window_candles: int | None = None,
) -> BacktestResult:
    end = None if window_candles is None else start_index + window_candles
    window = candles[start_index:end]
    if not window:
        raise RuntimeError(f"Empty window for start_index={start_index}")
    result = run_historical_backtest(
        SYMBOL,
        DIRECTION,
        window,
        max_candles=max(0, len(window) - 1),
        fill_model=FILL_MODEL,
        config_source=CONFIG_SOURCE,
        tp_profit_target_pct=TP_PROFIT_TARGET_PCT,
        long_fill_distance_pct=LONG_ADD_PCT,
        target_profit_usdt=TARGET_PROFIT_USDT,
    )
    result.start_index = start_index
    result.window_candles = len(window)
    return result


def parity_snapshot(result: BacktestResult) -> dict[str, Any]:
    return {
        "status": normalize_trade_status(result),
        "realized_pnl": safe_float(result.realized_pnl),
        "unrealized_pnl": safe_float(result.unrealized_pnl),
        "mtm_pnl": safe_float(result.overall_pnl, safe_float(result.realized_pnl) + safe_float(result.unrealized_pnl)),
        "final_long_qty": safe_float(result.final_long_qty),
        "final_short_qty": safe_float(result.final_short_qty),
        "final_long_avg": safe_float(result.final_long_avg_price),
        "final_short_avg": safe_float(result.final_short_avg_price),
        "max_cycle": _max_cycle(result),
        "active_exit_price": _active_exit_price(result),
    }


def _max_cycle(result: BacktestResult) -> int:
    max_cycle = 0
    for fill in result.fill_log or []:
        match = CYCLE_LEG_RE.match(str(fill.get("purpose") or ""))
        if match:
            max_cycle = max(max_cycle, int(match.group(1)))
    if result.cycles_seen is not None:
        max_cycle = max(max_cycle, int(result.cycles_seen))
    return max_cycle


def compare_parity(
    *,
    start_index: int,
    baseline: dict[str, Any],
    replay: BacktestResult,
) -> dict[str, Any]:
    snap = parity_snapshot(replay)
    checks = [
        ("status", str(baseline.get("status") or "").lower(), snap["status"]),
        ("realized_pnl", safe_float(baseline.get("realized_pnl")), snap["realized_pnl"]),
        ("unrealized_pnl", safe_float(baseline.get("unrealized_pnl")), snap["unrealized_pnl"]),
        ("mtm_pnl", safe_float(baseline.get("mtm_pnl")), snap["mtm_pnl"]),
        ("final_long_qty", safe_float(baseline.get("final_long_qty")), snap["final_long_qty"]),
        ("final_short_qty", safe_float(baseline.get("final_short_qty")), snap["final_short_qty"]),
        ("final_long_avg", safe_float(baseline.get("final_long_avg")), snap["final_long_avg"]),
        ("final_short_avg", safe_float(baseline.get("final_short_avg")), snap["final_short_avg"]),
        ("max_cycle", int(safe_float(baseline.get("max_cycle"))), snap["max_cycle"]),
        (
            "active_exit_price",
            safe_float(baseline.get("active_exit_price")) if baseline.get("active_exit_price") not in (None, "") else None,
            snap["active_exit_price"],
        ),
    ]
    mismatches: list[dict[str, Any]] = []
    for name, expected, actual in checks:
        if name == "status":
            ok = expected == actual
        elif expected is None and actual is None:
            ok = True
        elif expected is None or actual is None:
            ok = False
        elif isinstance(expected, str) or isinstance(actual, str):
            ok = expected == actual
        else:
            ok = _float_close(float(expected), float(actual))
        if not ok:
            mismatches.append({"field": name, "expected": expected, "actual": actual})
    return {
        "start_index": start_index,
        "parity_ok": len(mismatches) == 0,
        "mismatch_count": len(mismatches),
        "mismatches_json": json.dumps(mismatches),
        "baseline_status": baseline.get("status"),
        "replay_status": snap["status"],
        "baseline_mtm": safe_float(baseline.get("mtm_pnl")),
        "replay_mtm": snap["mtm_pnl"],
        "baseline_active_exit": baseline.get("active_exit_price"),
        "replay_active_exit": snap["active_exit_price"],
    }


def reconstruct_mtm_path(
    result: BacktestResult,
    window_candles: list[Any],
) -> dict[str, Any]:
    fills = sorted(
        list(result.fill_log or []),
        key=lambda row: (
            int(row.get("candle_index") if row.get("candle_index") is not None else 10**12),
            str(row.get("timestamp") or ""),
            str(row.get("purpose") or ""),
        ),
    )
    by_candle: dict[int, list[dict[str, Any]]] = {}
    for fill in fills:
        ci = fill.get("candle_index")
        if ci is None:
            continue
        by_candle.setdefault(int(ci), []).append(fill)

    realized = 0.0
    long_qty = short_qty = 0.0
    long_avg = short_avg = 0.0
    worst_mtm = None
    worst_idx = None
    worst_ts = None
    best_after_worst = None
    path_rows: list[dict[str, Any]] = []

    closed = normalize_trade_status(result) == "closed"
    close_idx = None
    if closed and fills:
        close_idx = max(
            (int(f["candle_index"]) for f in fills if f.get("candle_index") is not None),
            default=None,
        )

    limit = len(window_candles)
    if result.candles_processed:
        limit = min(limit, int(result.candles_processed) + 1)

    for idx in range(limit):
        for fill in by_candle.get(idx, []):
            realized += safe_float(fill.get("closed_pnl") or fill.get("confirmed_closed_pnl"))
            long_qty = safe_float(fill.get("long_qty_after"), long_qty)
            short_qty = safe_float(fill.get("short_qty_after"), short_qty)
            long_avg = safe_float(fill.get("long_avg_after"), long_avg)
            short_avg = safe_float(fill.get("short_avg_after"), short_avg)
        mark = _candle_field(window_candles[idx], "close")
        _, _, unreal = calculate_unrealized_pnl(long_qty, long_avg, short_qty, short_avg, mark)
        mtm = realized + safe_float(unreal)
        ts = _candle_ts(window_candles[idx])
        if worst_mtm is None or mtm < worst_mtm:
            worst_mtm = mtm
            worst_idx = idx
            worst_ts = ts
            best_after_worst = mtm
        elif best_after_worst is None or mtm > best_after_worst:
            best_after_worst = mtm
        path_rows.append(
            {
                "candle_index_local": idx,
                "timestamp": ts,
                "realized_pnl": realized,
                "unrealized_pnl": safe_float(unreal),
                "mtm_pnl": mtm,
                "mark_price": mark,
                "long_qty": long_qty,
                "short_qty": short_qty,
            }
        )
        if closed and close_idx is not None and idx >= close_idx and long_qty <= 1e-12 and short_qty <= 1e-12:
            # Stop path shortly after flat if desired; keep scanning until processed end.
            pass

    recovery = None
    if worst_mtm is not None and best_after_worst is not None:
        recovery = best_after_worst - worst_mtm

    candles_worst_to_end = None
    if worst_idx is not None:
        end_idx = close_idx if (closed and close_idx is not None) else (limit - 1)
        candles_worst_to_end = max(0, end_idx - worst_idx)

    return {
        "worst_mtm": worst_mtm,
        "worst_mtm_candle_index_local": worst_idx,
        "worst_mtm_timestamp": worst_ts,
        "best_mtm_after_worst": best_after_worst,
        "mtm_recovery_from_worst": recovery,
        "candles_worst_to_close_or_end": candles_worst_to_end,
        "path_rows": path_rows,
        "close_candle_index_local": close_idx,
    }


def _final_rebuild_high_stats(
    rebuilds: list[dict[str, Any]],
    window_candles: list[Any],
    active_exit: float | None,
) -> dict[str, Any]:
    if not rebuilds:
        return {
            "final_exit_rebuild_timestamp": None,
            "final_exit_rebuild_candle_index": None,
            "highest_high_after_final_rebuild": None,
            "distance_to_final_exit": None,
            "pct_distance_to_final_exit": None,
            "had_realistic_close_chance": None,
        }
    last = rebuilds[-1]
    ci = last.get("candle_index")
    try:
        start = int(ci) + 1 if ci is not None else 0
    except (TypeError, ValueError):
        start = 0
    future = window_candles[start:]
    max_high = max((_candle_field(c, "high") for c in future), default=None)
    exit_price = active_exit if active_exit is not None else last.get("new_exit_price")
    distance = None
    pct = None
    chance = None
    if exit_price is not None and max_high is not None:
        distance = float(exit_price) - float(max_high)
        pct = (distance / float(exit_price) * 100.0) if exit_price else None
        chance = bool(max_high >= float(exit_price) - 1e-12)
    elif exit_price is not None:
        mark = _candle_field(window_candles[-1], "close") if window_candles else None
        if mark is not None:
            distance = float(exit_price) - mark
            pct = (distance / float(exit_price) * 100.0) if exit_price else None
            chance = False
    return {
        "final_exit_rebuild_timestamp": last.get("timestamp"),
        "final_exit_rebuild_candle_index": ci,
        "highest_high_after_final_rebuild": max_high,
        "distance_to_final_exit": distance,
        "pct_distance_to_final_exit": pct,
        "had_realistic_close_chance": chance,
    }


def analyze_full_runner(
    *,
    start_index: int,
    candles: list[Any],
    baseline_10k: dict[str, Any],
    result: BacktestResult,
) -> dict[str, Any]:
    window = candles[start_index:]
    status = normalize_trade_status(result)
    fills = list(result.fill_log or [])
    exposure = exposure_from_fills(fills)
    rebuild = exit_rebuild_stats(result, window_candles=window)
    same_candle = same_candle_long_add_short_reduce(fills)
    coverage = build_pnl_coverage_audit(result)
    undercoverage = sum(1 for row in coverage if "undercover" in str(row.get("status") or "").lower())
    pending_final = sum(1 for row in coverage if "pending_final" in str(row.get("status") or "").lower())
    legs = cycle_leg_map(fills)
    completed_cycles = sum(1 for _, legs_row in legs.items() if legs_row.get("long_add") and legs_row.get("short_reduce"))
    mtm_path = reconstruct_mtm_path(result, window)
    active_exit = _active_exit_price(result)
    high_stats = _final_rebuild_high_stats(rebuild.get("rebuilds") or [], window, active_exit)

    realized = safe_float(result.realized_pnl)
    unrealized = safe_float(result.unrealized_pnl)
    mtm = safe_float(result.overall_pnl, realized + unrealized)
    baseline_mtm = safe_float(baseline_10k.get("mtm_pnl"))
    long_qty = safe_float(result.final_long_qty)
    short_qty = safe_float(result.final_short_qty)
    mark = safe_float(result.final_price)

    close_ts = ""
    if status == "closed" and result.end_time is not None:
        close_ts = result.end_time.isoformat()
    elif status == "closed" and fills:
        close_ts = str(fills[-1].get("timestamp") or "")

    duration = int(result.candles_processed or 0)
    harmful = int(rebuild.get("old_exit_later_reachable_count") or 0)

    strong_rebound_close = None
    recovery_factor = None
    earlier_exit_would_have_closed_sooner = None
    worst_before_close = mtm_path.get("worst_mtm")
    candles_worst_to_close = mtm_path.get("candles_worst_to_close_or_end")

    if status == "closed":
        if worst_before_close is not None and abs(realized) > 1e-12 and worst_before_close < 0:
            recovery_factor = abs(worst_before_close) / abs(realized)
        # Strong rebound: needed to climb back from a deep negative MTM hole.
        strong_rebound_close = bool(
            worst_before_close is not None
            and worst_before_close <= -1.0
            and recovery_factor is not None
            and recovery_factor >= 5.0
        )
        earlier_exit_would_have_closed_sooner = harmful > 0

    structurally_stuck = None
    if status == "open":
        chance = high_stats.get("had_realistic_close_chance")
        pct_dist = high_stats.get("pct_distance_to_final_exit")
        structurally_stuck = bool(
            chance is False
            and pct_dist is not None
            and pct_dist > 5.0
        )

    max_long = max((safe_float(f.get("long_qty_after")) for f in fills), default=0.0)
    max_short = max((safe_float(f.get("short_qty_after")) for f in fills), default=0.0)
    max_net = max(
        (abs(safe_float(f.get("long_qty_after")) - safe_float(f.get("short_qty_after"))) for f in fills),
        default=0.0,
    )

    return {
        "start_index": start_index,
        "start_timestamp": result.start_time.isoformat() if result.start_time else _candle_ts(window[0]),
        "available_candles_from_start": len(window),
        "data_end_timestamp": _candle_ts(window[-1]),
        "status_at_data_end": status,
        "close_timestamp": close_ts,
        "duration_candles": duration,
        "duration_to_close_candles": duration if status == "closed" else None,
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "final_mtm": mtm,
        "baseline_10k_mtm": baseline_mtm,
        "mtm_change_vs_10k": mtm - baseline_mtm,
        "worst_mtm": mtm_path.get("worst_mtm"),
        "worst_mtm_timestamp": mtm_path.get("worst_mtm_timestamp"),
        "worst_mtm_candle_index_local": mtm_path.get("worst_mtm_candle_index_local"),
        "best_mtm_after_worst": mtm_path.get("best_mtm_after_worst"),
        "mtm_recovery_from_worst": mtm_path.get("mtm_recovery_from_worst"),
        "candles_worst_to_close_or_end": candles_worst_to_close,
        "max_cycle": _max_cycle(result),
        "completed_cycles": completed_cycles,
        "max_long_qty": max_long,
        "max_short_qty": max_short,
        "max_net_qty": max_net,
        "max_long_notional": exposure["max_long_notional"],
        "max_short_notional": exposure["max_short_notional"],
        "max_total_notional": exposure["max_total_notional"],
        "max_abs_net_exposure": exposure["max_abs_net_exposure"],
        "fees": exposure["fees"],
        "exit_rebuild_count": rebuild["exit_rebuild_count"],
        "exit_increase_count": rebuild["exit_increase_count"],
        "old_exit_later_reachable_count": harmful,
        "active_exit_price": active_exit,
        "highest_high_after_final_rebuild": high_stats["highest_high_after_final_rebuild"],
        "distance_to_final_exit": high_stats["distance_to_final_exit"],
        "pct_distance_to_final_exit": high_stats["pct_distance_to_final_exit"],
        "same_candle_long_add_short_reduce": len(same_candle),
        "undercoverage": undercoverage,
        "pending_final_exit": pending_final,
        "final_long_qty": long_qty,
        "final_short_qty": short_qty,
        "final_net_qty": long_qty - short_qty,
        "final_long_avg": safe_float(result.final_long_avg_price),
        "final_short_avg": safe_float(result.final_short_avg_price),
        "mark_price_end": mark,
        "exit_reason": result.exit_reason,
        "error": result.error,
        "closed_net_pnl": realized if status == "closed" else None,
        "worst_mtm_before_close": worst_before_close if status == "closed" else None,
        "candles_worst_to_close": candles_worst_to_close if status == "closed" else None,
        "recovery_factor_abs_worst_over_closed_pnl": recovery_factor,
        "closed_only_via_strong_rebound": strong_rebound_close,
        "earlier_lower_exit_would_have_been_reachable": earlier_exit_would_have_closed_sooner,
        "had_realistic_close_chance": high_stats["had_realistic_close_chance"],
        "structurally_stuck": structurally_stuck,
        "rebuilds": rebuild.get("rebuilds") or [],
        "mtm_path_rows": mtm_path.get("path_rows") or [],
        "final_state": {
            "status": status,
            "realized_pnl": realized,
            "unrealized_pnl": unrealized,
            "mtm_pnl": mtm,
            "long_qty": long_qty,
            "short_qty": short_qty,
            "long_avg": safe_float(result.final_long_avg_price),
            "short_avg": safe_float(result.final_short_avg_price),
            "mark_price": mark,
            "active_exit_price": active_exit,
            "exit_reason": result.exit_reason,
            "final_active_order_purposes": list(result.final_active_order_purposes or []),
        },
    }


def aggregate_runners(rows: list[dict[str, Any]]) -> dict[str, Any]:
    closed = [row for row in rows if row.get("status_at_data_end") == "closed"]
    open_rows = [row for row in rows if row.get("status_at_data_end") == "open"]
    closed_pnls = [safe_float(row.get("closed_net_pnl")) for row in closed]
    final_mtms = [safe_float(row.get("final_mtm")) for row in rows]
    durations = [safe_float(row.get("duration_candles")) for row in rows]
    worsts = [safe_float(row.get("worst_mtm")) for row in rows if row.get("worst_mtm") is not None]
    recoveries = [
        safe_float(row.get("candles_worst_to_close"))
        for row in closed
        if row.get("candles_worst_to_close") is not None
    ]
    rebound = [row for row in closed if row.get("closed_only_via_strong_rebound")]
    harmful = [row for row in rows if int(row.get("old_exit_later_reachable_count") or 0) > 0]
    sum_final = sum(final_mtms)
    return {
        "runners_at_10k": 10,
        "later_closed_count": len(closed),
        "still_open_at_data_end": len(open_rows),
        "sum_closed_pnl_later_closed": sum(closed_pnls),
        "sum_final_mtm_all_10": sum_final,
        "baseline_10k_open_mtm_sum": BASELINE_OPEN_MTM_SUM,
        "mtm_change_vs_baseline_10k_sum": sum_final - BASELINE_OPEN_MTM_SUM,
        "avg_duration_candles": (sum(durations) / len(durations)) if durations else None,
        "max_duration_candles": max(durations) if durations else None,
        "avg_worst_mtm": (sum(worsts) / len(worsts)) if worsts else None,
        "worst_drawdown_mtm": min(worsts) if worsts else None,
        "avg_recovery_candles_closed": (sum(recoveries) / len(recoveries)) if recoveries else None,
        "share_closed_via_strong_rebound": (len(rebound) / len(closed)) if closed else None,
        "share_with_harmful_exit_rebuild": (len(harmful) / len(rows)) if rows else None,
        "same_candle_violations_total": sum(int(row.get("same_candle_long_add_short_reduce") or 0) for row in rows),
        "undercoverage_total": sum(int(row.get("undercoverage") or 0) for row in rows),
    }


def write_report(
    path: Path,
    *,
    summaries: list[dict[str, Any]],
    aggregate: dict[str, Any],
    parity_rows: list[dict[str, Any]],
) -> None:
    closed = [row for row in summaries if row["status_at_data_end"] == "closed"]
    open_rows = [row for row in summaries if row["status_at_data_end"] == "open"]
    parity_ok = all(row.get("parity_ok") for row in parity_rows)
    mtm_improved = safe_float(aggregate.get("mtm_change_vs_baseline_10k_sum")) > 0
    harmful_share = safe_float(aggregate.get("share_with_harmful_exit_rebuild"))
    still_open_n = int(aggregate.get("still_open_at_data_end") or 0)
    later_closed_n = int(aggregate.get("later_closed_count") or 0)
    if still_open_n >= 5 or safe_float(aggregate.get("sum_final_mtm_all_10")) <= BASELINE_OPEN_MTM_SUM - 5:
        q7 = (
            "Not as an unconditional best baseline. Full-history shows material open-tail risk; "
            "1.2% won the fixed-window ranking mainly via fewer/milder open holes at 10k, "
            "but several runners remain structurally open to data end."
        )
    elif later_closed_n >= 7 and mtm_improved:
        q7 = (
            "Yes, provisionally: most runners eventually close and joint MTM improves vs the "
            "10k open-hole, so the 1.2% multi-start win is not only a window artifact."
        )
    else:
        q7 = (
            "Mixed. Some recovery exists, but remaining open runners and rebuild blockers "
            "mean 1.2% should not be promoted to live without an exit-rebuild fix."
        )

    lines = [
        "# la_1_2 Open-Runner Full-History Audit",
        "",
        f"Generated: `{datetime.now(timezone.utc).isoformat()}`",
        "",
        "## Setup",
        "",
        f"- Baseline: `{BASELINE_ROOT}` / `la_1_2`",
        f"- Parameters: `long_fill_distance_pct={LONG_ADD_PCT}`, "
        f"`target_profit_usdt={TARGET_PROFIT_USDT}`, `tp_profit_target_pct={TP_PROFIT_TARGET_PCT}`",
        f"- Fill model `{FILL_MODEL}`, config `{CONFIG_SOURCE}`, causal deferred fills",
        f"- Starts: `{', '.join(str(x) for x in OPEN_STARTS)}`",
        f"- Each run: start index → last available APTUSDT 5m candle",
        f"- 10k parity check: `{'PASS' if parity_ok else 'FAIL'}`",
        "",
        "## Answers",
        "",
        f"1. **Later closed:** `{later_closed_n}` of 10",
        f"2. **Still open at true data end:** `{still_open_n}` of 10",
        f"3. **Joint MTM vs −102.2996:** final sum "
        f"`{safe_float(aggregate.get('sum_final_mtm_all_10')):.4f}` "
        f"(Δ `{safe_float(aggregate.get('mtm_change_vs_baseline_10k_sum')):.4f}`; "
        f"{'improved' if mtm_improved else 'not improved'})",
        f"4. **Later recovery duration:** avg closed recovery from worst "
        f"`{aggregate.get('avg_recovery_candles_closed')}` candles; "
        f"avg overall duration `{safe_float(aggregate.get('avg_duration_candles')):.1f}`, "
        f"max `{aggregate.get('max_duration_candles')}`",
        f"5. **Max drawdowns:** avg worst MTM `{safe_float(aggregate.get('avg_worst_mtm')):.4f}`, "
        f"worst drawdown `{safe_float(aggregate.get('worst_drawdown_mtm')):.4f}`",
        f"6. **Window artifact or real tail risk?** "
        + (
            "Mostly **real tail risk**: "
            if still_open_n >= 3
            else "Partly window artifact with residual tail: "
        )
        + f"{later_closed_n} eventually close beyond 10k, but {still_open_n} remain open "
        "with unreachable/far exits and harmful rebuild history.",
        f"7. **Is 1.2% still the best LONG_ADD baseline?** {q7}",
        f"8. **Exit-rebuild blocker persists?** "
        f"`{'Yes' if harmful_share and harmful_share > 0 else 'No'}` "
        f"(share with harmful rebuilds `{harmful_share}`).",
        "",
        "## Per-runner summary",
        "",
        "| start | status | duration | final_mtm | Δ vs 10k | worst_mtm | harmful_rebuilds | active_exit |",
        "|---:|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summaries:
        lines.append(
            f"| {row['start_index']} | {row['status_at_data_end']} | {row['duration_candles']} | "
            f"{safe_float(row['final_mtm']):.4f} | {safe_float(row['mtm_change_vs_10k']):.4f} | "
            f"{safe_float(row['worst_mtm']):.4f} | {row['old_exit_later_reachable_count']} | "
            f"{row.get('active_exit_price')} |"
        )

    lines.extend(["", "## Closed runners", ""])
    if not closed:
        lines.append("_None closed before data end._")
    else:
        lines.append(
            "| start | close_ts | closed_pnl | worst_mtm | recovery_factor | strong_rebound | earlier_exit_reachable |"
        )
        lines.append("|---:|---|---:|---:|---:|---|---|")
        for row in closed:
            lines.append(
                f"| {row['start_index']} | {row.get('close_timestamp')} | "
                f"{safe_float(row.get('closed_net_pnl')):.4f} | {safe_float(row.get('worst_mtm')):.4f} | "
                f"{row.get('recovery_factor_abs_worst_over_closed_pnl')} | "
                f"{row.get('closed_only_via_strong_rebound')} | "
                f"{row.get('earlier_lower_exit_would_have_been_reachable')} |"
            )

    lines.extend(["", "## Still-open runners", ""])
    if not open_rows:
        lines.append("_None remain open._")
    else:
        lines.append(
            "| start | final_mtm | mark | exit | max_high_after_rebuild | pct_dist | stuck | realistic_chance |"
        )
        lines.append("|---:|---:|---:|---:|---:|---:|---|---|")
        for row in open_rows:
            lines.append(
                f"| {row['start_index']} | {safe_float(row['final_mtm']):.4f} | "
                f"{safe_float(row.get('mark_price_end')):.4f} | {row.get('active_exit_price')} | "
                f"{row.get('highest_high_after_final_rebuild')} | "
                f"{safe_float(row.get('pct_distance_to_final_exit')):.2f} | "
                f"{row.get('structurally_stuck')} | {row.get('had_realistic_close_chance')} |"
            )

    lines.extend(
        [
            "",
            "## Aggregate",
            "",
            f"- Later closed PnL sum: `{safe_float(aggregate.get('sum_closed_pnl_later_closed')):.4f}`",
            f"- Final MTM all 10: `{safe_float(aggregate.get('sum_final_mtm_all_10')):.4f}`",
            f"- Change vs −102.2996: `{safe_float(aggregate.get('mtm_change_vs_baseline_10k_sum')):.4f}`",
            f"- Same-candle violations: `{aggregate.get('same_candle_violations_total')}`",
            f"- Undercoverage total: `{aggregate.get('undercoverage_total')}`",
            "",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_audit(
    *,
    output_root: Path = DEFAULT_OUT,
    baseline_root: Path = BASELINE_ROOT,
    candle_limit: int = 50000,
) -> dict[str, Any]:
    if output_root.exists() and any(output_root.iterdir()):
        raise FileExistsError(
            f"Refusing to overwrite existing output directory: {output_root}"
        )
    output_root.mkdir(parents=True, exist_ok=True)

    live_before = resolve_backtest_config(config_source="live", signal="long", symbol=SYMBOL)
    live_long_add = float(live_before.config.long_fill_distance_pct)

    baseline_opens = load_baseline_open_trades(baseline_root)
    candles = load_candles_for_symbol(SYMBOL, limit=candle_limit)

    parity_rows: list[dict[str, Any]] = []
    print("=== 10k parity checks ===", flush=True)
    for start_index in OPEN_STARTS:
        replay = run_window(candles, start_index=start_index, window_candles=PARITY_WINDOW)
        row = compare_parity(
            start_index=start_index,
            baseline=baseline_opens[start_index],
            replay=replay,
        )
        parity_rows.append(row)
        print(
            f"parity start={start_index} ok={row['parity_ok']} mismatches={row['mismatch_count']}",
            flush=True,
        )
        if not row["parity_ok"]:
            _write_csv(output_root / "runner_10k_parity_check.csv", parity_rows)
            (output_root / "PARITY_FAILURE.json").write_text(
                json.dumps({"failed_start": start_index, "row": row}, indent=2) + "\n",
                encoding="utf-8",
            )
            raise RuntimeError(
                f"10k parity failed for start_index={start_index}: {row['mismatches_json']}"
            )

    _write_csv(output_root / "runner_10k_parity_check.csv", parity_rows)

    summaries: list[dict[str, Any]] = []
    close_details: list[dict[str, Any]] = []
    open_states: list[dict[str, Any]] = []
    mtm_drawdowns: list[dict[str, Any]] = []
    rebuild_rows: list[dict[str, Any]] = []

    print("=== Full-history runners ===", flush=True)
    for start_index in OPEN_STARTS:
        available = len(candles) - start_index
        print(f"runner start={start_index} available={available}", flush=True)
        result = run_window(candles, start_index=start_index, window_candles=None)
        analysis = analyze_full_runner(
            start_index=start_index,
            candles=candles,
            baseline_10k=baseline_opens[start_index],
            result=result,
        )
        compact = {
            key: value
            for key, value in analysis.items()
            if key not in {"rebuilds", "mtm_path_rows", "final_state"}
        }
        compact["final_state_json"] = json.dumps(analysis["final_state"])
        summaries.append(compact)

        mtm_drawdowns.append(
            {
                "start_index": start_index,
                "worst_mtm": analysis["worst_mtm"],
                "worst_mtm_timestamp": analysis["worst_mtm_timestamp"],
                "worst_mtm_candle_index_local": analysis["worst_mtm_candle_index_local"],
                "best_mtm_after_worst": analysis["best_mtm_after_worst"],
                "mtm_recovery_from_worst": analysis["mtm_recovery_from_worst"],
                "candles_worst_to_close_or_end": analysis["candles_worst_to_close_or_end"],
                "final_mtm": analysis["final_mtm"],
                "baseline_10k_mtm": analysis["baseline_10k_mtm"],
                "status_at_data_end": analysis["status_at_data_end"],
            }
        )

        for rebuild in analysis["rebuilds"]:
            rebuild_rows.append(
                {
                    "start_index": start_index,
                    **{k: v for k, v in rebuild.items()},
                }
            )

        if analysis["status_at_data_end"] == "closed":
            close_details.append(
                {
                    "start_index": start_index,
                    "close_timestamp": analysis["close_timestamp"],
                    "duration_to_close_candles": analysis["duration_to_close_candles"],
                    "closed_net_pnl": analysis["closed_net_pnl"],
                    "worst_mtm_before_close": analysis["worst_mtm_before_close"],
                    "candles_worst_to_close": analysis["candles_worst_to_close"],
                    "recovery_factor_abs_worst_over_closed_pnl": analysis[
                        "recovery_factor_abs_worst_over_closed_pnl"
                    ],
                    "closed_only_via_strong_rebound": analysis["closed_only_via_strong_rebound"],
                    "earlier_lower_exit_would_have_been_reachable": analysis[
                        "earlier_lower_exit_would_have_been_reachable"
                    ],
                    "exit_rebuild_count": analysis["exit_rebuild_count"],
                    "old_exit_later_reachable_count": analysis["old_exit_later_reachable_count"],
                }
            )
        else:
            open_states.append(
                {
                    "start_index": start_index,
                    "final_realized_pnl": analysis["realized_pnl"],
                    "final_unrealized_pnl": analysis["unrealized_pnl"],
                    "final_mtm": analysis["final_mtm"],
                    "mark_price_end": analysis["mark_price_end"],
                    "final_long_qty": analysis["final_long_qty"],
                    "final_short_qty": analysis["final_short_qty"],
                    "final_long_avg": analysis["final_long_avg"],
                    "final_short_avg": analysis["final_short_avg"],
                    "final_net_qty": analysis["final_net_qty"],
                    "active_exit_price": analysis["active_exit_price"],
                    "highest_high_after_final_rebuild": analysis[
                        "highest_high_after_final_rebuild"
                    ],
                    "distance_to_final_exit": analysis["distance_to_final_exit"],
                    "pct_distance_to_final_exit": analysis["pct_distance_to_final_exit"],
                    "had_realistic_close_chance": analysis["had_realistic_close_chance"],
                    "structurally_stuck": analysis["structurally_stuck"],
                    "old_exit_later_reachable_count": analysis["old_exit_later_reachable_count"],
                    "exit_reason": analysis["exit_reason"],
                    "final_state_json": json.dumps(analysis["final_state"]),
                }
            )

    aggregate = aggregate_runners(summaries)
    _write_csv(output_root / "runner_full_history_summary.csv", summaries)
    _write_csv(output_root / "runner_close_details.csv", close_details)
    _write_csv(output_root / "runner_final_open_state.csv", open_states)
    _write_csv(output_root / "runner_mtm_drawdown.csv", mtm_drawdowns)
    _write_csv(output_root / "runner_exit_rebuild_audit.csv", rebuild_rows)
    (output_root / "aggregate_summary.json").write_text(
        json.dumps(aggregate, indent=2) + "\n", encoding="utf-8"
    )

    live_after = resolve_backtest_config(config_source="live", signal="long", symbol=SYMBOL)
    if float(live_after.config.long_fill_distance_pct) != live_long_add:
        raise RuntimeError("Live long_fill_distance_pct changed during audit")

    manifest = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "git": _git_status(),
        "baseline_root": str(baseline_root),
        "baseline_open_mtm_sum_reference": BASELINE_OPEN_MTM_SUM,
        "open_starts": list(OPEN_STARTS),
        "parameters": {
            "long_fill_distance_pct": LONG_ADD_PCT,
            "target_profit_usdt": TARGET_PROFIT_USDT,
            "tp_profit_target_pct": TP_PROFIT_TARGET_PCT,
            "symbol": SYMBOL,
            "direction": DIRECTION,
            "fill_model": FILL_MODEL,
            "config_source": CONFIG_SOURCE,
        },
        "data_source": {
            "loader": "load_candles_for_symbol",
            "candle_count": len(candles),
            "candle_limit_requested": candle_limit,
            "data_end_timestamp": _candle_ts(candles[-1]) if candles else None,
        },
        "parity_window_candles": PARITY_WINDOW,
        "parity_all_ok": all(row.get("parity_ok") for row in parity_rows),
        "live_defaults_unchanged": {
            "long_fill_distance_pct": live_long_add,
            "target_profit_usdt": float(live_before.config.target_profit_usdt),
        },
        "aggregate": aggregate,
        "output_root": str(output_root),
    }
    (output_root / "run_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    write_report(
        output_root / "REPORT.md",
        summaries=summaries,
        aggregate=aggregate,
        parity_rows=parity_rows,
    )
    return {"manifest": manifest, "aggregate": aggregate, "summaries": summaries}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--baseline-root", type=Path, default=BASELINE_ROOT)
    parser.add_argument("--candle-limit", type=int, default=50000)
    args = parser.parse_args(argv)
    payload = run_audit(
        output_root=args.output_dir,
        baseline_root=args.baseline_root,
        candle_limit=args.candle_limit,
    )
    print(json.dumps({"aggregate": payload["aggregate"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
