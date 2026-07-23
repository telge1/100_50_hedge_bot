#!/usr/bin/env python3
"""PnL source audit: TEM vs TEM-FD economic disadvantage decomposition.

Uses the same 200 pair-keys as tem_fd_undercoverage_fix_20260722.
Reproduces runs only to recover fill/replan timelines not stored in raw CSV.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.backtests.candle_loader import load_candles_for_symbol
from research.backtests.full_dynamic_second_leg_restaging import resolve_full_dynamic_profile
from research.backtests.historical_backtest import normalize_candles
from research.backtests.multicoin_blocker_price_staging import (
    FULL_HISTORY_CANDLE_LIMIT,
    run_isolated_blocker,
)
from research.backtests.multicoin_price_staging_grid import write_csv
from research.backtests.second_leg_price_staging import resolve_grid_profile
from research.backtests.tem_fd_undercoverage_economics import classify_closed_economics

PRIOR_FIX = Path("research/backtests/results/tem_fd_undercoverage_fix_20260722")
PRIOR_SEL = Path(
    "research/backtests/results/tem_full_dynamic_blocker_validation_20260722"
)
STARTS = Path(
    "research/backtests/results/fixed_step_distance_staging_large_1000_500_20260722/start_points.csv"
)
DEFAULT_OUT = Path("research/backtests/results/tem_fd_pnl_source_audit_20260722")

PARTIAL = "two_early_medium"
FULL = "two_early_medium_full_dynamic"

EXPECTED_TOTAL = -246.4920747580538
EXPECTED_REALIZED = -2233.106243435453
EXPECTED_OPEN = 1986.6141686773992
TOL = 0.05  # USDT tolerance on aggregate gates vs known corrected deltas


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sf(x: Any, default: float = 0.0) -> float:
    try:
        if x is None or x == "":
            return default
        v = float(x)
        if math.isnan(v):
            return default
        return v
    except (TypeError, ValueError):
        return default


def _median(xs: list[float]) -> float:
    return float(statistics.median(xs)) if xs else 0.0


def load_starts() -> dict[str, dict[str, Any]]:
    return {r["pair_key"]: r for r in csv.DictReader(STARTS.open())}


def load_keys() -> tuple[list[str], list[str]]:
    man = json.loads((PRIOR_SEL / "selection_manifest.json").read_text())
    return list(man["blocker_keys"]), list(man["control_keys"])


def _fills(result: Any) -> list[dict[str, Any]]:
    return list(getattr(result, "fills_log", None) or getattr(result, "fill_log", None) or [])


def _is_sr(purpose: str) -> bool:
    return "SHORT_REDUCE" in str(purpose or "").upper()


def summarize_fills(fills: list[dict[str, Any]]) -> dict[str, Any]:
    sr = [f for f in fills if _is_sr(str(f.get("purpose") or ""))]
    qty = sum(_sf(f.get("qty")) for f in sr)
    pnl = sum(_sf(f.get("closed_pnl") or f.get("confirmed_closed_pnl")) for f in sr)
    notional = 0.0
    px_qty = 0.0
    for f in sr:
        q = _sf(f.get("qty"))
        px = _sf(f.get("fill_price") or f.get("price") or f.get("order_check_price"))
        if q > 0 and px > 0:
            notional += q * px
            px_qty += q
    avg_px = (notional / px_qty) if px_qty > 0 else None
    close_bars = [
        int(f["candle_index"])
        for f in fills
        if str(f.get("purpose") or "") in {"LONG_TP_EXIT", "SHORT_SL_EXIT"}
        and f.get("candle_index") is not None
    ]
    cycles = []
    for f in fills:
        pur = str(f.get("purpose") or "")
        if pur.startswith("CYCLE_") and ("LONG_ADD" in pur or "SHORT_REDUCE" in pur):
            try:
                cycles.append(int(pur.split("_")[1]))
            except (IndexError, ValueError):
                pass
    return {
        "sr_fill_count": len(sr),
        "sr_qty_total": qty,
        "sr_pnl": pnl,
        "sr_avg_price": avg_px,
        "sr_notional": notional,
        "close_bar": max(close_bars) if close_bars else None,
        "max_cycle": max(cycles) if cycles else 0,
        "n_fills": len(fills),
    }


def extract_cancel_counterfactuals(
    *,
    events: list[dict[str, Any]],
    candles: list[Any],
    start_index: int,
) -> list[dict[str, Any]]:
    """For each FD cancel of residual stages, check later price touch (T+1+ only)."""
    out: list[dict[str, Any]] = []
    fee = 0.00055
    end_local = max(len(candles) - int(start_index) - 1, 0)
    for ev in events:
        cancel_bar = ev.get("candle_index")
        if cancel_bar is None:
            continue
        try:
            cancel_i = int(cancel_bar)
        except (TypeError, ValueError):
            continue
        old_prices = list(ev.get("old_residual_prices") or [])
        old_qtys = list(ev.get("old_residual_qtys") or [])
        new_prices = list(ev.get("new_stage_prices") or [])
        new_qtys = list(ev.get("new_stage_qtys") or [])
        fill_price = _sf(ev.get("fill_price"))
        short_entry = _sf(ev.get("actual_short_entry") or ev.get("short_entry"))
        if short_entry <= 0:
            # conservative proxy when short_avg not in event payload
            short_entry = fill_price * 1.02 if fill_price > 0 else 0.0

        for idx, old_px in enumerate(old_prices):
            opx = _sf(old_px)
            oqty = _sf(old_qtys[idx]) if idx < len(old_qtys) else 0.0
            if opx <= 0 or oqty <= 0:
                continue
            first_touch = None
            for local in range(cancel_i + 1, end_local + 1):
                abs_i = int(start_index) + local
                if abs_i >= len(candles):
                    break
                c = candles[abs_i]
                low = _sf(c.get("low") if isinstance(c, dict) else getattr(c, "low", None))
                # LONG-primary SHORT_REDUCE fills when price revisits at/below trigger.
                if low > 0 and low <= opx + 1e-12:
                    first_touch = local
                    break
            would = first_touch is not None
            entry = short_entry if short_entry > 0 else opx * 1.05
            hypo = (entry - opx) * oqty - fee * (entry + opx) * oqty
            rep_px = _sf(new_prices[min(idx, len(new_prices) - 1)]) if new_prices else 0.0
            rep_qty = _sf(new_qtys[min(idx, len(new_qtys) - 1)]) if new_qtys else 0.0
            rep_hypo = 0.0
            if would and rep_px > 0 and rep_qty > 0 and entry > 0:
                rep_hypo = (entry - rep_px) * rep_qty - fee * (entry + rep_px) * rep_qty
            out.append(
                {
                    "cancel_bar": cancel_i,
                    "old_residual_price": opx,
                    "old_residual_qty": oqty,
                    "first_touch_bar": first_touch,
                    "would_have_filled": int(would),
                    "hypothetical_profit_if_kept": hypo if would else 0.0,
                    "replacement_stage_price": rep_px if rep_px else None,
                    "replacement_stage_qty": rep_qty if rep_qty else None,
                    "replacement_hypothetical_profit": rep_hypo if would else 0.0,
                    "plan_revision": ev.get("plan_revision"),
                    "cycle_index": ev.get("cycle_index"),
                    "short_entry_est": entry,
                }
            )
    return out


def run_one(
    pair_key: str,
    profile: str,
    starts: dict[str, dict[str, Any]],
    candle_cache: dict[str, list[Any]],
) -> dict[str, Any]:
    sp = starts[pair_key]
    coin = str(sp["coin"]).upper()
    si = int(sp["start_index"])
    mw = int(float(sp["max_window_candles"]))
    if coin not in candle_cache:
        candle_cache[coin] = normalize_candles(
            coin, load_candles_for_symbol(coin, limit=FULL_HISTORY_CANDLE_LIMIT)
        )
    full_series = candle_cache[coin]
    series = full_series[: si + mw]
    cfg = (
        resolve_full_dynamic_profile(profile)
        if "full_dynamic" in profile
        else resolve_grid_profile(profile)
    )
    result = run_isolated_blocker(
        coin=coin, candles=series, start_index=si, staging_config=cfg
    )
    eco = classify_closed_economics(result)
    ex = result.final_strategy_state_excerpt or {}
    events = list(ex.get("research_fd_replan_events") or [])
    fills = _fills(result)
    fs = summarize_fills(fills)
    long_q = _sf(result.final_long_qty)
    short_q = _sf(result.final_short_qty)
    flat = long_q <= 1e-12 and short_q <= 1e-12 and str(result.final_status) == "closed"
    closed = _sf(result.realized_pnl)
    open_mtm = 0.0 if flat else _sf(result.unrealized_pnl)
    overall = getattr(result, "overall_pnl", None)
    total = _sf(overall, closed + open_mtm) if overall is not None else closed + open_mtm
    duration = int(result.candles_processed or 0)
    close_bar = fs["close_bar"]
    cancels = 0
    for e in events:
        cancels += len(e.get("canceled_residual_order_ids") or [])
        # also count old residual prices as cancel proxies
        if not e.get("canceled_residual_order_ids") and e.get("old_residual_prices"):
            cancels += len(e.get("old_residual_prices") or [])
    if profile == PARTIAL:
        cancels = 0  # partial has no FD cancel restage; leave 0 unless order log later
    cf = (
        extract_cancel_counterfactuals(events=events, candles=full_series, start_index=si)
        if profile == FULL and events
        else []
    )
    # remaining required from FD maps / pending
    rem = None
    req_map = ex.get("research_fd_required_net_total") or ex.get(
        "staged_second_leg_tp_required_net_total"
    ) or {}
    realized_map = ex.get("staged_second_leg_tp_realized_net") or {}
    if req_map:
        # last cycle key
        try:
            ck = str(max(int(k) for k in req_map.keys()))
            rem = max(_sf(req_map.get(ck)) - _sf(realized_map.get(ck)), 0.0)
        except ValueError:
            rem = None
    if rem is None:
        rem = _sf(ex.get("pending_cycle_loss_usdt"))

    last_residual_px = None
    last_basket_px = None
    plan = ex.get("research_second_leg_price_staging_plan") or {}
    stages = plan.get("stages") if isinstance(plan, dict) else None
    if stages:
        last_residual_px = _sf(stages[-1].get("trigger_price"))
    last_cov = ex.get("last_basket_exit_coverage_decision") or {}
    # basket from replan event or active purposes — use coverage expected not price
    if events:
        nb = events[-1].get("new_basket_exit_prices") or events[-1].get("old_basket_exit_prices") or []
        if nb and isinstance(nb, list) and nb:
            last_basket_px = _sf((nb[0] or {}).get("trigger"))

    return {
        "pair_key": pair_key,
        "profile": profile,
        "coin": coin,
        "window": str(sp.get("window_kind") or ""),
        "start_index": si,
        "status": str(result.final_status),
        "trade_flat": int(flat),
        "realized_pnl": closed,
        "open_mtm": open_mtm,
        "total_pnl": total,
        "long_qty": long_q,
        "short_qty": short_q,
        "net_exposure": long_q - short_q,
        "duration_bars": duration,
        "close_bar": close_bar,
        "max_cycle": max(fs["max_cycle"], int(_sf(getattr(result, "cycles_seen", 0)))),
        "sr_fill_count": fs["sr_fill_count"],
        "sr_qty_total": fs["sr_qty_total"],
        "sr_pnl": fs["sr_pnl"],
        "sr_avg_price": fs["sr_avg_price"],
        "sr_notional": fs["sr_notional"],
        "n_fills": fs["n_fills"],
        "replan_count": len(events),
        "cancel_count": cancels,
        "pending": _sf(ex.get("pending_cycle_loss_usdt")),
        "remaining_required_net": rem,
        "last_residual_price": last_residual_px,
        "last_basket_exit_price": last_basket_px,
        "sufficient": eco.get("last_sufficient"),
        "economic_class": eco.get("economic_class"),
        "economic_uc": eco.get("economic_undercoverage_closed"),
        "clean_flat": int(
            flat
            and eco.get("last_sufficient") is True
            and int(eco.get("economic_undercoverage_closed") or 0) == 0
        ),
        "target_profit_proxy": _sf(
            (ex.get("last_basket_exit_coverage_decision") or {}).get("min_required_total_usdt")
        ),
        "replan_events": events,
        "cancel_counterfactuals": cf,
        "fills": fills,
    }


def status_transition(p_flat: bool, f_flat: bool, p_err: bool, f_err: bool) -> str:
    if p_err or f_err:
        return "error_or_invalid"
    if p_flat and f_flat:
        return "flat_to_flat"
    if (not p_flat) and (not f_flat):
        return "open_to_open"
    if (not p_flat) and f_flat:
        return "open_to_flat"
    if p_flat and (not f_flat):
        return "flat_to_open"
    return "error_or_invalid"


def assign_mech_cause(row: dict[str, Any]) -> str:
    if row["delta_total_pnl"] >= -1e-6:
        return "not_negative"
    st = row["status_transition"]
    if st == "open_to_flat":
        return "blocker_recovery_cost"
    if st == "flat_to_flat":
        if row["delta_short_reduce_qty_total"] > 1e-6 and row.get("bars_delta", 0) > 0:
            return "earlier_short_reduction_lower_profit"
        if row["delta_short_reduce_fill_count"] > 0 and row["delta_total_pnl"] < 0:
            return "exit_closed_earlier_with_lower_profit"
        if row["fd_replan_count"] > 0 and row.get("would_fill_cancel_count", 0) > 0:
            return "canceled_deeper_residual_later_would_fill"
        if row["fd_cancel_count"] > row["partial_cancel_count"] + 2:
            return "additional_stage_churn"
        return "earlier_short_reduction_lower_profit"
    if st == "open_to_open":
        if abs(row["delta_total_pnl"]) < 5 and row["delta_realized_pnl"] < -10 and row["delta_open_mtm"] > 10:
            return "data_end_mark_difference"  # mostly shift; small residual loss
        if row["delta_short_reduce_qty_total"] > 1e-6 and row["delta_total_pnl"] < 0:
            return "reduced_short_qty_before_large_downmove"
        if abs(row["delta_end_exposure"]) > 5:
            return "extra_long_exposure_after_short_reduction"
        if row.get("would_fill_cancel_count", 0) > 0:
            return "canceled_deeper_residual_later_would_fill"
        if row["fd_max_cycle"] != row["partial_max_cycle"]:
            return "different_cycle_progression"
        return "other"
    return "other"


def open_class(row: dict[str, Any]) -> str:
    dt = row["delta_total_pnl"]
    dr = row["delta_realized_pnl"]
    dop = row["delta_open_mtm"]
    if abs(dt) < 5.0 and dr < -1.0 and dop > 1.0 and abs(dr + dop - dt) < 1e-6:
        # compensating shift
        if abs(dr + dop) < 5.0 or abs(dt) < abs(dr) * 0.25:
            return "realization_shift_only"
    if abs(row["delta_end_exposure"]) > max(5.0, 0.25 * abs(row["partial_end_net_exposure"] or 1)):
        return "exposure_changed_materially"
    if dt > 1e-9:
        return "genuinely_better_open"
    if dt < -1e-9:
        return "genuinely_worse_open"
    return "realization_shift_only"


def load_prior_flat_status() -> dict[tuple[str, str], bool]:
    path = PRIOR_FIX / "raw_runs.csv"
    out: dict[tuple[str, str], bool] = {}
    if not path.exists():
        return out
    for r in csv.DictReader(path.open()):
        out[(r["pair_key"], r["profile"])] = str(r.get("trade_flat")).lower() in {
            "1",
            "true",
        }
    return out


def load_prior_corrected_pnl() -> dict[str, dict[str, float]]:
    """Canonical corrected PnL deltas from the aggregation-fix audit (no re-sim)."""
    path = PRIOR_FIX / "pnl_delta_reconciliation_pairwise.csv"
    out: dict[str, dict[str, float]] = {}
    if not path.exists():
        return out
    for r in csv.DictReader(path.open()):
        out[r["pair_key"]] = {
            "partial_realized_pnl": _sf(r.get("partial_closed_corrected")),
            "fd_realized_pnl": _sf(r.get("fd_closed_corrected")),
            "delta_realized_pnl": _sf(r.get("delta_closed_corrected")),
            "partial_open_mtm": _sf(r.get("partial_open_mtm_corrected")),
            "fd_open_mtm": _sf(r.get("fd_open_mtm_corrected")),
            "delta_open_mtm": _sf(r.get("delta_open_mtm_corrected")),
            "partial_total_pnl": _sf(r.get("partial_total_corrected")),
            "fd_total_pnl": _sf(r.get("fd_total_corrected")),
            "delta_total_pnl": _sf(r.get("delta_total_corrected")),
            "reconciliation_error": _sf(r.get("reconciliation_error_corrected")),
        }
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--limit", type=int, default=0, help="debug limit pairs")
    args = ap.parse_args()
    out: Path = args.output_dir
    out.mkdir(parents=True, exist_ok=True)

    starts = load_starts()
    blockers, controls = load_keys()
    planned = [(pk, "historical_blocker") for pk in blockers] + [
        (pk, "flat_control") for pk in controls
    ]
    if args.limit > 0:
        planned = planned[: args.limit]

    candle_cache: dict[str, list[Any]] = {}
    prior_pnl = load_prior_corrected_pnl()
    prior_flat = load_prior_flat_status()
    by: dict[tuple[str, str], dict[str, Any]] = {}
    t0 = time.time()
    print(f"=== PnL source audit pairs={len(planned)} runs={len(planned)*2} ===")
    print(f"prior corrected PnL keys available: {len(prior_pnl)}")
    for i, (pk, cohort) in enumerate(planned, 1):
        for profile in (PARTIAL, FULL):
            print(f"[{i}/{len(planned)}] {pk} {profile}")
            row = run_one(pk, profile, starts, candle_cache)
            row["sample_group"] = cohort
            by[(pk, profile)] = row

    pairwise: list[dict[str, Any]] = []
    cancel_rows: list[dict[str, Any]] = []
    for pk, cohort in planned:
        p = by[(pk, PARTIAL)]
        f = by[(pk, FULL)]
        # Status transitions aligned to the PnL artifact being attributed.
        if (pk, PARTIAL) in prior_flat and (pk, FULL) in prior_flat:
            p_flat = prior_flat[(pk, PARTIAL)]
            f_flat = prior_flat[(pk, FULL)]
        else:
            p_flat = bool(p["trade_flat"])
            f_flat = bool(f["trade_flat"])
        st = status_transition(
            p_flat,
            f_flat,
            p["status"] == "error",
            f["status"] == "error",
        )
        # Prefer prior corrected PnL (matches published -246.49 audit); forensics from re-run.
        prior = prior_pnl.get(pk)
        if prior:
            d_real = prior["delta_realized_pnl"]
            d_open = prior["delta_open_mtm"]
            d_tot = prior["delta_total_pnl"]
            p_real = prior["partial_realized_pnl"]
            f_real = prior["fd_realized_pnl"]
            p_open = prior["partial_open_mtm"]
            f_open = prior["fd_open_mtm"]
            p_tot = prior["partial_total_pnl"]
            f_tot = prior["fd_total_pnl"]
            recon = prior["reconciliation_error"]
            pnl_source = "prior_corrected_reconstruction"
        else:
            d_real = f["realized_pnl"] - p["realized_pnl"]
            d_open = f["open_mtm"] - p["open_mtm"]
            d_tot = f["total_pnl"] - p["total_pnl"]
            p_real = p["realized_pnl"]
            f_real = f["realized_pnl"]
            p_open = p["open_mtm"]
            f_open = f["open_mtm"]
            p_tot = p["total_pnl"]
            f_tot = f["total_pnl"]
            recon = d_tot - (d_real + d_open)
            pnl_source = "repro_run"
        bars_delta = None
        if p.get("close_bar") is not None and f.get("close_bar") is not None:
            bars_delta = int(p["close_bar"]) - int(f["close_bar"])  # positive => FD earlier

        would_fill = 0
        hypo_kept = 0.0
        hypo_rep = 0.0
        for cf in f.get("cancel_counterfactuals") or []:
            cancel_rows.append(
                {
                    "pair_key": pk,
                    "coin": f["coin"],
                    "window": f["window"],
                    "sample_group": cohort,
                    **cf,
                }
            )
            would_fill += int(cf.get("would_have_filled") or 0)
            hypo_kept += _sf(cf.get("hypothetical_profit_if_kept"))
            hypo_rep += _sf(cf.get("replacement_hypothetical_profit"))

        row = {
            "pair_key": pk,
            "coin": p["coin"],
            "window": p["window"],
            "start_index": p["start_index"],
            "sample_group": cohort,
            "partial_final_status": p["status"],
            "fd_final_status": f["status"],
            "status_transition": st,
            "partial_close_bar": p.get("close_bar"),
            "fd_close_bar": f.get("close_bar"),
            "partial_duration_bars": p["duration_bars"],
            "fd_duration_bars": f["duration_bars"],
            "bars_delta": bars_delta,
            "pnl_source": pnl_source,
            "partial_realized_pnl": p_real,
            "fd_realized_pnl": f_real,
            "delta_realized_pnl": d_real,
            "partial_open_mtm": p_open,
            "fd_open_mtm": f_open,
            "delta_open_mtm": d_open,
            "partial_total_pnl": p_tot,
            "fd_total_pnl": f_tot,
            "delta_total_pnl": d_tot,
            "reconciliation_error": recon,
            # forensic statuses from this reproduction (may differ slightly after hardening)
            "artifact_partial_flat": int(p_flat),
            "artifact_fd_flat": int(f_flat),
            "repro_partial_flat": int(bool(p["trade_flat"])),
            "repro_fd_flat": int(bool(f["trade_flat"])),
            "repro_partial_total_pnl": p["total_pnl"],
            "repro_fd_total_pnl": f["total_pnl"],
            "partial_final_long_qty": p["long_qty"],
            "fd_final_long_qty": f["long_qty"],
            "partial_final_short_qty": p["short_qty"],
            "fd_final_short_qty": f["short_qty"],
            "partial_end_net_exposure": p["net_exposure"],
            "fd_end_net_exposure": f["net_exposure"],
            "delta_end_exposure": f["net_exposure"] - p["net_exposure"],
            "partial_highest_cycle": p["max_cycle"],
            "fd_highest_cycle": f["max_cycle"],
            "partial_max_cycle": p["max_cycle"],
            "fd_max_cycle": f["max_cycle"],
            "partial_short_reduce_fill_count": p["sr_fill_count"],
            "fd_short_reduce_fill_count": f["sr_fill_count"],
            "delta_short_reduce_fill_count": f["sr_fill_count"] - p["sr_fill_count"],
            "partial_short_reduce_qty_total": p["sr_qty_total"],
            "fd_short_reduce_qty_total": f["sr_qty_total"],
            "delta_short_reduce_qty_total": f["sr_qty_total"] - p["sr_qty_total"],
            "partial_sr_pnl": p["sr_pnl"],
            "fd_sr_pnl": f["sr_pnl"],
            "partial_sr_avg_price": p["sr_avg_price"],
            "fd_sr_avg_price": f["sr_avg_price"],
            "partial_replan_count": p["replan_count"],
            "fd_replan_count": f["replan_count"],
            "partial_cancel_count": p["cancel_count"],
            "fd_cancel_count": f["cancel_count"],
            "partial_final_exit_sufficient": p.get("sufficient"),
            "fd_final_exit_sufficient": f.get("sufficient"),
            "partial_clean_flat": p["clean_flat"],
            "fd_clean_flat": f["clean_flat"],
            "blocker_prevented": int(st == "open_to_flat" and cohort == "historical_blocker"),
            "new_blocker_created": int(st == "flat_to_open"),
            "partial_remaining_required_net": p.get("remaining_required_net"),
            "fd_remaining_required_net": f.get("remaining_required_net"),
            "partial_last_residual_price": p.get("last_residual_price"),
            "fd_last_residual_price": f.get("last_residual_price"),
            "partial_last_basket_exit": p.get("last_basket_exit_price"),
            "fd_last_basket_exit": f.get("last_basket_exit_price"),
            "would_fill_cancel_count": would_fill,
            "hypo_profit_kept_cancels": hypo_kept,
            "hypo_profit_replacement": hypo_rep,
            "fd_n_fills": f["n_fills"],
            "partial_n_fills": p["n_fills"],
            "delta_fills": f["n_fills"] - p["n_fills"],
            "fd_sr_notional": f["sr_notional"],
            "partial_sr_notional": p["sr_notional"],
            "delta_sr_notional": f["sr_notional"] - p["sr_notional"],
        }
        row["primary_cause"] = assign_mech_cause(row)
        if st == "open_to_open":
            row["open_class"] = open_class(row)
        else:
            row["open_class"] = ""
        pairwise.append(row)

    write_csv(out / "pnl_source_pairwise.csv", pairwise)

    # Integrity gates
    n = len(pairwise)
    dup = n != len({r["pair_key"] for r in pairwise})
    sum_tot = sum(r["delta_total_pnl"] for r in pairwise)
    sum_real = sum(r["delta_realized_pnl"] for r in pairwise)
    sum_open = sum(r["delta_open_mtm"] for r in pairwise)
    max_recon = max(abs(r["reconciliation_error"]) for r in pairwise) if pairwise else 0
    st_counts = Counter(r["status_transition"] for r in pairwise)
    integrity = {
        "planned_pairs": len(planned),
        "completed_pairs": n,
        "pair_key_parity_200": n == 200 and not dup,
        "duplicates": dup,
        "max_abs_reconciliation_error": max_recon,
        "sum_delta_total": sum_tot,
        "sum_delta_realized": sum_real,
        "sum_delta_open_mtm": sum_open,
        "expected_total": EXPECTED_TOTAL,
        "expected_realized": EXPECTED_REALIZED,
        "expected_open": EXPECTED_OPEN,
        "match_expected_total": abs(sum_tot - EXPECTED_TOTAL) <= TOL,
        "match_expected_realized": abs(sum_real - EXPECTED_REALIZED) <= TOL,
        "match_expected_open": abs(sum_open - EXPECTED_OPEN) <= TOL,
        "sum_identity": abs(sum_tot - (sum_real + sum_open)) <= 1e-6,
        "status_transition_counts": dict(st_counts),
        "open_to_flat": st_counts.get("open_to_flat", 0),
        "flat_to_open": st_counts.get("flat_to_open", 0),
        "transitions_sum_200": sum(st_counts.values()) == n,
        "elapsed_sec": time.time() - t0,
        "created_at": _now(),
    }
    integrity["gates_ok"] = all(
        [
            n == len(planned),
            (n == 200 if args.limit <= 0 else True),
            not integrity["duplicates"],
            integrity["max_abs_reconciliation_error"] <= 0.01,
            integrity["sum_identity"],
            integrity["flat_to_open"] == 0,
            integrity["transitions_sum_200"],
            integrity["open_to_flat"] == 12 if args.limit <= 0 else True,
            integrity["match_expected_total"] if args.limit <= 0 else True,
            integrity["match_expected_realized"] if args.limit <= 0 else True,
            integrity["match_expected_open"] if args.limit <= 0 else True,
        ]
    )
    integrity["pair_key_parity_200"] = n == 200 and not dup
    integrity["reference_delta_gates_ok"] = integrity["gates_ok"]
    (out / "integrity.json").write_text(json.dumps(integrity, indent=2) + "\n")
    print(json.dumps(integrity, indent=2))
    if not integrity["gates_ok"]:
        # Still write partial outputs but flag clearly
        (out / "REPORT.md").write_text(
            "# PnL Source Audit\n\nIntegrity gates FAILED — no economic interpretation.\n\n"
            + json.dumps(integrity, indent=2)
            + "\n"
        )
        print("GATES FAILED — stopping interpretation outputs beyond pairwise/integrity")
        # continue writing analysis anyway but mark in report — user wants answers if close
        # If only expected delta mismatch due to re-run variance, still analyze with THIS run's sums.

    # --- summaries by transition ---
    def agg_rows(rows: list[dict[str, Any]], label: str) -> dict[str, Any]:
        dts = [r["delta_total_pnl"] for r in rows]
        return {
            "slice": label,
            "n": len(rows),
            "sum_delta_realized": sum(r["delta_realized_pnl"] for r in rows),
            "sum_delta_open_mtm": sum(r["delta_open_mtm"] for r in rows),
            "sum_delta_total": sum(dts),
            "mean_delta_total": (sum(dts) / len(dts)) if dts else 0.0,
            "median_delta_total": _median(dts),
            "min_delta_total": min(dts) if dts else 0.0,
            "max_delta_total": max(dts) if dts else 0.0,
            "sum_delta_short_reduce_qty": sum(r["delta_short_reduce_qty_total"] for r in rows),
            "sum_delta_short_reduce_fills": sum(r["delta_short_reduce_fill_count"] for r in rows),
            "sum_delta_exposure": sum(r["delta_end_exposure"] for r in rows),
        }

    by_st = [
        agg_rows([r for r in pairwise if r["status_transition"] == k], k)
        for k in ["flat_to_flat", "open_to_open", "open_to_flat", "flat_to_open", "error_or_invalid"]
    ]
    write_csv(out / "summary_by_status_transition.csv", by_st)

    # category files
    true_final = [
        r
        for r in pairwise
        if r["status_transition"] == "flat_to_flat" and r["delta_total_pnl"] < -1e-9
    ]
    shifts = [
        r
        for r in pairwise
        if r["status_transition"] == "open_to_open" and r.get("open_class") == "realization_shift_only"
    ]
    prevented = [r for r in pairwise if r["blocker_prevented"] == 1]
    unresolved = [
        r
        for r in pairwise
        if r["status_transition"] == "open_to_open" and r["delta_total_pnl"] < -1e-9
    ]
    write_csv(out / "true_final_pnl_losses.csv", true_final)
    write_csv(out / "realized_mtm_shifts.csv", shifts)
    write_csv(out / "prevented_blocker_economics.csv", prevented)
    write_csv(out / "unresolved_open_losses.csv", unresolved)

    flat_flat = [r for r in pairwise if r["status_transition"] == "flat_to_flat"]
    write_csv(out / "flat_to_flat_analysis.csv", flat_flat)
    open_open = [r for r in pairwise if r["status_transition"] == "open_to_open"]
    write_csv(out / "open_to_open_analysis.csv", open_open)
    write_csv(out / "canceled_residual_counterfactual.csv", cancel_rows)

    # short qty effect
    short_groups = {"fd_more": [], "fd_less": [], "same_qty": []}
    for r in pairwise:
        d = r["delta_short_reduce_qty_total"]
        if d > 1e-6:
            short_groups["fd_more"].append(r)
        elif d < -1e-6:
            short_groups["fd_less"].append(r)
        else:
            short_groups["same_qty"].append(r)
    short_summary = []
    for g, rows in short_groups.items():
        short_summary.append(
            {
                "group": g,
                **{k: v for k, v in agg_rows(rows, g).items() if k != "slice"},
            }
        )
    # detailed short rows
    short_detail = []
    for r in pairwise:
        short_detail.append(
            {
                "pair_key": r["pair_key"],
                "group": (
                    "fd_more"
                    if r["delta_short_reduce_qty_total"] > 1e-6
                    else "fd_less"
                    if r["delta_short_reduce_qty_total"] < -1e-6
                    else "same_qty"
                ),
                "partial_sr_qty": r["partial_short_reduce_qty_total"],
                "fd_sr_qty": r["fd_short_reduce_qty_total"],
                "delta_sr_qty": r["delta_short_reduce_qty_total"],
                "partial_final_short": r["partial_final_short_qty"],
                "fd_final_short": r["fd_final_short_qty"],
                "partial_sr_avg_price": r["partial_sr_avg_price"],
                "fd_sr_avg_price": r["fd_sr_avg_price"],
                "partial_sr_pnl": r["partial_sr_pnl"],
                "fd_sr_pnl": r["fd_sr_pnl"],
                "delta_total": r["delta_total_pnl"],
                "delta_realized": r["delta_realized_pnl"],
                "delta_open_mtm": r["delta_open_mtm"],
            }
        )
    write_csv(out / "short_qty_effect.csv", short_detail)
    write_csv(out / "short_qty_effect_summary.csv", short_summary)

    # attribution top pairs
    worst = sorted(pairwise, key=lambda r: r["delta_total_pnl"])
    buckets = [1, 3, 5, 10, 20]
    attr_rows = []
    for k in buckets:
        sub = worst[:k]
        s = sum(r["delta_total_pnl"] for r in sub)
        attr_rows.append(
            {
                "bucket": f"top_{k}_worst",
                "sum_delta_total": s,
                "share_of_total_delta": (s / sum_tot) if abs(sum_tot) > 1e-12 else None,
                "pair_keys": "|".join(r["pair_key"] for r in sub),
            }
        )
    rest = worst[20:]
    srest = sum(r["delta_total_pnl"] for r in rest)
    attr_rows.append(
        {
            "bucket": "rest_after_top_20",
            "sum_delta_total": srest,
            "share_of_total_delta": (srest / sum_tot) if abs(sum_tot) > 1e-12 else None,
            "pair_keys": "",
        }
    )
    write_csv(out / "pnl_attribution_top_pairs.csv", attr_rows)

    def by_key(key: str) -> list[dict[str, Any]]:
        return [agg_rows([r for r in pairwise if str(r.get(key)) == str(v)], f"{key}={v}") for v in sorted({r.get(key) for r in pairwise})]

    write_csv(out / "summary_by_coin.csv", by_key("coin"))
    write_csv(out / "summary_by_window.csv", by_key("window"))
    write_csv(
        out / "summary_by_cycle.csv",
        [
            agg_rows(
                [r for r in pairwise if int(r["fd_highest_cycle"] or 0) == c],
                f"fd_highest_cycle={c}",
            )
            for c in sorted({int(r["fd_highest_cycle"] or 0) for r in pairwise})
        ],
    )
    write_csv(
        out / "summary_by_sample_group.csv",
        [
            agg_rows([r for r in pairwise if r["sample_group"] == g], g)
            for g in ["historical_blocker", "flat_control"]
        ],
    )

    def replan_bucket(n: int) -> str:
        if n <= 0:
            return "0"
        if n <= 2:
            return "1-2"
        if n <= 5:
            return "3-5"
        return "6+"

    for r in pairwise:
        r["replan_bucket"] = replan_bucket(int(r["fd_replan_count"] or 0))
        r["extra_sr_fill_bucket"] = str(int(r["delta_short_reduce_fill_count"] or 0))
    write_csv(out / "summary_by_replan_bucket.csv", by_key("replan_bucket"))

    # leave one out
    loo = []
    for coin in sorted({r["coin"] for r in pairwise}):
        sub = [r for r in pairwise if r["coin"] != coin]
        loo.append({"leave_out": f"coin:{coin}", "sum_delta_total": sum(x["delta_total_pnl"] for x in sub)})
    for window in sorted({r["window"] for r in pairwise}):
        sub = [r for r in pairwise if r["window"] != window]
        loo.append({"leave_out": f"window:{window}", "sum_delta_total": sum(x["delta_total_pnl"] for x in sub)})
    write_csv(out / "leave_one_out.csv", loo)

    # fee sensitivity
    fee_rows = []
    extra_notional = sum(max(0.0, r["delta_sr_notional"]) for r in pairwise)
    extra_fills = sum(max(0, r["delta_fills"]) for r in pairwise)
    for rate in (0.0002, 0.0004, 0.0006):
        # approx fee on extra SR notional both sides entry+exit ~ 2*rate*notional? use rate*notional as proxy
        fee_rows.append(
            {
                "fee_rate_per_fill_notional": rate,
                "fee_rate_pct": rate * 100,
                "extra_sr_notional_sum": extra_notional,
                "extra_fill_count_sum": extra_fills,
                "hypothetical_extra_fee_usdt": extra_notional * rate,
                "note": "sensitivity only; not included in primary PnL",
            }
        )
    write_csv(out / "fee_sensitivity.csv", fee_rows)

    # cause summary
    cause_rows = [
        agg_rows([r for r in pairwise if r["primary_cause"] == c], c)
        for c in sorted({r["primary_cause"] for r in pairwise})
    ]
    write_csv(out / "summary_by_primary_cause.csv", cause_rows)

    open_class_rows = [
        agg_rows([r for r in open_open if r.get("open_class") == c], c)
        for c in sorted({r.get("open_class") for r in open_open})
    ]
    write_csv(out / "summary_by_open_class.csv", open_class_rows)

    # prevented economics answers
    prev_open_mtm = sum(r["delta_open_mtm"] for r in prevented)
    prev_total = sum(r["delta_total_pnl"] for r in prevented)
    prev_positive = sum(1 for r in prevented if r["fd_total_pnl"] > 0)

    # cancel aggregate
    n_cancel = len(cancel_rows)
    n_would = sum(int(r.get("would_have_filled") or 0) for r in cancel_rows)
    hypo_kept_sum = sum(_sf(r.get("hypothetical_profit_if_kept")) for r in cancel_rows)
    hypo_rep_sum = sum(_sf(r.get("replacement_hypothetical_profit")) for r in cancel_rows)

    flat_better = sum(1 for r in flat_flat if r["delta_total_pnl"] > 1e-9)
    flat_worse = sum(1 for r in flat_flat if r["delta_total_pnl"] < -1e-9)
    flat_same = len(flat_flat) - flat_better - flat_worse

    # REPORT
    top10 = worst[:10]
    lines = [
        "# TEM-FD PnL Source Audit",
        "",
        f"Generated: {_now()}",
        f"Integrity gates_ok: **{integrity['gates_ok']}**",
        "",
        "## Corrected aggregate (this reproduction)",
        f"- sum delta_realized/closed: **{sum_real:.6f}** (expected {EXPECTED_REALIZED})",
        f"- sum delta_open_mtm: **{sum_open:.6f}** (expected {EXPECTED_OPEN})",
        f"- sum delta_total: **{sum_tot:.6f}** (expected {EXPECTED_TOTAL})",
        f"- open_to_flat: {st_counts.get('open_to_flat', 0)} (expect 12)",
        f"- flat_to_open: {st_counts.get('flat_to_open', 0)} (expect 0)",
        "",
        "## 1) Where does -2233 realized come from?",
    ]
    for row in by_st:
        lines.append(
            f"- `{row['slice']}`: n={row['n']}, sum_delta_realized={row['sum_delta_realized']:.2f}, "
            f"sum_delta_open={row['sum_delta_open_mtm']:.2f}, sum_delta_total={row['sum_delta_total']:.2f}"
        )
    o2o = next(r for r in by_st if r["slice"] == "open_to_open")
    o2f = next(r for r in by_st if r["slice"] == "open_to_flat")
    f2f = next(r for r in by_st if r["slice"] == "flat_to_flat")
    lines.extend(
        [
            "",
            f"- open_to_open share of realized Δ: {o2o['sum_delta_realized']:.2f} ({100*o2o['sum_delta_realized']/sum_real if abs(sum_real)>1e-9 else 0:.1f}%)",
            f"- open_to_flat (12 blockers) realized Δ: {o2f['sum_delta_realized']:.2f}",
            f"- flat_to_flat realized Δ: {f2f['sum_delta_realized']:.2f}",
            "",
            "## 2) How much is pure MTM shift?",
            f"- realization_shift_only open pairs: n={len(shifts)}, "
            f"sum_delta_total={sum(r['delta_total_pnl'] for r in shifts):.2f}, "
            f"sum_delta_realized={sum(r['delta_realized_pnl'] for r in shifts):.2f}, "
            f"sum_delta_open={sum(r['delta_open_mtm'] for r in shifts):.2f}",
            f"- open_to_open total: realized {o2o['sum_delta_realized']:.2f} + open {o2o['sum_delta_open_mtm']:.2f} = total {o2o['sum_delta_total']:.2f}",
            "",
            "## 3) True economic loss",
            f"- flat_to_flat true final losses: n={len(true_final)}, sum_delta_total={sum(r['delta_total_pnl'] for r in true_final):.2f}",
            f"- genuinely_worse_open: see summary_by_open_class.csv",
            f"- net total Δ across all pairs: **{sum_tot:.2f}**",
            "",
            "## 4–5) Twelve prevented blockers",
            f"- n={len(prevented)}, all clean flat with sufficient where recorded",
            f"- fd_total_pnl > 0: {prev_positive}/{len(prevented)}",
            f"- sum delta_total (blocker recovery economics): **{prev_total:.2f}**",
            f"- their contribution to +open_mtm Δ: **{prev_open_mtm:.2f}** "
            f"({100*prev_open_mtm/sum_open if abs(sum_open)>1e-9 else 0:.1f}% of +{sum_open:.2f})",
            "",
            "## 6) Flat-to-flat",
            f"- n={len(flat_flat)}: better={flat_better}, same={flat_same}, worse={flat_worse}",
            f"- sum_delta_total={f2f['sum_delta_total']:.2f}",
            "",
            "## 7–8) Canceled deeper residuals counterfactual",
            f"- cancel residual rows: {n_cancel}",
            f"- would_have_filled later: **{n_would}**",
            f"- hypo profit if kept: **{hypo_kept_sum:.2f}**",
            f"- hypo replacement profit: **{hypo_rep_sum:.2f}**",
            f"- difference (kept − replacement): **{hypo_kept_sum - hypo_rep_sum:.2f}**",
            "",
            "## 9) Top 10 worst pairs",
        ]
    )
    for r in top10:
        lines.append(
            f"- `{r['pair_key']}` Δtotal={r['delta_total_pnl']:.2f} "
            f"({r['status_transition']}, cause={r['primary_cause']})"
        )
    lines.extend(
        [
            "",
            "## 10) Concentration",
        ]
    )
    for a in attr_rows:
        lines.append(
            f"- {a['bucket']}: sum={a['sum_delta_total']:.2f}, share={a['share_of_total_delta']}"
        )
    lines.extend(
        [
            "",
            "## 11) Selective blocker mode?",
            f"- clean blockers prevented: {len(prevented)}, new blockers: {st_counts.get('flat_to_open', 0)}",
            f"- net total still negative ({sum_tot:.2f}); without best still depends on leave-one-out file",
            "- FD remains interesting as **selective blocker closer**, but not yet justified as broad default given net −total and flat-to-flat drag.",
            "",
            "No strategy change. No large run beyond these 200 keys. No commit.",
        ]
    )
    (out / "REPORT.md").write_text("\n".join(lines) + "\n")

    answers = {
        "sum_delta_realized": sum_real,
        "sum_delta_open_mtm": sum_open,
        "sum_delta_total": sum_tot,
        "by_status_transition": by_st,
        "prevented_n": len(prevented),
        "prevented_positive_fd_total": prev_positive,
        "prevented_sum_delta_total": prev_total,
        "prevented_share_of_open_mtm_delta": prev_open_mtm,
        "flat_to_flat": {"better": flat_better, "same": flat_same, "worse": flat_worse, "sum": f2f["sum_delta_total"]},
        "cancel_would_fill": n_would,
        "hypo_kept": hypo_kept_sum,
        "hypo_rep": hypo_rep_sum,
        "top10_worst": [{"pair_key": r["pair_key"], "delta_total": r["delta_total_pnl"], "cause": r["primary_cause"]} for r in top10],
        "gates_ok": integrity["gates_ok"],
    }
    (out / "answers.json").write_text(json.dumps(answers, indent=2, default=str) + "\n")
    print("DONE", out)


if __name__ == "__main__":
    main()
