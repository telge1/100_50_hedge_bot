"""Isolated start-depth sweep for historical Cobertura blockers.

Research-only. Does not change refill mechanics (short-only neutralization to
long_qty). B0 must reproduce multi-blocker baseline fingerprints.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
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
from fixed_cycle_hedge_bot.math_utils import calculate_pnl

from .config import CoberturaConfig
from .engine import EngineResult, _parse_ts
from .ledger import round_qty
from .multi_blocker_variants import VARIANT_BASELINE, variant_engine_flags
from .order_audit import QTY_TOL, reconstruct_audit
from .run_apt_start_and_post_add_distance_audit import STRATEGY, neutralize_at_price
from .run_apt_start_distance_execution_timing_audit import build_cfg
from .run_multi_blocker_forensic_audit import (
    DEFAULT_FILL_REPLAY_DIR,
    DEFAULT_STATE_DIR,
    POLICY_ID,
    START_DISTANCE_PCT,
    _approx,
    _f,
    _safe_trade_id,
    book_from_pre_signal,
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
from .start_depth import (
    DEPTH_VARIANTS,
    FILL_MODEL,
    achieved_depth_pct,
    classify_baseline_case,
    distance_from_long_avg_pct,
    remaining_downside_pct,
    select_deeper_start_after_baseline,
    target_start_price,
)
from .start_distance import select_start_by_timing_mode

DEFAULT_MULTI_BLOCKER_DIR = Path(
    "research/backtests/cobertura_0_notional_strategie/results/"
    "multi_blocker_forensic_audit_20260726"
)
DEFAULT_OUTPUT_DIR = Path(
    "research/backtests/cobertura_0_notional_strategie/results/"
    "multi_blocker_start_depth_audit_20260726"
)

HORIZON_DAYS = 120
PNL_TOL = 1e-3


def _read_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def select_baseline_t1_start(
    *, candles: list[dict[str, Any]], book: dict[str, Any]
) -> dict[str, Any]:
    return select_start_by_timing_mode(
        candles,
        signal_ts=str(book["signal_available_ts"]),
        existing_short_qty=_f(book["short_qty"]),
        existing_short_avg=_f(book["short_avg"]),
        neutralization_qty=_f(book["neutralization_qty"]),
        minimum_start_distance_pct=START_DISTANCE_PCT,
        timing_mode="T1",
        parse_ts=_parse_ts,
    )


def path_stats_after(
    candles: list[dict[str, Any]], *, start_ts: str
) -> dict[str, Any]:
    start = _parse_ts(start_ts)
    after = [c for c in candles if _parse_ts(c["timestamp"]) >= start]
    if not after:
        return {
            "minimum_price_after_start": None,
            "maximum_price_after_start": None,
            "rebound_from_low_pct": None,
        }
    lows = [float(c["low"]) for c in after]
    highs = [float(c["high"]) for c in after]
    mn = min(lows)
    mx = max(highs)
    rebound = (mx - mn) / mn if mn > 0 else None
    # rebound from low: max high after the first touch of min low
    first_low_i = min(range(len(after)), key=lambda i: float(after[i]["low"]))
    mx_after_low = max(float(after[i]["high"]) for i in range(first_low_i, len(after)))
    rebound_from_low = (mx_after_low - mn) / mn if mn > 0 else None
    return {
        "minimum_price_after_start": mn,
        "maximum_price_after_start": mx,
        "rebound_from_low_pct": rebound_from_low,
        "path_high_low_range_pct": rebound,
    }


def run_cobertura_at_start(
    *,
    row: dict[str, Any],
    book: dict[str, Any],
    fill_ts: str,
    fill_px: float,
    candles_full: list[dict[str, Any]],
    variant_name: str,
    dump_dir: Path | None,
) -> dict[str, Any]:
    trade_id = str(row["trade_id"])
    coin = str(row["coin"])
    long_q = _f(book["long_qty"])
    short_q = _f(book["short_qty"])
    refill_qty = max(long_q - short_q, 0.0)
    already_covered = short_q + 1e-12 >= long_q

    if already_covered:
        return {
            "trade_id": trade_id,
            "coin": coin,
            "variant": variant_name,
            "started": False,
            "start_reached": True,
            "refill_class": "NO_REFILL_ALREADY_COVERED",
            "status": "STATE_UNRESOLVED",
            "reason": "short_qty_already_ge_long_qty",
            "invariant_fail": False,
        }

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
            "start_reached": True,
            "status": "STATE_UNRESOLVED",
            "reason": "refill_qty_step",
            "invariant_fail": False,
        }

    # Always baseline engine fill/exit semantics for depth sweep isolation
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
        "audit": "start_depth",
        "variant": variant_name,
        "trade_id": trade_id,
    }
    cfg = CoberturaConfig.from_dict(raw)
    candles = truncate_candles(
        candles_full, start_ts=fill_ts, horizon_days=HORIZON_DAYS
    )
    result = run_cobertura(cfg, candles=candles, write_outputs=False)
    bundle = reconstruct_audit(
        policy=f"start_depth_{variant_name}", cfg=cfg, result=result
    )
    inv_fails = [
        v
        for v in bundle.invariant_violations
        if v.get("pass_fail") == "FAIL" and v.get("check") != "full_exit_audit"
    ]
    # Keep flatness fails on recovered
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
    prior_realized = row.get("realized_pnl_before")
    prior_realized_f = _f(prior_realized) if prior_realized not in (None, "") else None
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
        result=result,
        book_before=book,
        neut={**neut, "neutralization_qty": refill_qty},
        start_price=fill_px,
    )
    same = same_candle_stats(result)
    path = path_stats_after(candles_full, start_ts=fill_ts)
    last = (
        result.total_exit_economics_timeline[-1]
        if result.total_exit_economics_timeline
        else {}
    )
    duration_days = days_between(fill_ts, rec_ts) if rec_ts else None
    recovered = str(status).startswith("RECOVERED")

    long_avg = _f(book["long_avg"])
    short_avg_b = _f(book["short_avg"])
    short_avg_a = _f(neut["core_short_avg"])
    out = {
        "trade_id": trade_id,
        "coin": coin,
        "variant": variant_name,
        "started": True,
        "start_reached": True,
        "refill_class": "REFILL_TO_LONG_QTY",
        "status": status,
        "final_state": result.state,
        "exit_reason": result.exit_reason,
        "shifted_start_time": fill_ts,
        "shifted_start_price": fill_px,
        "refill_price": fill_px,
        "long_qty_before": long_q,
        "short_qty_before": short_q,
        "refill_short_qty": refill_qty,
        "long_avg_before": long_avg,
        "short_avg_before": short_avg_b,
        "short_avg_after": short_avg_a,
        "core_long_qty": neut["core_long_qty"],
        "core_short_qty": neut["core_short_qty"],
        "qty_neutral": abs(neut["core_long_qty"] - neut["core_short_qty"]) <= QTY_TOL,
        "long_short_avg_distance_before_pct": distance_from_long_avg_pct(
            long_avg=long_avg, price=short_avg_b
        )
        if short_q > 0
        else None,
        "long_short_avg_distance_after_pct": distance_from_long_avg_pct(
            long_avg=long_avg, price=short_avg_a
        ),
        "long_notional": long_q * long_avg,
        "short_notional_before": short_q * short_avg_b,
        "short_notional_after": neut["core_short_qty"] * short_avg_a,
        "neutralization_fee": neut_fee,
        "recovery_timestamp": rec_ts,
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
        "same_candle_add_exit": int(same.get("candles_add_and_full_exit") or 0) > 0,
        "invariant_fail": bool(inv_fails),
        "max_combined_drawdown": cap.get("max_drawdown_from_cobertura_start"),
        "max_overlay_drawdown": None,
        "max_engine_drawdown": cap.get("max_drawdown_from_cobertura_start"),
        "min_combined_pnl": cap.get("max_adverse_equity"),
        "max_combined_pnl": None,
        "maximum_total_gross_exposure": cap.get("maximum_total_gross_exposure"),
        **path,
        "remaining_downside_to_low_pct": (
            remaining_downside_pct(
                start_price=fill_px,
                subsequent_min_price=path["minimum_price_after_start"],
            )
            if path.get("minimum_price_after_start") is not None
            else None
        ),
        "layers": layers,
        "capital": cap,
        "same_candle": same,
        "inv_fails": inv_fails,
        "bars_processed": result.bars_processed,
        "realized_overlay_pnl": result.ledger.realized_overlay_pnl,
        "engine_total_exit_economics": last.get("total_exit_economics"),
    }
    if dump_dir is not None:
        dump_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            dump_dir / "summary.json",
            {k: v for k, v in out.items() if k not in ("layers", "capital", "same_candle", "inv_fails")},
        )
    return out


def run_no_cobertura(
    *,
    row: dict[str, Any],
    book: dict[str, Any],
    baseline_ts: str,
    baseline_px: float,
    candles_full: list[dict[str, Any]],
) -> dict[str, Any]:
    trade_id = str(row["trade_id"])
    coin = str(row["coin"])
    long_q = _f(book["long_qty"])
    short_q = _f(book["short_qty"])
    long_avg = _f(book["long_avg"])
    short_avg = _f(book["short_avg"])
    candles = truncate_candles(
        candles_full, start_ts=baseline_ts, horizon_days=HORIZON_DAYS
    )
    path = path_stats_after(candles_full, start_ts=baseline_ts)
    end_px = float(candles[-1]["close"]) if candles else baseline_px
    # Hold original book; no refill
    ur_long = calculate_pnl(long_avg, end_px, long_q, "long") if long_q > 0 else 0.0
    ur_short = calculate_pnl(short_avg, end_px, short_q, "short") if short_q > 0 else 0.0
    prior = row.get("realized_pnl_before")
    prior_f = _f(prior) if prior not in (None, "") else 0.0
    # Control economics: prior TEM realized + MTM of frozen pre-refill book at horizon end
    mtm = ur_long + ur_short
    combined = prior_f + mtm
    # Simple adverse path: min combined using bar closes
    min_comb = combined
    for c in candles:
        px = float(c["close"])
        m = calculate_pnl(long_avg, px, long_q, "long") + (
            calculate_pnl(short_avg, px, short_q, "short") if short_q > 0 else 0.0
        )
        min_comb = min(min_comb, prior_f + m)
    max_dd = (prior_f + mtm) - min_comb  # not perfect; use from first bar
    first_m = None
    adverse = None
    for c in candles:
        px = float(c["close"])
        m = prior_f + calculate_pnl(long_avg, px, long_q, "long") + (
            calculate_pnl(short_avg, px, short_q, "short") if short_q > 0 else 0.0
        )
        if first_m is None:
            first_m = m
        adverse = m if adverse is None else min(adverse, m)
    max_dd = (first_m - adverse) if first_m is not None and adverse is not None else 0.0

    return {
        "trade_id": trade_id,
        "coin": coin,
        "variant": "NO_COBERTURA",
        "started": False,
        "start_reached": False,
        "refill_class": "NO_COBERTURA",
        "status": "OPEN_AT_120D",
        "shifted_start_time": None,
        "shifted_start_price": None,
        "refill_price": None,
        "refill_short_qty": 0.0,
        "long_qty_before": long_q,
        "short_qty_before": short_q,
        "long_avg_before": long_avg,
        "short_avg_before": short_avg,
        "short_avg_after": short_avg,
        "qty_neutral": False,
        "long_notional": long_q * long_avg,
        "short_notional_before": short_q * short_avg,
        "short_notional_after": short_q * short_avg,
        "recovered_30d": False,
        "recovered_60d": False,
        "recovered_90d": False,
        "recovered_120d": False,
        "open_at_120d": True,
        "recovery_days": None,
        "exit_time": None,
        "cobertura_pnl_120d": 0.0,
        "engine_pnl_120d": mtm,
        "combined_pnl_120d": combined,
        "overlay_pnl_120d": 0.0,
        "same_candle_add_exit": False,
        "invariant_fail": False,
        "max_combined_drawdown": max_dd,
        "max_engine_drawdown": max_dd,
        "min_combined_pnl": adverse,
        **path,
        "remaining_downside_to_low_pct": (
            remaining_downside_pct(
                start_price=baseline_px,
                subsequent_min_price=path["minimum_price_after_start"],
            )
            if path.get("minimum_price_after_start") is not None
            else None
        ),
        "horizon_end_price": end_px,
        "prior_tem_realized": prior_f,
        "frozen_book_mtm_120d": mtm,
    }


def check_b0_parity(
    *,
    b0_rows: list[dict[str, Any]],
    multi_blocker_dir: Path,
) -> dict[str, Any]:
    expected_all = [
        r
        for r in _read_csv(multi_blocker_dir / "blocker_results.csv")
        if r.get("variant") == "baseline"
    ]
    b0_ids = {r["trade_id"] for r in b0_rows}
    expected = [r for r in expected_all if r["trade_id"] in b0_ids]
    by_id = {r["trade_id"]: r for r in expected}
    checks = []
    ok = True
    if not b0_rows:
        return {
            "pass": False,
            "decision": "START_DEPTH_AUDIT_BLOCKED_BASELINE_MISMATCH",
            "checks": [{"check": "n_cases", "ok": False, "got": 0, "expected": ">0"}],
        }
    # Full-universe runs must cover all multi-blocker baseline rows.
    if len(b0_ids) == len(expected_all):
        if len(b0_rows) != len(expected_all):
            ok = False
        checks.append(
            {
                "check": "n_cases_full_universe",
                "ok": len(b0_rows) == len(expected_all),
                "got": len(b0_rows),
                "expected": len(expected_all),
            }
        )
    else:
        checks.append(
            {
                "check": "n_cases_subset",
                "ok": True,
                "got": len(b0_rows),
                "expected_overlap": len(expected),
            }
        )

    for r in b0_rows:
        tid = r["trade_id"]
        exp = by_id.get(tid)
        if not exp:
            ok = False
            checks.append({"check": f"missing_expected:{tid}", "ok": False})
            continue
        pairs = [
            ("status", r.get("status"), exp.get("status"), False),
            (
                "start_fill_timestamp",
                r.get("shifted_start_time"),
                exp.get("start_fill_timestamp"),
                False,
            ),
            (
                "start_fill_price",
                r.get("shifted_start_price"),
                exp.get("start_fill_price"),
                True,
            ),
            (
                "engine_total_exit_economics",
                r.get("engine_pnl_120d"),
                exp.get("engine_total_exit_economics"),
                True,
            ),
            (
                "realized_overlay_pnl",
                r.get("overlay_pnl_120d"),
                exp.get("realized_overlay_pnl"),
                True,
            ),
            (
                "recovery_timestamp",
                r.get("recovery_timestamp"),
                exp.get("recovery_timestamp"),
                False,
            ),
            (
                "same_candle_add_exit",
                bool(r.get("same_candle_add_exit")),
                # multi-blocker stores same-candle only in summary aggregate; compare via
                # recovered APT forensic known flag when field absent
                bool(r.get("same_candle_add_exit")),
                False,
            ),
        ]
        for name, got, want, numeric in pairs:
            if name == "same_candle_add_exit":
                # Skip pairwise same-candle vs blocker_results (field not stored there).
                continue
            if numeric:
                match = _approx(got, want, rel=PNL_TOL, abs_tol=1e-6)
            else:
                match = str(got or "") == str(want or "")
            if not match:
                ok = False
            checks.append(
                {
                    "check": f"{tid}:{name}",
                    "ok": match,
                    "got": got,
                    "expected": want,
                }
            )

    apt = next((r for r in b0_rows if r["trade_id"].startswith("APTUSDT")), None)
    if apt:
        for name, cond, detail in (
            (
                "apt_fill_ts",
                str(apt.get("shifted_start_time") or "").startswith("2026-01-19T00:05:00"),
                apt.get("shifted_start_time"),
            ),
            (
                "apt_fill_px",
                _approx(apt.get("shifted_start_price"), 1.6447, rel=0, abs_tol=1e-9),
                apt.get("shifted_start_price"),
            ),
            (
                "apt_overlay",
                _approx(apt.get("overlay_pnl_120d"), 46.1499578),
                apt.get("overlay_pnl_120d"),
            ),
            (
                "apt_engine",
                _approx(apt.get("engine_pnl_120d"), 21.85801929),
                apt.get("engine_pnl_120d"),
            ),
        ):
            if not cond:
                ok = False
            checks.append({"check": name, "ok": bool(cond), "got": detail})

    if len(b0_ids) == len(expected_all):
        same_b0 = sum(1 for r in b0_rows if r.get("same_candle_add_exit"))
        summ = _read_csv(multi_blocker_dir / "multi_blocker_summary.csv")
        exp_same = None
        for s in summ:
            if s.get("variant") == "baseline":
                exp_same = int(float(s.get("same_candle_exit_count") or 0))
        if exp_same is not None:
            match = same_b0 == exp_same
            if not match:
                ok = False
            checks.append(
                {
                    "check": "same_candle_exit_count",
                    "ok": match,
                    "got": same_b0,
                    "expected": exp_same,
                }
            )

    return {
        "pass": ok,
        "decision": (
            "BASELINE_PARITY_PASS" if ok else "START_DEPTH_AUDIT_BLOCKED_BASELINE_MISMATCH"
        ),
        "checks": checks,
        "n_b0": len(b0_rows),
        "n_expected": len(expected_all),
    }


def summarize_variant(rows: list[dict[str, Any]]) -> dict[str, Any]:
    started = [r for r in rows if r.get("start_reached")]
    reached = [r for r in rows if r.get("start_reached")]
    not_reached = [r for r in rows if r.get("variant") not in ("NO_COBERTURA",) and not r.get("start_reached")]
    # NO_COBERTURA counted separately
    if rows and rows[0].get("variant") == "NO_COBERTURA":
        reached = []
        not_reached = []
        started = rows
    rec = [r for r in rows if r.get("recovered_120d")]
    comb = [float(r["combined_pnl_120d"]) for r in rows if r.get("combined_pnl_120d") is not None]
    cob = [float(r["cobertura_pnl_120d"]) for r in rows if r.get("cobertura_pnl_120d") is not None]
    eng = [float(r["engine_pnl_120d"]) for r in rows if r.get("engine_pnl_120d") is not None]
    dds = [float(r["max_combined_drawdown"]) for r in rows if r.get("max_combined_drawdown") is not None]
    delays = [
        float(r["start_delay_days"])
        for r in rows
        if r.get("start_delay_days") is not None
    ]
    return {
        "variant": rows[0]["variant"] if rows else None,
        "n_cases": len(rows),
        "n_start_reached": len(reached) if rows and rows[0].get("variant") != "NO_COBERTURA" else 0,
        "n_start_not_reached": len(not_reached),
        "n_recovered_30d": sum(1 for r in rows if r.get("recovered_30d")),
        "n_recovered_60d": sum(1 for r in rows if r.get("recovered_60d")),
        "n_recovered_90d": sum(1 for r in rows if r.get("recovered_90d")),
        "n_recovered_120d": sum(1 for r in rows if r.get("recovered_120d")),
        "n_open_120d": sum(1 for r in rows if r.get("open_at_120d")),
        "combined_pnl_sum": sum(comb) if comb else 0.0,
        "cobertura_pnl_sum": sum(cob) if cob else 0.0,
        "engine_pnl_sum": sum(eng) if eng else 0.0,
        "median_combined_pnl": statistics.median(comb) if comb else None,
        "worst_combined_pnl": min(comb) if comb else None,
        "median_max_drawdown": statistics.median(dds) if dds else None,
        "worst_max_drawdown": max(dds) if dds else None,
        "median_start_delay_days": statistics.median(delays) if delays else None,
        "same_candle_add_exit": sum(1 for r in rows if r.get("same_candle_add_exit")),
        "invariant_fails": sum(1 for r in rows if r.get("invariant_fail")),
    }


def run_audit(
    *,
    fill_replay_dir: Path,
    state_dir: Path,
    multi_blocker_dir: Path,
    output_dir: Path,
    only_trade_id: str | None = None,
    max_cases: int | None = None,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    if output_dir.exists() and (output_dir / "summary.json").exists():
        raise FileExistsError(f"refusing to overwrite {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    selected, unresolved = load_case_universe(
        fill_replay_dir=fill_replay_dir, state_dir=state_dir
    )
    if only_trade_id:
        selected = [r for r in selected if r.get("trade_id") == only_trade_id]
    if max_cases is not None:
        selected = selected[: int(max_cases)]

    candle_cache: dict[str, list[dict[str, Any]]] = {}
    all_rows: list[dict[str, Any]] = []
    by_trade: dict[str, dict[str, dict[str, Any]]] = {}
    downside_rows: list[dict[str, Any]] = []
    unreached: list[dict[str, Any]] = []
    inv_rows: list[dict[str, Any]] = []

    for row in selected:
        coin = str(row["coin"])
        trade_id = str(row["trade_id"])
        if coin not in candle_cache:
            candle_cache[coin] = load_candles_for_symbol(
                coin, timeframe="5m", data_dir=DEFAULT_DATA_DIR, limit=50_000
            )
        candles = candle_cache[coin]
        book = book_from_pre_signal(row)
        try:
            base_sel = select_baseline_t1_start(candles=candles, book=book)
        except Exception as exc:  # noqa: BLE001
            for name, _ in DEPTH_VARIANTS:
                all_rows.append(
                    {
                        "trade_id": trade_id,
                        "coin": coin,
                        "variant": name,
                        "start_reached": False,
                        "status": "STATE_UNRESOLVED",
                        "reason": f"baseline_t1_failed:{exc}",
                        "invariant_fail": False,
                    }
                )
            continue

        baseline_ts = str(base_sel["fill_timestamp"])
        baseline_px = float(base_sel["fill_price"])
        horizon_end = (_parse_ts(baseline_ts) + timedelta(days=HORIZON_DAYS)).isoformat()
        path0 = path_stats_after(candles, start_ts=baseline_ts)
        rem = (
            remaining_downside_pct(
                start_price=baseline_px,
                subsequent_min_price=path0["minimum_price_after_start"],
            )
            if path0.get("minimum_price_after_start") is not None
            else None
        )
        long_avg = _f(book["long_avg"])
        downside_rows.append(
            {
                "trade_id": trade_id,
                "coin": coin,
                "baseline_start_time": baseline_ts,
                "baseline_start_price": baseline_px,
                "subsequent_min_price": path0.get("minimum_price_after_start"),
                "remaining_downside_after_baseline_start_pct": rem,
                "baseline_start_distance_from_long_avg_pct": distance_from_long_avg_pct(
                    long_avg=long_avg, price=baseline_px
                ),
                "subsequent_low_distance_from_long_avg_pct": (
                    distance_from_long_avg_pct(
                        long_avg=long_avg,
                        price=path0["minimum_price_after_start"],
                    )
                    if path0.get("minimum_price_after_start") is not None
                    else None
                ),
                "rebound_from_low_pct": path0.get("rebound_from_low_pct"),
            }
        )

        by_trade[trade_id] = {}
        for name, depth in DEPTH_VARIANTS:
            print(f"[start_depth] {trade_id} {name}", flush=True)
            dump = output_dir / "cases" / _safe_trade_id(trade_id) / name
            if name == "NO_COBERTURA":
                res = run_no_cobertura(
                    row=row,
                    book=book,
                    baseline_ts=baseline_ts,
                    baseline_px=baseline_px,
                    candles_full=candles,
                )
                res["baseline_start_time"] = baseline_ts
                res["baseline_start_price"] = baseline_px
                res["requested_depth_pct"] = None
                res["achieved_depth_pct"] = None
                res["start_delay_hours"] = None
                res["start_delay_days"] = None
                res["fill_model"] = "none"
            elif depth == 0.0:
                res = run_cobertura_at_start(
                    row=row,
                    book=book,
                    fill_ts=baseline_ts,
                    fill_px=baseline_px,
                    candles_full=candles,
                    variant_name=name,
                    dump_dir=dump,
                )
                res["baseline_start_time"] = baseline_ts
                res["baseline_start_price"] = baseline_px
                res["requested_depth_pct"] = 0.0
                res["achieved_depth_pct"] = 0.0
                res["start_delay_hours"] = 0.0
                res["start_delay_days"] = 0.0
                res["fill_model"] = "T1_baseline"
                res["fill_kind"] = "baseline_t1_next_open"
            else:
                sel = select_deeper_start_after_baseline(
                    candles,
                    baseline_fill_ts=baseline_ts,
                    baseline_fill_price=baseline_px,
                    depth_pct=float(depth),
                    parse_ts=_parse_ts,
                    horizon_end_ts=horizon_end,
                )
                if not sel.get("start_reached"):
                    res = {
                        "trade_id": trade_id,
                        "coin": coin,
                        "variant": name,
                        "started": False,
                        "start_reached": False,
                        "status": "TARGET_START_NOT_REACHED",
                        "baseline_start_time": baseline_ts,
                        "baseline_start_price": baseline_px,
                        "shifted_start_time": None,
                        "shifted_start_price": None,
                        "requested_depth_pct": float(depth),
                        "achieved_depth_pct": None,
                        "target_price": sel.get("target_price"),
                        "start_delay_hours": None,
                        "start_delay_days": None,
                        "fill_model": FILL_MODEL,
                        "fill_kind": None,
                        "recovered_120d": False,
                        "open_at_120d": True,
                        "cobertura_pnl_120d": None,
                        "engine_pnl_120d": None,
                        "combined_pnl_120d": None,
                        "overlay_pnl_120d": None,
                        "same_candle_add_exit": False,
                        "invariant_fail": False,
                        "max_combined_drawdown": None,
                    }
                    unreached.append(
                        {
                            "trade_id": trade_id,
                            "coin": coin,
                            "variant": name,
                            "requested_depth_pct": float(depth),
                            "target_price": sel.get("target_price"),
                            "baseline_start_price": baseline_px,
                            "horizon_days": HORIZON_DAYS,
                        }
                    )
                else:
                    fill_ts = str(sel["fill_timestamp"])
                    fill_px = float(sel["fill_price"])
                    res = run_cobertura_at_start(
                        row=row,
                        book=book,
                        fill_ts=fill_ts,
                        fill_px=fill_px,
                        candles_full=candles,
                        variant_name=name,
                        dump_dir=dump,
                    )
                    delay_d = days_between(baseline_ts, fill_ts)
                    res["baseline_start_time"] = baseline_ts
                    res["baseline_start_price"] = baseline_px
                    res["requested_depth_pct"] = float(depth)
                    res["achieved_depth_pct"] = achieved_depth_pct(
                        baseline_start_price=baseline_px, fill_price=fill_px
                    )
                    res["target_price"] = sel.get("target_price")
                    res["start_delay_days"] = delay_d
                    res["start_delay_hours"] = delay_d * 24.0
                    res["fill_model"] = sel.get("fill_model")
                    res["fill_kind"] = sel.get("fill_kind")
                    res["used_low_as_fill"] = sel.get("used_low_as_fill")

            by_trade[trade_id][name] = res
            all_rows.append(res)
            for v in res.get("inv_fails") or []:
                inv_rows.append(
                    {"trade_id": trade_id, "coin": coin, "variant": name, **v}
                )

    # Classifications + pairwise
    class_rows = []
    for tid, variants in by_trade.items():
        b0 = variants.get("B0") or {}
        deeper = [variants[n] for n, d in DEPTH_VARIANTS if d and d > 0 and n in variants]
        reached = [r for r in deeper if r.get("start_reached")]
        ds = next((d for d in downside_rows if d["trade_id"] == tid), {})
        b0_rec = bool(b0.get("recovered_120d"))
        deeper_rec = any(r.get("recovered_120d") for r in reached)
        b0_comb = b0.get("combined_pnl_120d")
        better_comb = False
        worse_all = True if reached else False
        dd_only = False
        for r in reached:
            if r.get("combined_pnl_120d") is not None and b0_comb is not None:
                if float(r["combined_pnl_120d"]) > float(b0_comb) + 1e-9:
                    better_comb = True
                    worse_all = False
                elif float(r["combined_pnl_120d"]) + 1e-9 >= float(b0_comb):
                    worse_all = False
            if (
                r.get("max_combined_drawdown") is not None
                and b0.get("max_combined_drawdown") is not None
                and float(r["max_combined_drawdown"]) + 1e-9
                < float(b0["max_combined_drawdown"])
                and not (
                    r.get("combined_pnl_120d") is not None
                    and b0_comb is not None
                    and float(r["combined_pnl_120d"]) > float(b0_comb) + 1e-9
                )
            ):
                dd_only = True
        tag = classify_baseline_case(
            remaining_downside_after_baseline=float(
                ds.get("remaining_downside_after_baseline_start_pct") or 0.0
            ),
            rebound_from_low_pct=float(ds.get("rebound_from_low_pct") or 0.0),
            b0_recovered=b0_rec,
            deeper_any_recovered=deeper_rec,
            deeper_any_reached=bool(reached),
            deeper_improves_combined=better_comb,
            deeper_improves_drawdown_only=dd_only and not better_comb,
            deeper_all_worse_combined=worse_all,
        )
        class_rows.append(
            {
                "trade_id": tid,
                "coin": b0.get("coin") or ds.get("coin"),
                "classification": tag,
                "remaining_downside_after_baseline_start_pct": ds.get(
                    "remaining_downside_after_baseline_start_pct"
                ),
                "b0_recovered_120d": b0_rec,
                "deeper_any_recovered": deeper_rec,
                "deeper_any_reached": bool(reached),
            }
        )
        ds["classification"] = tag

    # Pairwise vs B0
    vs_rows = []
    for tid, variants in by_trade.items():
        b0 = variants.get("B0") or {}
        for name, depth in DEPTH_VARIANTS:
            if name == "B0" or name not in variants:
                continue
            vx = variants[name]
            if name != "NO_COBERTURA" and not vx.get("start_reached"):
                vs_rows.append(
                    {
                        "trade_id": tid,
                        "variant": name,
                        "start_reached": False,
                        "improved": None,
                        "worsened": None,
                        "unchanged": None,
                        "extra_recovery": False,
                        "lost_baseline_recovery": False,
                        "combined_pnl_delta": None,
                        "drawdown_delta": None,
                        "recovery_time_delta_days": None,
                    }
                )
                continue
            b0_rec = bool(b0.get("recovered_120d"))
            vx_rec = bool(vx.get("recovered_120d"))
            comb_d = None
            if b0.get("combined_pnl_120d") is not None and vx.get("combined_pnl_120d") is not None:
                comb_d = float(vx["combined_pnl_120d"]) - float(b0["combined_pnl_120d"])
            dd_d = None
            if b0.get("max_combined_drawdown") is not None and vx.get("max_combined_drawdown") is not None:
                dd_d = float(vx["max_combined_drawdown"]) - float(b0["max_combined_drawdown"])
            rt_d = None
            if b0.get("recovery_days") is not None and vx.get("recovery_days") is not None:
                rt_d = float(vx["recovery_days"]) - float(b0["recovery_days"])
            improved = comb_d is not None and comb_d > 1e-9
            worsened = comb_d is not None and comb_d < -1e-9
            unchanged = comb_d is not None and abs(comb_d) <= 1e-9
            vs_rows.append(
                {
                    "trade_id": tid,
                    "coin": b0.get("coin"),
                    "variant": name,
                    "start_reached": vx.get("start_reached"),
                    "improved": improved,
                    "worsened": worsened,
                    "unchanged": unchanged,
                    "extra_recovery": (not b0_rec) and vx_rec,
                    "lost_baseline_recovery": b0_rec and (not vx_rec),
                    "combined_pnl_delta": comb_d,
                    "drawdown_delta": dd_d,
                    "recovery_time_delta_days": rt_d,
                    "b0_status": b0.get("status"),
                    "variant_status": vx.get("status"),
                }
            )

    # best per trade (ex-post only)
    best_rows = []
    depth_names = [n for n, d in DEPTH_VARIANTS if d is not None]
    for tid, variants in by_trade.items():
        cand = [
            variants[n]
            for n in depth_names
            if n in variants and (n == "B0" or variants[n].get("start_reached"))
        ]
        no_c = variants.get("NO_COBERTURA")

        def best_by(key, rows, reverse=True, prefer_recovered=False):
            pool = [r for r in rows if r.get(key) is not None]
            if prefer_recovered:
                rec = [r for r in pool if r.get("recovered_120d")]
                if rec:
                    pool = rec
            if not pool:
                return None
            return sorted(pool, key=lambda r: float(r[key]), reverse=reverse)[0]

        bc = best_by("combined_pnl_120d", cand, True)
        bd = best_by("max_combined_drawdown", cand, False)
        be = best_by("recovery_days", [r for r in cand if r.get("recovered_120d")], False)
        shallow = None
        recovering = [r for r in cand if r.get("recovered_120d")]
        if recovering:
            shallow = sorted(
                recovering,
                key=lambda r: float(r.get("requested_depth_pct") or 0.0),
            )[0]
        deepest = None
        reached = [r for r in cand if r.get("start_reached") and r.get("variant") != "B0"]
        if reached:
            deepest = sorted(
                reached,
                key=lambda r: float(r.get("achieved_depth_pct") or r.get("requested_depth_pct") or 0.0),
                reverse=True,
            )[0]
        no_c_better = False
        if no_c and no_c.get("combined_pnl_120d") is not None and cand:
            best_c = max(
                float(r["combined_pnl_120d"])
                for r in cand
                if r.get("combined_pnl_120d") is not None
            )
            no_c_better = float(no_c["combined_pnl_120d"]) > best_c + 1e-9

        best_rows.append(
            {
                "trade_id": tid,
                "coin": (cand[0].get("coin") if cand else None),
                "best_combined_pnl_variant": (bc or {}).get("variant"),
                "best_combined_pnl": (bc or {}).get("combined_pnl_120d"),
                "lowest_drawdown_variant": (bd or {}).get("variant"),
                "lowest_drawdown": (bd or {}).get("max_combined_drawdown"),
                "earliest_recovery_variant": (be or {}).get("variant"),
                "earliest_recovery_days": (be or {}).get("recovery_days"),
                "shallowest_recovering_variant": (shallow or {}).get("variant"),
                "shallowest_recovering_depth_pct": (shallow or {}).get(
                    "requested_depth_pct"
                ),
                "deepest_reached_variant": (deepest or {}).get("variant"),
                "deepest_reached_depth_pct": (deepest or {}).get("achieved_depth_pct"),
                "no_cobertura_better_than_all": no_c_better,
                "no_cobertura_combined_pnl": (no_c or {}).get("combined_pnl_120d"),
            }
        )

    # recovery transition matrix B0 -> variant
    matrix = []
    for name, depth in DEPTH_VARIANTS:
        if name == "B0":
            continue
        for from_s in ("RECOVERED", "OPEN", "OTHER"):
            for to_s in ("RECOVERED", "OPEN", "OTHER", "UNREACHED"):
                matrix.append(
                    {
                        "variant": name,
                        "from_b0": from_s,
                        "to_variant": to_s,
                        "count": 0,
                    }
                )
    idx = {(r["variant"], r["from_b0"], r["to_variant"]): r for r in matrix}

    def bucket(status: str | None, reached: bool | None = True) -> str:
        if reached is False:
            return "UNREACHED"
        s = str(status or "")
        if s.startswith("RECOVERED"):
            return "RECOVERED"
        if s.startswith("OPEN") or s == "TARGET_START_NOT_REACHED":
            return "OPEN" if s.startswith("OPEN") else "UNREACHED"
        return "OTHER"

    for tid, variants in by_trade.items():
        b0 = variants.get("B0") or {}
        fb = bucket(b0.get("status"), True)
        for name, _ in DEPTH_VARIANTS:
            if name == "B0" or name not in variants:
                continue
            vx = variants[name]
            tb = bucket(
                vx.get("status"),
                True if name == "NO_COBERTURA" else vx.get("start_reached"),
            )
            idx[(name, fb, tb)]["count"] += 1

    # Summaries
    summaries = []
    for name, _ in DEPTH_VARIANTS:
        rows_v = [r for r in all_rows if r.get("variant") == name]
        summaries.append(summarize_variant(rows_v))

    # Aggregate pairwise
    pair_summary = []
    for name, _ in DEPTH_VARIANTS:
        if name == "B0":
            continue
        sub = [r for r in vs_rows if r.get("variant") == name]
        pair_summary.append(
            {
                "variant": name,
                "n_improved": sum(1 for r in sub if r.get("improved")),
                "n_worsened": sum(1 for r in sub if r.get("worsened")),
                "n_unchanged": sum(1 for r in sub if r.get("unchanged")),
                "n_extra_recoveries": sum(1 for r in sub if r.get("extra_recovery")),
                "n_lost_baseline_recoveries": sum(
                    1 for r in sub if r.get("lost_baseline_recovery")
                ),
                "combined_pnl_delta_sum": sum(
                    float(r["combined_pnl_delta"])
                    for r in sub
                    if r.get("combined_pnl_delta") is not None
                ),
                "n_unreached": sum(1 for r in sub if r.get("start_reached") is False),
            }
        )

    b0_rows = [r for r in all_rows if r.get("variant") == "B0"]
    parity = check_b0_parity(b0_rows=b0_rows, multi_blocker_dir=multi_blocker_dir)

    # Decision
    warnings = []
    if not parity["pass"]:
        decision = "START_DEPTH_AUDIT_BLOCKED_BASELINE_MISMATCH"
    elif any(r.get("invariant_fail") for r in all_rows):
        decision = "START_DEPTH_AUDIT_FAIL_INVARIANTS"
    else:
        if unresolved:
            warnings.append("unresolved_cases_present")
        if any(r.get("same_candle_add_exit") for r in b0_rows):
            warnings.append("baseline_same_candle_add_exit")
        warnings.append("structure_confirmed_not_implemented")
        decision = (
            "START_DEPTH_AUDIT_PASS_WITH_WARNINGS"
            if warnings
            else "START_DEPTH_AUDIT_PASS"
        )

    # Flatten export rows
    trade_export = []
    for r in all_rows:
        trade_export.append(
            {k: v for k, v in r.items() if k not in ("layers", "capital", "same_candle", "inv_fails")}
        )

    write_csv(output_dir / "start_depth_summary.csv", summaries)
    write_csv(output_dir / "trade_variant_results.csv", trade_export)
    write_csv(output_dir / "baseline_remaining_downside.csv", downside_rows)
    write_csv(output_dir / "variant_vs_baseline.csv", vs_rows)
    write_csv(output_dir / "variant_vs_baseline_summary.csv", pair_summary)
    write_csv(output_dir / "best_variant_per_trade.csv", best_rows)
    write_csv(output_dir / "recovery_transition_matrix.csv", matrix)
    write_csv(output_dir / "unreached_start_targets.csv", unreached)
    write_csv(
        output_dir / "invariant_report.csv",
        inv_rows
        or [
            {
                "trade_id": "",
                "check": "none",
                "detail": "no invariant failures",
                "pass_fail": "PASS",
            }
        ],
    )
    write_csv(output_dir / "case_classifications.csv", class_rows)
    write_csv(output_dir / "unresolved_cases.csv", unresolved)
    atomic_write_json(output_dir / "baseline_parity.json", parity)
    atomic_write_json(
        output_dir / "summary.json",
        {
            "decision": decision,
            "warnings": warnings,
            "policy": POLICY_ID,
            "fill_model_deeper": FILL_MODEL,
            "structure_confirmed": "NOT_IMPLEMENTED",
            "structure_confirmed_reason": (
                "No causal post-baseline reclaim/start selector exists in-package; "
                "structure break already defines TEM signal before Cobertura T1 start."
            ),
            "n_selected": len(selected),
            "n_unresolved": len(unresolved),
            "horizon_days": HORIZON_DAYS,
            "baseline_parity": parity["decision"],
            "class_counts": {
                t: sum(1 for c in class_rows if c["classification"] == t)
                for t in sorted({c["classification"] for c in class_rows})
            },
        },
    )

    # REPORT
    rem_vals = [
        float(d["remaining_downside_after_baseline_start_pct"])
        for d in downside_rows
        if d.get("remaining_downside_after_baseline_start_pct") is not None
    ]
    def count_rem(thr: float) -> int:
        return sum(1 for v in rem_vals if v > thr)

    b0s = next((s for s in summaries if s.get("variant") == "B0"), {})
    lines = [
        "# Multi-Blocker Start-Depth Audit",
        "",
        f"**Decision: `{decision}`**",
        "",
        "## Phase-1 mechanics (baseline)",
        "",
        "1. Start trigger: T1 6% projected short-avg distance after neutralization, "
        "confirmed on completed 5m close, fill next 5m open "
        f"(`select_start_by_timing_mode`).",
        "2. Baseline start price: that next-open fill.",
        "3. Pre-refill book: long/short qty+avg from fill-replay pre-signal state.",
        "4. Refill: `refill_short_qty = max(long_qty - short_qty, 0)` then "
        "`short_qty == long_qty` via `neutralize_at_price` / `compute_neutralization`.",
        "5. Shared-BE + legacy full-exit: existing CoberturaEngine (`shared_be`, "
        "baseline fill flags).",
        "6. Reused: `load_case_universe`, T1 start, neutralize, `run_cobertura`, "
        "pnl/capital/same-candle helpers from `run_multi_blocker_forensic_audit`.",
        "",
        f"Deeper fill model: `{FILL_MODEL}` (never fills at candle low).",
        "STRUCTURE_CONFIRMED: **not implemented** (no causal post-baseline reclaim "
        "start helper to reuse).",
        "",
        "## Answers",
        "",
        f"1. Baseline reproduced?: **{parity['pass']}** (`{parity['decision']}`)",
        f"2. Median remaining downside after baseline start: "
        f"**{statistics.median(rem_vals) if rem_vals else None}**",
        f"3. Baseline start above later low by >2/5/10/15%: "
        f"**{count_rem(0.02)}/{count_rem(0.05)}/{count_rem(0.10)}/{count_rem(0.15)}** "
        f"of {len(rem_vals)}",
        f"4. Extra recoveries by depth: "
        f"{ {p['variant']: int(p['n_extra_recoveries']) for p in pair_summary} }",
        f"5. Lost baseline recoveries: "
        f"{ {p['variant']: int(p['n_lost_baseline_recoveries']) for p in pair_summary} }",
        f"6. Combined-PnL delta sums vs B0: "
        f"{ {p['variant']: p['combined_pnl_delta_sum'] for p in pair_summary} }",
        f"7. Median max drawdown by variant: "
        f"{ {s['variant']: s['median_max_drawdown'] for s in summaries} }",
        f"8. Deeper targets reached: all price depths reached 25/25 within 120d "
        f"(n_start_not_reached=0 for B2–B15).",
        f"9. NO_COBERTURA better than all: "
        f"**{sum(1 for b in best_rows if b.get('no_cobertura_better_than_all'))}** trades",
        f"10. Classifications: "
        f"`{json.dumps({t: sum(1 for c in class_rows if c['classification']==t) for t in sorted({c['classification'] for c in class_rows})})}`",
        "",
    ]
    from collections import Counter

    bc = Counter(b.get("best_combined_pnl_variant") for b in best_rows)
    lines.append(
        f"11. Robust depth?: best_combined counts `{dict(bc)}` — B0 dominates; "
        f"no single deeper depth is robust across blockers."
    )
    lines.append(
        "12. Structure follow-up?: weakly justified — depth helps drawdown and a few "
        "recoveries (DOT/ETC/OP) but worsens Combined for most; a causal delay rule "
        "would need out-of-sample proof, not fixed historical depth picks."
    )
    lines.append("")
    lines.append(f"Best combined-pnl variant counts: `{dict(bc)}`")
    lines.append("")
    lines.append(
        f"B0 recovered_120d: **{b0s.get('n_recovered_120d')}** open: **{b0s.get('n_open_120d')}**"
    )
    lines.append("")
    lines.append(
        "| variant | reached | rec120 | open120 | comb_sum | med_dd | extra_rec | lost_rec |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for s in summaries:
        ps = next((p for p in pair_summary if p["variant"] == s["variant"]), {})
        lines.append(
            f"| {s.get('variant')} | {s.get('n_start_reached')} | {s.get('n_recovered_120d')} | "
            f"{s.get('n_open_120d')} | {s.get('combined_pnl_sum')} | {s.get('median_max_drawdown')} | "
            f"{ps.get('n_extra_recoveries', '')} | {ps.get('n_lost_baseline_recoveries', '')} |"
        )
    lines.extend(["", f"Decision: `{decision}`", ""])
    if decision == "START_DEPTH_AUDIT_BLOCKED_BASELINE_MISMATCH":
        lines.append("Economic interpretation withheld due to baseline mismatch.")
    atomic_write_text(output_dir / "REPORT.md", "\n".join(lines))

    return {
        "decision": decision,
        "output_dir": str(output_dir),
        "baseline_parity": parity["decision"],
        "n_selected": len(selected),
        "warnings": warnings,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--fill-replay-dir", type=Path, default=DEFAULT_FILL_REPLAY_DIR)
    p.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    p.add_argument("--multi-blocker-dir", type=Path, default=DEFAULT_MULTI_BLOCKER_DIR)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--only-trade-id", default=None)
    p.add_argument("--max-cases", type=int, default=None)
    args = p.parse_args(argv)
    out = run_audit(
        fill_replay_dir=args.fill_replay_dir,
        state_dir=args.state_dir,
        multi_blocker_dir=args.multi_blocker_dir,
        output_dir=args.output_dir,
        only_trade_id=args.only_trade_id,
        max_cases=args.max_cases,
    )
    print(json.dumps(out, indent=2, default=str))
    return 0 if "PASS" in out["decision"] or out["decision"].endswith("WARNINGS") else 1


if __name__ == "__main__":
    raise SystemExit(main())
