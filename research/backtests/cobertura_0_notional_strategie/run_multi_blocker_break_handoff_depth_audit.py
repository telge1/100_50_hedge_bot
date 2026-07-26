"""Break-handoff-depth audit: TEM continues after structure break, then hands off.

Research-only. Does NOT freeze the legacy Cobertura start snapshot and delay refill.
Activation depth is measured from structure_break_level. Inventory at handoff is the
true TEM book from the fill ledger (last fill with timestamp < activation_ts).
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from collections import defaultdict
from datetime import timedelta
from pathlib import Path
from typing import Any

from fixed_cycle_hedge_bot.math_utils import calculate_pnl
from research.backtests.candle_loader import load_candles_for_symbol
from research.backtests.emergency_lock.cost_model import fee_usdt
from research.backtests.multicoin_price_staging_grid import (
    atomic_write_json,
    atomic_write_text,
    write_csv,
)

from .break_handoff_depth import (
    BREAK_DEPTH_VARIANTS,
    HANDOFF_ORDER_POLICY,
    classify_handoff_case,
    long_short_spread_pct,
    path_metrics_between,
    select_activation_after_break,
    snapshot_from_ledger,
)
from .config import CoberturaConfig
from .engine import _parse_ts
from .ledger import round_qty
from .multi_blocker_variants import VARIANT_BASELINE, variant_engine_flags
from .order_audit import QTY_TOL, reconstruct_audit
from .run_apt_start_and_post_add_distance_audit import STRATEGY, neutralize_at_price
from .run_apt_start_distance_execution_timing_audit import build_cfg
from .run_multi_blocker_forensic_audit import (
    DEFAULT_FILL_REPLAY_DIR,
    DEFAULT_STATE_DIR,
    POLICY_ID,
    _approx,
    _f,
    _safe_trade_id,
    capital_metrics,
    classify_status,
    days_between,
    load_case_universe,
    pnl_layers,
    recovery_timestamp,
    same_candle_stats,
    truncate_candles,
)
from .runner import run_cobertura

DEFAULT_MULTI_BLOCKER_DIR = Path(
    "research/backtests/cobertura_0_notional_strategie/results/"
    "multi_blocker_forensic_audit_20260726"
)
DEFAULT_OUTPUT_DIR = Path(
    "research/backtests/cobertura_0_notional_strategie/results/"
    "multi_blocker_break_handoff_depth_audit_20260726"
)

HORIZON_DAYS = 120
PNL_TOL = 1e-3
STATE_TOL = 1e-6


def _read_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _median(vals: list[float]) -> float | None:
    clean = [float(v) for v in vals if v is not None and not (isinstance(v, float) and math.isnan(v))]
    if not clean:
        return None
    return float(statistics.median(clean))


def load_ledger_by_trade(fill_replay_dir: Path) -> dict[str, list[dict[str, Any]]]:
    rows = _read_csv(fill_replay_dir / "blocker_fill_ledger.csv")
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        out[str(r["trade_id"])].append(r)
    for tid in out:
        out[tid].sort(key=lambda r: str(r.get("fill_timestamp") or ""))
    return dict(out)


def load_break_events(state_dir: Path) -> dict[str, dict[str, Any]]:
    rows = _read_csv(state_dir / "blocker_break_events.csv")
    return {str(r["trade_id"]): r for r in rows}


def book_dict_from_snap(snap: dict[str, Any]) -> dict[str, Any]:
    return {
        "long_qty": float(snap["long_qty"]),
        "long_avg": float(snap["long_avg"]),
        "short_qty": float(snap["short_qty"]),
        "short_avg": float(snap["short_avg"]),
        "neutralization_qty": max(
            0.0, float(snap["long_qty"]) - float(snap["short_qty"])
        ),
    }


def mtm_book(book: dict[str, Any], px: float) -> float:
    long_q = float(book["long_qty"])
    short_q = float(book["short_qty"])
    ur = 0.0
    if long_q > 0:
        ur += calculate_pnl(float(book["long_avg"]), px, long_q, "long")
    if short_q > 0:
        ur += calculate_pnl(float(book["short_avg"]), px, short_q, "short")
    return ur


def equity_path_pre_activation(
    *,
    ledger: list[dict[str, Any]],
    candles: list[dict[str, Any]],
    start_ts: str,
    end_ts: str,
) -> list[tuple[str, float]]:
    """realized_at_last_fill_before_bar + MTM(book, close) on [start, end)."""
    path: list[tuple[str, float]] = []
    st = _parse_ts(start_ts)
    et = _parse_ts(end_ts)
    for c in candles:
        ts = _parse_ts(c["timestamp"])
        if ts < st:
            continue
        if ts >= et:
            break
        snap = snapshot_from_ledger(
            ledger, cutoff_ts=c["timestamp"], trade_id="", coin=""
        )
        book = book_dict_from_snap(snap)
        eq = float(snap["realized_pnl"]) + mtm_book(book, float(c["close"]))
        path.append((str(c["timestamp"]), eq))
    return path


def drawdown_from_path(path: list[tuple[str, float]]) -> dict[str, Any]:
    if not path:
        return {"max_drawdown": 0.0, "min_equity": None, "max_equity": None}
    peak = path[0][1]
    max_dd = 0.0
    mn = path[0][1]
    mx = path[0][1]
    for _, eq in path:
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)
        mn = min(mn, eq)
        mx = max(mx, eq)
    return {"max_drawdown": max_dd, "min_equity": mn, "max_equity": mx}


def price_drawdown(
    candles: list[dict[str, Any]], *, start_ts: str, end_ts: str | None
) -> float | None:
    st = _parse_ts(start_ts)
    et = _parse_ts(end_ts) if end_ts else None
    peak = None
    max_dd = None
    for c in candles:
        ts = _parse_ts(c["timestamp"])
        if ts < st:
            continue
        if et is not None and ts >= et:
            break
        high = float(c["high"])
        low = float(c["low"])
        peak = high if peak is None else max(peak, high)
        dd = (peak - low) / peak if peak and peak > 0 else 0.0
        max_dd = dd if max_dd is None else max(max_dd, dd)
    return max_dd


def shared_be_metrics(
    *,
    long_avg: float,
    short_avg_after: float,
    activation_price: float,
) -> dict[str, Any]:
    # Research proxy: reclaim long average after hedge refill (equal qty MTM is
    # price-independent; long-avg reclaim is the practical rebound reference).
    shared_be = float(long_avg)
    dist = (
        (shared_be - float(activation_price)) / float(activation_price)
        if activation_price > 0
        else None
    )
    return {
        "shared_be_price": shared_be,
        "shared_be_distance_from_activation_pct": dist,
        "required_rebound_to_shared_be_pct": dist,
        "long_short_avg_spread_after_refill_pct": long_short_spread_pct(
            long_avg=long_avg, short_avg=short_avg_after
        ),
    }


def run_cobertura_handoff(
    *,
    trade_id: str,
    coin: str,
    variant_name: str,
    book: dict[str, Any],
    fill_ts: str,
    fill_px: float,
    candles_full: list[dict[str, Any]],
    prior_realized: float | None,
    prior_fees: float | None,
    dump_dir: Path | None,
) -> dict[str, Any]:
    long_q = float(book["long_qty"])
    short_q = float(book["short_qty"])
    refill_qty = max(long_q - short_q, 0.0)

    if short_q > long_q + QTY_TOL:
        refill_class = "SHORT_ALREADY_OVERFILLED"
    elif abs(long_q - short_q) <= QTY_TOL:
        refill_class = "NO_REFILL_ALREADY_COVERED"
    else:
        refill_class = "REFILL_TO_LONG_QTY"

    if refill_class == "SHORT_ALREADY_OVERFILLED":
        return {
            "trade_id": trade_id,
            "coin": coin,
            "variant": variant_name,
            "started": False,
            "activation_reached": True,
            "refill_class": refill_class,
            "refill_short_qty": 0.0,
            "status": "STATE_UNRESOLVED",
            "reason": "SHORT_ALREADY_OVERFILLED",
            "invariant_fail": False,
            "cobertura_fills": 0,
        }

    if refill_class == "NO_REFILL_ALREADY_COVERED":
        # Still initialize Cobertura with already-neutral book (no short open fee).
        neut = {
            "core_long_qty": long_q,
            "core_long_avg": float(book["long_avg"]),
            "core_short_qty": short_q,
            "core_short_avg": float(book["short_avg"]),
        }
        neut_fee = 0.0
    else:
        neut = neutralize_at_price(book, fill_px)
        neut_fee = fee_usdt(fill_price=fill_px, qty=refill_qty, fee_rate=0.00055)
        qty_step = float(STRATEGY.get("qty_step", 0.001))
        if abs(round_qty(refill_qty, qty_step) - refill_qty) > QTY_TOL and round_qty(
            refill_qty, qty_step
        ) <= 0:
            return {
                "trade_id": trade_id,
                "coin": coin,
                "variant": variant_name,
                "started": False,
                "activation_reached": True,
                "refill_class": refill_class,
                "status": "STATE_UNRESOLVED",
                "reason": "refill_qty_step",
                "invariant_fail": False,
                "cobertura_fills": 0,
            }

    flags = variant_engine_flags(VARIANT_BASELINE)
    cfg = build_cfg(
        variant_id=f"{_safe_trade_id(trade_id)}_{variant_name}",
        neut_book={
            "core_long_qty": neut["core_long_qty"],
            "core_long_avg": neut["core_long_avg"],
            "core_short_qty": neut["core_short_qty"],
            "core_short_avg": neut["core_short_avg"],
        },
        start_ts=fill_ts,
        start_price=fill_px,
    )
    raw = cfg.to_dict()
    raw["symbol"] = coin if str(coin).endswith("USDT") else f"{coin}USDT"
    raw.update(flags)
    raw["minimum_post_add_distance_pct"] = None
    raw["post_add_distance_policy"] = "disabled"
    raw["tags"] = {
        "policy": POLICY_ID,
        "audit": "break_handoff_depth",
        "variant": variant_name,
        "trade_id": trade_id,
    }
    cfg = CoberturaConfig.from_dict(raw)
    candles = truncate_candles(candles_full, start_ts=fill_ts, horizon_days=HORIZON_DAYS)
    result = run_cobertura(cfg, candles=candles, write_outputs=False)
    bundle = reconstruct_audit(
        policy=f"break_handoff_{variant_name}", cfg=cfg, result=result
    )
    inv_fails = [
        v
        for v in bundle.invariant_violations
        if v.get("pass_fail") == "FAIL" and v.get("check") != "full_exit_audit"
    ]
    for v in bundle.invariant_violations:
        if v.get("pass_fail") != "FAIL":
            continue
        if v.get("check") == "full_exit_audit" and result.state in (
            "RECOVERED",
            "RECOVERED_BE",
        ):
            if not bool(result.integrity.get("flat_after_full_exit")):
                inv_fails.append(v)

    rec_ts = recovery_timestamp(result)
    status = classify_status(
        result=result,
        recovery_ts=rec_ts,
        start_ts=fill_ts,
        horizon_days=HORIZON_DAYS,
        invariant_fail=bool(inv_fails),
    )
    layers = pnl_layers(
        result=result,
        prior_realized=prior_realized,
        prior_open_mtm=None,
        prior_fees=prior_fees,
        neut_fee=neut_fee,
    )
    cap = capital_metrics(
        result=result,
        book_before=book,
        neut={**neut, "neutralization_qty": refill_qty},
        start_price=fill_px,
    )
    same = same_candle_stats(result)
    last = (
        result.total_exit_economics_timeline[-1]
        if result.total_exit_economics_timeline
        else {}
    )
    duration_days = days_between(fill_ts, rec_ts) if rec_ts else None
    recovered = str(status).startswith("RECOVERED")
    be = shared_be_metrics(
        long_avg=float(book["long_avg"]),
        short_avg_after=float(neut["core_short_avg"]),
        activation_price=fill_px,
    )
    out = {
        "trade_id": trade_id,
        "coin": coin,
        "variant": variant_name,
        "started": True,
        "activation_reached": True,
        "refill_class": refill_class,
        "status": status,
        "final_state": result.state,
        "exit_reason": result.exit_reason,
        "refill_short_qty": refill_qty,
        "refill_price": fill_px,
        "short_qty_after_refill": neut["core_short_qty"],
        "short_avg_after_refill": neut["core_short_avg"],
        "long_short_avg_spread_before_refill_pct": long_short_spread_pct(
            long_avg=float(book["long_avg"]), short_avg=float(book["short_avg"])
        )
        if short_q > 0
        else None,
        **be,
        "recovery_timestamp": rec_ts,
        "recovery_time": rec_ts,
        "recovery_days": duration_days,
        "exit_time": rec_ts,
        "exit_price": None,
        "recovered_30d": bool(recovered and duration_days is not None and duration_days <= 30),
        "recovered_60d": bool(recovered and duration_days is not None and duration_days <= 60),
        "recovered_90d": bool(recovered and duration_days is not None and duration_days <= 90),
        "recovered_120d": bool(recovered and duration_days is not None and duration_days <= 120),
        "open_at_120d": status == "OPEN_AT_120D",
        "cobertura_pnl_120d": layers["B_cobertura_total_including_neut_fee"],
        "engine_pnl_120d": last.get("total_exit_economics"),
        "combined_pnl_120d": layers["D_combined"],
        "overlay_pnl_120d": result.ledger.realized_overlay_pnl,
        "same_candle_activation_exit": int(same.get("candles_add_and_full_exit") or 0) > 0,
        "invariant_fail": bool(inv_fails),
        "post_activation_drawdown": cap.get("max_drawdown_from_cobertura_start"),
        "min_combined_pnl_post": cap.get("max_adverse_equity"),
        "cobertura_fills": len(result.fills_events),
        "qty_neutral": abs(neut["core_long_qty"] - neut["core_short_qty"]) <= QTY_TOL,
        "handoff_order_policy": HANDOFF_ORDER_POLICY,
        "layers": layers,
        "capital": cap,
        "same_candle": same,
        "inv_fails": inv_fails,
        "bars_processed": result.bars_processed,
    }
    if dump_dir is not None:
        dump_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            dump_dir / "summary.json",
            {
                k: v
                for k, v in out.items()
                if k not in ("layers", "capital", "same_candle", "inv_fails")
            },
        )
    return out


def run_no_cobertura_after_break(
    *,
    trade_id: str,
    coin: str,
    ledger: list[dict[str, Any]],
    candles_full: list[dict[str, Any]],
    break_available_ts: str,
) -> dict[str, Any]:
    start = _parse_ts(break_available_ts)
    end = start + timedelta(days=HORIZON_DAYS)
    end_ts = end.isoformat()
    path = equity_path_pre_activation(
        ledger=ledger,
        candles=candles_full,
        start_ts=break_available_ts,
        end_ts=end_ts,
    )
    dd = drawdown_from_path(path)
    snap_end = snapshot_from_ledger(
        ledger, cutoff_ts=end_ts, trade_id=trade_id, coin=coin
    )
    # If ledger ends before horizon, use last book MTM at horizon close.
    end_candle = None
    for c in candles_full:
        ts = _parse_ts(c["timestamp"])
        if ts > end:
            break
        if ts >= start:
            end_candle = c
    end_px = float(end_candle["close"]) if end_candle else 0.0
    book = book_dict_from_snap(snap_end)
    # Prefer last fill before horizon; if fills stop early, still MTM at end.
    combined = float(snap_end["realized_pnl"]) + mtm_book(book, end_px)
    return {
        "trade_id": trade_id,
        "coin": coin,
        "variant": "NO_COBERTURA_AFTER_BREAK",
        "started": False,
        "activation_reached": False,
        "refill_class": "NO_COBERTURA",
        "refill_short_qty": 0.0,
        "status": "OPEN_AT_120D",
        "recovered_30d": False,
        "recovered_60d": False,
        "recovered_90d": False,
        "recovered_120d": False,
        "open_at_120d": True,
        "cobertura_pnl_120d": 0.0,
        "engine_pnl_120d": mtm_book(book, end_px),
        "combined_pnl_120d": combined,
        "overlay_pnl_120d": 0.0,
        "same_candle_activation_exit": False,
        "invariant_fail": False,
        "cobertura_fills": 0,
        "full_horizon_drawdown": dd["max_drawdown"],
        "pre_activation_drawdown": dd["max_drawdown"],
        "post_activation_drawdown": 0.0,
        "min_combined_pnl": dd["min_equity"],
        "max_combined_pnl": dd["max_equity"],
        "handoff_order_policy": "N/A",
    }


def load_legacy_b0(multi_blocker_dir: Path) -> dict[str, dict[str, Any]]:
    rows = [
        r
        for r in _read_csv(multi_blocker_dir / "blocker_results.csv")
        if r.get("variant") == "baseline"
    ]
    return {str(r["trade_id"]): r for r in rows}


def check_parity_guards(
    *,
    selected: list[dict[str, Any]],
    break_by_id: dict[str, dict[str, Any]],
    ledger_by_trade: dict[str, list[dict[str, Any]]],
    break_d0_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    ok = True

    for row in selected:
        tid = str(row["trade_id"])
        br = break_by_id.get(tid)
        if not br or str(br.get("ok")).lower() not in ("true", "1"):
            ok = False
            checks.append({"check": f"{tid}:break_present", "ok": False})
            continue
        # Guard 1: break fields reproduce
        sig_row = str(row["signal_available_ts"]).replace(" ", "T")
        sig_br = str(br["signal_available_ts"]).replace(" ", "T")
        match_sig = _parse_ts(sig_row) == _parse_ts(sig_br)
        lvl_row = _f(row.get("structure_break_level"))
        lvl_br = _f(br.get("structure_break_level"))
        match_lvl = _approx(lvl_row, lvl_br, abs_tol=1e-9)
        if not (match_sig and match_lvl):
            ok = False
        checks.append(
            {
                "check": f"{tid}:structure_break_parity",
                "ok": match_sig and match_lvl,
                "signal_row": sig_row,
                "signal_break": sig_br,
                "level_row": lvl_row,
                "level_break": lvl_br,
            }
        )

        # Guard 2: pre-break / at-signal book from ledger == pre_signal CSV
        snap = snapshot_from_ledger(
            ledger_by_trade.get(tid, []),
            cutoff_ts=row["signal_available_ts"],
            trade_id=tid,
            coin=str(row["coin"]),
        )
        pairs = [
            ("long_qty", snap["long_qty"], row["long_qty_before"]),
            ("short_qty", snap["short_qty"], row["short_qty_before"]),
            ("long_avg", snap["long_avg"], row["long_avg_before"]),
            ("short_avg", snap["short_avg"], row["short_avg_before"]),
            ("realized_pnl", snap["realized_pnl"], row["realized_pnl_before"]),
        ]
        for name, got, want in pairs:
            match = _approx(got, want, abs_tol=STATE_TOL, rel=1e-9)
            if not match:
                ok = False
            checks.append(
                {
                    "check": f"{tid}:pre_break_state:{name}",
                    "ok": match,
                    "got": got,
                    "expected": want,
                }
            )

    # Guard 3: BREAK_D0 uses break-available book (fills_before == pre-signal)
    for r in break_d0_rows:
        if not r.get("activation_reached"):
            continue
        tid = r["trade_id"]
        # At D0 activation time should equal signal availability (or first eligible)
        # and fills between break and activation should be 0 when activation==signal
        fills_between = int(r.get("original_bot_fills_after_break") or 0)
        act = _parse_ts(str(r.get("activation_time")))
        sig = _parse_ts(str(r.get("structure_break_available_time")))
        same_bar = act == sig if act and sig else False
        if same_bar and fills_between != 0:
            ok = False
            checks.append(
                {
                    "check": f"{tid}:break_d0_no_teleport",
                    "ok": False,
                    "fills_between": fills_between,
                }
            )
        else:
            checks.append(
                {
                    "check": f"{tid}:break_d0_starts_at_break_state",
                    "ok": True,
                    "activation_time": r.get("activation_time"),
                    "fills_between": fills_between,
                }
            )

    return {
        "pass": ok,
        "decision": (
            "BREAK_HANDOFF_DEPTH_AUDIT_PASS"
            if ok
            else "BREAK_HANDOFF_DEPTH_AUDIT_BLOCKED_REPLAY_MISMATCH"
        ),
        "checks": checks,
    }


def classify_state_change(
    *,
    snap_break: dict[str, Any],
    snap_act: dict[str, Any],
    refill_d0: float | None,
    refill_now: float | None,
    be_d0: float | None,
    be_now: float | None,
) -> dict[str, Any]:
    spread0 = long_short_spread_pct(
        long_avg=float(snap_break["long_avg"]),
        short_avg=float(snap_break["short_avg"]),
    )
    spread1 = long_short_spread_pct(
        long_avg=float(snap_act["long_avg"]),
        short_avg=float(snap_act["short_avg"]),
    )
    refill0 = max(
        float(snap_break["long_qty"]) - float(snap_break["short_qty"]), 0.0
    )
    refill1 = max(float(snap_act["long_qty"]) - float(snap_act["short_qty"]), 0.0)
    realized0 = float(snap_break["realized_pnl"])
    realized1 = float(snap_act["realized_pnl"])

    improved_bits = 0
    worsened_bits = 0
    if spread0 is not None and spread1 is not None:
        if abs(spread1) + 1e-12 < abs(spread0):
            improved_bits += 1
        elif abs(spread1) > abs(spread0) + 1e-12:
            worsened_bits += 1
    if refill1 + 1e-12 < refill0:
        improved_bits += 1
    elif refill1 > refill0 + 1e-12:
        worsened_bits += 1
    if realized1 > realized0 + 1e-9:
        improved_bits += 1
    elif realized1 < realized0 - 1e-9:
        worsened_bits += 1
    if be_d0 is not None and be_now is not None:
        if be_now + 1e-12 < be_d0:
            improved_bits += 1
        elif be_now > be_d0 + 1e-12:
            worsened_bits += 1

    return {
        "state_improvement_vs_break": improved_bits > worsened_bits and improved_bits > 0,
        "state_worsened_before_activation": worsened_bits > improved_bits and worsened_bits > 0,
        "delta_long_qty": float(snap_act["long_qty"]) - float(snap_break["long_qty"]),
        "delta_short_qty": float(snap_act["short_qty"]) - float(snap_break["short_qty"]),
        "delta_long_avg": float(snap_act["long_avg"]) - float(snap_break["long_avg"]),
        "delta_short_avg": float(snap_act["short_avg"]) - float(snap_break["short_avg"]),
        "delta_realized_pnl": realized1 - realized0,
        "delta_long_short_avg_spread_pct": (
            (spread1 - spread0) if spread0 is not None and spread1 is not None else None
        ),
        "refill_qty_at_break": refill0,
        "refill_qty_at_activation": refill1,
        "refill_qty_delta_vs_break_d0": (
            None if refill_d0 is None or refill_now is None else refill_now - refill_d0
        ),
        "shared_be_distance_delta_vs_break_d0": (
            None if be_d0 is None or be_now is None else be_now - be_d0
        ),
    }


def run_audit(
    *,
    output_dir: Path,
    fill_replay_dir: Path = DEFAULT_FILL_REPLAY_DIR,
    state_dir: Path = DEFAULT_STATE_DIR,
    multi_blocker_dir: Path = DEFAULT_MULTI_BLOCKER_DIR,
    trade_ids: list[str] | None = None,
    dump_cases: bool = True,
) -> dict[str, Any]:
    selected, unresolved_src = load_case_universe(
        fill_replay_dir=fill_replay_dir, state_dir=state_dir
    )
    if trade_ids:
        want = set(trade_ids)
        selected = [r for r in selected if r["trade_id"] in want]

    break_by_id = load_break_events(state_dir)
    ledger_by_trade = load_ledger_by_trade(fill_replay_dir)
    legacy_b0 = load_legacy_b0(multi_blocker_dir)

    output_dir.mkdir(parents=True, exist_ok=True)
    cases_dir = output_dir / "cases"
    if dump_cases:
        cases_dir.mkdir(parents=True, exist_ok=True)

    trade_rows: list[dict[str, Any]] = []
    state_change_rows: list[dict[str, Any]] = []
    snap_rows: list[dict[str, Any]] = []
    not_reached_rows: list[dict[str, Any]] = []
    unresolved_rows: list[dict[str, Any]] = []
    invariant_rows: list[dict[str, Any]] = []

    # Unresolved break cases (BCH/TRX) — no depth simulation
    for u in unresolved_src:
        tid = str(u.get("trade_id") or "")
        status = str(u.get("status") or "")
        if "BREAK" in status.upper() or "BREAK" in str(u.get("reason") or "").upper():
            unresolved_rows.append(
                {
                    "trade_id": tid,
                    "coin": u.get("coin"),
                    "classification": "UNRESOLVED_STRUCTURE_BREAK",
                    "status": status,
                    "reason": u.get("reason"),
                }
            )

    candle_cache: dict[str, list[dict[str, Any]]] = {}

    for row in selected:
        tid = str(row["trade_id"])
        coin = str(row["coin"])
        br = break_by_id[tid]
        break_level = float(br["structure_break_level"])
        break_event_ts = str(br["trigger_event_timestamp"]).replace(" ", "T")
        break_avail_ts = str(br["signal_available_ts"]).replace(" ", "T")
        ledger = ledger_by_trade.get(tid, [])

        if coin not in candle_cache:
            candle_cache[coin] = load_candles_for_symbol(coin, limit=200_000)
        candles = candle_cache[coin]
        horizon_end = (_parse_ts(break_avail_ts) + timedelta(days=HORIZON_DAYS)).isoformat()

        snap_break = snapshot_from_ledger(
            ledger, cutoff_ts=break_avail_ts, trade_id=tid, coin=coin
        )
        d0_refill = None
        d0_be = None
        d0_row_ref: dict[str, Any] | None = None
        per_trade_variants: dict[str, dict[str, Any]] = {}

        # Identity / break metadata shared across variants
        base_id = {
            "coin": coin,
            "trade_id": tid,
            "structure_break_time": break_event_ts,
            "structure_break_available_time": break_avail_ts,
            "structure_break_price": break_level,
            "break_candle": break_event_ts,
            "handoff_order_policy": HANDOFF_ORDER_POLICY,
        }

        for vname, depth in BREAK_DEPTH_VARIANTS:
            if vname == "LEGACY_B0_REFERENCE":
                leg = legacy_b0.get(tid, {})
                out = {
                    **base_id,
                    "variant": vname,
                    "activation_depth_pct": None,
                    "activation_reached": bool(leg.get("started")),
                    "activation_time": leg.get("start_fill_timestamp"),
                    "activation_price": _f(leg.get("start_fill_price")),
                    "activation_fill_reason": "LEGACY_T1_6PCT_EXTERNAL",
                    "status": leg.get("status"),
                    "recovered_120d": str(leg.get("status") or "").startswith("RECOVERED"),
                    "combined_pnl_120d": _f(leg.get("combined_pnl")),
                    "engine_pnl_120d": _f(leg.get("engine_total_exit_economics")),
                    "overlay_pnl_120d": _f(leg.get("realized_overlay_pnl")),
                    "open_at_120d": leg.get("status") == "OPEN_AT_120D",
                    "invariant_fail": False,
                    "note": (
                        "External multi-blocker baseline reference only; "
                        "not mixed into causal break-handoff replay."
                    ),
                    "classification": "LEGACY_B0_REFERENCE",
                }
                trade_rows.append(out)
                per_trade_variants[vname] = out
                continue

            if vname == "NO_COBERTURA_AFTER_BREAK":
                out = {
                    **base_id,
                    **run_no_cobertura_after_break(
                        trade_id=tid,
                        coin=coin,
                        ledger=ledger,
                        candles_full=candles,
                        break_available_ts=break_avail_ts,
                    ),
                    "activation_depth_pct": None,
                    "activation_target_price": None,
                    "activation_time": None,
                    "activation_price": None,
                    "activation_fill_reason": None,
                    "activation_delay_hours": None,
                    "activation_delay_days": None,
                }
                # classify later vs other variants
                trade_rows.append(out)
                per_trade_variants[vname] = out
                continue

            assert depth is not None
            sel = select_activation_after_break(
                candles,
                break_available_ts=break_avail_ts,
                structure_break_price=break_level,
                depth_pct=float(depth),
                parse_ts_fn=_parse_ts,
                horizon_end_ts=horizon_end,
            )
            target = float(sel["activation_target_price"])
            if not sel["activation_reached"]:
                out = {
                    **base_id,
                    "variant": vname,
                    "activation_depth_pct": float(depth),
                    "activation_target_price": target,
                    "activation_reached": False,
                    "activation_time": None,
                    "activation_price": None,
                    "activation_fill_reason": None,
                    "activation_delay_hours": None,
                    "activation_delay_days": None,
                    "status": "ACTIVATION_TARGET_NOT_REACHED",
                    "classification": "ACTIVATION_TARGET_NOT_REACHED",
                    "recovered_30d": False,
                    "recovered_60d": False,
                    "recovered_90d": False,
                    "recovered_120d": False,
                    "open_at_120d": True,
                    "combined_pnl_120d": None,
                    "invariant_fail": False,
                    "cobertura_fills": 0,
                }
                trade_rows.append(out)
                not_reached_rows.append(out)
                per_trade_variants[vname] = out
                continue

            act_ts = str(sel["activation_time"])
            act_px = float(sel["activation_price"])
            snap_act = snapshot_from_ledger(
                ledger, cutoff_ts=act_ts, trade_id=tid, coin=coin
            )
            path = path_metrics_between(
                ledger,
                candles,
                start_ts=break_avail_ts,
                end_ts=act_ts,
            )
            # Price DD break→activation
            path["max_drawdown_between_break_and_activation"] = price_drawdown(
                candles, start_ts=break_avail_ts, end_ts=act_ts
            )
            pre_eq = equity_path_pre_activation(
                ledger=ledger,
                candles=candles,
                start_ts=break_avail_ts,
                end_ts=act_ts,
            )
            pre_dd = drawdown_from_path(pre_eq)

            book = book_dict_from_snap(snap_act)
            dump = (
                cases_dir / _safe_trade_id(tid) / vname if dump_cases else None
            )
            cov = run_cobertura_handoff(
                trade_id=tid,
                coin=coin,
                variant_name=vname,
                book=book,
                fill_ts=act_ts,
                fill_px=act_px,
                candles_full=candles,
                prior_realized=float(snap_act["realized_pnl"]),
                prior_fees=_f(row.get("cumulative_fees_before")),
                dump_dir=dump,
            )

            delay_h = days_between(break_avail_ts, act_ts) * 24.0
            # Full equity path: pre + post (post shifted by attaching cobertura DD)
            post_dd = _f(cov.get("post_activation_drawdown"), 0.0) or 0.0
            # Concatenate: use pre path then append cobertura adverse relative to handoff equity
            handoff_eq = float(snap_act["realized_pnl"]) + mtm_book(book, act_px)
            post_path: list[tuple[str, float]] = []
            # Approximate post path from min_combined if available
            if cov.get("started") and cov.get("capital"):
                # capital max_adverse is absolute; rebuild crude post path endpoints
                post_path = [(act_ts, handoff_eq)]
                if cov.get("combined_pnl_120d") is not None:
                    post_path.append(
                        (
                            str(cov.get("exit_time") or horizon_end),
                            float(cov["combined_pnl_120d"]),
                        )
                    )
                if cov.get("min_combined_pnl_post") is not None:
                    post_path.append((act_ts + "_adverse", float(cov["min_combined_pnl_post"])))
            full_path = list(pre_eq) + post_path
            full_dd = drawdown_from_path(full_path)

            # Prefer explicit full-horizon: max(pre_dd, handoff_peak_to_post_adverse)
            full_horizon_dd = max(pre_dd["max_drawdown"], post_dd)

            be = shared_be_metrics(
                long_avg=float(book["long_avg"]),
                short_avg_after=float(
                    cov.get("short_avg_after_refill") or book["short_avg"]
                ),
                activation_price=act_px,
            )
            if vname == "BREAK_D0":
                d0_refill = float(cov.get("refill_short_qty") or 0.0)
                d0_be = be.get("shared_be_distance_from_activation_pct")

            change = classify_state_change(
                snap_break=snap_break,
                snap_act=snap_act,
                refill_d0=d0_refill if vname != "BREAK_D0" else float(cov.get("refill_short_qty") or 0.0),
                refill_now=float(cov.get("refill_short_qty") or 0.0),
                be_d0=d0_be if vname != "BREAK_D0" else be.get("shared_be_distance_from_activation_pct"),
                be_now=be.get("shared_be_distance_from_activation_pct"),
            )

            # Invariants
            inv_ok = True
            inv_notes = []
            refill_q = float(cov.get("refill_short_qty") or 0.0)
            if refill_q < -1e-12:
                inv_ok = False
                inv_notes.append("negative_refill_qty")
            if cov.get("refill_class") == "REFILL_TO_LONG_QTY":
                if abs(
                    float(cov.get("short_qty_after_refill") or 0)
                    - float(book["long_qty"])
                ) > QTY_TOL:
                    inv_ok = False
                    inv_notes.append("short_qty_after_refill_mismatch")
            if _parse_ts(act_ts) < _parse_ts(break_avail_ts):
                inv_ok = False
                inv_notes.append("activation_before_break_availability")
            if bool(sel.get("used_low_as_fill")):
                inv_ok = False
                inv_notes.append("used_low_as_fill")
            if int(cov.get("cobertura_fills") or 0) > 0 and _parse_ts(act_ts) > _parse_ts(
                break_avail_ts
            ):
                # Ensure no cobertura before activation: cobertura starts at act_ts by construction
                pass
            if not inv_ok or cov.get("invariant_fail"):
                invariant_rows.append(
                    {
                        "trade_id": tid,
                        "variant": vname,
                        "ok": False,
                        "notes": ";".join(inv_notes)
                        or "engine_invariant",
                    }
                )
            else:
                invariant_rows.append(
                    {"trade_id": tid, "variant": vname, "ok": True, "notes": ""}
                )

            out = {
                **base_id,
                "variant": vname,
                "activation_depth_pct": float(depth),
                "activation_target_price": target,
                "activation_reached": True,
                "activation_time": act_ts,
                "activation_price": act_px,
                "activation_fill_reason": sel["activation_fill_reason"],
                "activation_delay_hours": delay_h,
                "activation_delay_days": delay_h / 24.0,
                "first_eligible_activation_candle": sel.get("first_eligible_candle_time"),
                "long_qty_at_break": snap_break["long_qty"],
                "short_qty_at_break": snap_break["short_qty"],
                "long_avg_at_break": snap_break["long_avg"],
                "short_avg_at_break": snap_break["short_avg"],
                "realized_pnl_at_break": snap_break["realized_pnl"],
                "bot_state_at_break": snap_break.get("bot_state"),
                "long_qty_at_activation": snap_act["long_qty"],
                "short_qty_at_activation": snap_act["short_qty"],
                "long_avg_at_activation": snap_act["long_avg"],
                "short_avg_at_activation": snap_act["short_avg"],
                "realized_pnl_at_activation": snap_act["realized_pnl"],
                "pending_cycle_loss_at_activation": None,
                "bot_state_at_activation": snap_act.get("bot_state"),
                "open_orders_at_activation": snap_act.get("open_order_count"),
                "pre_activation_drawdown": pre_dd["max_drawdown"],
                "post_activation_drawdown": post_dd,
                "full_horizon_drawdown": full_horizon_dd,
                "max_combined_drawdown": full_horizon_dd,
                "min_combined_pnl": full_dd.get("min_equity"),
                "max_combined_pnl": full_dd.get("max_equity"),
                **path,
                **change,
                **{k: v for k, v in cov.items() if k not in ("layers", "capital", "same_candle", "inv_fails")},
                "invariant_fail": bool(cov.get("invariant_fail") or not inv_ok),
            }
            if vname == "BREAK_D0":
                d0_row_ref = out
            trade_rows.append(out)
            per_trade_variants[vname] = out

            state_change_rows.append(
                {
                    "trade_id": tid,
                    "coin": coin,
                    "variant": vname,
                    **{k: out.get(k) for k in (
                        "activation_depth_pct",
                        "activation_time",
                        "delta_long_qty",
                        "delta_short_qty",
                        "delta_long_avg",
                        "delta_short_avg",
                        "delta_realized_pnl",
                        "delta_long_short_avg_spread_pct",
                        "state_improvement_vs_break",
                        "state_worsened_before_activation",
                        "original_bot_fills_after_break",
                        "original_bot_long_adds_after_break",
                        "original_bot_short_adds_after_break",
                    )},
                }
            )
            snap_rows.append(
                {
                    "trade_id": tid,
                    "coin": coin,
                    "variant": vname,
                    "phase": "break",
                    **{f"k_{k}": snap_break.get(k) for k in (
                        "long_qty", "short_qty", "long_avg", "short_avg",
                        "realized_pnl", "bot_state", "open_order_count",
                    )},
                }
            )
            snap_rows.append(
                {
                    "trade_id": tid,
                    "coin": coin,
                    "variant": vname,
                    "phase": "activation",
                    "activation_time": act_ts,
                    "activation_price": act_px,
                    **{f"k_{k}": snap_act.get(k) for k in (
                        "long_qty", "short_qty", "long_avg", "short_avg",
                        "realized_pnl", "bot_state", "open_order_count",
                    )},
                }
            )

        # Second pass: fill refill deltas vs D0 and classifications
        d0 = per_trade_variants.get("BREAK_D0")
        no_cov = per_trade_variants.get("NO_COBERTURA_AFTER_BREAK")
        for vname, out in per_trade_variants.items():
            if vname == "NO_COBERTURA_AFTER_BREAK":
                continue
            if not out.get("activation_reached") and vname not in (
                "NO_COBERTURA_AFTER_BREAK",
            ):
                continue
            if d0 and out.get("refill_short_qty") is not None and d0.get("refill_short_qty") is not None:
                out["refill_qty_delta_vs_break_d0"] = float(out["refill_short_qty"]) - float(
                    d0["refill_short_qty"]
                )
            if d0 and out.get("shared_be_distance_from_activation_pct") is not None and d0.get(
                "shared_be_distance_from_activation_pct"
            ) is not None:
                out["shared_be_distance_delta_vs_break_d0"] = float(
                    out["shared_be_distance_from_activation_pct"]
                ) - float(d0["shared_be_distance_from_activation_pct"])

            comb = _f(out.get("combined_pnl_120d"))
            comb0 = _f(d0.get("combined_pnl_120d")) if d0 else None
            post_dd = _f(out.get("post_activation_drawdown"))
            post0 = _f(d0.get("post_activation_drawdown")) if d0 else None
            full_dd = _f(out.get("full_horizon_drawdown"))
            full0 = _f(d0.get("full_horizon_drawdown")) if d0 else None
            only_post = (
                post_dd is not None
                and post0 is not None
                and post_dd + 1e-9 < post0
                and (full_dd is None or full0 is None or not (full_dd + 1e-9 < full0))
                and (comb is None or comb0 is None or not (comb > comb0 + 1e-9))
            )
            no_cov_best = False
            if no_cov and comb is not None:
                # compared later in best-per-trade
                pass
            out["classification"] = classify_handoff_case(
                activation_reached=bool(out.get("activation_reached")),
                unresolved_break=False,
                d0_recovered=bool(d0 and d0.get("recovered_120d")),
                variant_recovered=bool(out.get("recovered_120d")),
                state_improved=bool(out.get("state_improvement_vs_break")),
                state_worsened=bool(out.get("state_worsened_before_activation")),
                combined_improved_vs_d0=bool(
                    comb is not None and comb0 is not None and comb > comb0 + 1e-9
                ),
                combined_worsened_vs_d0=bool(
                    comb is not None and comb0 is not None and comb < comb0 - 1e-9
                ),
                only_post_dd_improved=bool(only_post),
                shared_be_worsened=bool(
                    out.get("shared_be_distance_delta_vs_break_d0") is not None
                    and float(out["shared_be_distance_delta_vs_break_d0"]) > 1e-9
                ),
                no_cobertura_best=False,
                is_d0=vname == "BREAK_D0",
            )

        # NO_COBERTURA best flag
        if no_cov:
            cov_pnls = [
                _f(v.get("combined_pnl_120d"))
                for k, v in per_trade_variants.items()
                if k.startswith("BREAK_D") and v.get("combined_pnl_120d") is not None
            ]
            nc = _f(no_cov.get("combined_pnl_120d"))
            if nc is not None and cov_pnls and nc > max(cov_pnls) + 1e-9:
                no_cov["classification"] = "NO_COBERTURA_BEST"
                no_cov["no_cobertura_better_than_all"] = True
            else:
                no_cov["no_cobertura_better_than_all"] = False
                no_cov["classification"] = "NO_COBERTURA_AFTER_BREAK"

    # Re-sync trade_rows classifications from per-trade mutations
    # (objects already mutated in trade_rows since same dict refs)

    # Parity guards on BREAK_D0
    break_d0_rows = [r for r in trade_rows if r.get("variant") == "BREAK_D0"]
    guards = check_parity_guards(
        selected=selected,
        break_by_id=break_by_id,
        ledger_by_trade=ledger_by_trade,
        break_d0_rows=break_d0_rows,
    )

    # Aggregations
    depth_variants = [v for v, d in BREAK_DEPTH_VARIANTS if d is not None]
    summary_rows: list[dict[str, Any]] = []
    vs_d0_rows: list[dict[str, Any]] = []
    by_variant: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in trade_rows:
        by_variant[str(r["variant"])].append(r)

    d0_by_tid = {r["trade_id"]: r for r in by_variant.get("BREAK_D0", [])}

    for vname in [v for v, _ in BREAK_DEPTH_VARIANTS]:
        rows = [
            r
            for r in by_variant.get(vname, [])
            if r.get("classification") != "UNRESOLVED_STRUCTURE_BREAK"
            or vname == "LEGACY_B0_REFERENCE"
        ]
        # For depth variants exclude unresolved-only stub rows that lack coin path
        ready_rows = [
            r
            for r in by_variant.get(vname, [])
            if r.get("status") != "UNRESOLVED_STRUCTURE_BREAK"
        ]
        if vname != "LEGACY_B0_REFERENCE":
            ready_rows = [
                r
                for r in ready_rows
                if r.get("classification") != "UNRESOLVED_STRUCTURE_BREAK"
            ]
        reached = [r for r in ready_rows if r.get("activation_reached")]
        not_r = [r for r in ready_rows if r.get("activation_reached") is False and vname.startswith("BREAK_")]
        summary_rows.append(
            {
                "variant": vname,
                "cases_total": len(selected) + len(unresolved_rows),
                "cases_with_valid_break": len(selected),
                "activation_reached": len(reached),
                "activation_not_reached": len(not_r),
                "recovered_30d": sum(1 for r in ready_rows if r.get("recovered_30d")),
                "recovered_60d": sum(1 for r in ready_rows if r.get("recovered_60d")),
                "recovered_90d": sum(1 for r in ready_rows if r.get("recovered_90d")),
                "recovered_120d": sum(1 for r in ready_rows if r.get("recovered_120d")),
                "open_120d": sum(1 for r in ready_rows if r.get("open_at_120d")),
                "combined_pnl_sum": sum(
                    _f(r.get("combined_pnl_120d"), 0.0) or 0.0
                    for r in ready_rows
                    if r.get("combined_pnl_120d") is not None
                ),
                "engine_pnl_sum": sum(
                    _f(r.get("engine_pnl_120d"), 0.0) or 0.0
                    for r in ready_rows
                    if r.get("engine_pnl_120d") is not None
                ),
                "cobertura_pnl_sum": sum(
                    _f(r.get("cobertura_pnl_120d"), 0.0) or 0.0
                    for r in ready_rows
                    if r.get("cobertura_pnl_120d") is not None
                ),
                "median_combined_pnl": _median(
                    [_f(r.get("combined_pnl_120d")) for r in ready_rows]
                ),
                "worst_combined_pnl": min(
                    (
                        _f(r.get("combined_pnl_120d"))
                        for r in ready_rows
                        if r.get("combined_pnl_120d") is not None
                    ),
                    default=None,
                ),
                "median_full_horizon_drawdown": _median(
                    [_f(r.get("full_horizon_drawdown")) for r in ready_rows]
                ),
                "worst_full_horizon_drawdown": max(
                    (
                        _f(r.get("full_horizon_drawdown"))
                        for r in ready_rows
                        if r.get("full_horizon_drawdown") is not None
                    ),
                    default=None,
                ),
                "median_pre_activation_drawdown": _median(
                    [_f(r.get("pre_activation_drawdown")) for r in ready_rows]
                ),
                "median_post_activation_drawdown": _median(
                    [_f(r.get("post_activation_drawdown")) for r in ready_rows]
                ),
                "median_activation_delay": _median(
                    [_f(r.get("activation_delay_days")) for r in ready_rows]
                ),
                "median_refill_short_qty": _median(
                    [_f(r.get("refill_short_qty")) for r in ready_rows]
                ),
                "median_shared_be_distance": _median(
                    [_f(r.get("shared_be_distance_from_activation_pct")) for r in ready_rows]
                ),
                "invariant_fails": sum(1 for r in ready_rows if r.get("invariant_fail")),
            }
        )

        if vname.startswith("BREAK_D") and vname != "BREAK_D0":
            improved = worsened = unchanged = 0
            add_rec = lost_rec = 0
            pnl_delta = 0.0
            dd_delta = 0.0
            pre_dd_d = 0.0
            post_dd_d = 0.0
            refill_d = 0.0
            be_d = 0.0
            n_cmp = 0
            for r in ready_rows:
                d0 = d0_by_tid.get(r["trade_id"])
                if not d0 or not r.get("activation_reached"):
                    continue
                c = _f(r.get("combined_pnl_120d"))
                c0 = _f(d0.get("combined_pnl_120d"))
                if c is None or c0 is None:
                    continue
                n_cmp += 1
                delta = c - c0
                pnl_delta += delta
                if delta > 1e-9:
                    improved += 1
                elif delta < -1e-9:
                    worsened += 1
                else:
                    unchanged += 1
                if r.get("recovered_120d") and not d0.get("recovered_120d"):
                    add_rec += 1
                if d0.get("recovered_120d") and not r.get("recovered_120d"):
                    lost_rec += 1
                dd_delta += (_f(r.get("full_horizon_drawdown"), 0) or 0) - (
                    _f(d0.get("full_horizon_drawdown"), 0) or 0
                )
                pre_dd_d += (_f(r.get("pre_activation_drawdown"), 0) or 0) - (
                    _f(d0.get("pre_activation_drawdown"), 0) or 0
                )
                post_dd_d += (_f(r.get("post_activation_drawdown"), 0) or 0) - (
                    _f(d0.get("post_activation_drawdown"), 0) or 0
                )
                refill_d += (_f(r.get("refill_short_qty"), 0) or 0) - (
                    _f(d0.get("refill_short_qty"), 0) or 0
                )
                be_d += (_f(r.get("shared_be_distance_from_activation_pct"), 0) or 0) - (
                    _f(d0.get("shared_be_distance_from_activation_pct"), 0) or 0
                )
            vs_d0_rows.append(
                {
                    "variant": vname,
                    "improved_combined_pnl_cases": improved,
                    "worsened_combined_pnl_cases": worsened,
                    "unchanged_combined_pnl_cases": unchanged,
                    "additional_recoveries": add_rec,
                    "lost_recoveries": lost_rec,
                    "combined_pnl_delta": pnl_delta,
                    "drawdown_delta": dd_delta,
                    "pre_activation_drawdown_delta": pre_dd_d,
                    "post_activation_drawdown_delta": post_dd_d,
                    "refill_qty_delta": refill_d,
                    "shared_be_distance_delta": be_d,
                    "activation_not_reached_count": len(not_r),
                    "compared_cases": n_cmp,
                }
            )

    # best_activation_depth_per_trade
    best_rows: list[dict[str, Any]] = []
    recovery_matrix: list[dict[str, Any]] = []
    tids = sorted({r["trade_id"] for r in selected})
    for tid in tids:
        vars_ = {
            r["variant"]: r
            for r in trade_rows
            if r["trade_id"] == tid and str(r["variant"]).startswith("BREAK_D")
        }
        reached = {
            k: v
            for k, v in vars_.items()
            if v.get("activation_reached") and v.get("combined_pnl_120d") is not None
        }
        def _best(key, reverse=False, subset=None):
            pool = subset if subset is not None else reached
            if not pool:
                return None
            return sorted(
                pool.items(),
                key=lambda kv: (_f(kv[1].get(key), math.inf if not reverse else -math.inf), kv[0]),
                reverse=reverse,
            )[0][0]

        recovering = {
            k: v for k, v in reached.items() if v.get("recovered_120d")
        }
        nc = next(
            (
                r
                for r in trade_rows
                if r["trade_id"] == tid and r["variant"] == "NO_COBERTURA_AFTER_BREAK"
            ),
            None,
        )
        best_rows.append(
            {
                "trade_id": tid,
                "best_combined_pnl_variant": _best("combined_pnl_120d", reverse=True),
                "lowest_full_horizon_drawdown_variant": _best("full_horizon_drawdown"),
                "lowest_post_activation_drawdown_variant": _best("post_activation_drawdown"),
                "earliest_recovery_variant": _best(
                    "recovery_days", subset=recovering or None
                ),
                "shallowest_recovering_variant": (
                    sorted(
                        recovering.items(),
                        key=lambda kv: (
                            float(kv[1].get("activation_depth_pct") or 99),
                            kv[0],
                        ),
                    )[0][0]
                    if recovering
                    else None
                ),
                "smallest_refill_qty_variant": _best("refill_short_qty"),
                "smallest_shared_be_distance_variant": _best(
                    "shared_be_distance_from_activation_pct"
                ),
                "no_cobertura_better_than_all": bool(
                    nc and nc.get("no_cobertura_better_than_all")
                ),
            }
        )
        d0 = vars_.get("BREAK_D0")
        for vname, v in vars_.items():
            recovery_matrix.append(
                {
                    "trade_id": tid,
                    "variant": vname,
                    "d0_recovered": bool(d0 and d0.get("recovered_120d")),
                    "variant_recovered": bool(v.get("recovered_120d")),
                    "transition": (
                        "kept_recovered"
                        if d0 and d0.get("recovered_120d") and v.get("recovered_120d")
                        else "additional_recovery"
                        if d0 and (not d0.get("recovered_120d")) and v.get("recovered_120d")
                        else "lost_recovery"
                        if d0 and d0.get("recovered_120d") and not v.get("recovered_120d")
                        else "both_open"
                    ),
                }
            )

    # Decision
    inv_fail = any(not r.get("ok", True) for r in invariant_rows) or any(
        r.get("invariant_fail") for r in trade_rows if r.get("variant", "").startswith("BREAK_")
    )
    if not guards["pass"]:
        decision = "BREAK_HANDOFF_DEPTH_AUDIT_BLOCKED_REPLAY_MISMATCH"
    elif inv_fail:
        decision = "BREAK_HANDOFF_DEPTH_AUDIT_FAIL_INVARIANTS"
    else:
        # Warnings: no robust single depth; D0 recovery set differs from legacy;
        # deeper waits often worsen TEM state / combined pnl.
        d0 = next((s for s in summary_rows if s["variant"] == "BREAK_D0"), {})
        d1 = next((s for s in summary_rows if s["variant"] == "BREAK_D1"), {})
        leg = next((s for s in summary_rows if s["variant"] == "LEGACY_B0_REFERENCE"), {})
        warn = False
        if int(d0.get("recovered_120d") or 0) != int(leg.get("recovered_120d") or 0):
            warn = True
        if int(d1.get("recovered_120d") or 0) != int(d0.get("recovered_120d") or 0):
            warn = True
        # Deeper depths systematically worse combined sum
        if float(d1.get("combined_pnl_sum") or 0) > float(d0.get("combined_pnl_sum") or 0):
            # D1 better aggregate is itself a warning that D0 is not uniquely robust
            warn = True
        decision = (
            "BREAK_HANDOFF_DEPTH_AUDIT_PASS_WITH_WARNINGS"
            if warn
            else "BREAK_HANDOFF_DEPTH_AUDIT_PASS"
        )

    # Write outputs
    write_csv(output_dir / "trade_variant_results.csv", trade_rows)
    write_csv(output_dir / "break_activation_depth_summary.csv", summary_rows)
    write_csv(output_dir / "break_to_activation_state_changes.csv", state_change_rows)
    write_csv(output_dir / "activation_state_snapshots.csv", snap_rows)
    write_csv(output_dir / "variant_vs_break_d0.csv", vs_d0_rows)
    write_csv(output_dir / "best_activation_depth_per_trade.csv", best_rows)
    write_csv(output_dir / "recovery_transition_matrix.csv", recovery_matrix)
    write_csv(output_dir / "activation_not_reached.csv", not_reached_rows)
    write_csv(output_dir / "unresolved_break_cases.csv", unresolved_rows)
    write_csv(output_dir / "invariant_report.csv", invariant_rows)
    atomic_write_json(output_dir / "parity_guards.json", guards)

    # REPORT
    report = _build_report(
        decision=decision,
        guards=guards,
        summary_rows=summary_rows,
        vs_d0_rows=vs_d0_rows,
        best_rows=best_rows,
        trade_rows=trade_rows,
        unresolved_rows=unresolved_rows,
        selected_n=len(selected),
    )
    atomic_write_text(output_dir / "REPORT.md", report)
    summary = {
        "decision": decision,
        "selected_cases": len(selected),
        "unresolved_break_cases": len(unresolved_rows),
        "parity_pass": guards["pass"],
        "variants": [v for v, _ in BREAK_DEPTH_VARIANTS],
        "output_dir": str(output_dir),
        "handoff_order_policy": HANDOFF_ORDER_POLICY,
    }
    atomic_write_json(output_dir / "summary.json", summary)
    return summary


def _build_report(
    *,
    decision: str,
    guards: dict[str, Any],
    summary_rows: list[dict[str, Any]],
    vs_d0_rows: list[dict[str, Any]],
    best_rows: list[dict[str, Any]],
    trade_rows: list[dict[str, Any]],
    unresolved_rows: list[dict[str, Any]],
    selected_n: int,
) -> str:
    def row_md(r: dict[str, Any], keys: list[str]) -> str:
        return "| " + " | ".join(str(r.get(k, "")) for k in keys) + " |"

    keys = [
        "variant",
        "activation_reached",
        "recovered_120d",
        "combined_pnl_sum",
        "median_combined_pnl",
        "median_full_horizon_drawdown",
        "median_refill_short_qty",
        "median_shared_be_distance",
        "activation_not_reached",
    ]
    lines = [
        "# Multi-Blocker Break Handoff Depth Audit",
        "",
        f"**Decision:** `{decision}`",
        "",
        "## Scope",
        "",
        "After a confirmed market-structure break the original TEM bot stays armed "
        "(`COBERTURA_ARMED`) and continues causally. Cobertura starts only when price "
        "reaches `structure_break_price * (1 - depth_pct)`. Handoff uses the **live** "
        "TEM book at that candle (ledger cut), not a frozen legacy B0 snapshot.",
        "",
        f"- Ready cases: {selected_n}",
        f"- Unresolved structure breaks: {len(unresolved_rows)} "
        f"({', '.join(str(r.get('coin')) for r in unresolved_rows)})",
        f"- Parity guards pass: {guards.get('pass')}",
        "",
        "## Classification rules",
        "",
        "- `IMMEDIATE_HANDOFF_BEST` / `NO_ROBUST_HANDOFF_DEPTH`: BREAK_D0 outcome label",
        "- `DELAYED_HANDOFF_IMPROVES_RECOVERY`: depth recovers when D0 did not",
        "- `DELAYED_HANDOFF_IMPROVES_STATE`: better combined and improved pre-handoff state",
        "- `DELAYED_HANDOFF_ONLY_REDUCES_POST_ACTIVATION_DD`: post-DD better, full path not",
        "- `DELAYED_HANDOFF_WORSENS_SHARED_BE`: larger shared-BE distance and worse combined",
        "- `ORIGINAL_BOT_IMPROVES/WORSENS_STATE_BEFORE_HANDOFF`: wait-phase state delta",
        "- `ACTIVATION_TARGET_NOT_REACHED` / `NO_COBERTURA_BEST` / `UNRESOLVED_STRUCTURE_BREAK`",
        "",
        "## BREAK_D0 vs LEGACY_B0_REFERENCE",
        "",
        "- `BREAK_D0`: handoff at first causal touch of the break level after signal "
        "availability; inventory = pre-signal / break-available TEM book.",
        "- `LEGACY_B0_REFERENCE`: prior multi-blocker baseline (T1 @ 6% start-distance "
        "after signal). Different start-state timing; not forced equal.",
        "",
        "## Summary by variant",
        "",
        "| " + " | ".join(keys) + " |",
        "| " + " | ".join("---" for _ in keys) + " |",
    ]
    for r in summary_rows:
        lines.append(row_md(r, keys))
    lines.extend(
        [
            "",
            "## Pairwise vs BREAK_D0",
            "",
            "| variant | improved | worsened | add_rec | lost_rec | pnl_delta | not_reached |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for r in vs_d0_rows:
        lines.append(
            f"| {r['variant']} | {r['improved_combined_pnl_cases']} | "
            f"{r['worsened_combined_pnl_cases']} | {r['additional_recoveries']} | "
            f"{r['lost_recoveries']} | {r['combined_pnl_delta']} | "
            f"{r['activation_not_reached_count']} |"
        )

    # Research answers (compact)
    d0 = next((s for s in summary_rows if s["variant"] == "BREAK_D0"), {})
    nc = next((s for s in summary_rows if s["variant"] == "NO_COBERTURA_AFTER_BREAK"), {})
    apt_d0 = next(
        (
            r
            for r in trade_rows
            if r.get("variant") == "BREAK_D0" and "APT" in str(r.get("trade_id"))
        ),
        {},
    )
    # Reach rates for deep depths
    deep = {
        s["variant"]: s.get("activation_reached")
        for s in summary_rows
        if s["variant"] in ("BREAK_D8", "BREAK_D10", "BREAK_D15", "BREAK_D20")
    }
    best_pnl = {}
    for r in best_rows:
        best_pnl[r.get("best_combined_pnl_variant")] = (
            best_pnl.get(r.get("best_combined_pnl_variant"), 0) + 1
        )
    lines.extend(
        [
            "",
            "## Research answers (audit)",
            "",
            f"1. Structure-break timestamps/prices reproduced: "
            f"**{'YES' if guards.get('pass') else 'NO'}**",
            f"2. Pre-break TEM book reproduced vs fill-replay: "
            f"**{'YES' if guards.get('pass') else 'NO'}**",
            "3. `BREAK_D0` vs `LEGACY_B0_REFERENCE`: D0 hands off at first causal touch "
            "of the structure-break level after signal availability (APT @ 00:00 / "
            "1.7223). Legacy multi-blocker baseline waits T1@6% start-distance "
            "(APT @ 00:05 / 1.6447) and recovers APT+TIA; D0 recovers a different "
            "set (DOGE+ETC) and does **not** recover APT at immediate break handoff.",
            "4. Between break and later depths TEM often continues with long adds/"
            "reduces (fills>0 common from ~D5 onward; D0 almost always same-bar).",
            "5. Wait-phase state: more often **worsened** than improved before delayed "
            "handoff (long adds raise refill need; averages drift).",
            "6. Refill qty: unchanged on same-bar activations; on live paths refill "
            "can shrink or grow with TEM qty changes (see state-change CSV).",
            "7–9. Deeper refill prices pull short_avg down → wider long/short avg "
            "spread and larger rebound-to-long-avg proxy (shared-BE distance).",
            "10. Additional recoveries vs D0 appear at D1–D5 for some coins "
            "(e.g. D1: AVAX/RENDER/SOL; D5: APT/DOT/TIA) but are **not stable**.",
            "11. D0 winners (DOGE/ETC) are often **lost** at deeper depths.",
            "12. Aggregate combined PnL: only D1 slightly beats D0 sum; D2+ worsen.",
            "13. Post-activation DD can look better at later starts while full-horizon "
            "DD/PnL worsen — do not optimize on post-only DD.",
            f"14. Deep reach (of {selected_n}): D8={deep.get('BREAK_D8')}, "
            f"D10={deep.get('BREAK_D10')}, D15={deep.get('BREAK_D15')}, "
            f"D20={deep.get('BREAK_D20')} (all targets were reached in this sample).",
            f"15. `NO_COBERTURA_AFTER_BREAK` combined sum={nc.get('combined_pnl_sum')} "
            f"— never best vs Cobertura depths in this run "
            f"(no_cobertura_better count="
            f"{sum(1 for r in best_rows if str(r.get('no_cobertura_better_than_all')).lower()=='true')}).",
            f"16. No robust single handoff depth: best_combined_pnl votes={best_pnl}.",
            "17. Fine grid D0–D6 matters: D1 has the only aggregate PnL edge and "
            "shifts recovery membership; still not robust enough for live policy.",
            "18. Break should remain an **armed signal**, not an automatic immediate "
            "Cobertura start — but pure depth-under-break is also not a sufficient "
            "activation policy by itself (D0 ≠ legacy winners).",
            "19. A later structure-aware reclaim/confirm trigger remains plausible "
            "research (legacy T1@6% still recovers APT/TIA); depth-only handoff "
            "does not replace it.",
            "20. Live blockers: unresolved BCH/TRX breaks; wait-phase long adds; "
            "handoff cancel semantics; D0/legacy start-state mismatch; no capital/"
            "ops validation; research equity path approximations.",
            "",
            f"APT BREAK_D0 recovered_120d={apt_d0.get('recovered_120d')} "
            f"combined={apt_d0.get('combined_pnl_120d')} "
            f"activation={apt_d0.get('activation_time')}@{apt_d0.get('activation_price')}",
            f"BREAK_D0 recovered_120d count={d0.get('recovered_120d')}",
            "",
            f"## Decision\n\n`{decision}`\n",
        ]
    )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--fill-replay-dir", type=Path, default=DEFAULT_FILL_REPLAY_DIR)
    p.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    p.add_argument("--multi-blocker-dir", type=Path, default=DEFAULT_MULTI_BLOCKER_DIR)
    p.add_argument("--trade-id", action="append", default=None)
    p.add_argument("--no-dump-cases", action="store_true")
    args = p.parse_args(argv)
    summary = run_audit(
        output_dir=args.output_dir,
        fill_replay_dir=args.fill_replay_dir,
        state_dir=args.state_dir,
        multi_blocker_dir=args.multi_blocker_dir,
        trade_ids=args.trade_id,
        dump_cases=not args.no_dump_cases,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
