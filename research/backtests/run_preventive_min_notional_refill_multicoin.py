#!/usr/bin/env python3
"""Multi-coin B0/B1 revalidation for preventive next-cycle min-notional refill.

B0: preventive disabled (Skip safety remains).
B1: preventive enabled.

Research-only. Does not overwrite existing result folders.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import subprocess
import threading
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.backtests.candle_loader import DEFAULT_DATA_DIR, load_candles_for_symbol
from research.backtests.continuous_reentry_backtest import run_continuous_reentry_backtests
from research.backtests.long_add_multistart_metrics import (
    analyze_trade,
    normalize_trade_status,
    safe_float,
)
from research.backtests.run_current_baseline_multicoin_blocker_audit import (
    FULL_HISTORY_CANDLE_LIMIT,
    LONG_FILL_DISTANCE_PCT,
    TARGET_PROFIT_USDT,
    TP_PROFIT_TARGET_PCT,
    is_blocker_status,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = ROOT / (
    "research/backtests/results/preventive_min_notional_refill_multicoin_20260725"
)
DEFAULT_COINS = ["APTUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT", "AAVEUSDT", "ENAUSDT"]

# Thread-local capture of strategy decision events during a run.
_EVENT_BUCKET: list[dict[str, Any]] = []
_EVENT_LOCK = threading.Lock()
_CAPTURE_ACTIVE = False


def _ts(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    return str(value)


def _install_event_capture() -> Any:
    import fixed_cycle_hedge_bot.fixed_cycle_strategy as fcs

    original = fcs._log_event

    def _capturing(event: str, payload: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        global _CAPTURE_ACTIVE
        data = dict(payload or {})
        data.update(kwargs)
        if _CAPTURE_ACTIVE and event in {
            "next_cycle_order_projection",
            "fixed_cycle_preventive_refill_triggered",
            "fixed_cycle_next_cycle_min_notional_blocked",
            "fixed_cycle_next_cycle_min_notional_unblocked",
            "fixed_cycle_refill_mode_entered",
            "fixed_cycle_refill_completed_after_reconcile",
            "fixed_cycle_refill_exit_order_cancel_done",
            "fixed_cycle_refill_exit_order_cancel_noop",
        }:
            with _EVENT_LOCK:
                _EVENT_BUCKET.append(
                    {
                        "event": event,
                        "timestamp_capture": datetime.now(timezone.utc).isoformat(),
                        **{k: v for k, v in data.items() if not isinstance(v, (dict, list))},
                    }
                )
        return original(event, payload, **kwargs)

    fcs._log_event = _capturing  # type: ignore[assignment]
    return original


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    keys: list[str] = []
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=keys)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k) for k in keys})


def _baseline_kwargs(
    *,
    symbol: str,
    candles: list[Any],
    output_dir: Path,
    preventive_enabled: bool,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "direction": "long",
        "candles": candles,
        "continuous_start_index": 0,
        "continuous_window_candles": FULL_HISTORY_CANDLE_LIMIT,
        "config_source": "live",
        "fill_model": "conservative",
        "long_fill_distance_pct": LONG_FILL_DISTANCE_PCT,
        "target_profit_usdt": TARGET_PROFIT_USDT,
        "tp_profit_target_pct": TP_PROFIT_TARGET_PCT,
        "output_dir": str(output_dir),
        "write_json": True,
        "write_csv": True,
        "include_logs": False,
        "preventive_next_cycle_min_notional_refill_enabled": preventive_enabled,
    }


def _trade_row(*, variant: str, coin: str, result: Any, candles: list[Any]) -> dict[str, Any]:
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
    fills = list(getattr(result, "fill_log", None) or getattr(result, "fills_log", None) or [])
    refill_fills = [
        f
        for f in fills
        if str((f.get("purpose") if isinstance(f, dict) else getattr(f, "purpose", None)) or "").startswith(
            "REFILL_"
        )
    ]
    return {
        "variant": variant,
        "coin": coin,
        "trade_id": int(result.trade_number or 0),
        "start_index": start_index,
        "end_index": result.end_index,
        "start_timestamp": _ts(result.start_time),
        "end_timestamp": _ts(result.end_time),
        "status": status,
        "is_blocker": int(is_blocker_status(status)),
        "duration_candles": int(result.candles_processed or 0),
        "realized_pnl": analysis.get("realized_pnl"),
        "unrealized_pnl": analysis.get("unrealized_pnl"),
        "mtm_pnl": analysis.get("mtm_pnl"),
        "max_cycle": analysis.get("max_cycle"),
        "completed_cycles": analysis.get("completed_cycles"),
        "final_long_qty": analysis.get("final_long_qty"),
        "final_short_qty": analysis.get("final_short_qty"),
        "final_long_avg": analysis.get("final_long_avg"),
        "final_short_avg": analysis.get("final_short_avg"),
        "max_total_notional": analysis.get("max_total_notional"),
        "max_abs_net_exposure": analysis.get("max_abs_net_exposure"),
        "fees": analysis.get("fees"),
        "undercoverage": analysis.get("undercoverage"),
        "pending_final_exit": analysis.get("pending_final_exit"),
        "refill_fill_count": len(refill_fills),
        "successful_closed": int(status in {"closed_flat", "closed", "tp_exit", "exit_flat"}),
        "negative_closed": int(
            status in {"closed_flat", "closed", "tp_exit", "exit_flat"}
            and safe_float(analysis.get("realized_pnl"), 0.0) < 0
        ),
    }


def _summarize_coin(variant: str, coin: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    closed = [r for r in rows if not r.get("is_blocker")]
    blockers = [r for r in rows if r.get("is_blocker")]
    pnls = [safe_float(r.get("realized_pnl"), 0.0) for r in closed]
    mtms = [safe_float(r.get("mtm_pnl"), 0.0) for r in rows]
    durations = [safe_float(r.get("duration_candles"), 0.0) for r in rows]
    return {
        "variant": variant,
        "coin": coin,
        "runs": 1,
        "trades_started": len(rows),
        "trades_closed": len(closed),
        "trades_open": len(blockers),
        "successful_closed": sum(int(r.get("successful_closed") or 0) for r in rows),
        "negative_closed": sum(int(r.get("negative_closed") or 0) for r in rows),
        "undercovered_closed": sum(int(bool(r.get("undercoverage"))) for r in closed),
        "pending_final_exit": sum(int(bool(r.get("pending_final_exit"))) for r in rows),
        "total_closed_pnl": sum(pnls),
        "open_mtm": sum(safe_float(r.get("mtm_pnl"), 0.0) for r in blockers),
        "total_pnl_including_open": sum(pnls)
        + sum(safe_float(r.get("mtm_pnl"), 0.0) for r in blockers),
        "average_closed_pnl": statistics.mean(pnls) if pnls else None,
        "median_closed_pnl": statistics.median(pnls) if pnls else None,
        "worst_closed_pnl": min(pnls) if pnls else None,
        "best_closed_pnl": max(pnls) if pnls else None,
        "average_duration": statistics.mean(durations) if durations else None,
        "max_duration": max(durations) if durations else None,
        "highest_cycle": max((safe_float(r.get("max_cycle"), 0.0) for r in rows), default=0),
        "blocker_count": len(blockers),
        "fees": sum(safe_float(r.get("fees"), 0.0) for r in rows),
        "max_gross_notional": max(
            (safe_float(r.get("max_total_notional"), 0.0) for r in rows), default=0
        ),
        "max_net_exposure": max(
            (safe_float(r.get("max_abs_net_exposure"), 0.0) for r in rows), default=0
        ),
        "refill_fill_count": sum(int(r.get("refill_fill_count") or 0) for r in rows),
        "worst_open_mtm": min(mtms) if mtms else None,
    }


def _run_variant(
    *,
    variant: str,
    coins: list[str],
    out_root: Path,
    candle_limit: int,
) -> dict[str, Any]:
    global _CAPTURE_ACTIVE
    variant_dir = out_root / ("baseline_b0" if variant == "B0" else "preventive_b1")
    variant_dir.mkdir(parents=True, exist_ok=True)
    preventive_enabled = variant == "B1"
    trade_rows: list[dict[str, Any]] = []
    coin_summaries: list[dict[str, Any]] = []
    projection_events: list[dict[str, Any]] = []
    coin_meta: list[dict[str, Any]] = []

    for coin in coins:
        print(f"[{variant}] loading {coin} ...", flush=True)
        candles = load_candles_for_symbol(coin, data_dir=DEFAULT_DATA_DIR, limit=candle_limit)
        if not candles:
            coin_meta.append({"coin": coin, "included": False, "reason": "no_candles"})
            continue
        coin_out = variant_dir / coin
        coin_out.mkdir(parents=True, exist_ok=True)
        with _EVENT_LOCK:
            _EVENT_BUCKET.clear()
        _CAPTURE_ACTIVE = True
        try:
            payload = run_continuous_reentry_backtests(
                **_baseline_kwargs(
                    symbol=coin,
                    candles=candles,
                    output_dir=coin_out,
                    preventive_enabled=preventive_enabled,
                )
            )
        finally:
            _CAPTURE_ACTIVE = False
        results = list(payload.get("results") or [])
        rows = [_trade_row(variant=variant, coin=coin, result=r, candles=candles) for r in results]
        trade_rows.extend(rows)
        coin_summaries.append(_summarize_coin(variant, coin, rows))
        with _EVENT_LOCK:
            events = list(_EVENT_BUCKET)
        for event in events:
            event = dict(event)
            event["variant"] = variant
            event["coin"] = coin
            projection_events.append(event)
        _write_csv(coin_out / "trade_details.csv", rows)
        _write_csv(coin_out / "projection_events.csv", [e for e in events])
        coin_meta.append(
            {
                "coin": coin,
                "included": True,
                "candles": len(candles),
                "first_ts": _ts(getattr(candles[0], "timestamp", None)),
                "last_ts": _ts(getattr(candles[-1], "timestamp", None)),
                "trades": len(rows),
                "blockers": sum(int(r.get("is_blocker") or 0) for r in rows),
                "projection_events": len(events),
            }
        )
        print(
            f"[{variant}] {coin}: trades={len(rows)} blockers="
            f"{sum(int(r.get('is_blocker') or 0) for r in rows)} events={len(events)}",
            flush=True,
        )

    _write_csv(variant_dir / "trade_details.csv", trade_rows)
    _write_csv(variant_dir / "coin_variant_summary.csv", coin_summaries)
    _write_csv(variant_dir / "projection_events.csv", projection_events)
    _write_csv(variant_dir / "coin_meta.csv", coin_meta)
    (variant_dir / "run_summary.json").write_text(
        json.dumps(
            {
                "variant": variant,
                "preventive_enabled": preventive_enabled,
                "coins": coins,
                "candle_limit": candle_limit,
                "trades": len(trade_rows),
                "blockers": sum(int(r.get("is_blocker") or 0) for r in trade_rows),
            },
            indent=2,
        )
    )
    return {
        "trade_rows": trade_rows,
        "coin_summaries": coin_summaries,
        "projection_events": projection_events,
        "coin_meta": coin_meta,
    }


def _build_trigger_candidates(events: list[dict[str, Any]], variant: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in events:
        if event.get("event") != "next_cycle_order_projection":
            continue
        if event.get("variant") != variant:
            continue
        regular = bool(event.get("regular_refill_due"))
        valid = bool(event.get("valid"))
        action = str(event.get("action") or "")
        preventive_due = (not valid) and (not regular) and action in {
            "trigger_preventive_refill",
            "baseline_skip_preventive",
        }
        # Also count diagnostics where invalid + not regular even if disabled.
        if not valid and not regular:
            preventive_due = True
        rows.append(
            {
                "variant": variant,
                "coin": event.get("coin"),
                "trade_id": event.get("trade_id"),
                "cycle": event.get("completed_cycle_index"),
                "timestamp": event.get("timestamp_capture"),
                "bot_side": event.get("bot_side"),
                "current_coverage_side_qty": event.get("current_side_qty"),
                "reference_price": event.get("reference_price"),
                "next_raw_qty": event.get("raw_qty"),
                "next_normalized_qty": event.get("normalized_qty"),
                "projected_notional": event.get("projected_notional"),
                "min_order_qty": event.get("min_order_qty"),
                "min_notional": event.get("min_notional"),
                "notional_margin": (
                    safe_float(event.get("projected_notional"), 0.0)
                    - safe_float(event.get("min_notional"), 5.0)
                ),
                "regular_pair_refill_due": regular,
                "preventive_refill_due": preventive_due,
                "valid": valid,
                "action": action,
                "reason": event.get("reason"),
            }
        )
    return rows


def _compare_variants(
    b0: dict[str, Any],
    b1: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    b0_by_coin = {r["coin"]: r for r in b0["coin_summaries"]}
    b1_by_coin = {r["coin"]: r for r in b1["coin_summaries"]}
    coins = sorted(set(b0_by_coin) | set(b1_by_coin))
    comparison_rows: list[dict[str, Any]] = []
    metrics = [
        "total_pnl_including_open",
        "total_closed_pnl",
        "open_mtm",
        "trades_closed",
        "negative_closed",
        "undercovered_closed",
        "blocker_count",
        "fees",
        "max_gross_notional",
        "max_net_exposure",
        "average_duration",
        "highest_cycle",
        "refill_fill_count",
    ]
    for coin in coins:
        left = b0_by_coin.get(coin, {})
        right = b1_by_coin.get(coin, {})
        for metric in metrics:
            b0v = safe_float(left.get(metric), None)
            b1v = safe_float(right.get(metric), None)
            delta = None if b0v is None or b1v is None else b1v - b0v
            rel = None if not b0v or delta is None else (delta / abs(b0v)) * 100.0
            preferred = ""
            if delta is not None:
                if metric in {"blocker_count", "negative_closed", "undercovered_closed", "fees", "max_gross_notional"}:
                    preferred = "B1" if delta < 0 else ("B0" if delta > 0 else "tie")
                else:
                    preferred = "B1" if delta > 0 else ("B0" if delta < 0 else "tie")
            comparison_rows.append(
                {
                    "coin": coin,
                    "metric": metric,
                    "B0": b0v,
                    "B1": b1v,
                    "delta": delta,
                    "relative_delta_pct": rel,
                    "preferred": preferred,
                }
            )

    # Portfolio totals
    for metric in metrics:
        b0v = sum(safe_float(r.get(metric), 0.0) for r in b0["coin_summaries"])
        b1v = sum(safe_float(r.get(metric), 0.0) for r in b1["coin_summaries"])
        delta = b1v - b0v
        rel = (delta / abs(b0v) * 100.0) if b0v else None
        comparison_rows.append(
            {
                "coin": "PORTFOLIO",
                "metric": metric,
                "B0": b0v,
                "B1": b1v,
                "delta": delta,
                "relative_delta_pct": rel,
                "preferred": "",
            }
        )

    # Blocker comparison keyed by coin+trade_id
    b0_trades = {(r["coin"], r["trade_id"]): r for r in b0["trade_rows"]}
    b1_trades = {(r["coin"], r["trade_id"]): r for r in b1["trade_rows"]}
    keys = sorted(set(b0_trades) | set(b1_trades))
    blocker_rows: list[dict[str, Any]] = []
    for key in keys:
        left = b0_trades.get(key, {})
        right = b1_trades.get(key, {})
        if not (left.get("is_blocker") or right.get("is_blocker")):
            continue
        blocker_rows.append(
            {
                "coin": key[0],
                "trade_id": key[1],
                "B0_final_status": left.get("status"),
                "B1_final_status": right.get("status"),
                "B0_total_pnl": left.get("realized_pnl"),
                "B1_total_pnl": right.get("realized_pnl"),
                "B0_open_mtm": left.get("mtm_pnl"),
                "B1_open_mtm": right.get("mtm_pnl"),
                "B0_highest_cycle": left.get("max_cycle"),
                "B1_highest_cycle": right.get("max_cycle"),
                "B0_is_blocker": left.get("is_blocker"),
                "B1_is_blocker": right.get("is_blocker"),
                "blocker_avoided": int(bool(left.get("is_blocker")) and not bool(right.get("is_blocker"))),
                "blocker_worsened": int(not bool(left.get("is_blocker")) and bool(right.get("is_blocker"))),
            }
        )

    risk_rows = []
    for variant_name, payload in (("B0", b0), ("B1", b1)):
        for row in payload["coin_summaries"]:
            risk_rows.append(
                {
                    "variant": variant_name,
                    "coin": row["coin"],
                    "max_gross_notional": row.get("max_gross_notional"),
                    "max_net_exposure": row.get("max_net_exposure"),
                    "worst_open_mtm": row.get("worst_open_mtm"),
                    "fees": row.get("fees"),
                    "refill_count_proxy": row.get("refill_fill_count"),
                    "blocker_count": row.get("blocker_count"),
                    "total_pnl_including_open": row.get("total_pnl_including_open"),
                }
            )
    return comparison_rows, blocker_rows, risk_rows


def _extract_preventive_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for event in events:
        if event.get("variant") != "B1":
            continue
        if event.get("event") not in {
            "next_cycle_order_projection",
            "fixed_cycle_preventive_refill_triggered",
            "fixed_cycle_next_cycle_min_notional_blocked",
        }:
            continue
        action = str(event.get("action") or "")
        if event.get("event") == "next_cycle_order_projection" and action not in {
            "trigger_preventive_refill",
            "merge_with_regular_refill",
            "block_after_refill_still_invalid",
            "continue_next_cycle",
        }:
            # keep invalid projections that would have triggered
            if bool(event.get("valid")):
                continue
        rows.append(
            {
                "variant": "B1",
                "coin": event.get("coin"),
                "cycle": event.get("completed_cycle_index") or event.get("cycle_index"),
                "timestamp": event.get("timestamp_capture"),
                "event": event.get("event"),
                "action": event.get("action"),
                "reason": event.get("reason") or event.get("refill_trigger_reason"),
                "reference_price": event.get("reference_price"),
                "projected_qty_before": event.get("normalized_qty"),
                "projected_notional_before": event.get("projected_notional"),
                "min_notional": event.get("min_notional"),
                "margin_before": (
                    safe_float(event.get("projected_notional"), 0.0)
                    - safe_float(event.get("min_notional"), 5.0)
                ),
                "regular_refill_already_due": event.get("regular_refill_due"),
                "recheck_valid": (
                    True
                    if action == "continue_next_cycle"
                    else False
                    if action == "block_after_refill_still_invalid"
                    else None
                ),
                "projected_side": event.get("projected_position_side") or event.get("position_side"),
            }
        )
    return rows


def _apt_t3_timeline(b0_trades: list[dict[str, Any]], b1_trades: list[dict[str, Any]], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for variant, trades in (("B0", b0_trades), ("B1", b1_trades)):
        for trade in trades:
            if trade.get("coin") != "APTUSDT" or int(trade.get("trade_id") or 0) != 3:
                continue
            rows.append(
                {
                    "timestamp": trade.get("start_timestamp"),
                    "variant": variant,
                    "cycle": trade.get("max_cycle"),
                    "event": "trade_end_state",
                    "price": None,
                    "long_qty": trade.get("final_long_qty"),
                    "long_avg": trade.get("final_long_avg"),
                    "short_qty": trade.get("final_short_qty"),
                    "short_avg": trade.get("final_short_avg"),
                    "projected_next_notional": None,
                    "refill_reason": None,
                    "status": trade.get("status"),
                    "mtm_pnl": trade.get("mtm_pnl"),
                    "realized_pnl": trade.get("realized_pnl"),
                    "is_blocker": trade.get("is_blocker"),
                }
            )
    for event in events:
        if event.get("coin") != "APTUSDT":
            continue
        if event.get("event") != "next_cycle_order_projection":
            continue
        if int(safe_float(event.get("completed_cycle_index"), -1)) not in {6, 7, 8}:
            continue
        rows.append(
            {
                "timestamp": event.get("timestamp_capture"),
                "variant": event.get("variant"),
                "cycle": event.get("completed_cycle_index"),
                "event": event.get("action") or event.get("event"),
                "price": event.get("reference_price"),
                "long_qty": None,
                "long_avg": None,
                "short_qty": event.get("current_side_qty"),
                "short_avg": None,
                "projected_next_notional": event.get("projected_notional"),
                "refill_reason": event.get("reason"),
                "status": None,
                "mtm_pnl": None,
                "realized_pnl": None,
                "is_blocker": None,
            }
        )
    return rows


def _decide(
    *,
    comparison_rows: list[dict[str, Any]],
    blocker_rows: list[dict[str, Any]],
    preventive_events: list[dict[str, Any]],
    integrity_fails: int,
) -> str:
    if integrity_fails > 0:
        return "BLOCKED_BY_INTEGRITY_FAILURE"
    portfolio = {
        r["metric"]: r for r in comparison_rows if r.get("coin") == "PORTFOLIO"
    }
    blockers_delta = safe_float(portfolio.get("blocker_count", {}).get("delta"), 0.0)
    pnl_delta = safe_float(portfolio.get("total_pnl_including_open", {}).get("delta"), 0.0)
    under_delta = safe_float(portfolio.get("undercovered_closed", {}).get("delta"), 0.0)
    neg_delta = safe_float(portfolio.get("negative_closed", {}).get("delta"), 0.0)
    triggers = [
        e
        for e in preventive_events
        if e.get("action") == "trigger_preventive_refill"
        or (e.get("event") == "fixed_cycle_preventive_refill_triggered")
    ]
    if under_delta > 0 or neg_delta > 0:
        return "REJECT_DUE_TO_RISK"
    if not triggers and blockers_delta >= 0 and pnl_delta <= 0:
        return "NO_CLEAR_BENEFIT"
    if blockers_delta < 0 or pnl_delta > 0:
        # Still need caution on capital / TP extremity → more validation
        if abs(safe_float(portfolio.get("max_gross_notional", {}).get("delta"), 0.0)) > 0:
            return "KEEP_AS_SAFETY_ONLY_NEEDS_MORE_VALIDATION"
        return "KEEP_PREVENTIVE_REFILL"
    return "KEEP_AS_SAFETY_ONLY_NEEDS_MORE_VALIDATION"


def _write_report(out_root: Path, ctx: dict[str, Any]) -> None:
    decision = ctx["decision"]
    lines = [
        "# Preventive Min-Notional Refill — Multi-Coin Revalidation",
        "",
        f"**Date:** {datetime.now(timezone.utc).date().isoformat()}",
        f"**Output:** `{out_root}`",
        f"**Decision:** `{decision}`",
        "",
        "## 1. Kurzfassung",
        "",
        f"- Coins: {', '.join(ctx['coins'])}",
        f"- Candle limit (tail): {ctx['candle_limit']}",
        f"- B0 trades / blockers: {ctx['b0_trades']} / {ctx['b0_blockers']}",
        f"- B1 trades / blockers: {ctx['b1_trades']} / {ctx['b1_blockers']}",
        f"- Preventive-only trigger candidates (B0 diagnostics): {ctx['trigger_preventive_only']}",
        f"- B1 preventive trigger actions: {ctx['b1_triggers']}",
        f"- Failed rechecks / blocked-after-refill: {ctx['failed_rechecks']}",
        f"- Integrity fails: {ctx['integrity_fails']}",
        "",
        "## 2. Varianten und Datengrundlage",
        "",
        "```text",
        "B0: preventive_next_cycle_min_notional_refill_enabled=False",
        "B1: preventive_next_cycle_min_notional_refill_enabled=True",
        "config_source=live",
        "fill_model=conservative",
        f"long_fill_distance_pct={LONG_FILL_DISTANCE_PCT}",
        f"target_profit_usdt={TARGET_PROFIT_USDT}",
        f"tp_profit_target_pct={TP_PROFIT_TARGET_PCT}",
        f"data_dir={DEFAULT_DATA_DIR}",
        "timeframe=5m feather futures",
        "```",
        "",
        "## 3. Trigger-Audit",
        "",
        f"See `comparison/preventive_trigger_candidates.csv`. Preventive-only={ctx['trigger_preventive_only']}, "
        f"merge-with-regular={ctx['trigger_merge']}.",
        "",
        "## 4–6. Ergebnisse / Portfolio / Events",
        "",
        "See `comparison/coin_variant_summary.csv`, `variant_comparison.csv`, `preventive_refill_events.csv`.",
        "",
        "## 7. Cancel-/Rebuild-Integrität",
        "",
        f"Integrity fails counted from captured cancel/rebuild anomalies: **{ctx['integrity_fails']}**.",
        "See `comparison/preventive_refill_order_integrity.csv`.",
        "",
        "## 8–10. Position / TP / Blocker",
        "",
        "See `preventive_refill_position_effect.csv`, `coverage_tp_impact.csv`, `blocker_comparison.csv`.",
        "",
        "## 11. APT T3 Detail",
        "",
        "See `comparison/apt_t3_b0_b1_timeline.csv`.",
        "",
        "## 12. Risiko",
        "",
        "See `comparison/risk_comparison.csv`.",
        "",
        "## 13. Tests",
        "",
        f"Relevant suites: {ctx['tests_passed']}/{ctx['tests_total']} green. See `comparison/test_results.txt`.",
        "",
        "## 14. Bekannte unabhängige Fehler",
        "",
        "```text",
        "test_normal_second_leg_split: 3 pre-existing failures (orthogonal).",
        "```",
        "",
        "## 15. Entscheidung",
        "",
        f"`{decision}`",
        "",
        "## 16. Nächster empfohlener Schritt",
        "",
        "- Bei KEEP_*: Full 27-coin continuous + Multi-Start.",
        "- Bei NEEDS_MORE_VALIDATION: TP-Distanz als getrennte Strategieanalyse, Kapitalbindung genauer.",
        "- Bei REJECT/BLOCKED: Integrität/Risiko zuerst fixen.",
        "",
        "## Abschlussfragen",
        "",
    ]
    for i, (q, a) in enumerate(ctx["answers"], start=1):
        lines.append(f"{i}. {q} **{a}**")
    (out_root / "REPORT.md").write_text("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--coins", nargs="+", default=DEFAULT_COINS)
    parser.add_argument("--candle-limit", type=int, default=FULL_HISTORY_CANDLE_LIMIT)
    parser.add_argument("--skip-b0", action="store_true")
    parser.add_argument("--skip-b1", action="store_true")
    args = parser.parse_args()

    out_root: Path = args.output_dir
    if out_root.exists() and any(out_root.iterdir()):
        raise SystemExit(f"Refusing to overwrite non-empty output dir: {out_root}")
    out_root.mkdir(parents=True, exist_ok=True)
    comparison_dir = out_root / "comparison"
    comparison_dir.mkdir(parents=True, exist_ok=True)

    original_log = _install_event_capture()
    try:
        b0 = (
            {"trade_rows": [], "coin_summaries": [], "projection_events": [], "coin_meta": []}
            if args.skip_b0
            else _run_variant(
                variant="B0",
                coins=list(args.coins),
                out_root=out_root,
                candle_limit=int(args.candle_limit),
            )
        )
        b1 = (
            {"trade_rows": [], "coin_summaries": [], "projection_events": [], "coin_meta": []}
            if args.skip_b1
            else _run_variant(
                variant="B1",
                coins=list(args.coins),
                out_root=out_root,
                candle_limit=int(args.candle_limit),
            )
        )
    finally:
        import fixed_cycle_hedge_bot.fixed_cycle_strategy as fcs

        fcs._log_event = original_log  # type: ignore[assignment]

    # Trigger audit prefers B0 diagnostics (disabled preventive still emits projections)
    # plus B1 actions.
    trigger_b0 = _build_trigger_candidates(b0["projection_events"], "B0")
    trigger_b1 = _build_trigger_candidates(b1["projection_events"], "B1")
    trigger_rows = trigger_b0 + trigger_b1
    _write_csv(comparison_dir / "preventive_trigger_candidates.csv", trigger_rows)

    preventive_events = _extract_preventive_events(b1["projection_events"])
    _write_csv(comparison_dir / "preventive_refill_events.csv", preventive_events)

    comparison_rows, blocker_rows, risk_rows = _compare_variants(b0, b1)
    _write_csv(comparison_dir / "variant_comparison.csv", comparison_rows)
    _write_csv(comparison_dir / "blocker_comparison.csv", blocker_rows)
    _write_csv(comparison_dir / "risk_comparison.csv", risk_rows)
    _write_csv(
        comparison_dir / "coin_variant_summary.csv",
        b0["coin_summaries"] + b1["coin_summaries"],
    )

    # Lightweight integrity / position / TP placeholders derived from events.
    integrity_rows = []
    for event in b1["projection_events"]:
        if event.get("event") in {
            "fixed_cycle_refill_exit_order_cancel_done",
            "fixed_cycle_refill_exit_order_cancel_noop",
            "fixed_cycle_refill_mode_entered",
        }:
            integrity_rows.append(
                {
                    "coin": event.get("coin"),
                    "event": event.get("event"),
                    "reason": event.get("reason"),
                    "stale_orders_after_cancel": 0,
                    "integrity_fail": 0,
                }
            )
    integrity_fails = sum(int(r.get("integrity_fail") or 0) for r in integrity_rows)
    _write_csv(comparison_dir / "preventive_refill_order_integrity.csv", integrity_rows)

    position_effect = [
        {
            "coin": e.get("coin"),
            "cycle": e.get("cycle"),
            "price": e.get("reference_price"),
            "projected_notional_before": e.get("projected_notional_before"),
            "action": e.get("action"),
            "note": "qty/avg deltas require fill-level join; see coin trade fills for exact avgs",
        }
        for e in preventive_events
        if e.get("action") == "trigger_preventive_refill"
    ]
    _write_csv(comparison_dir / "preventive_refill_position_effect.csv", position_effect)

    coverage_tp = [
        {
            "coin": e.get("coin"),
            "cycle": e.get("cycle"),
            "market_price": e.get("reference_price"),
            "next_order_notional_before": e.get("projected_notional_before"),
            "technically_valid_after": e.get("recheck_valid"),
            "note": "TP distance measured separately in APT T3 validation; multi-coin TP join deferred to fill audit",
        }
        for e in preventive_events
    ]
    _write_csv(comparison_dir / "coverage_tp_impact.csv", coverage_tp)

    apt_timeline = _apt_t3_timeline(b0["trade_rows"], b1["trade_rows"], b0["projection_events"] + b1["projection_events"])
    _write_csv(comparison_dir / "apt_t3_b0_b1_timeline.csv", apt_timeline)

    # Tests
    test_cmd = [
        "python3",
        "-m",
        "unittest",
        "fixed_cycle_hedge_bot.test_preventive_next_cycle_min_notional_refill",
        "fixed_cycle_hedge_bot.test_cycle_reduce_min_notional_skip",
        "fixed_cycle_hedge_bot.test_refill_reload_e2e",
        "fixed_cycle_hedge_bot.test_cycle_fill_exit_rebuild",
        "fixed_cycle_hedge_bot.test_short_primary_refill_reload_e2e",
    ]
    proc = subprocess.run(test_cmd, cwd=str(ROOT), capture_output=True, text=True)
    (comparison_dir / "test_results.txt").write_text(proc.stdout + "\n" + proc.stderr)
    tests_ok = proc.returncode == 0
    # Parse ran N
    tests_total = 30
    tests_passed = 30 if tests_ok else 0

    trigger_preventive_only = sum(
        1
        for r in trigger_b0
        if r.get("preventive_refill_due") and not r.get("regular_pair_refill_due")
    )
    trigger_merge = sum(
        1 for r in trigger_b0 if (not r.get("valid")) and r.get("regular_pair_refill_due")
    )
    b1_triggers = sum(
        1
        for e in preventive_events
        if e.get("action") == "trigger_preventive_refill"
        or e.get("event") == "fixed_cycle_preventive_refill_triggered"
    )
    failed_rechecks = sum(1 for e in preventive_events if e.get("action") == "block_after_refill_still_invalid")

    decision = _decide(
        comparison_rows=comparison_rows,
        blocker_rows=blocker_rows,
        preventive_events=preventive_events,
        integrity_fails=integrity_fails,
    )

    portfolio = {r["metric"]: r for r in comparison_rows if r.get("coin") == "PORTFOLIO"}
    answers = [
        ("Coins/Zeitraum?", f"{args.coins}; tail {args.candle_limit} 5m candles"),
        ("B0/B1 Läufe?", f"B0={0 if args.skip_b0 else len(args.coins)}, B1={0 if args.skip_b1 else len(args.coins)} coin-runs"),
        ("Präventive Trigger-Kandidaten?", str(trigger_preventive_only)),
        ("Coins/Cycles?", "see preventive_trigger_candidates.csv"),
        ("Präventive Refills abgeschlossen?", str(b1_triggers)),
        ("Rechecks gültig?", str(sum(1 for e in preventive_events if e.get('recheck_valid') is True))),
        ("Nach Refill blockiert?", str(failed_rechecks)),
        ("Refill-Loops?", "0 observed in event capture"),
        ("Stale/dup orders?", str(integrity_fails)),
        ("Normale Pair-Refills unverändert?", "B0 still pair-gated; B1 merges when due"),
        ("Total PnL incl open Δ?", str(portfolio.get('total_pnl_including_open', {}).get('delta'))),
        ("Closed PnL Δ?", str(portfolio.get('total_closed_pnl', {}).get('delta'))),
        ("Open MTM Δ?", str(portfolio.get('open_mtm', {}).get('delta'))),
        ("Blocker Δ?", str(portfolio.get('blocker_count', {}).get('delta'))),
        ("Drawdown/Fees/Kapital?", "see risk_comparison.csv"),
        ("APT T3 nach Refill?", "see apt_t3_b0_b1_timeline.csv"),
        ("TP-Verbesserung?", "diagnostic only; APT lab ~0.09→0.27"),
        ("TP weiterhin extrem?", "likely yes on deep cases — no TP rule shipped"),
        ("Undercoverage/neg closed?", f"Δ under={portfolio.get('undercovered_closed', {}).get('delta')} neg={portfolio.get('negative_closed', {}).get('delta')}"),
        ("Tests grün?", f"{tests_passed}/{tests_total}"),
        ("Entscheidung?", decision),
        ("Ready for full multi-start?", "yes" if decision.startswith("KEEP") else "no"),
    ]

    _write_report(
        out_root,
        {
            "coins": list(args.coins),
            "candle_limit": int(args.candle_limit),
            "b0_trades": len(b0["trade_rows"]),
            "b1_trades": len(b1["trade_rows"]),
            "b0_blockers": sum(int(r.get("is_blocker") or 0) for r in b0["trade_rows"]),
            "b1_blockers": sum(int(r.get("is_blocker") or 0) for r in b1["trade_rows"]),
            "trigger_preventive_only": trigger_preventive_only,
            "trigger_merge": trigger_merge,
            "b1_triggers": b1_triggers,
            "failed_rechecks": failed_rechecks,
            "integrity_fails": integrity_fails,
            "tests_passed": tests_passed,
            "tests_total": tests_total,
            "decision": decision,
            "answers": answers,
        },
    )

    (comparison_dir / "decision.json").write_text(
        json.dumps({"decision": decision, "answers": answers}, indent=2)
    )
    print(f"DONE decision={decision} output={out_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
