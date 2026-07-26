"""Multi-blocker forensic Cobertura audit: T1@6% policy across ready historical blockers.

Research-only. No live/runtime integration. Variants are isolated full replays.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from research.backtests.candle_loader import DEFAULT_DATA_DIR, load_candles_for_symbol
from research.backtests.emergency_lock.cost_model import fee_usdt
from research.backtests.multicoin_price_staging_grid import (
    atomic_write_json,
    atomic_write_text,
    write_csv,
)

from .config import CoberturaConfig
from .engine import EngineResult, _parse_ts
from .ledger import round_qty
from .multi_blocker_variants import (
    ALL_VARIANTS,
    APT_REGRESSION,
    APT_TRADE_ID,
    DEFAULT_HORIZONS_DAYS,
    VARIANT_BASELINE,
    parse_variants,
    variant_engine_flags,
)
from .order_audit import QTY_TOL, reconstruct_audit
from .run_apt_start_and_post_add_distance_audit import STRATEGY, neutralize_at_price
from .run_apt_start_distance_execution_timing_audit import build_cfg
from .runner import run_cobertura
from .start_distance import select_start_by_timing_mode

DEFAULT_FILL_REPLAY_DIR = Path(
    "research/backtests/cobertura_0_notional_strategie/results/"
    "historical_blocker_fill_replay_20260726"
)
DEFAULT_STATE_DIR = Path(
    "research/backtests/cobertura_0_notional_strategie/results/"
    "historical_blocker_states_20260726"
)
DEFAULT_OUTPUT_DIR = Path(
    "research/backtests/cobertura_0_notional_strategie/results/"
    "multi_blocker_forensic_audit_20260726"
)

POLICY_ID = "shared_be_t1_6pct"
START_DISTANCE_PCT = 0.06
BARS_PER_DAY = 24 * 12  # 5m
PNL_TOL = 1e-3
ABS_TOL = 1e-6


def _read_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _f(x: Any, default: float = 0.0) -> float:
    if x is None or x == "":
        return float(default)
    return float(x)


def _truthy(x: Any) -> bool:
    return str(x).strip().lower() in ("1", "true", "yes", "y")


def _safe_trade_id(trade_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", trade_id)


def _approx(a: Any, b: Any, rel: float = PNL_TOL, abs_tol: float = ABS_TOL) -> bool:
    try:
        aa, bb = float(a), float(b)
    except (TypeError, ValueError):
        return str(a) == str(b)
    return abs(aa - bb) <= max(abs_tol, rel * max(abs(aa), abs(bb), 1e-12))


def load_case_universe(
    *,
    fill_replay_dir: Path,
    state_dir: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (selected, unresolved) case rows. No estimation."""
    pre = _read_csv(fill_replay_dir / "blocker_pre_signal_states.csv")
    unresolved_src = _read_csv(fill_replay_dir / "unresolved_replays.csv")
    states = {
        r.get("trade_id"): r
        for r in _read_csv(state_dir / "historical_blocker_states.csv")
    }

    selected: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []

    seen_unresolved = {r.get("trade_id") for r in unresolved_src}
    for row in unresolved_src:
        reason = str(row.get("state_quality_flags") or row.get("reason") or "UNRESOLVED_REPLAY")
        status = "STATE_UNRESOLVED"
        if "BREAK_EVENT_UNRESOLVED" in reason:
            status = "BREAK_EVENT_UNRESOLVED"
        elif "ENTRY_BLOCKED" in reason:
            status = "ENTRY_BLOCKED"
        elif "POSITION_UNRESOLVED" in reason:
            status = "POSITION_UNRESOLVED"
        elif "CANDLE_UNRESOLVED" in reason:
            status = "CANDLE_UNRESOLVED"
        elif "REPLAY" in reason.upper() and "MISMATCH" in reason.upper():
            status = "REPLAY_MISMATCH"
        trade_id = str(row.get("trade_id") or "")
        coin = row.get("coin") or (trade_id.split("|")[0] if "|" in trade_id else None)
        unresolved.append(
            {
                "trade_id": trade_id,
                "coin": coin,
                "status": status,
                "reason": reason,
                "ready_for_neutralization": False,
                "replay_match_status": row.get("replay_match_status"),
            }
        )

    for row in pre:
        trade_id = str(row.get("trade_id") or "")
        coin = str(row.get("coin") or "")
        flags = str(row.get("state_quality_flags") or "")
        replay = str(row.get("replay_match_status") or "")
        ready = _truthy(row.get("ready_for_neutralization"))
        base = {
            **row,
            "state_break_ready": _truthy(
                (states.get(trade_id) or {}).get("ready_for_cobertura")
            ),
            "structure_break_level": (states.get(trade_id) or {}).get(
                "structure_break_level"
            ),
        }
        if trade_id in seen_unresolved:
            continue
        if not ready:
            reason = "STATE_UNRESOLVED"
            if "BREAK_EVENT_UNRESOLVED" in flags:
                reason = "BREAK_EVENT_UNRESOLVED"
            elif "ENTRY_BLOCKED" in flags:
                reason = "ENTRY_BLOCKED"
            elif "POSITION_UNRESOLVED" in flags:
                reason = "POSITION_UNRESOLVED"
            elif "CANDLE_UNRESOLVED" in flags:
                reason = "CANDLE_UNRESOLVED"
            elif replay and replay != "REPLAY_MATCH":
                reason = "REPLAY_MISMATCH"
            unresolved.append(
                {
                    "trade_id": trade_id,
                    "coin": coin,
                    "status": reason,
                    "reason": flags or replay or "not_ready",
                    "ready_for_neutralization": False,
                    "replay_match_status": replay,
                }
            )
            continue
        if replay != "REPLAY_MATCH":
            unresolved.append(
                {
                    "trade_id": trade_id,
                    "coin": coin,
                    "status": "REPLAY_MISMATCH",
                    "reason": replay,
                    "ready_for_neutralization": ready,
                    "replay_match_status": replay,
                }
            )
            continue
        # Exact pre-signal book required
        if any(
            row.get(k) in (None, "")
            for k in (
                "long_qty_before",
                "long_avg_before",
                "short_qty_before",
                "short_avg_before",
                "signal_available_ts",
            )
        ):
            unresolved.append(
                {
                    "trade_id": trade_id,
                    "coin": coin,
                    "status": "POSITION_UNRESOLVED",
                    "reason": "missing_pre_signal_book_fields",
                    "ready_for_neutralization": ready,
                    "replay_match_status": replay,
                }
            )
            continue
        selected.append(base)
    return selected, unresolved


def book_from_pre_signal(row: dict[str, Any]) -> dict[str, Any]:
    long_q = _f(row["long_qty_before"])
    short_q = _f(row["short_qty_before"])
    return {
        "long_qty": long_q,
        "long_avg": _f(row["long_avg_before"]),
        "short_qty": short_q,
        "short_avg": _f(row["short_avg_before"]),
        "neutralization_qty": max(0.0, long_q - short_q),
        "signal_available_ts": str(row["signal_available_ts"]).replace(" ", "T"),
        "structure_break_level": _f(row.get("structure_break_level"), 0.0) or None,
    }


def truncate_candles(
    candles: list[dict[str, Any]], *, start_ts: str, horizon_days: int
) -> list[dict[str, Any]]:
    start = _parse_ts(start_ts)
    end = start + timedelta(days=int(horizon_days))
    out: list[dict[str, Any]] = []
    for c in candles:
        ts = _parse_ts(c["timestamp"])
        if ts < start:
            continue
        if ts > end:
            break
        out.append(c)
    return out


def classify_status(
    *,
    result: EngineResult,
    recovery_ts: str | None,
    start_ts: str,
    horizon_days: int,
    invariant_fail: bool,
) -> str:
    if invariant_fail:
        return "INVARIANT_FAIL"
    if result.state == "STOPPED" and result.exit_reason in (
        "max_overlay_qty_multiple",
        "max_total_gross_notional",
        "max_net_notional",
    ):
        return "CAPITAL_LIMIT_EXCEEDED"
    if result.state in ("RECOVERED", "RECOVERED_BE"):
        last = result.total_exit_economics_timeline[-1] if result.total_exit_economics_timeline else {}
        econ = _f(last.get("total_exit_economics"))
        return "RECOVERED_PROFIT" if econ >= 0 else "RECOVERED_LOSS"
    # Still open at end of truncated horizon window
    if int(horizon_days) <= 30:
        return "OPEN_AT_30D"
    if int(horizon_days) <= 60:
        return "OPEN_AT_60D"
    if int(horizon_days) <= 90:
        return "OPEN_AT_90D"
    return "OPEN_AT_120D"


def recovery_timestamp(result: EngineResult) -> str | None:
    if result.state not in ("RECOVERED", "RECOVERED_BE"):
        return None
    for row in reversed(result.per_bar_trace):
        if row.get("state") in ("RECOVERED", "RECOVERED_BE"):
            return str(row.get("timestamp"))
    return None


def days_between(a: str, b: str) -> float:
    return (_parse_ts(b) - _parse_ts(a)).total_seconds() / 86400.0


def same_candle_stats(result: EngineResult) -> dict[str, Any]:
    by_ts: dict[str, list[dict[str, Any]]] = {}
    for f in result.fills_events:
        by_ts.setdefault(str(f.get("timestamp")), []).append(f)
    multi_add = 0
    add_be = 0
    add_exit = 0
    gt3 = 0
    max_adds = 0
    exit_ts = None
    for ts, group in by_ts.items():
        kinds = [g.get("kind") for g in group]
        n_add = sum(1 for k in kinds if k == "overlay_short_add")
        n_be = sum(1 for k in kinds if k == "overlay_be_close")
        n_fe = sum(1 for k in kinds if k == "full_exit")
        max_adds = max(max_adds, n_add)
        if n_add > 1:
            multi_add += 1
        if n_add and n_be:
            add_be += 1
        if n_add and n_fe:
            add_exit += 1
            exit_ts = ts
        if len(group) > 3:
            gt3 += 1
    exit_pnl_share = None
    hyp = {
        "economics_before_final_candle_adds": None,
        "first_full_exit_gate_after_add_index": None,
        "would_exit_without_final_candle_adds": None,
    }
    if result.state in ("RECOVERED", "RECOVERED_BE") and exit_ts:
        # PnL share of overlay realized on exit candle vs total overlay
        overlay_total = float(result.ledger.realized_overlay_pnl)
        exit_delta = sum(
            _f(f.get("realized_pnl_delta"))
            for f in by_ts.get(exit_ts, [])
            if f.get("kind") in ("overlay_be_close", "full_exit", "overlay_short_add")
        )
        exit_pnl_share = (
            exit_delta / overlay_total if abs(overlay_total) > 1e-12 else None
        )
        # Hypothetical: economics on exit candle before adds from per-bar + fills order
        adds_on_exit = [f for f in by_ts[exit_ts] if f.get("kind") == "overlay_short_add"]
        hyp["final_candle_add_count"] = len(adds_on_exit)
        hyp["would_exit_without_final_candle_adds"] = len(adds_on_exit) == 0
    return {
        "candles_multi_add": multi_add,
        "candles_add_and_be": add_be,
        "candles_add_and_full_exit": add_exit,
        "candles_gt3_fills": gt3,
        "max_adds_on_one_candle": max_adds,
        "exit_candle_pnl_share": exit_pnl_share,
        "exit_candle_timestamp": exit_ts,
        **hyp,
    }


def capital_metrics(
    *,
    result: EngineResult,
    book_before: dict[str, Any],
    neut: dict[str, Any],
    start_price: float,
) -> dict[str, Any]:
    long_n = _f(book_before["long_qty"]) * _f(book_before["long_avg"])
    short_n = _f(book_before["short_qty"]) * _f(book_before["short_avg"])
    neut_n = _f(neut.get("neutralization_qty") or book_before.get("neutralization_qty")) * float(
        start_price
    )
    cum_add_n = 0.0
    peak_ov_n = 0.0
    peak_gross = 0.0
    min_px = float("inf")
    max_round = 0
    equity_path: list[float] = []
    start_econ = None
    for row in result.per_bar_trace:
        px = _f(row.get("close"), start_price)
        min_px = min(min_px, px, _f(row.get("low"), px))
        max_round = max(max_round, int(row.get("recovery_round") or 0))
        ov_q = _f(row.get("overlay_short_qty"))
        peak_ov_n = max(peak_ov_n, ov_q * px)
        peak_gross = max(peak_gross, _f(row.get("gross_notional")))
        econ = _f(row.get("total_exit_economics"))
        if start_econ is None:
            start_econ = econ
        equity_path.append(econ)
    for f in result.fills_events:
        if f.get("kind") == "overlay_short_add":
            cum_add_n += _f(f.get("qty")) * _f(f.get("fill_price"))
    max_adverse = min(equity_path) if equity_path else 0.0
    max_dd = (start_econ - max_adverse) if start_econ is not None else 0.0
    scale = 100.0 / long_n if long_n > 1e-12 else float("nan")
    return {
        "initial_long_notional": long_n,
        "initial_short_notional": short_n,
        "neutralization_notional": neut_n,
        "cumulative_overlay_add_notional": cum_add_n,
        "maximum_simultaneous_overlay_notional": peak_ov_n,
        "maximum_total_gross_exposure": peak_gross,
        "maximum_margin_proxy": peak_gross * 0.5,
        "max_adverse_equity": max_adverse,
        "max_drawdown_from_cobertura_start": max_dd,
        "lowest_market_price": (min_px if min_px < float("inf") else None),
        "highest_cycle_round": max_round,
        "pnl_per_100_initial_long_scale": scale,
        "max_gross_exposure_per_100_initial_long": peak_gross * scale
        if long_n > 1e-12
        else None,
        "max_drawdown_per_100_initial_long": max_dd * scale if long_n > 1e-12 else None,
        "neutralization_notional_over_initial_long": neut_n / long_n
        if long_n > 1e-12
        else None,
        "peak_overlay_notional_over_initial_long": peak_ov_n / long_n
        if long_n > 1e-12
        else None,
    }


def pnl_layers(
    *,
    result: EngineResult,
    prior_realized: float | None,
    prior_open_mtm: float | None,
    prior_fees: float | None,
    neut_fee: float,
) -> dict[str, Any]:
    overlay_gross = float(result.ledger.realized_overlay_pnl)
    entry_fees = float(result.ledger.cumulative_entry_fees)
    close_fees = float(result.ledger.cumulative_close_fees)
    # Approximate overlay close fees as sum of overlay_be / overlay portion of full_exit
    overlay_close = 0.0
    for f in result.fills_events:
        if f.get("kind") == "overlay_be_close":
            overlay_close += _f(f.get("fee"))
        elif f.get("kind") == "full_exit" and f.get("leg") == "overlay":
            overlay_close += _f(f.get("fee"))
    last = (
        result.total_exit_economics_timeline[-1]
        if result.total_exit_economics_timeline
        else {}
    )
    engine_b = _f(last.get("total_exit_economics"))
    # B includes neutralization fee as Cobertura cost
    b = engine_b - float(neut_fee)
    c = float(prior_realized) if prior_realized is not None else None
    fee_quality = (
        "FEE_RECONSTRUCTION_UNRESOLVED"
        if prior_fees is None or prior_fees == ""
        else "FEE_RECONSTRUCTION_OK"
    )
    unresolved_fee = None if fee_quality.endswith("OK") else True
    d = (b + c) if c is not None else None
    return {
        "A_realized_overlay_gross": overlay_gross,
        "A_overlay_entry_fees": entry_fees,
        "A_overlay_close_fees": overlay_close,
        "A_realized_overlay_net": overlay_gross - entry_fees - overlay_close,
        "B_engine_total_exit_economics": engine_b,
        "B_neutralization_fee": float(neut_fee),
        "B_cobertura_total_including_neut_fee": b,
        "C_prior_tem_realized": c,
        "C_prior_tem_open_mtm_at_start": prior_open_mtm,
        "C_prior_tem_fees": prior_fees,
        "C_quality": fee_quality,
        "D_combined": d,
        "combined_before_unresolved_fees": d,
        "combined_after_known_fees": d,
        "unresolved_fee_amount": None,
        "combined_quality": (
            "PASS_WITH_UNRESOLVED_PRIOR_FEES"
            if fee_quality != "FEE_RECONSTRUCTION_OK"
            else "PASS"
        ),
    }


def build_order_fill_rows(
    *,
    trade_id: str,
    coin: str,
    variant: str,
    result: EngineResult,
    bundle,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    orders: list[dict[str, Any]] = []
    fills: list[dict[str, Any]] = []
    pos: list[dict[str, Any]] = []
    candle_by_ts = {
        str(r.get("timestamp")): r for r in result.per_bar_trace
    }
    for i, ev in enumerate(result.order_events):
        ts = str(ev.get("timestamp"))
        bar = candle_by_ts.get(ts, {})
        orders.append(
            {
                "trade_id": trade_id,
                "coin": coin,
                "variant": variant,
                "global_event_index": i,
                "timestamp": ts,
                "bar_index": bar.get("candle_index")
                if "candle_index" in bar
                else None,
                "open": bar.get("open"),
                "high": bar.get("high"),
                "low": bar.get("low"),
                "close": bar.get("close"),
                "order_id": f"{variant}-OE{i}",
                "purpose": ev.get("event"),
                "side": ev.get("side"),
                "trigger_price": ev.get("trigger"),
                "qty": ev.get("qty"),
                "payload": json.dumps(ev, sort_keys=True, default=str),
                "causal_ok": True,
                "warning_flags": "",
            }
        )
    ledger = list(bundle.fill_ledger)
    for i, f in enumerate(result.fills_events):
        ts = str(f.get("timestamp"))
        bar = candle_by_ts.get(ts, {})
        led = ledger[i] if i < len(ledger) else {}
        fills.append(
            {
                "trade_id": trade_id,
                "coin": coin,
                "variant": variant,
                "global_fill_index": i,
                "timestamp": ts,
                "bar_index": led.get("bar_index"),
                "open": bar.get("open"),
                "high": bar.get("high"),
                "low": bar.get("low"),
                "close": bar.get("close"),
                "order_id": led.get("order_id"),
                "purpose": led.get("purpose") or f.get("kind"),
                "side": f.get("side") or led.get("side"),
                "trigger_price": f.get("trigger"),
                "fill_price": f.get("fill_price"),
                "qty": f.get("qty"),
                "fee": f.get("fee"),
                "realized_pnl_delta": f.get("realized_pnl_delta")
                or led.get("gross_realized_pnl"),
                "long_qty_after": led.get("core_long_qty_after"),
                "long_avg_after": led.get("core_long_avg_after"),
                "core_short_qty_after": led.get("core_short_qty_after"),
                "overlay_short_qty_after": led.get("overlay_short_qty_after"),
                "total_short_qty_after": led.get("total_short_qty_after"),
                "total_short_avg_after": led.get("total_short_avg_after"),
                "net_qty_after": led.get("net_qty_after"),
                "total_economics_after": None,
                "causal_ok": True,
                "warning_flags": "",
            }
        )
        pos.append({"trade_id": trade_id, "coin": coin, "variant": variant, **led})
    return orders, fills, pos


def run_one_case_variant(
    *,
    row: dict[str, Any],
    variant: str,
    candles_full: list[dict[str, Any]],
    horizon_days: int,
    dump_dir: Path | None,
) -> dict[str, Any]:
    trade_id = str(row["trade_id"])
    coin = str(row["coin"])
    book = book_from_pre_signal(row)
    try:
        sel = select_start_by_timing_mode(
            candles_full,
            signal_ts=str(book["signal_available_ts"]),
            existing_short_qty=_f(book["short_qty"]),
            existing_short_avg=_f(book["short_avg"]),
            neutralization_qty=_f(book["neutralization_qty"]),
            minimum_start_distance_pct=START_DISTANCE_PCT,
            timing_mode="T1",
            parse_ts=_parse_ts,
        )
    except Exception as exc:  # noqa: BLE001 — unresolved start is a case status
        return {
            "trade_id": trade_id,
            "coin": coin,
            "variant": variant,
            "status": "STATE_UNRESOLVED",
            "reason": f"t1_start_failed:{exc}",
            "started": False,
        }
    if not sel.get("fill_timestamp"):
        return {
            "trade_id": trade_id,
            "coin": coin,
            "variant": variant,
            "status": "STATE_UNRESOLVED",
            "reason": "no_t1_start_within_data",
            "started": False,
        }
    fill_ts = str(sel["fill_timestamp"])
    fill_px = float(sel["fill_price"])
    neut = neutralize_at_price(book, fill_px)
    neut_qty = _f(book["neutralization_qty"])
    neut_fee = fee_usdt(fill_price=fill_px, qty=neut_qty, fee_rate=0.00055)
    # qty-step check
    qty_step = float(STRATEGY.get("qty_step", 0.001))
    rounded_neut = round_qty(neut_qty, qty_step)
    if abs(rounded_neut - neut_qty) > QTY_TOL and rounded_neut <= 0:
        return {
            "trade_id": trade_id,
            "coin": coin,
            "variant": variant,
            "status": "STATE_UNRESOLVED",
            "reason": "neutralization_qty_step",
            "started": False,
        }

    flags = variant_engine_flags(variant)
    cfg = build_cfg(
        variant_id=f"{_safe_trade_id(trade_id)}_{variant}",
        neut_book={
            "core_long_qty": neut["core_long_qty"],
            "core_long_avg": neut["core_long_avg"],
            "core_short_qty": neut["core_short_qty"],
            "core_short_avg": neut["core_short_avg"],
        },
        start_ts=fill_ts,
        start_price=fill_px,
    )
    # Per-coin symbol, identical policy params otherwise
    raw = cfg.to_dict()
    raw["symbol"] = coin if str(coin).endswith("USDT") else f"{coin}USDT"
    raw.update(flags)
    raw["minimum_post_add_distance_pct"] = None
    raw["post_add_distance_policy"] = "disabled"
    raw["tags"] = {
        "policy": POLICY_ID,
        "variant": variant,
        "trade_id": trade_id,
        "tem_orders_imported": False,
    }
    cfg = CoberturaConfig.from_dict(raw)

    candles = truncate_candles(
        candles_full, start_ts=fill_ts, horizon_days=horizon_days
    )
    result = run_cobertura(cfg, candles=candles, write_outputs=False)
    bundle = reconstruct_audit(policy=f"{POLICY_ID}_{variant}", cfg=cfg, result=result)

    inv_fails = [
        v for v in bundle.invariant_violations if v.get("pass_fail") == "FAIL"
    ]
    # order_audit's full_exit_audit is net_be-oriented. For legacy shared_be:
    # - DATA_END_OPEN / STOPPED without exit is not an invariant failure
    # - RECOVERED/recovered_profit flat exit is acceptable
    filtered_inv: list[dict[str, Any]] = []
    for v in inv_fails:
        if v.get("check") == "full_exit_audit":
            if result.state in ("DATA_END_OPEN", "STOPPED", "WAITING_MOVE", "OVERLAY_ACTIVE"):
                continue
            if (
                result.state == "RECOVERED"
                and result.exit_reason == "recovered_profit"
                and bool(result.integrity.get("flat_after_full_exit"))
            ):
                continue
            if result.state in ("RECOVERED", "RECOVERED_BE") and bool(
                result.integrity.get("flat_after_full_exit")
            ):
                continue
        filtered_inv.append(v)
    inv_fails = filtered_inv

    rec_ts = recovery_timestamp(result)
    status = classify_status(
        result=result,
        recovery_ts=rec_ts,
        start_ts=fill_ts,
        horizon_days=horizon_days,
        invariant_fail=bool(inv_fails),
    )
    # horizon tagging for recovered
    duration_days = days_between(fill_ts, rec_ts) if rec_ts else None
    duration_bars = None
    if rec_ts:
        for i, row_b in enumerate(result.per_bar_trace):
            if str(row_b.get("timestamp")) == rec_ts:
                duration_bars = i + 1
                break

    prior_realized = row.get("realized_pnl_before")
    prior_realized_f = (
        _f(prior_realized) if prior_realized not in (None, "") else None
    )
    prior_mtm = row.get("unrealized_pnl_at_signal_price")
    prior_mtm_f = _f(prior_mtm) if prior_mtm not in (None, "") else None
    prior_fees = row.get("cumulative_fees_before")
    prior_fees_f = _f(prior_fees) if prior_fees not in (None, "") else None

    layers = pnl_layers(
        result=result,
        prior_realized=prior_realized_f,
        prior_open_mtm=prior_mtm_f,
        prior_fees=prior_fees_f,
        neut_fee=neut_fee,
    )
    cap = capital_metrics(
        result=result, book_before=book, neut={**neut, "neutralization_qty": neut_qty}, start_price=fill_px
    )
    same = same_candle_stats(result)
    orders, fills, pos = build_order_fill_rows(
        trade_id=trade_id, coin=coin, variant=variant, result=result, bundle=bundle
    )

    adds = sum(1 for f in result.fills_events if f.get("kind") == "overlay_short_add")
    bes = sum(1 for f in result.fills_events if f.get("kind") == "overlay_be_close")
    last_econ = (
        result.total_exit_economics_timeline[-1]
        if result.total_exit_economics_timeline
        else {}
    )

    # Cashflow fee reconcile
    open_fees = sum(
        _f(f.get("fee"))
        for f in result.fills_events
        if f.get("kind") == "overlay_short_add"
    )
    close_fees = sum(
        _f(f.get("fee"))
        for f in result.fills_events
        if f.get("kind") in ("overlay_be_close", "full_exit")
    )
    fee_entry_ok = abs(open_fees - result.ledger.cumulative_entry_fees) <= 1e-6
    fee_close_ok = abs(close_fees - result.ledger.cumulative_close_fees) <= 1e-6

    # V1 invariant: no same-candle add+full_exit
    v1_ok = True
    if flags["defer_full_exit_after_same_bar_adds"] and same["candles_add_and_full_exit"] > 0:
        v1_ok = False
        inv_fails.append(
            {
                "check": "v1_no_post_add_same_candle_full_exit",
                "pass_fail": "FAIL",
                "detail": same["exit_candle_timestamp"],
            }
        )
        status = "INVARIANT_FAIL"

    # Gap fill never better than open
    gap_ok = True
    for g in result.gap_fill_events:
        side = g.get("side")
        open_px = _f(g.get("candle_open"))
        if side == "short":
            # short open fill must be <= open when gap-adjusted
            if _f(g.get("fill_price")) - open_px > 1e-9:
                gap_ok = False
        elif side == "buy":
            if open_px - _f(g.get("fill_price")) > 1e-9:
                gap_ok = False

    out = {
        "trade_id": trade_id,
        "coin": coin,
        "variant": variant,
        "policy": POLICY_ID,
        "started": True,
        "status": status,
        "final_state": result.state,
        "exit_reason": result.exit_reason,
        "signal_available_ts": book["signal_available_ts"],
        "start_fill_timestamp": fill_ts,
        "start_fill_price": fill_px,
        "projected_start_distance_pct": sel.get("projected_distance_at_fill"),
        "neutralization_qty": neut_qty,
        "neutralization_fee": neut_fee,
        "core_long_qty": neut["core_long_qty"],
        "core_short_qty": neut["core_short_qty"],
        "core_short_avg": neut["core_short_avg"],
        "qty_neutral": abs(neut["core_long_qty"] - neut["core_short_qty"]) <= QTY_TOL,
        "recovery_timestamp": rec_ts,
        "duration_days": duration_days,
        "duration_bars": duration_bars,
        "bars_processed": result.bars_processed,
        "recovery_rounds": result.recovery_rounds,
        "overlay_add_fills": adds,
        "overlay_be_closes": bes,
        "n_order_events": len(result.order_events),
        "n_fill_events": len(result.fills_events),
        "n_gap_adjusted_fills": len(result.gap_fill_events),
        "realized_overlay_pnl": result.ledger.realized_overlay_pnl,
        "engine_total_exit_economics": last_econ.get("total_exit_economics"),
        "cobertura_total_incl_neut_fee": layers["B_cobertura_total_including_neut_fee"],
        "combined_pnl": layers["D_combined"],
        "combined_quality": layers["combined_quality"],
        "fee_entry_match": fee_entry_ok,
        "fee_close_match": fee_close_ok,
        "v1_same_candle_exit_ok": v1_ok,
        "gap_fill_ok": gap_ok,
        "invariant_fail_count": len(inv_fails),
        "flat_after_exit": bool(result.integrity.get("flat_after_full_exit")),
        "horizon_days": horizon_days,
        "layers": layers,
        "capital": cap,
        "same_candle": same,
        "orders": orders,
        "fills": fills,
        "positions": pos,
        "gap_events": list(result.gap_fill_events),
        "full_exit_audit": list(bundle.full_exit_audit),
        "invariant_violations": inv_fails,
        "sel": sel,
        "neut": neut,
    }

    if dump_dir is not None:
        dump_dir.mkdir(parents=True, exist_ok=True)
        write_csv(dump_dir / "orders.csv", orders)
        write_csv(dump_dir / "fills.csv", fills)
        write_csv(dump_dir / "positions.csv", pos)
        atomic_write_json(dump_dir / "summary.json", {k: v for k, v in out.items() if k not in ("orders", "fills", "positions", "gap_events", "full_exit_audit", "invariant_violations", "layers", "capital", "same_candle", "sel", "neut")})
        atomic_write_json(dump_dir / "pnl_layers.json", layers)
        atomic_write_json(dump_dir / "same_candle.json", same)

    return out


def check_apt_regression(v0: dict[str, Any]) -> dict[str, Any]:
    fp = APT_REGRESSION
    checks = []

    def add(name: str, ok: bool, got: Any, exp: Any) -> None:
        checks.append({"check": name, "ok": ok, "got": got, "expected": exp})

    add(
        "fill_timestamp",
        str(v0.get("start_fill_timestamp", "")).startswith(fp["fill_timestamp_prefix"]),
        v0.get("start_fill_timestamp"),
        fp["fill_timestamp_prefix"],
    )
    add(
        "fill_price",
        _approx(v0.get("start_fill_price"), fp["fill_price"], rel=0, abs_tol=1e-9),
        v0.get("start_fill_price"),
        fp["fill_price"],
    )
    add(
        "neut_qty",
        _approx(v0.get("neutralization_qty"), fp["neutralization_qty"], rel=0, abs_tol=1e-6),
        v0.get("neutralization_qty"),
        fp["neutralization_qty"],
    )
    add(
        "core_qty",
        _approx(v0.get("core_long_qty"), fp["core_qty"], rel=0, abs_tol=1e-6)
        and _approx(v0.get("core_short_qty"), fp["core_qty"], rel=0, abs_tol=1e-6),
        (v0.get("core_long_qty"), v0.get("core_short_qty")),
        fp["core_qty"],
    )
    add(
        "core_short_avg",
        _approx(v0.get("core_short_avg"), fp["core_short_avg"], rel=1e-9, abs_tol=1e-9),
        v0.get("core_short_avg"),
        fp["core_short_avg"],
    )
    add(
        "overlay_adds",
        int(v0.get("overlay_add_fills") or -1) == fp["overlay_add_fills"],
        v0.get("overlay_add_fills"),
        fp["overlay_add_fills"],
    )
    add(
        "overlay_be",
        int(v0.get("overlay_be_closes") or -1) == fp["overlay_be_closes"],
        v0.get("overlay_be_closes"),
        fp["overlay_be_closes"],
    )
    add(
        "exit_ts",
        str(v0.get("recovery_timestamp") or "").startswith(fp["exit_timestamp_prefix"]),
        v0.get("recovery_timestamp"),
        fp["exit_timestamp_prefix"],
    )
    add(
        "overlay_pnl",
        _approx(v0.get("realized_overlay_pnl"), fp["realized_overlay_pnl"]),
        v0.get("realized_overlay_pnl"),
        fp["realized_overlay_pnl"],
    )
    add(
        "engine_econ",
        _approx(v0.get("engine_total_exit_economics"), fp["final_total_exit_economics"]),
        v0.get("engine_total_exit_economics"),
        fp["final_total_exit_economics"],
    )
    # Combined uses B incl neut fee + prior; forensic quoted combined on engine B + prior
    # Compare engine_econ + prior ≈ forensic combined
    prior = -11.900133102067503
    combined_like = _f(v0.get("engine_total_exit_economics")) + prior
    add(
        "combined_like_forensic",
        _approx(combined_like, fp["combined_before_unresolved_fees"]),
        combined_like,
        fp["combined_before_unresolved_fees"],
    )
    ok = all(c["ok"] for c in checks)
    return {
        "pass": ok,
        "decision": "APT_REGRESSION_PASS" if ok else "APT_REGRESSION_FAIL",
        "checks": checks,
    }


def _percentile(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    ys = sorted(xs)
    k = (len(ys) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return ys[int(k)]
    return ys[f] * (c - k) + ys[c] * (k - f)


def summarize_variant(rows: list[dict[str, Any]], *, n_selected: int) -> dict[str, Any]:
    started = [r for r in rows if r.get("started")]
    rec = [r for r in started if str(r.get("status", "")).startswith("RECOVERED")]
    def within(dmax: int) -> int:
        n = 0
        for r in rec:
            dd = r.get("duration_days")
            if dd is not None and float(dd) <= dmax:
                n += 1
        return n

    open120 = sum(1 for r in started if r.get("status") == "OPEN_AT_120D")
    profit = sum(1 for r in started if r.get("status") == "RECOVERED_PROFIT")
    loss = sum(1 for r in started if r.get("status") == "RECOVERED_LOSS")
    comb_pos = sum(
        1
        for r in started
        if r.get("combined_pnl") is not None and float(r["combined_pnl"]) > 0
    )
    comb_neg = sum(
        1
        for r in started
        if r.get("combined_pnl") is not None and float(r["combined_pnl"]) < 0
    )
    durs = [float(r["duration_days"]) for r in rec if r.get("duration_days") is not None]
    dds = [
        float(r["capital"]["max_drawdown_from_cobertura_start"])
        for r in started
        if r.get("capital")
    ]
    peaks = [
        float(r["capital"]["maximum_total_gross_exposure"])
        for r in started
        if r.get("capital")
    ]
    return {
        "variant": started[0]["variant"] if started else None,
        "n_selected": n_selected,
        "n_started": len(started),
        "n_recovered_30d": within(30),
        "n_recovered_60d": within(60),
        "n_recovered_90d": within(90),
        "n_recovered_120d": within(120),
        "n_open_120d": open120,
        "n_recovered_profit": profit,
        "n_recovered_loss": loss,
        "recovery_rate_120d": (within(120) / len(started)) if started else None,
        "combined_positive_count": comb_pos,
        "combined_negative_count": comb_neg,
        "cobertura_pnl_sum": sum(
            _f(r.get("cobertura_total_incl_neut_fee")) for r in started
        ),
        "combined_pnl_sum": sum(_f(r.get("combined_pnl")) for r in started if r.get("combined_pnl") is not None),
        "median_duration_days": statistics.median(durs) if durs else None,
        "p90_duration_days": _percentile(durs, 0.9),
        "max_duration_days": max(durs) if durs else None,
        "median_max_drawdown": statistics.median(dds) if dds else None,
        "worst_max_drawdown": max(dds) if dds else None,
        "median_peak_gross_exposure": statistics.median(peaks) if peaks else None,
        "worst_peak_gross_exposure": max(peaks) if peaks else None,
        "same_candle_exit_count": sum(
            1
            for r in started
            if (r.get("same_candle") or {}).get("candles_add_and_full_exit", 0) > 0
        ),
        "gap_adjusted_fill_count": sum(int(r.get("n_gap_adjusted_fills") or 0) for r in started),
        "invariant_fail_count": sum(int(r.get("invariant_fail_count") or 0) for r in started),
    }


def compare_variants(
    by_trade: dict[str, dict[str, dict[str, Any]]]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for trade_id, variants in by_trade.items():
        v0 = variants.get(VARIANT_BASELINE)
        if not v0:
            continue
        for name, vx in variants.items():
            if name == VARIANT_BASELINE:
                continue
            rows.append(
                {
                    "trade_id": trade_id,
                    "coin": v0.get("coin"),
                    "baseline_variant": VARIANT_BASELINE,
                    "compare_variant": name,
                    "baseline_status": v0.get("status"),
                    "compare_status": vx.get("status"),
                    "status_changed": v0.get("status") != vx.get("status"),
                    "recovery_timestamp_changed": v0.get("recovery_timestamp")
                    != vx.get("recovery_timestamp"),
                    "duration_delta_days": (
                        None
                        if v0.get("duration_days") is None or vx.get("duration_days") is None
                        else float(vx["duration_days"]) - float(v0["duration_days"])
                    ),
                    "cobertura_pnl_delta": _f(vx.get("cobertura_total_incl_neut_fee"))
                    - _f(v0.get("cobertura_total_incl_neut_fee")),
                    "combined_pnl_delta": (
                        None
                        if v0.get("combined_pnl") is None or vx.get("combined_pnl") is None
                        else _f(vx.get("combined_pnl")) - _f(v0.get("combined_pnl"))
                    ),
                    "max_drawdown_delta": _f(
                        (vx.get("capital") or {}).get("max_drawdown_from_cobertura_start")
                    )
                    - _f(
                        (v0.get("capital") or {}).get("max_drawdown_from_cobertura_start")
                    ),
                    "peak_exposure_delta": _f(
                        (vx.get("capital") or {}).get("maximum_total_gross_exposure")
                    )
                    - _f(
                        (v0.get("capital") or {}).get("maximum_total_gross_exposure")
                    ),
                    "n_fills_changed": int(vx.get("n_fill_events") or 0)
                    != int(v0.get("n_fill_events") or 0),
                    "winner_remained_winner": str(v0.get("status")).startswith("RECOVERED")
                    and str(vx.get("status")).startswith("RECOVERED"),
                    "winner_became_loser": str(v0.get("status")) == "RECOVERED_PROFIT"
                    and str(vx.get("status")) == "RECOVERED_LOSS",
                    "winner_became_open_or_unresolved": str(v0.get("status")).startswith(
                        "RECOVERED"
                    )
                    and not str(vx.get("status")).startswith("RECOVERED"),
                }
            )
    return rows


def write_report(
    *,
    output_dir: Path,
    decision: str,
    selected: list[dict[str, Any]],
    unresolved: list[dict[str, Any]],
    summaries: list[dict[str, Any]],
    comparisons: list[dict[str, Any]],
    apt_reg: dict[str, Any],
    all_results: list[dict[str, Any]],
) -> None:
    v0 = next((s for s in summaries if s.get("variant") == VARIANT_BASELINE), {})
    v3 = next(
        (s for s in summaries if s.get("variant") == "next_bar_exit_gap_open"), {}
    )
    baseline_winners = [
        r
        for r in all_results
        if r.get("variant") == VARIANT_BASELINE
        and str(r.get("status")).startswith("RECOVERED")
    ]
    v3_keep = 0
    for w in baseline_winners:
        tid = w["trade_id"]
        other = next(
            (
                r
                for r in all_results
                if r.get("trade_id") == tid
                and r.get("variant") == "next_bar_exit_gap_open"
            ),
            None,
        )
        if other and str(other.get("status")).startswith("RECOVERED"):
            v3_keep += 1
    flip_next = [
        c
        for c in comparisons
        if c.get("compare_variant") == "next_bar_exit"
        and c.get("winner_became_open_or_unresolved")
    ]
    flip_gap = [
        c
        for c in comparisons
        if c.get("compare_variant") in ("gap_open", "next_bar_exit_gap_open")
        and (c.get("winner_became_open_or_unresolved") or c.get("winner_became_loser"))
    ]
    risk_coins = sorted(
        [
            (
                r.get("coin"),
                _f((r.get("capital") or {}).get("max_drawdown_from_cobertura_start")),
                _f((r.get("capital") or {}).get("maximum_total_gross_exposure")),
            )
            for r in all_results
            if r.get("variant") == VARIANT_BASELINE and r.get("started")
        ],
        key=lambda x: -x[1],
    )[:5]
    lines = [
        "# Multi-Blocker Forensic Audit",
        "",
        f"**Decision: `{decision}`**",
        "",
        f"Policy: `{POLICY_ID}` (T1 close→next-open, 6% start distance, shared_be)",
        f"APT regression: `{apt_reg.get('decision')}`",
        "",
        "## Answers",
        "",
        f"1. Exakt testbare Blocker: **{len(selected)}** (unresolved **{len(unresolved)}**)",
        f"2. Recovery V0 bis 30/60/90/120d: "
        f"**{v0.get('n_recovered_30d')}/{v0.get('n_recovered_60d')}/"
        f"{v0.get('n_recovered_90d')}/{v0.get('n_recovered_120d')}**",
        f"3. Offen nach 120d (V0): **{v0.get('n_open_120d')}**",
        f"4. Combined positiv (V0): **{v0.get('combined_positive_count')}**",
        f"5. Summe Cobertura-PnL (V0, inkl. Neut-Fee): **{v0.get('cobertura_pnl_sum')}**",
        f"6. Summe Combined-PnL (V0): **{v0.get('combined_pnl_sum')}**",
        f"7. Median/Worst Drawdown (V0): **{v0.get('median_max_drawdown')}** / "
        f"**{v0.get('worst_max_drawdown')}**; "
        f"Median/Worst Peak Gross: **{v0.get('median_peak_gross_exposure')}** / "
        f"**{v0.get('worst_peak_gross_exposure')}**",
        f"8. Größte Drawdown-Risiken (Coin, dd, peak_gross): `{risk_coins}`",
        f"9. Same-Candle Add+Exit Fälle (V0): **{v0.get('same_candle_exit_count')}**",
        f"10. Baseline-Winner die unter V3 Winner bleiben: "
        f"**{v3_keep}/{len(baseline_winners)}**",
        f"11. Winner→open/unresolved bei next_bar_exit: **{len(flip_next)}** "
        f"`{[c.get('trade_id') for c in flip_next]}`",
        f"12. Kipper durch Gap-Varianten: **{len(flip_gap)}** "
        f"`{[c.get('trade_id') for c in flip_gap[:10]]}`",
        f"13. APT reproduziert Einzel-Audit: **{apt_reg.get('pass')}**",
        f"14. Invariant fails (V0 sum): **{v0.get('invariant_fail_count')}**; "
        f"V3: **{v3.get('invariant_fail_count')}**",
        f"15. Policy technisch für weitere Forschung freigegeben?: "
        f"**{'Ja' if 'PASS' in decision else 'Nein'}**",
        "",
        f"Decision: `{decision}`",
        "",
    ]
    atomic_write_text(output_dir / "REPORT.md", "\n".join(lines))


def run_audit(
    *,
    fill_replay_dir: Path,
    state_dir: Path,
    output_dir: Path,
    variants: list[str],
    only_trade_id: str | None = None,
    only_symbol: str | None = None,
    max_cases: int | None = None,
    horizon_days: int = 120,
    dump_full_ledgers: bool = False,
    resume: bool = False,
    strict: bool = False,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    if output_dir.exists() and (output_dir / "integrity.json").exists() and not resume:
        raise FileExistsError(f"refusing to overwrite {output_dir} (use --resume)")
    output_dir.mkdir(parents=True, exist_ok=True)

    selected, unresolved = load_case_universe(
        fill_replay_dir=fill_replay_dir, state_dir=state_dir
    )
    if only_trade_id:
        selected = [r for r in selected if r.get("trade_id") == only_trade_id]
    if only_symbol:
        sym = only_symbol.upper()
        selected = [
            r
            for r in selected
            if str(r.get("coin", "")).upper().startswith(sym.replace("USDT", ""))
            or str(r.get("coin", "")).upper() == sym
        ]
    if max_cases is not None:
        selected = selected[: int(max_cases)]

    write_csv(
        output_dir / "case_selection.csv",
        [
            {
                "trade_id": r.get("trade_id"),
                "coin": r.get("coin"),
                "selected": True,
                "ready_for_neutralization": r.get("ready_for_neutralization"),
                "replay_match_status": r.get("replay_match_status"),
                "signal_available_ts": r.get("signal_available_ts"),
            }
            for r in selected
        ]
        + [
            {
                "trade_id": r.get("trade_id"),
                "coin": r.get("coin"),
                "selected": False,
                "ready_for_neutralization": False,
                "replay_match_status": r.get("replay_match_status"),
                "signal_available_ts": "",
                "unresolved_status": r.get("status"),
                "reason": r.get("reason"),
            }
            for r in unresolved
        ],
    )

    candle_cache: dict[str, list[dict[str, Any]]] = {}
    all_results: list[dict[str, Any]] = []
    by_trade: dict[str, dict[str, dict[str, Any]]] = {}

    for row in selected:
        coin = str(row["coin"])
        trade_id = str(row["trade_id"])
        if coin not in candle_cache:
            candle_cache[coin] = load_candles_for_symbol(
                coin, timeframe="5m", data_dir=DEFAULT_DATA_DIR, limit=50_000
            )
        candles = candle_cache[coin]
        by_trade.setdefault(trade_id, {})
        for variant in variants:
            dump = None
            if dump_full_ledgers:
                dump = output_dir / "cases" / _safe_trade_id(trade_id) / variant
            print(f"[multi] {trade_id} {variant}", flush=True)
            res = run_one_case_variant(
                row=row,
                variant=variant,
                candles_full=candles,
                horizon_days=horizon_days,
                dump_dir=dump,
            )
            all_results.append(res)
            by_trade[trade_id][variant] = res

    # Flatten exports
    blocker_results = []
    pnl_rows = []
    capital_rows = []
    dd_rows = []
    dur_rows = []
    same_rows = []
    gap_rows = []
    order_rows = []
    fill_rows = []
    pos_rows = []
    full_exit_rows = []
    cashflow_rows = []
    inv_rows = []
    recovered_rows = []
    losing_rows = []
    open_rows = []

    for r in all_results:
        flat = {k: v for k, v in r.items() if k not in (
            "layers", "capital", "same_candle", "orders", "fills", "positions",
            "gap_events", "full_exit_audit", "invariant_violations", "sel", "neut",
        )}
        blocker_results.append(flat)
        layers = r.get("layers") or {}
        pnl_rows.append({"trade_id": r.get("trade_id"), "coin": r.get("coin"), "variant": r.get("variant"), **layers})
        cap = r.get("capital") or {}
        capital_rows.append({"trade_id": r.get("trade_id"), "coin": r.get("coin"), "variant": r.get("variant"), **cap})
        dd_rows.append(
            {
                "trade_id": r.get("trade_id"),
                "coin": r.get("coin"),
                "variant": r.get("variant"),
                "max_adverse_equity": cap.get("max_adverse_equity"),
                "max_drawdown_from_cobertura_start": cap.get(
                    "max_drawdown_from_cobertura_start"
                ),
                "max_drawdown_per_100_initial_long": cap.get(
                    "max_drawdown_per_100_initial_long"
                ),
            }
        )
        dur_rows.append(
            {
                "trade_id": r.get("trade_id"),
                "coin": r.get("coin"),
                "variant": r.get("variant"),
                "duration_days": r.get("duration_days"),
                "duration_bars": r.get("duration_bars"),
                "recovery_timestamp": r.get("recovery_timestamp"),
                "bars_processed": r.get("bars_processed"),
            }
        )
        same_rows.append(
            {
                "trade_id": r.get("trade_id"),
                "coin": r.get("coin"),
                "variant": r.get("variant"),
                **(r.get("same_candle") or {}),
            }
        )
        for g in r.get("gap_events") or []:
            gap_rows.append(
                {
                    "trade_id": r.get("trade_id"),
                    "coin": r.get("coin"),
                    "variant": r.get("variant"),
                    **g,
                }
            )
        order_rows.extend(r.get("orders") or [])
        fill_rows.extend(r.get("fills") or [])
        pos_rows.extend(r.get("positions") or [])
        for fe in r.get("full_exit_audit") or []:
            full_exit_rows.append(
                {
                    "trade_id": r.get("trade_id"),
                    "coin": r.get("coin"),
                    "variant": r.get("variant"),
                    **fe,
                }
            )
        cashflow_rows.append(
            {
                "trade_id": r.get("trade_id"),
                "coin": r.get("coin"),
                "variant": r.get("variant"),
                "fee_entry_match": r.get("fee_entry_match"),
                "fee_close_match": r.get("fee_close_match"),
                "pass_fail": (
                    "PASS"
                    if r.get("fee_entry_match") and r.get("fee_close_match")
                    else "FAIL"
                ),
            }
        )
        for v in r.get("invariant_violations") or []:
            inv_rows.append(
                {
                    "trade_id": r.get("trade_id"),
                    "coin": r.get("coin"),
                    "variant": r.get("variant"),
                    **v,
                }
            )
        if str(r.get("status")).startswith("RECOVERED"):
            recovered_rows.append(flat)
            if r.get("status") == "RECOVERED_LOSS":
                losing_rows.append(flat)
        if str(r.get("status", "")).startswith("OPEN_"):
            open_rows.append(flat)

    summaries = []
    for variant in variants:
        rows_v = [r for r in all_results if r.get("variant") == variant]
        summaries.append(summarize_variant(rows_v, n_selected=len(selected)))

    comparisons = compare_variants(by_trade)

    apt_v0 = next(
        (
            r
            for r in all_results
            if r.get("trade_id") == APT_TRADE_ID and r.get("variant") == VARIANT_BASELINE
        ),
        None,
    )
    if apt_v0 is None:
        apt_reg = {
            "pass": False,
            "decision": "APT_REGRESSION_FAIL",
            "checks": [{"check": "apt_present", "ok": False}],
        }
    else:
        apt_reg = check_apt_regression(apt_v0)

    # Decision
    hard_fail = False
    warnings = []
    if not apt_reg.get("pass"):
        hard_fail = True
    if any(int(s.get("invariant_fail_count") or 0) > 0 for s in summaries):
        hard_fail = True
    if any(
        (r.get("same_candle") or {}).get("candles_add_and_full_exit", 0) > 0
        for r in all_results
        if r.get("variant") == VARIANT_BASELINE
    ):
        warnings.append("baseline_same_candle_add_full_exit")
    if unresolved:
        warnings.append("unresolved_cases_present")
    if hard_fail:
        decision = "MULTI_BLOCKER_FORENSIC_AUDIT_FAIL"
    elif warnings:
        decision = "MULTI_BLOCKER_FORENSIC_AUDIT_PASS_WITH_WARNINGS"
    else:
        decision = "MULTI_BLOCKER_FORENSIC_AUDIT_PASS"
    if strict and warnings:
        decision = "MULTI_BLOCKER_FORENSIC_AUDIT_FAIL"

    write_csv(output_dir / "multi_blocker_summary.csv", summaries)
    write_csv(output_dir / "variant_comparison.csv", comparisons)
    write_csv(output_dir / "blocker_results.csv", blocker_results)
    write_csv(output_dir / "blocker_pnl_layers.csv", pnl_rows)
    write_csv(output_dir / "blocker_capital_metrics.csv", capital_rows)
    write_csv(output_dir / "blocker_drawdown_metrics.csv", dd_rows)
    write_csv(output_dir / "blocker_duration_metrics.csv", dur_rows)
    write_csv(output_dir / "blocker_same_candle_audit.csv", same_rows)
    write_csv(output_dir / "blocker_gap_fill_audit.csv", gap_rows)
    write_csv(output_dir / "blocker_order_events.csv", order_rows)
    write_csv(output_dir / "blocker_fill_events.csv", fill_rows)
    write_csv(output_dir / "blocker_position_reconciliation.csv", pos_rows)
    write_csv(output_dir / "blocker_full_exit_audit.csv", full_exit_rows)
    write_csv(output_dir / "blocker_cashflow_reconciliation.csv", cashflow_rows)
    write_csv(output_dir / "recovered_cases.csv", recovered_rows)
    write_csv(output_dir / "unresolved_cases.csv", unresolved)
    write_csv(output_dir / "losing_recoveries.csv", losing_rows)
    write_csv(output_dir / "open_after_120d.csv", open_rows)
    write_csv(output_dir / "invariant_violations.csv", inv_rows or [
        {
            "trade_id": "",
            "check": "none",
            "detail": "no invariant failures",
            "pass_fail": "PASS",
        }
    ])
    write_csv(
        output_dir / "replay_mismatches.csv",
        [u for u in unresolved if u.get("status") == "REPLAY_MISMATCH"],
    )
    atomic_write_json(output_dir / "apt_regression.json", apt_reg)
    atomic_write_json(
        output_dir / "config_snapshot.json",
        {
            "policy": POLICY_ID,
            "start_distance_pct": START_DISTANCE_PCT,
            "timing_mode": "T1",
            "variants": variants,
            "horizon_days": horizon_days,
            "strategy": STRATEGY,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        },
    )
    integrity = {
        "decision": decision,
        "warnings": warnings,
        "n_selected": len(selected),
        "n_unresolved": len(unresolved),
        "n_results": len(all_results),
        "apt_regression": apt_reg.get("decision"),
        "multi_blocker_release_allowed": "PASS" in decision,
    }
    atomic_write_json(output_dir / "integrity.json", integrity)
    write_csv(
        output_dir / "source_manifest.csv",
        [
            {"key": "fill_replay_dir", "path": str(fill_replay_dir)},
            {"key": "state_dir", "path": str(state_dir)},
            {"key": "apt_forensic_ref", "path": "results/apt_winner_forensic_order_audit_20260726"},
        ],
    )
    write_report(
        output_dir=output_dir,
        decision=decision,
        selected=selected,
        unresolved=unresolved,
        summaries=summaries,
        comparisons=comparisons,
        apt_reg=apt_reg,
        all_results=all_results,
    )
    return {
        "decision": decision,
        "output_dir": str(output_dir),
        "n_selected": len(selected),
        "n_unresolved": len(unresolved),
        "apt_regression": apt_reg.get("decision"),
        "integrity": integrity,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--fill-replay-dir", type=Path, default=DEFAULT_FILL_REPLAY_DIR)
    p.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--policy", default=POLICY_ID)
    p.add_argument(
        "--variants",
        default=",".join(ALL_VARIANTS),
        help="comma-separated variants",
    )
    p.add_argument("--only-trade-id", default=None)
    p.add_argument("--only-symbol", default=None)
    p.add_argument("--max-cases", type=int, default=None)
    p.add_argument("--horizon-days", type=int, default=120)
    p.add_argument("--dump-full-ledgers", action="store_true")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--strict", action="store_true")
    args = p.parse_args(argv)
    if args.policy != POLICY_ID:
        raise SystemExit(f"only {POLICY_ID} supported in this research runner")
    variants = parse_variants(args.variants)
    out = run_audit(
        fill_replay_dir=args.fill_replay_dir,
        state_dir=args.state_dir,
        output_dir=args.output_dir,
        variants=variants,
        only_trade_id=args.only_trade_id,
        only_symbol=args.only_symbol,
        max_cases=args.max_cases,
        horizon_days=args.horizon_days,
        dump_full_ledgers=args.dump_full_ledgers,
        resume=args.resume,
        strict=args.strict,
    )
    print(json.dumps(out, indent=2, default=str))
    return 0 if "PASS" in out["decision"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
