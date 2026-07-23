"""Clean live-baseline continuous multi-coin blocker audit (research-only).

Hard constraints (must not be violated by this module):

* No ``exit_rebuild_policy_config`` is ever passed to the backtest engine
  (not even ``None`` — the kwarg is simply omitted).
* No LONG_ADD optimization beyond the live value (``long_fill_distance_pct``
  is pinned to the live default, never swept).
* No recovery expansion (``addon_short_recovery_config`` /
  ``recovery_bot_config`` are never passed).
* No strategy changes of any kind.
* Live config files under ``live_bots/`` are only ever read, never written.
* Refuses to overwrite a non-empty output directory.

This is a diagnostic/audit tool: it runs the *current* live strategy
continuously (one trade at a time; a trade blocks the coin until it closes
flat) across a curated multi-coin corpus and studies which trades never
close ("blockers") versus which close normally, using only causal
(no-look-ahead) features.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from research.backtests.backtest_config_loader import resolve_backtest_config
from research.backtests.backtest_report import BacktestResult
from research.backtests.candle_loader import DEFAULT_DATA_DIR, load_candles_for_symbol
from research.backtests.continuous_reentry_backtest import run_continuous_reentry_backtests
from research.backtests.historical_backtest import normalize_candles
from research.backtests.long_add_multistart_metrics import (
    analyze_trade,
    build_cycle_rows,
    exit_rebuild_stats,
    exposure_from_fills,
    normalize_trade_status,
    percentile,
    safe_float,
)
from research.backtests.pnl_coverage_audit import build_pnl_coverage_audit
from research.backtests.run_exit_policy_multicoin_continuous import (
    CURATED_MAJORS,
    discover_coins,
)
from research.regime_scanner.indicators import atr_percent, atr_wilder, ema, ema_distance_pct

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = ROOT / (
    "research/backtests/results/"
    "current_baseline_multicoin_continuous_blocker_audit_20260720"
)

# --- Fixed call params (must match live) ------------------------------------
DIRECTION = "long"
CONFIG_SOURCE = "live"
FILL_MODEL = "conservative"
CONTINUOUS_START_INDEX = 0
LONG_FILL_DISTANCE_PCT = 0.5
TARGET_PROFIT_USDT = 0.015
TP_PROFIT_TARGET_PCT = 0.25
FULL_HISTORY_CANDLE_LIMIT = 50000
APT_SYMBOL = "APTUSDT"

DEFAULT_MAX_COINS = 40
DEFAULT_MIN_TRADES = 100
DEFAULT_PREFERRED_TRADES = 250
PREFERRED_TRADES_CAP = 500

CYCLE_PURPOSE_RE = re.compile(r"^CYCLE_(\d+)_")

STAGE_DEFINITIONS: tuple[tuple[str, Any], ...] = (
    ("after_initial", None),
    ("after_cycle_1", 1),
    ("after_cycle_2", 2),
    ("after_cycle_3", 3),
    ("after_cycle_5", 5),
    ("after_last_completed_cycle", "last"),
    ("at_100", 100),
    ("at_500", 500),
    ("at_1000", 1000),
    ("at_3000", 3000),
    ("at_5000", 5000),
)

NUMERIC_STAGE_METRICS = (
    "price_change_pct",
    "mae_pct",
    "mfe_pct",
    "max_cycle_so_far",
    "exit_rebuilds_so_far",
    "exit_increases_so_far",
    "net_exposure",
    "long_short_qty_ratio",
    "inventory_mtm",
    "fees_so_far",
    "duration",
)


# ---------------------------------------------------------------------------
# Small shared utilities
# ---------------------------------------------------------------------------


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
# Baseline call kwargs / live-config documentation (no exit policy, no sweep)
# ---------------------------------------------------------------------------


def build_baseline_call_kwargs(*, symbol: str, candles: list[Any]) -> dict[str, Any]:
    """Kwargs for :func:`run_continuous_reentry_backtests` under the clean baseline.

    Intentionally omits ``exit_rebuild_policy_config``, ``addon_short_recovery_config``
    and ``recovery_bot_config`` entirely (hard constraint: no exit-rebuild policy,
    no recovery expansion, no strategy changes).
    """
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
        "write_json": False,
        "write_csv": False,
    }


def resolve_and_document_baseline_params(output_root: Path) -> dict[str, Any]:
    """Resolve the live config once and write ``applied_baseline_params.json``.

    Asserts the three pinned scalars match the live config and that no
    exit-rebuild policy override is active before any backtest is run.
    """
    load_result = resolve_backtest_config(config_source=CONFIG_SOURCE, signal="long", symbol=APT_SYMBOL)
    cfg = load_result.config

    payload = {
        "source_path": load_result.config_path,
        "config_source": load_result.config_source,
        "long_fill_distance_pct": float(cfg.long_fill_distance_pct),
        "target_profit_usdt": float(cfg.target_profit_usdt),
        "tp_profit_target_pct": float(cfg.tp_profit_target_pct),
        "tp_buffer_pct": float(cfg.tp_buffer_pct),
        "short_fill_distance_pct": float(cfg.short_fill_distance_pct),
        "base_notional_usdt": float(cfg.base_notional_usdt),
        "hedge_ratio_short": float(cfg.hedge_ratio_short),
        "hard_stop_cycle": int(cfg.hard_stop_cycle),
        "max_cycles": int(cfg.max_cycles),
        "pinned_values": {
            "long_fill_distance_pct": LONG_FILL_DISTANCE_PCT,
            "target_profit_usdt": TARGET_PROFIT_USDT,
            "tp_profit_target_pct": TP_PROFIT_TARGET_PCT,
        },
        "exit_rebuild_policy_override_active": False,
        "note": (
            "The only explicit kwargs passed to the backtest are the three pinned "
            "scalars (long_fill_distance_pct, target_profit_usdt, tp_profit_target_pct) "
            "plus config_source='live'. Every other field (tp_buffer_pct, "
            "short_fill_distance_pct, base_notional_usdt, hedge_ratio_short, "
            "hard_stop_cycle, max_cycles, ...) is taken verbatim from the live JSON config."
        ),
    }

    assert abs(payload["long_fill_distance_pct"] - LONG_FILL_DISTANCE_PCT) < 1e-9, "live long_fill_distance_pct drifted from pinned value"
    assert abs(payload["target_profit_usdt"] - TARGET_PROFIT_USDT) < 1e-9, "live target_profit_usdt drifted from pinned value"
    assert abs(payload["tp_profit_target_pct"] - TP_PROFIT_TARGET_PCT) < 1e-9, "live tp_profit_target_pct drifted from pinned value"
    assert not payload["exit_rebuild_policy_override_active"], "exit-rebuild policy override must stay inactive"

    _write_json(output_root / "applied_baseline_params.json", payload)
    return payload


# ---------------------------------------------------------------------------
# Continuous-trade classification helpers
# ---------------------------------------------------------------------------


def is_blocker_status(status: str) -> bool:
    """A trade "blocks" its coin's continuous chain iff it is not closed."""
    return str(status) != "closed"


def validate_continuous_trade_sequence(rows: list[dict[str, Any]]) -> None:
    """Assert real continuous semantics: once a coin blocks, no later trade exists.

    ``rows`` must contain ``coin``, ``trade_number`` and ``status`` keys.
    """
    by_coin: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_coin.setdefault(row["coin"], []).append(row)
    for coin, coin_rows in by_coin.items():
        ordered = sorted(coin_rows, key=lambda r: int(r["trade_number"]))
        blocked_at: int | None = None
        for row in ordered:
            if blocked_at is not None and int(row["trade_number"]) > blocked_at:
                raise AssertionError(
                    f"{coin}: trade_number {row['trade_number']} found after blocker at {blocked_at}"
                )
            if is_blocker_status(row["status"]):
                blocked_at = int(row["trade_number"])


def count_fill_families(fills: list[dict[str, Any]]) -> dict[str, int]:
    """Count fills whose ``purpose`` contains each tracked family substring."""
    counts = {"LONG_ADD": 0, "SHORT_REDUCE": 0, "SHORT_TP": 0, "REFILL": 0}
    for fill in fills:
        purpose = str(fill.get("purpose") or "")
        for family in counts:
            if family in purpose:
                counts[family] += 1
    return counts


# ---------------------------------------------------------------------------
# Coin discovery + coin-by-coin corpus selection
# ---------------------------------------------------------------------------


def discover_coins_for_audit(*, max_coins: int) -> list[dict[str, Any]]:
    """APTUSDT first, then :data:`CURATED_MAJORS`, then alphabetical (reused)."""
    return discover_coins(data_dir=DEFAULT_DATA_DIR, max_coins=max_coins, min_rows=40000)


def select_coins_for_run(
    coins_meta: list[dict[str, Any]],
    *,
    max_coins: int,
    min_trades: int,
    preferred_trades: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, int]:
    """Run the clean baseline coin-by-coin until enough real trades accumulate.

    Stops once ``total_trades >= preferred`` (``preferred = min(500,
    max(preferred_trades, min_trades, 200))``) or ``max_coins`` is reached.
    Returns ``(included, manifest_rows, total_trades, preferred)``.
    """
    preferred = min(PREFERRED_TRADES_CAP, max(int(preferred_trades), int(min_trades), 200))

    included: list[dict[str, Any]] = []
    manifest_rows: list[dict[str, Any]] = []
    total_trades = 0

    for rank, meta in enumerate(coins_meta, start=1):
        if len(included) >= max_coins:
            manifest_rows.append(
                {**meta, "priority_rank": rank, "included": False, "trades_started": None, "reason": "max_coins_reached"}
            )
            continue
        if total_trades >= preferred:
            manifest_rows.append(
                {**meta, "priority_rank": rank, "included": False, "trades_started": None, "reason": "preferred_trades_reached"}
            )
            continue

        symbol = meta["symbol"]
        try:
            candles = normalize_candles(symbol, load_candles_for_symbol(symbol, limit=FULL_HISTORY_CANDLE_LIMIT))
        except Exception as exc:  # pragma: no cover - defensive, corpus dependent
            manifest_rows.append(
                {**meta, "priority_rank": rank, "included": False, "trades_started": None, "reason": f"load_error:{exc}"}
            )
            continue
        if not candles:
            manifest_rows.append(
                {**meta, "priority_rank": rank, "included": False, "trades_started": None, "reason": "no_candles"}
            )
            continue

        payload = run_continuous_reentry_backtests(**build_baseline_call_kwargs(symbol=symbol, candles=candles))
        results: list[BacktestResult] = list(payload["results"])
        if not results:
            manifest_rows.append(
                {**meta, "priority_rank": rank, "included": False, "trades_started": 0, "reason": "no_trades_produced"}
            )
            continue

        trades_started = len(results)
        total_trades += trades_started
        included.append({"symbol": symbol, "meta": meta, "candles": candles, "results": results})
        manifest_rows.append(
            {
                **meta,
                "priority_rank": rank,
                "included": True,
                "candles_loaded": len(candles),
                "trades_started": trades_started,
                "reason": "ok",
            }
        )

    return included, manifest_rows, total_trades, preferred


# ---------------------------------------------------------------------------
# Phase A: per-trade detail row
# ---------------------------------------------------------------------------


def build_trade_detail_row(*, coin: str, result: BacktestResult, candles: list[Any]) -> dict[str, Any]:
    start_index = int(result.start_index or 0)
    window = candles[start_index:]
    analysis = analyze_trade(
        result,
        variant="current_baseline",
        long_add_pct=LONG_FILL_DISTANCE_PCT,
        target_profit_usdt=TARGET_PROFIT_USDT,
        window_candles=window,
        valid=True,
        skip_reason="ok",
    )
    status = normalize_trade_status(result)
    is_blocker = is_blocker_status(status)
    fills = list(result.fill_log or [])
    fill_counts = count_fill_families(fills)

    return {
        "coin": coin,
        "trade_number": int(result.trade_number or 0),
        "start_index": start_index,
        "end_index": result.end_index,
        "start_timestamp": _ts(result.start_time),
        "end_timestamp": _ts(result.end_time),
        "status": status,
        "is_blocker": int(is_blocker),
        "duration_candles": int(result.candles_processed or 0),
        "realized_pnl": analysis.get("realized_pnl"),
        "unrealized_pnl": analysis.get("unrealized_pnl"),
        "mtm_pnl": analysis.get("mtm_pnl"),
        "max_cycle": analysis.get("max_cycle"),
        "completed_cycles": analysis.get("completed_cycles"),
        "final_long_qty": analysis.get("final_long_qty"),
        "final_short_qty": analysis.get("final_short_qty"),
        "final_net_qty": analysis.get("final_net_qty"),
        "final_long_avg": analysis.get("final_long_avg"),
        "final_short_avg": analysis.get("final_short_avg"),
        "mark_price_end": analysis.get("mark_price_end"),
        "active_exit_price": analysis.get("active_exit_price"),
        "distance_to_exit": analysis.get("distance_to_exit"),
        "max_total_notional": analysis.get("max_total_notional"),
        "max_abs_net_exposure": analysis.get("max_abs_net_exposure"),
        "fees": analysis.get("fees"),
        "exit_rebuild_count": analysis.get("exit_rebuild_count"),
        "exit_increase_count": analysis.get("exit_increase_count"),
        "old_exit_later_reachable_count": analysis.get("old_exit_later_reachable_count"),
        "undercoverage": analysis.get("undercoverage"),
        "pending_final_exit": analysis.get("pending_final_exit"),
        "same_candle_long_add_short_reduce": analysis.get("same_candle_long_add_short_reduce"),
        "fills_LONG_ADD": fill_counts["LONG_ADD"],
        "fills_SHORT_REDUCE": fill_counts["SHORT_REDUCE"],
        "fills_SHORT_TP": fill_counts["SHORT_TP"],
        "fills_REFILL": fill_counts["REFILL"],
        "fills_count_total": len(fills),
        "exit_reason": result.exit_reason,
    }


# ---------------------------------------------------------------------------
# Phase C: blocker-only timelines
# ---------------------------------------------------------------------------


def _purpose_cycle(purpose: Any) -> int | None:
    match = CYCLE_PURPOSE_RE.match(str(purpose or ""))
    return int(match.group(1)) if match else None


def build_fill_event_rows(*, coin: str, trade_number: int, result: BacktestResult) -> list[dict[str, Any]]:
    """One row per fill plus LONG_TP_EXIT submit/cancel order events (blockers only)."""
    fills = list(result.fill_log or [])
    exit_orders = [
        order
        for order in (result.order_log or [])
        if str(order.get("purpose") or "") == "LONG_TP_EXIT"
        and str(order.get("event_type") or "").lower() in {"submitted", "cancelled"}
    ]

    events: list[tuple[int, dict[str, Any], str]] = []
    for fill in fills:
        events.append((0, fill, "fill"))
    for order in exit_orders:
        events.append((1, order, "order"))
    events.sort(key=lambda item: (str(item[1].get("timestamp") or ""), item[0]))

    rows: list[dict[str, Any]] = []
    cum_realized = 0.0
    active_exit: float | None = None

    for _, payload, kind in events:
        if kind == "order":
            event_type = str(payload.get("event_type") or "").lower()
            trigger = safe_float(payload.get("trigger_price") or payload.get("price"))
            if event_type == "submitted":
                active_exit = trigger
            rows.append(
                {
                    "coin": coin,
                    "trade_number": trade_number,
                    "timestamp": payload.get("timestamp"),
                    "candle_index": payload.get("candle_index"),
                    "event_kind": f"order_{event_type}",
                    "purpose": payload.get("purpose"),
                    "cycle": None,
                    "fill_price": None,
                    "qty": None,
                    "closed_pnl": None,
                    "cum_realized": cum_realized,
                    "long_qty_after": None,
                    "short_qty_after": None,
                    "net_qty": None,
                    "long_notional": None,
                    "short_notional": None,
                    "inventory_mtm_approx": None,
                    "active_exit": active_exit,
                    "distance_to_exit": None,
                }
            )
            continue

        purpose = str(payload.get("purpose") or "")
        pnl = safe_float(payload.get("closed_pnl") or payload.get("confirmed_closed_pnl"))
        cum_realized += pnl
        long_qty = safe_float(payload.get("long_qty_after"))
        short_qty = safe_float(payload.get("short_qty_after"))
        long_avg = safe_float(payload.get("long_avg_after"))
        short_avg = safe_float(payload.get("short_avg_after"))
        mark = safe_float(payload.get("fill_price"))
        net_qty = long_qty - short_qty
        long_notional = long_qty * long_avg
        short_notional = short_qty * short_avg
        inventory_mtm = cum_realized + long_qty * (mark - long_avg) + short_qty * (short_avg - mark)
        distance_to_exit = (active_exit - mark) if active_exit is not None and mark else None

        rows.append(
            {
                "coin": coin,
                "trade_number": trade_number,
                "timestamp": payload.get("timestamp"),
                "candle_index": payload.get("candle_index"),
                "event_kind": "fill",
                "purpose": purpose,
                "cycle": _purpose_cycle(purpose),
                "fill_price": mark,
                "qty": safe_float(payload.get("qty")),
                "closed_pnl": pnl,
                "cum_realized": cum_realized,
                "long_qty_after": long_qty,
                "short_qty_after": short_qty,
                "net_qty": net_qty,
                "long_notional": long_notional,
                "short_notional": short_notional,
                "inventory_mtm_approx": inventory_mtm,
                "active_exit": active_exit,
                "distance_to_exit": distance_to_exit,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Phase D: causal features (entry + path-to-stage)
# ---------------------------------------------------------------------------


def compute_causal_indicator_frame(candles: list[Any]) -> pd.DataFrame:
    """EMA/ATR indicator frame; every row depends only on candles up to it.

    ``ewm(adjust=False)`` is a strictly recursive/causal formula, so computing
    it once over the full coin history and indexing into row ``t`` afterwards
    is equivalent to recomputing it fresh from ``candles[:t+1]`` each time.
    """
    high = pd.Series([float(c.high) for c in candles], dtype="float64")
    low = pd.Series([float(c.low) for c in candles], dtype="float64")
    close = pd.Series([float(c.close) for c in candles], dtype="float64")
    atr14 = atr_wilder(high, low, close, 14)
    ema20 = ema(close, 20)
    ema59 = ema(close, 59)
    return pd.DataFrame(
        {
            "close": close,
            "atr14": atr14,
            "atr14_pct": atr_percent(atr14, close),
            "ema20_dist_pct": ema_distance_pct(close, ema20),
            "ema59_dist_pct": ema_distance_pct(close, ema59),
        }
    )


def compute_entry_features(*, indicator_frame: pd.DataFrame, candles: list[Any], start_index: int) -> dict[str, Any]:
    idx = int(start_index)
    if idx < 0 or idx >= len(candles):
        return {}
    candle = candles[idx]
    row = indicator_frame.iloc[idx]
    entry_price = float(candle.close)
    timestamp = candle.timestamp
    entry_hour_utc = timestamp.hour if hasattr(timestamp, "hour") else None

    def _enough(period: int) -> bool:
        return idx >= period

    atr14 = float(row["atr14"]) if _enough(14) and pd.notna(row["atr14"]) else None
    atr14_pct = float(row["atr14_pct"]) if _enough(14) and pd.notna(row["atr14_pct"]) else None
    ema20_dist_pct = float(row["ema20_dist_pct"]) if _enough(20) and pd.notna(row["ema20_dist_pct"]) else None
    ema59_dist_pct = float(row["ema59_dist_pct"]) if _enough(59) and pd.notna(row["ema59_dist_pct"]) else None
    close_vs_ema20_sign = None
    if ema20_dist_pct is not None:
        close_vs_ema20_sign = 1 if ema20_dist_pct > 0 else (-1 if ema20_dist_pct < 0 else 0)

    return {
        "entry_hour_utc": entry_hour_utc,
        "entry_price": entry_price,
        "atr14": atr14,
        "atr14_pct": atr14_pct,
        "ema20_dist_pct": ema20_dist_pct,
        "ema59_dist_pct": ema59_dist_pct,
        "close_vs_ema20_sign": close_vs_ema20_sign,
    }


def _local_index_for_cycle(fills: list[dict[str, Any]], cycle_n: int) -> int | None:
    for fill in fills:
        if str(fill.get("purpose") or "") == f"CYCLE_{cycle_n}_SHORT_REDUCE":
            idx = fill.get("candle_index")
            return int(idx) if idx is not None else None
    return None


def _last_completed_cycle_local_index(fills: list[dict[str, Any]]) -> tuple[int | None, int | None]:
    best_cycle: int | None = None
    best_idx: int | None = None
    for fill in fills:
        purpose = str(fill.get("purpose") or "")
        if not purpose.endswith("_SHORT_REDUCE"):
            continue
        cycle_n = _purpose_cycle(purpose)
        idx = fill.get("candle_index")
        if cycle_n is None or idx is None:
            continue
        if best_cycle is None or cycle_n > best_cycle:
            best_cycle = cycle_n
            best_idx = int(idx)
    return best_cycle, best_idx


def resolve_stage_local_index(
    *, stage_name: str, spec: Any, fills: list[dict[str, Any]], window_len: int
) -> tuple[int | None, bool]:
    """Return ``(local_index, incomplete)`` for one named stage.

    ``incomplete`` is True whenever the stage target was never reached within
    the trade's own window (a cycle that never completed, or a candle-count
    target beyond the trade's actual duration) — in that case the caller
    should fall back to the trade's end state rather than inventing data.
    """
    if stage_name == "after_initial":
        idx = None
        for fill in fills:
            purpose = str(fill.get("purpose") or "")
            if purpose == "INITIAL_LONG_ENTRY" or purpose.endswith("INITIAL_LONG_ENTRY"):
                idx = fill.get("candle_index")
                break
        if idx is None and fills:
            idx = fills[0].get("candle_index")
        if idx is None:
            return None, True
        return int(idx), False

    if spec == "last":
        _, idx = _last_completed_cycle_local_index(fills)
        if idx is None:
            return None, True
        return idx, False

    if isinstance(spec, int) and spec <= 5:
        idx = _local_index_for_cycle(fills, spec)
        if idx is None:
            # Cycle never completed — stage is unavailable (do NOT clamp to trade end).
            return None, True
        return idx, False

    candle_target = int(spec)
    target_idx = candle_target - 1
    if window_len <= 0:
        return None, True
    if target_idx <= window_len - 1:
        return target_idx, False
    # Trade did not last long enough for this candle milestone.
    return None, True


def compute_stage_feature_row(
    *,
    coin: str,
    trade_number: int,
    group: str,
    entry_price: float | None,
    window: list[Any],
    fills: list[dict[str, Any]],
    rebuilds: list[dict[str, Any]],
    local_index: int | None,
    incomplete: bool,
) -> dict[str, Any]:
    base = {"coin": coin, "trade_number": trade_number, "group": group}
    if local_index is None or not window:
        return {**base, "available": False, "incomplete": incomplete, "local_index": None}

    candles_slice = window[: local_index + 1]
    if not candles_slice:
        return {**base, "available": False, "incomplete": incomplete, "local_index": None}

    highs = [float(c.high) for c in candles_slice]
    lows = [float(c.low) for c in candles_slice]
    last_close = float(candles_slice[-1].close)
    price_change_pct = ((last_close - entry_price) / entry_price * 100.0) if entry_price else None
    mfe_pct = ((max(highs) - entry_price) / entry_price * 100.0) if entry_price else None
    mae_pct = ((min(lows) - entry_price) / entry_price * 100.0) if entry_price else None

    fills_upto = [
        fill
        for fill in fills
        if fill.get("candle_index") is not None and int(fill.get("candle_index")) <= local_index
    ]

    max_cycle_so_far = 0
    for fill in fills_upto:
        cycle_n = _purpose_cycle(fill.get("purpose"))
        if cycle_n is not None:
            max_cycle_so_far = max(max_cycle_so_far, cycle_n)

    rebuilds_upto = [
        rebuild
        for rebuild in rebuilds
        if rebuild.get("candle_index") is not None and int(rebuild.get("candle_index")) <= local_index
    ]
    exit_rebuilds_so_far = len(rebuilds_upto)
    exit_increases_so_far = sum(1 for rebuild in rebuilds_upto if rebuild.get("is_increase"))

    long_qty = short_qty = long_avg = short_avg = 0.0
    cum_realized = 0.0
    for fill in fills_upto:
        cum_realized += safe_float(fill.get("closed_pnl") or fill.get("confirmed_closed_pnl"))
        if fill.get("long_qty_after") is not None:
            long_qty = safe_float(fill.get("long_qty_after"))
        if fill.get("short_qty_after") is not None:
            short_qty = safe_float(fill.get("short_qty_after"))
        if fill.get("long_avg_after") is not None:
            long_avg = safe_float(fill.get("long_avg_after"))
        if fill.get("short_avg_after") is not None:
            short_avg = safe_float(fill.get("short_avg_after"))

    net_exposure = long_qty - short_qty
    long_short_qty_ratio = (long_qty / short_qty) if short_qty > 1e-12 else None
    inventory_mtm = cum_realized + long_qty * (last_close - long_avg) + short_qty * (short_avg - last_close)
    fees_so_far = exposure_from_fills(fills_upto)["fees"]

    return {
        **base,
        "available": True,
        "incomplete": incomplete,
        "local_index": local_index,
        "price_change_pct": price_change_pct,
        "mae_pct": mae_pct,
        "mfe_pct": mfe_pct,
        "max_cycle_so_far": max_cycle_so_far,
        "exit_rebuilds_so_far": exit_rebuilds_so_far,
        "exit_increases_so_far": exit_increases_so_far,
        "net_exposure": net_exposure,
        "long_short_qty_ratio": long_short_qty_ratio,
        "inventory_mtm": inventory_mtm,
        "fees_so_far": fees_so_far,
        "duration": local_index + 1,
    }


def mae_before_mfe_triggered(*, entry_price: float | None, window: list[Any], local_index: int | None) -> bool:
    """True iff a running -3% MAE occurs strictly before any running +1% MFE."""
    if entry_price is None or local_index is None or not entry_price:
        return False
    running_min_low: float | None = None
    running_max_high: float | None = None
    for candle in window[: local_index + 1]:
        low = float(candle.low)
        high = float(candle.high)
        running_min_low = low if running_min_low is None else min(running_min_low, low)
        running_max_high = high if running_max_high is None else max(running_max_high, high)
        mfe_now = (running_max_high - entry_price) / entry_price * 100.0
        if mfe_now > 1.0:
            return False
        mae_now = (running_min_low - entry_price) / entry_price * 100.0
        if mae_now <= -3.0:
            return True
    return False


def analyze_trade_for_blocker_audit(
    *, coin: str, result: BacktestResult, candles: list[Any], indicator_frame: pd.DataFrame
) -> dict[str, Any]:
    """Central per-trade computation reused across Phases A/C/D/E/F."""
    row = build_trade_detail_row(coin=coin, result=result, candles=candles)
    group = "blocker" if row["is_blocker"] else "closed"
    start_index = row["start_index"]
    window = candles[start_index:]
    fills = list(result.fill_log or [])
    rebuilds = exit_rebuild_stats(result, window_candles=window).get("rebuilds") or []
    entry_features = compute_entry_features(indicator_frame=indicator_frame, candles=candles, start_index=start_index)
    entry_price = entry_features.get("entry_price")
    window_len = len(window)

    stage_rows: list[dict[str, Any]] = []
    for stage_name, spec in STAGE_DEFINITIONS:
        local_index, incomplete = resolve_stage_local_index(
            stage_name=stage_name, spec=spec, fills=fills, window_len=window_len
        )
        feature = compute_stage_feature_row(
            coin=coin,
            trade_number=row["trade_number"],
            group=group,
            entry_price=entry_price,
            window=window,
            fills=fills,
            rebuilds=rebuilds,
            local_index=local_index,
            incomplete=incomplete,
        )
        feature["stage"] = stage_name
        feature.update(entry_features)
        stage_rows.append(feature)

    at_500 = next((stage for stage in stage_rows if stage["stage"] == "at_500"), None)
    mae_before_mfe = bool(
        at_500 is not None
        and at_500.get("available")
        and mae_before_mfe_triggered(entry_price=entry_price, window=window, local_index=at_500.get("local_index"))
    )

    return {
        "coin": coin,
        "row": row,
        "group": group,
        "entry_features": entry_features,
        "stage_rows": stage_rows,
        "at_500": at_500,
        "mae_before_mfe": mae_before_mfe,
        "fills": fills,
        "rebuilds": rebuilds,
        "window": window,
    }


def aggregate_closed_vs_blocker(
    feature_rows: list[dict[str, Any]], *, metrics: tuple[str, ...] = NUMERIC_STAGE_METRICS
) -> list[dict[str, Any]]:
    """One row per (stage, metric) with closed/blocker mean/median + delta."""
    out: list[dict[str, Any]] = []
    stages = sorted({row.get("stage") for row in feature_rows if row.get("stage")})
    for stage in stages:
        stage_rows = [row for row in feature_rows if row.get("stage") == stage and row.get("available")]
        closed_rows = [row for row in stage_rows if row.get("group") == "closed"]
        blocker_rows = [row for row in stage_rows if row.get("group") == "blocker"]
        for metric in metrics:
            closed_vals = [safe_float(row.get(metric)) for row in closed_rows if row.get(metric) is not None]
            blocker_vals = [safe_float(row.get(metric)) for row in blocker_rows if row.get(metric) is not None]
            closed_mean = statistics.mean(closed_vals) if closed_vals else None
            blocker_mean = statistics.mean(blocker_vals) if blocker_vals else None
            out.append(
                {
                    "stage": stage,
                    "metric": metric,
                    "closed_n": len(closed_vals),
                    "blocker_n": len(blocker_vals),
                    "closed_mean": closed_mean,
                    "blocker_mean": blocker_mean,
                    "closed_median": statistics.median(closed_vals) if closed_vals else None,
                    "blocker_median": statistics.median(blocker_vals) if blocker_vals else None,
                    "delta_mean": (
                        (blocker_mean - closed_mean) if closed_mean is not None and blocker_mean is not None else None
                    ),
                }
            )
    return out


# ---------------------------------------------------------------------------
# Phase E: hypotheses 1-12
# ---------------------------------------------------------------------------


def _group_field_values(analyzed: list[dict[str, Any]], group: str, field: str) -> list[float]:
    return [
        safe_float(item["row"].get(field))
        for item in analyzed
        if item["group"] == group and item["row"].get(field) is not None
    ]


def _group_entry_values(analyzed: list[dict[str, Any]], group: str, field: str) -> list[float]:
    return [
        safe_float(item["entry_features"].get(field))
        for item in analyzed
        if item["group"] == group and item["entry_features"].get(field) is not None
    ]


def _mode_share(values: list[Any]) -> float | None:
    cleaned = [value for value in values if value is not None]
    if not cleaned:
        return None
    counts = Counter(cleaned)
    top = counts.most_common(1)[0][1]
    return top / len(cleaned)


def compute_hypotheses(analyzed_trades: list[dict[str, Any]], early_warning_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    closed = [item for item in analyzed_trades if item["group"] == "closed"]
    blocker = [item for item in analyzed_trades if item["group"] == "blocker"]

    def _mean(values: list[float]) -> float | None:
        return statistics.mean(values) if values else None

    def _rate(items: list[dict[str, Any]], predicate) -> float | None:
        return (sum(1 for item in items if predicate(item)) / len(items)) if items else None

    hypotheses: list[dict[str, Any]] = []

    def add(hid: int, text: str, closed_value: Any, blocker_value: Any, supported: bool | None) -> None:
        hypotheses.append(
            {"id": hid, "text": text, "closed_value": closed_value, "blocker_value": blocker_value, "supported": supported}
        )

    c_cycle = _mean(_group_field_values(closed, "closed", "max_cycle"))
    b_cycle = _mean(_group_field_values(blocker, "blocker", "max_cycle"))
    add(1, "Blocker trades reach a higher max_cycle than closed trades.", c_cycle, b_cycle,
        bool(c_cycle is not None and b_cycle is not None and b_cycle > c_cycle))

    c_inc = _mean(_group_field_values(closed, "closed", "exit_increase_count"))
    b_inc = _mean(_group_field_values(blocker, "blocker", "exit_increase_count"))
    add(2, "Blocker trades accumulate more exit-rebuild increases than closed trades.", c_inc, b_inc,
        bool(c_inc is not None and b_inc is not None and b_inc > c_inc))

    c_sc = _mean(_group_field_values(closed, "closed", "same_candle_long_add_short_reduce"))
    b_sc = _mean(_group_field_values(blocker, "blocker", "same_candle_long_add_short_reduce"))
    add(3, "Blocker trades show more same-candle LONG_ADD/SHORT_REDUCE violations than closed trades.", c_sc, b_sc,
        bool(c_sc is not None and b_sc is not None and b_sc > c_sc))

    c_under = _rate(closed, lambda item: int(item["row"].get("undercoverage") or 0) > 0)
    b_under = _rate(blocker, lambda item: int(item["row"].get("undercoverage") or 0) > 0)
    add(4, "Blocker trades have a higher undercoverage rate than closed trades.", c_under, b_under,
        bool(c_under is not None and b_under is not None and b_under > c_under))

    c_fees = _mean(_group_field_values(closed, "closed", "fees"))
    b_fees = _mean(_group_field_values(blocker, "blocker", "fees"))
    add(5, "Blocker trades accumulate higher total fees than closed trades.", c_fees, b_fees,
        bool(c_fees is not None and b_fees is not None and b_fees > c_fees))

    c_atr = _mean(_group_entry_values(closed, "closed", "atr14_pct"))
    b_atr = _mean(_group_entry_values(blocker, "blocker", "atr14_pct"))
    add(6, "Blocker trades enter in higher-ATR% (more volatile) regimes than closed trades.", c_atr, b_atr,
        bool(c_atr is not None and b_atr is not None and b_atr > c_atr))

    c_below = _rate(closed, lambda item: (item["entry_features"].get("close_vs_ema20_sign") or 0) < 0)
    b_below = _rate(blocker, lambda item: (item["entry_features"].get("close_vs_ema20_sign") or 0) < 0)
    add(7, "Blocker trades enter below EMA20 (downtrend proxy) more often than closed trades.", c_below, b_below,
        bool(c_below is not None and b_below is not None and b_below > c_below))

    c_exp = _mean(_group_field_values(closed, "closed", "max_abs_net_exposure"))
    b_exp = _mean(_group_field_values(blocker, "blocker", "max_abs_net_exposure"))
    add(8, "Blocker trades reach a larger max_abs_net_exposure than closed trades.", c_exp, b_exp,
        bool(c_exp is not None and b_exp is not None and b_exp > c_exp))

    c_refill = _rate(closed, lambda item: int(item["row"].get("fills_REFILL") or 0) > 0)
    b_refill = _rate(blocker, lambda item: int(item["row"].get("fills_REFILL") or 0) > 0)
    add(9, "Blocker trades use REFILL fills more often than closed trades.", c_refill, b_refill,
        bool(c_refill is not None and b_refill is not None and b_refill > c_refill))

    c_dist = _mean(_group_field_values(closed, "closed", "distance_to_exit"))
    b_dist = _mean(_group_field_values(blocker, "blocker", "distance_to_exit"))
    add(10, "Blocker trades' final exit sits further from the mark price than closed trades'.", c_dist, b_dist,
        bool(c_dist is not None and b_dist is not None and b_dist > c_dist))

    cycle_rule = next((row for row in early_warning_rows if row.get("rule") == "max_cycle_ge_3_by_500"), None)
    recall = (cycle_rule or {}).get("recall")
    fpr = (cycle_rule or {}).get("fpr")
    add(
        11,
        "The early-warning rule max_cycle_so_far>=3 by candle 500 has meaningfully higher "
        "recall than false-positive rate (diagnostic only).",
        fpr,
        recall,
        bool(recall is not None and fpr is not None and recall > fpr),
    )

    c_hour_share = _mode_share([item["entry_features"].get("entry_hour_utc") for item in closed])
    b_hour_share = _mode_share([item["entry_features"].get("entry_hour_utc") for item in blocker])
    add(12, "Blocker trades cluster more strongly around one entry hour (UTC) than closed trades.",
        c_hour_share, b_hour_share,
        bool(c_hour_share is not None and b_hour_share is not None and b_hour_share > c_hour_share))

    return hypotheses


# ---------------------------------------------------------------------------
# Phase F: causal early-warning candidates (diagnostic only)
# ---------------------------------------------------------------------------


EARLY_WARNING_RULE_IDS = (
    "max_cycle_ge_3_by_500",
    "exit_increase_ge_2_by_500",
    "long_short_ratio_ge_1_5_by_500",
    "mae_before_mfe",
    "inventory_mtm_lt_neg1_by_500",
)


def _evaluate_rule_hit(rule_id: str, *, feature: dict[str, Any] | None, mae_before_mfe: bool) -> bool:
    if rule_id == "mae_before_mfe":
        return bool(mae_before_mfe)
    if feature is None or not feature.get("available"):
        return False
    if rule_id == "max_cycle_ge_3_by_500":
        return int(feature.get("max_cycle_so_far") or 0) >= 3
    if rule_id == "exit_increase_ge_2_by_500":
        return int(feature.get("exit_increases_so_far") or 0) >= 2
    if rule_id == "long_short_ratio_ge_1_5_by_500":
        ratio = feature.get("long_short_qty_ratio")
        return ratio is not None and ratio >= 1.5
    if rule_id == "inventory_mtm_lt_neg1_by_500":
        mtm = feature.get("inventory_mtm")
        return mtm is not None and mtm < -1.0
    return False


def evaluate_early_warning_rules(analyzed_trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Diagnostic precision/recall stats for each causal early-warning rule.

    Uses only the ``at_500`` stage feature (already causal / no-look-ahead:
    it is clamped to the trade's own end state whenever the trade finished
    before candle 500). ``avoided_mtm_diagnostic`` is explicitly NOT a trading
    claim — it is a raw diagnostic sum for research context only.
    """
    total_blockers = sum(1 for item in analyzed_trades if item["group"] == "blocker")
    total_closed = sum(1 for item in analyzed_trades if item["group"] == "closed")

    out: list[dict[str, Any]] = []
    for rule_id in EARLY_WARNING_RULE_IDS:
        blocker_hit_items = [
            item
            for item in analyzed_trades
            if item["group"] == "blocker"
            and _evaluate_rule_hit(rule_id, feature=item.get("at_500"), mae_before_mfe=item.get("mae_before_mfe", False))
        ]
        closed_fp_items = [
            item
            for item in analyzed_trades
            if item["group"] == "closed"
            and _evaluate_rule_hit(rule_id, feature=item.get("at_500"), mae_before_mfe=item.get("mae_before_mfe", False))
        ]
        blocker_hits = len(blocker_hit_items)
        closed_fp = len(closed_fp_items)
        precision = (blocker_hits / (blocker_hits + closed_fp)) if (blocker_hits + closed_fp) else None
        recall = (blocker_hits / total_blockers) if total_blockers else None
        fpr = (closed_fp / total_closed) if total_closed else None
        leads = [
            max(0, int(item["row"].get("duration_candles") or 0) - int((item.get("at_500") or {}).get("local_index") or 0) - 1)
            for item in blocker_hit_items
            if item.get("at_500") is not None
        ]
        median_lead = statistics.median(leads) if leads else None
        avoided_mtm = sum(safe_float(item["row"].get("mtm_pnl")) for item in blocker_hit_items)
        out.append(
            {
                "rule": rule_id,
                "blocker_hits": blocker_hits,
                "closed_false_positives": closed_fp,
                "total_blockers": total_blockers,
                "total_closed": total_closed,
                "precision": precision,
                "recall": recall,
                "fpr": fpr,
                "median_lead_candles_before_end": median_lead,
                "avoided_mtm_diagnostic": avoided_mtm,
                "note": "diagnostic only - not a trading claim",
            }
        )
    return out


# ---------------------------------------------------------------------------
# Aggregation outputs
# ---------------------------------------------------------------------------


def summarize_coin(*, coin: str, trade_rows: list[dict[str, Any]], candles_loaded: int) -> dict[str, Any]:
    closed_rows = [row for row in trade_rows if not row["is_blocker"]]
    blocker_rows = [row for row in trade_rows if row["is_blocker"]]
    mtm_values = [safe_float(row["mtm_pnl"]) for row in trade_rows]
    blocker = blocker_rows[-1] if blocker_rows else None
    return {
        "coin": coin,
        "candles_loaded": candles_loaded,
        "trades_started": len(trade_rows),
        "trades_closed": len(closed_rows),
        "trades_blocker": len(blocker_rows),
        "has_blocker": int(bool(blocker_rows)),
        "closed_rate": (len(closed_rows) / len(trade_rows)) if trade_rows else 0.0,
        "sum_closed_pnl": sum(safe_float(row["realized_pnl"]) for row in closed_rows),
        "series_mtm": sum(mtm_values),
        "worst_trade_mtm": min(mtm_values) if mtm_values else None,
        "best_trade_mtm": max(mtm_values) if mtm_values else None,
        "undercoverage": sum(int(row["undercoverage"] or 0) for row in trade_rows),
        "same_candle_violations": sum(int(row["same_candle_long_add_short_reduce"] or 0) for row in trade_rows),
        "fees": sum(safe_float(row["fees"]) for row in trade_rows),
        "blocker_trade_number": (blocker or {}).get("trade_number"),
        "blocker_start_timestamp": (blocker or {}).get("start_timestamp"),
        "blocker_duration_candles": (blocker or {}).get("duration_candles"),
        "blocker_mtm_pnl": (blocker or {}).get("mtm_pnl"),
    }


def build_undercoverage_case_rows(
    *, coin: str, trade_number: int, status: str, result: BacktestResult
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for audit_row in build_pnl_coverage_audit(result):
        if "undercover" not in str(audit_row.get("status") or "").lower():
            continue
        rows.append(
            {
                "coin": coin,
                "trade_number": trade_number,
                "trade_status": status,
                "cycle_index": audit_row.get("cycle_index"),
                "loss_purpose": audit_row.get("loss_purpose"),
                "cover_purpose": audit_row.get("cover_purpose"),
                "loss_pnl": audit_row.get("loss_pnl"),
                "cover_pnl": audit_row.get("cover_pnl"),
                "missing_pnl_gap": audit_row.get("missing_pnl"),
                "status": audit_row.get("status"),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# REPORT.md
# ---------------------------------------------------------------------------


def write_report(
    path: Path,
    *,
    baseline_params: dict[str, Any],
    coin_manifest_rows: list[dict[str, Any]],
    coin_summaries: list[dict[str, Any]],
    total_trades: int,
    preferred_trades: int,
    min_trades: int,
    closed_rows: list[dict[str, Any]],
    blocker_rows: list[dict[str, Any]],
    comparison_rows: list[dict[str, Any]],
    hypotheses: list[dict[str, Any]],
    early_warning_rows: list[dict[str, Any]],
    undercoverage_rows: list[dict[str, Any]],
    live_defaults_unchanged: bool,
) -> None:
    total = len(closed_rows) + len(blocker_rows)
    blocker_coins = sum(1 for row in coin_summaries if row.get("has_blocker"))

    lines: list[str] = [
        "# Current Live-Baseline Multi-Coin Continuous Blocker Audit",
        "",
        f"Generated: `{datetime.now(timezone.utc).isoformat()}`",
        "",
        "## Setup (clean baseline — no exit-rebuild policy, no LONG_ADD sweep, no recovery expansion)",
        "",
        f"- `direction={DIRECTION}`, `config_source={CONFIG_SOURCE}`, `fill_model={FILL_MODEL}`",
        f"- Pinned: `long_fill_distance_pct={LONG_FILL_DISTANCE_PCT}`, "
        f"`target_profit_usdt={TARGET_PROFIT_USDT}`, `tp_profit_target_pct={TP_PROFIT_TARGET_PCT}`",
        f"- `tp_buffer_pct` from live config: `{baseline_params.get('tp_buffer_pct')}`",
        f"- Candle window: `{FULL_HISTORY_CANDLE_LIMIT}` (via `load_candles_for_symbol`)",
        f"- Coins included: `{len(coin_manifest_rows)}` manifest rows, "
        f"`{sum(1 for r in coin_manifest_rows if r.get('included'))}` included, total real trades `{total_trades}` "
        f"(target band: preferred=`{preferred_trades}`, min=`{min_trades}`)",
        f"- Real trades analyzed: `{total}` (closed=`{len(closed_rows)}`, blocker=`{len(blocker_rows)}`)",
        f"- Coins with a blocker: `{blocker_coins}` / `{len(coin_summaries)}`",
        f"- Live `long_fill_distance_pct` unchanged after run: `{live_defaults_unchanged}`",
        "",
        "## Phase E — Hypotheses 1-12",
        "",
        "| # | hypothesis | closed | blocker | supported |",
        "|---:|---|---:|---:|:---:|",
    ]
    for hyp in hypotheses:
        closed_val = hyp["closed_value"]
        blocker_val = hyp["blocker_value"]
        closed_str = f"{closed_val:.4f}" if isinstance(closed_val, float) else closed_val
        blocker_str = f"{blocker_val:.4f}" if isinstance(blocker_val, float) else blocker_val
        lines.append(f"| {hyp['id']} | {hyp['text']} | {closed_str} | {blocker_str} | {hyp['supported']} |")

    lines.extend(
        [
            "",
            "## Phase F — Early-warning candidates (diagnostic only, not a trading claim)",
            "",
            "| rule | blocker_hits | closed_fp | precision | recall | fpr | median_lead | avoided_mtm_diag |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    def _fmt(value: float | None) -> str:
        return f"{value:.3f}" if value is not None else "n/a"

    for row in early_warning_rows:
        lines.append(
            f"| {row['rule']} | {row['blocker_hits']} | {row['closed_false_positives']} | "
            f"{_fmt(row.get('precision'))} | {_fmt(row.get('recall'))} | {_fmt(row.get('fpr'))} | "
            f"{row.get('median_lead_candles_before_end')} | {safe_float(row.get('avoided_mtm_diagnostic')):.4f} |"
        )

    blocker_by_coin = Counter(row["coin"] for row in blocker_rows)
    top_blocker_coins = ", ".join(f"{coin}({count})" for coin, count in blocker_by_coin.most_common(5)) or "none"

    # Top distinguishing metrics by absolute delta_mean at after_last_completed_cycle / overall
    top_diffs = sorted(
        [
            row
            for row in comparison_rows
            if row.get("delta_mean") is not None
            and row.get("stage") in {"after_last_completed_cycle", "at_entry", "at_500", "overall"}
        ],
        key=lambda row: abs(safe_float(row.get("delta_mean"))),
        reverse=True,
    )[:8]
    top_diff_txt = "; ".join(
        f"{row.get('stage')}/{row.get('metric')}: Δmean={safe_float(row.get('delta_mean')):.4f}"
        for row in top_diffs[:5]
    ) or "n/a"

    total_mtm = sum(safe_float(r.get("mtm_pnl")) for r in closed_rows + blocker_rows)
    closed_mtm = sum(safe_float(r.get("mtm_pnl")) for r in closed_rows)
    blocker_mtm = sum(safe_float(r.get("mtm_pnl")) for r in blocker_rows)

    # Earliest divergence stage: first stage where |delta| on max_cycle or inventory_mtm is large
    divergence_hint = "unbekannt"
    for stage in ("after_cycle_1", "after_cycle_2", "after_cycle_3", "at_100", "at_500", "at_1000"):
        stage_rows = [r for r in comparison_rows if r.get("stage") == stage]
        if not stage_rows:
            continue
        notable = [
            r
            for r in stage_rows
            if r.get("metric") in {"max_cycle_so_far", "inventory_mtm", "mae_pct", "exit_increases_so_far"}
            and abs(safe_float(r.get("delta_mean"))) > 0.5
        ]
        if notable:
            divergence_hint = f"{stage} (u.a. {[r.get('metric') for r in notable[:3]]})"
            break

    hyp_by_id = {int(h["id"]): h for h in hypotheses}
    early_hits = sum(1 for row in early_warning_rows if int(row.get("blocker_hits") or 0) > 0)
    best_recall_rule = max(
        (row for row in early_warning_rows if row.get("recall") is not None),
        key=lambda row: safe_float(row.get("recall")),
        default=None,
    )

    # Primary problem class heuristic
    problem_votes = {
        "Trend": bool((hyp_by_id.get(7) or {}).get("supported")) or bool((hyp_by_id.get(6) or {}).get("supported")),
        "Exposure": bool((hyp_by_id.get(8) or {}).get("supported")),
        "Cycle": bool((hyp_by_id.get(1) or {}).get("supported")) if 1 in hyp_by_id else bool((hyp_by_id.get(0) or {}).get("supported")),
        "Exit": bool((hyp_by_id.get(2) or {}).get("supported")) if 2 in hyp_by_id else bool((hyp_by_id.get(1) or {}).get("supported")),
    }
    # Map to actual hypothesis ids in this file (0-indexed list but id field 1..12)
    problem_votes = {
        "Trend": bool((hyp_by_id.get(7) or {}).get("supported")),
        "Exposure": bool((hyp_by_id.get(8) or {}).get("supported")),
        "Cycle": bool((hyp_by_id.get(1) or {}).get("supported")),
        "Exit": bool((hyp_by_id.get(2) or {}).get("supported")),
    }
    primary_problem = max(problem_votes, key=lambda k: int(problem_votes[k])) if any(problem_votes.values()) else "gemischt (kein einzelnes Muster dominant)"

    next_hypothesis = (
        f"`{(best_recall_rule or {}).get('rule', 'exit_increase_ge_2_by_500')}` isoliert gegen Closed-Trades "
        f"(diagnostische Recall/FPR-Trennung) — ohne Live-Änderung."
    )

    lines.extend(
        [
            "",
            "## Abschlussfragen",
            "",
            f"1. **Wie viele reale Trades und Coins wurden getestet?** "
            f"`{total}` reale Continuous-Trades auf `{len(coin_summaries)}` Coins "
            f"(manifest included=`{sum(1 for r in coin_manifest_rows if r.get('included'))}`).",
            f"2. **Wie viele Trades schließen erfolgreich?** `{len(closed_rows)}` "
            f"(Closed-Rate `{(len(closed_rows) / total) if total else 0:.1%}`).",
            f"3. **Wie viele Coins enden mit einem Blocker?** `{blocker_coins}` / `{len(coin_summaries)}` "
            f"(`{len(blocker_rows)}` offene Blocker-Trades).",
            f"4. **Wie hoch ist das reale Gesamt-MTM?** `{total_mtm:.4f}` USDT "
            f"(closed=`{closed_mtm:.4f}`, blocker=`{blocker_mtm:.4f}`).",
            f"5. **Welche fünf Merkmale unterscheiden Blocker am stärksten von geschlossenen Trades?** "
            f"{top_diff_txt}",
            f"6. **Ab welchem Cycle oder Zeitpunkt beginnt die Divergenz?** {divergence_hint}",
            f"7. **Sind Blocker primär ein Trend-, Exposure-, Cycle- oder Exit-Problem?** "
            f"Primär **{primary_problem}** "
            f"(Votes Trend={problem_votes['Trend']}, Exposure={problem_votes['Exposure']}, "
            f"Cycle={problem_votes['Cycle']}, Exit={problem_votes['Exit']}).",
            f"8. **Haben Blocker coinübergreifend dasselbe Muster?** "
            f"Teilweise — Top-Blocker-Coins: `{top_blocker_coins}`; "
            f"gemeinsame Tendenzen siehe Hypothesen-Tabelle; Konzentration Entry-Hour "
            f"closed=`{(hyp_by_id.get(12) or {}).get('closed_value')}` vs "
            f"blocker=`{(hyp_by_id.get(12) or {}).get('blocker_value')}`.",
            f"9. **Wie viele Blocker wären theoretisch früh kausal erkennbar gewesen?** "
            f"Beste Recall-Regel diagnostisch: `{(best_recall_rule or {}).get('rule')}` "
            f"mit blocker_hits=`{(best_recall_rule or {}).get('blocker_hits')}` / `{len(blocker_rows)}` "
            f"(recall=`{(best_recall_rule or {}).get('recall')}`, "
            f"fpr=`{(best_recall_rule or {}).get('fpr')}`). "
            f"Regeln mit ≥1 Hit: `{early_hits}` / `{len(early_warning_rows)}`. "
            f"**Kein Handelssignal — nur Diagnose.**",
            f"10. **Welche eine Hypothese sollte als Nächstes isoliert getestet werden?** "
            f"{next_hypothesis}",
            "",
            "### Undercoverage (separat von Blockern)",
            f"- Fälle: `{len(undercoverage_rows)}` — siehe `undercoverage_cases.csv`. "
            f"Nicht automatisch mit Blocker-Ursache gleichsetzen "
            f"(Hypothese 4/Undercoverage-Rate closed=`{(hyp_by_id.get(4) or {}).get('closed_value')}` "
            f"vs blocker=`{(hyp_by_id.get(4) or {}).get('blocker_value')}`).",
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
    preferred_trades: int = DEFAULT_PREFERRED_TRADES,
) -> dict[str, Any]:
    if output_root.exists() and any(output_root.iterdir()):
        raise FileExistsError(f"Refusing to overwrite existing output directory: {output_root}")
    output_root.mkdir(parents=True, exist_ok=True)

    live_before = resolve_backtest_config(config_source=CONFIG_SOURCE, signal="long", symbol=APT_SYMBOL)
    live_long_fill_distance_pct_before = float(live_before.config.long_fill_distance_pct)

    baseline_params = resolve_and_document_baseline_params(output_root)

    coins_meta = discover_coins_for_audit(max_coins=max_coins)
    included, coin_manifest_rows, total_trades, preferred = select_coins_for_run(
        coins_meta, max_coins=max_coins, min_trades=min_trades, preferred_trades=preferred_trades
    )

    # Phase A + central per-trade analysis (also feeds C/D/E/F).
    analyzed_trades: list[dict[str, Any]] = []
    coin_trade_rows: dict[str, list[dict[str, Any]]] = {}
    for entry in included:
        coin = entry["symbol"]
        candles = entry["candles"]
        indicator_frame = compute_causal_indicator_frame(candles)
        coin_rows: list[dict[str, Any]] = []
        for result in entry["results"]:
            analyzed = analyze_trade_for_blocker_audit(
                coin=coin, result=result, candles=candles, indicator_frame=indicator_frame
            )
            analyzed["result"] = result
            analyzed_trades.append(analyzed)
            coin_rows.append(analyzed["row"])
        coin_trade_rows[coin] = coin_rows

    validate_continuous_trade_sequence([item["row"] for item in analyzed_trades])

    all_trade_rows = [item["row"] for item in analyzed_trades]
    closed_rows = [row for row in all_trade_rows if not row["is_blocker"]]
    blocker_rows = [row for row in all_trade_rows if row["is_blocker"]]

    # Phase C: timelines for blockers only.
    blocker_items = [item for item in analyzed_trades if item["group"] == "blocker"]
    fill_timeline_rows: list[dict[str, Any]] = []
    cycle_timeline_rows: list[dict[str, Any]] = []
    exit_timeline_rows: list[dict[str, Any]] = []
    for item in blocker_items:
        coin = item["coin"]
        trade_number = item["row"]["trade_number"]
        result: BacktestResult = item["result"]
        fill_timeline_rows.extend(build_fill_event_rows(coin=coin, trade_number=trade_number, result=result))
        for cycle_row in build_cycle_rows(
            variant="current_baseline",
            long_add_pct=LONG_FILL_DISTANCE_PCT,
            start_index=item["row"]["start_index"],
            fills=item["fills"],
            target_profit_usdt=TARGET_PROFIT_USDT,
        ):
            cycle_timeline_rows.append({"coin": coin, "trade_number": trade_number, **cycle_row})
        for rebuild in item["rebuilds"]:
            exit_timeline_rows.append({"coin": coin, "trade_number": trade_number, **rebuild})

    # Phase D: causal comparison features (all trades feed the aggregation; the
    # per-trade table doubles as blocker_feature_comparison.csv).
    all_stage_rows: list[dict[str, Any]] = []
    for item in analyzed_trades:
        all_stage_rows.extend(item["stage_rows"])
    comparison_rows = aggregate_closed_vs_blocker(all_stage_rows)

    # Phase F (needs Phase D's at_500 stage features, already attached above).
    early_warning_rows = evaluate_early_warning_rules(analyzed_trades)

    # Phase E hypotheses (needs Phase F stats for hypothesis 11).
    hypotheses = compute_hypotheses(analyzed_trades, early_warning_rows)

    # Aggregation outputs.
    coin_summaries = [
        summarize_coin(coin=coin, trade_rows=rows, candles_loaded=len(next(e["candles"] for e in included if e["symbol"] == coin)))
        for coin, rows in coin_trade_rows.items()
    ]
    undercoverage_rows: list[dict[str, Any]] = []
    for item in analyzed_trades:
        if int(item["row"].get("undercoverage") or 0) <= 0:
            continue
        undercoverage_rows.extend(
            build_undercoverage_case_rows(
                coin=item["coin"],
                trade_number=item["row"]["trade_number"],
                status=item["row"]["status"],
                result=item["result"],
            )
        )

    baseline_summary_rows = [
        {
            "coins_included": len(included),
            "total_real_trades": len(all_trade_rows),
            "closed_trades": len(closed_rows),
            "blocker_trades": len(blocker_rows),
            "coins_with_blocker": sum(1 for row in coin_summaries if row.get("has_blocker")),
            "sum_closed_pnl": sum(safe_float(row["realized_pnl"]) for row in closed_rows),
            "series_mtm_total": sum(safe_float(row["mtm_pnl"]) for row in all_trade_rows),
            "total_undercoverage_rows": len(undercoverage_rows),
            "total_same_candle_violations": sum(
                int(row["same_candle_long_add_short_reduce"] or 0) for row in all_trade_rows
            ),
            "total_fees": sum(safe_float(row["fees"]) for row in all_trade_rows),
            "preferred_trades_target": preferred,
            "min_trades_target": min_trades,
        }
    ]

    # Write all Phase outputs.
    _write_csv(output_root / "continuous_trade_details.csv", all_trade_rows)
    _write_csv(output_root / "blocker_trades.csv", blocker_rows)
    _write_csv(output_root / "closed_trades.csv", closed_rows)
    _write_csv(output_root / "blocker_event_timeline.csv", fill_timeline_rows)
    _write_csv(output_root / "blocker_cycle_timeline.csv", cycle_timeline_rows)
    _write_csv(output_root / "blocker_exit_timeline.csv", exit_timeline_rows)
    _write_csv(output_root / "closed_vs_blocker_comparison.csv", comparison_rows)
    _write_csv(output_root / "blocker_feature_comparison.csv", all_stage_rows)
    _write_csv(output_root / "blocker_early_warning_candidates.csv", early_warning_rows)
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
            "trades_started",
            "reason",
        ],
    )
    _write_csv(output_root / "continuous_coin_summary.csv", coin_summaries)
    _write_csv(output_root / "baseline_summary.csv", baseline_summary_rows)
    _write_csv(output_root / "undercoverage_cases.csv", undercoverage_rows)
    _write_json(output_root / "hypotheses.json", hypotheses)

    live_after = resolve_backtest_config(config_source=CONFIG_SOURCE, signal="long", symbol=APT_SYMBOL)
    live_long_fill_distance_pct_after = float(live_after.config.long_fill_distance_pct)
    live_defaults_unchanged = live_long_fill_distance_pct_after == live_long_fill_distance_pct_before
    if not live_defaults_unchanged:
        raise RuntimeError("Live long_fill_distance_pct changed during run")

    manifest = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "git": _git_status(),
        "mode": "current_baseline_multicoin_continuous_blocker_audit",
        "fixed_params": {
            "direction": DIRECTION,
            "config_source": CONFIG_SOURCE,
            "fill_model": FILL_MODEL,
            "continuous_start_index": CONTINUOUS_START_INDEX,
            "long_fill_distance_pct": LONG_FILL_DISTANCE_PCT,
            "target_profit_usdt": TARGET_PROFIT_USDT,
            "tp_profit_target_pct": TP_PROFIT_TARGET_PCT,
            "candle_limit": FULL_HISTORY_CANDLE_LIMIT,
            "exit_rebuild_policy_config": None,
        },
        "baseline_params": baseline_params,
        "coins_discovered": len(coins_meta),
        "coins_included": len(included),
        "total_real_trades": len(all_trade_rows),
        "closed_trades": len(closed_rows),
        "blocker_trades": len(blocker_rows),
        "preferred_trades_target": preferred,
        "min_trades_target": min_trades,
        "max_coins": max_coins,
        "live_defaults_unchanged": {"long_fill_distance_pct": live_long_fill_distance_pct_after},
        "output_root": str(output_root),
    }
    _write_json(output_root / "run_manifest.json", manifest)

    write_report(
        output_root / "REPORT.md",
        baseline_params=baseline_params,
        coin_manifest_rows=coin_manifest_rows,
        coin_summaries=coin_summaries,
        total_trades=total_trades,
        preferred_trades=preferred,
        min_trades=min_trades,
        closed_rows=closed_rows,
        blocker_rows=blocker_rows,
        comparison_rows=comparison_rows,
        hypotheses=hypotheses,
        early_warning_rows=early_warning_rows,
        undercoverage_rows=undercoverage_rows,
        live_defaults_unchanged=live_defaults_unchanged,
    )

    return {
        "output_root": str(output_root),
        "coins_included": len(included),
        "total_real_trades": len(all_trade_rows),
        "closed_trades": len(closed_rows),
        "blocker_trades": len(blocker_rows),
        "coins_with_blocker": sum(1 for row in coin_summaries if row.get("has_blocker")),
        "preferred_trades_target": preferred,
        "min_trades_target": min_trades,
        "live_defaults_unchanged": live_defaults_unchanged,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--max-coins", type=int, default=DEFAULT_MAX_COINS)
    parser.add_argument("--min-trades", type=int, default=DEFAULT_MIN_TRADES)
    parser.add_argument("--preferred-trades", type=int, default=DEFAULT_PREFERRED_TRADES)
    args = parser.parse_args(argv)

    payload = run_pipeline(
        output_root=args.output_dir,
        max_coins=args.max_coins,
        min_trades=args.min_trades,
        preferred_trades=args.preferred_trades,
    )
    print(json.dumps(payload, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
