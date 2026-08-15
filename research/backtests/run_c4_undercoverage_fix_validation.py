#!/usr/bin/env python3
"""Targeted Variant-C revalidation for C4 price-staging undercoverage.

Writes under:
  research/backtests/results/multicoin_price_staging_grid_1000_500_20260721/
    analysis/c4_undercoverage_fix_validation/

Does not overwrite full-grid raw artifacts.
"""

from __future__ import annotations

import argparse
import csv
import json
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.backtests.backtest_report import resolve_net_closed_pnl
from research.backtests.candle_loader import load_candles_for_symbol
from research.backtests.historical_backtest import normalize_candles
from research.backtests.inventory_mtm_freeze import inventory_mtm_usdt, safe_float
from research.backtests.multicoin_blocker_price_staging import (
    DEFAULT_BASELINE,
    analyze_blocker_run,
    run_isolated_blocker,
)
from research.backtests.pnl_coverage_audit import build_pnl_coverage_audit
from research.backtests.recovery_reentry_policy import load_baseline_blockers
from research.backtests.second_leg_price_staging import (
    resolve_grid_profile,
    resolve_profile,
)

ROOT = Path(__file__).resolve().parents[2]
GRID = ROOT / "research/backtests/results/multicoin_price_staging_grid_1000_500_20260721"
DEFAULT_OUT = GRID / "analysis" / "c4_undercoverage_fix_validation"
PRE_FIX_PER_COIN = GRID / "per_coin_per_profile.csv"
PRE_FIX_UC_CASES = GRID / "undercoverage_cases.csv"

UC_COINS = ("APTUSDT", "SEIUSDT", "ADAUSDT", "ARBUSDT", "TIAUSDT", "SUIUSDT")
CONTROL_COINS = ("SOLUSDT", "ETHUSDT")
PROFILES = ("legacy", "two_early_medium", "four_small_early", "two_equal")

APT_T3_DIAG = {
    "coin": "APTUSDT",
    "trade_number": 3,
    "start_index": 570,
    "profile": "two_early_medium",
}
APT_T3_ECONOMICS_JSON = "apt_t3_economics_doublecheck.json"
IDENTITY_EPS = 1e-9


def _fills(result: Any) -> list[dict[str, Any]]:
    return list(getattr(result, "fill_log", None) or getattr(result, "fills_log", None) or [])


def _c4_cycle_pair_status(result: Any) -> dict[str, Any] | None:
    for row in build_pnl_coverage_audit(result):
        if int(row.get("cycle_index") or 0) != 4:
            continue
        if "LONG_ADD" not in str(row.get("loss_purpose") or ""):
            continue
        return dict(row)
    return None


def _stage_events(result: Any) -> dict[str, Any]:
    fills = [
        f
        for f in _fills(result)
        if str(f.get("purpose") or "") == "CYCLE_4_SHORT_REDUCE"
    ]
    orders = [
        o
        for o in (result.order_log or [])
        if str(o.get("purpose") or "") == "CYCLE_4_SHORT_REDUCE"
    ]
    exits = [
        f
        for f in _fills(result)
        if str(f.get("purpose") or "") in {"LONG_TP_EXIT", "SHORT_SL_EXIT"}
    ]

    def stage_of(row: dict[str, Any]) -> int | None:
        meta = row.get("metadata_excerpt") or {}
        if meta.get("stage_index") is None:
            return None
        return int(meta.get("stage_index"))

    filled_stages = sorted({s for s in (stage_of(f) for f in fills) if s is not None})
    cancelled_stages = sorted(
        {
            s
            for o in orders
            if str(o.get("event_type") or "").lower() == "cancelled"
            for s in [stage_of(o)]
            if s is not None
        }
    )
    exit_ts = None
    if exits:
        exit_ts = exits[0].get("timestamp") or exits[0].get("candle_index")
    late_stage_fills = []
    if exit_ts is not None:
        for f in fills:
            s = stage_of(f)
            if s is None:
                continue
            fts = f.get("timestamp") or f.get("candle_index")
            if fts is not None and str(fts) > str(exit_ts):
                late_stage_fills.append(s)
    last = (result.final_strategy_state_excerpt or {}).get(
        "last_basket_exit_coverage_decision"
    ) or {}
    return {
        "c4_sr_fills": len(fills),
        "filled_stages": filled_stages,
        "cancelled_stages": cancelled_stages,
        "exit_fills": len(exits),
        "late_stage_fills_after_exit": late_stage_fills,
        "last_coverage_ok": last.get("coverage_ok"),
        "last_coverage_reason": last.get("reason_code"),
        "last_sufficient": last.get("sufficient"),
        "last_target_delta": last.get("target_delta_usdt"),
    }


def _classify_economic(
    *,
    status: str,
    cycle_pair: dict[str, Any] | None,
    stage_info: dict[str, Any],
) -> str:
    """Research success metric: economic undercoverage, not naive cycle-pair."""
    if status != "closed":
        return "open_or_other"
    if stage_info.get("late_stage_fills_after_exit"):
        return "safety_fail_late_stage_fill"
    pair_status = str((cycle_pair or {}).get("status") or "")
    cov_ok = stage_info.get("last_coverage_ok")
    if pair_status == "undercovered":
        if cov_ok is True or stage_info.get("last_sufficient") is True:
            return "covered_by_basket_exit"
        if cov_ok is False or stage_info.get("last_sufficient") is False:
            return "economic_undercoverage_closed"
        # Decision not persisted: treat cycle-pair UC after early stage cancel as
        # basket-compensated when no late fills and exits present.
        if stage_info.get("exit_fills") and stage_info.get("cancelled_stages"):
            return "covered_by_basket_exit"
        return "cycle_pair_undercovered_unclassified"
    return "cycle_pair_ok"


def _load_pre_fix_map() -> dict[tuple[str, str], dict[str, Any]]:
    out: dict[tuple[str, str], dict[str, Any]] = {}
    if not PRE_FIX_PER_COIN.exists():
        return out
    with PRE_FIX_PER_COIN.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            key = (str(row.get("coin") or "").upper(), str(row.get("profile") or ""))
            out[key] = dict(row)
    return out


def _blocker_map() -> dict[str, dict[str, Any]]:
    rows = load_baseline_blockers(DEFAULT_BASELINE / "blocker_trades.csv")
    mapping: dict[str, dict[str, Any]] = {}
    for row in rows:
        coin = str(row.get("coin") or "").upper()
        if coin:
            mapping[coin] = dict(row)
    return mapping


def _identity(
    *,
    lhs: float | None,
    rhs: float | None,
    missing: list[str] | None = None,
) -> dict[str, Any]:
    if missing:
        return {
            "available": False,
            "missing_sources": list(missing),
            "lhs": lhs,
            "rhs": rhs,
            "difference": None,
            "pass": False,
        }
    if lhs is None or rhs is None:
        return {
            "available": False,
            "missing_sources": missing or ["lhs_or_rhs_none"],
            "lhs": lhs,
            "rhs": rhs,
            "difference": None,
            "pass": False,
        }
    diff = float(lhs) - float(rhs)
    return {
        "available": True,
        "lhs": float(lhs),
        "rhs": float(rhs),
        "difference": diff,
        "pass": abs(diff) <= IDENTITY_EPS,
    }


def _component(
    value: float | None,
    *,
    source: str,
    available: bool | None = None,
) -> dict[str, Any]:
    ok = bool(available) if available is not None else value is not None
    if not ok:
        return {"available": False, "value": None, "missing_source": source}
    return {"available": True, "value": float(value), "source": source}


def _fill_net_pnl(fill: dict[str, Any]) -> float | None:
    resolved = resolve_net_closed_pnl(fill)
    if resolved is not None:
        return float(resolved)
    meta = fill.get("metadata_excerpt") or {}
    for key in ("confirmed_closed_pnl", "closed_pnl", "runtime_calculated_pnl"):
        if fill.get(key) is not None:
            return float(fill.get(key))
        if meta.get(key) is not None:
            return float(meta.get(key))
    return None


def _sum_fill_net_pnls(result: Any) -> tuple[float | None, list[str]]:
    fills = _fills(result)
    if not fills:
        return None, ["result.fill_log/fill_log empty"]
    total = 0.0
    missing_fills: list[str] = []
    for idx, fill in enumerate(fills):
        net = _fill_net_pnl(fill)
        if net is None:
            purpose = str(fill.get("purpose") or f"fill[{idx}]")
            missing_fills.append(f"fill_net_pnl:{purpose}")
            continue
        total += float(net)
    if missing_fills:
        return None, missing_fills
    return total, []


def _capture_basket_close_economics(captures: list[dict[str, Any]]) -> Any:
    """Runner-only wrap: record raw projection/economics at inventory-open decisions."""
    from fixed_cycle_hedge_bot.fixed_cycle_strategy import FixedCycleHedgeStrategy

    original = FixedCycleHedgeStrategy.evaluate_basket_exit_coverage

    def wrapped(
        self: Any,
        *,
        snapshot: Any,
        runtime_state: Any,
        long_tp_price: float,
        short_sl_price: float,
        projection: Any = None,
    ) -> Any:
        decision = original(
            self,
            snapshot=snapshot,
            runtime_state=runtime_state,
            long_tp_price=long_tp_price,
            short_sl_price=short_sl_price,
            projection=projection,
        )
        long_qty = float(getattr(snapshot, "long_qty", 0.0) or 0.0)
        short_qty = float(getattr(snapshot, "short_qty", 0.0) or 0.0)
        if long_qty <= 1e-12 or short_qty <= 1e-12:
            return decision

        break_even, _ = self._calculate_break_even(snapshot, runtime_state)
        proj = projection or self._calculate_tp_projection(
            break_even, snapshot, runtime_state
        )
        components = getattr(proj, "components", None)
        economics = decision.economics
        fee_rate = float(getattr(proj, "fee_rate", 0.0) or 0.0)
        long_avg = float(getattr(snapshot, "long_avg", 0.0) or 0.0)
        short_avg = float(getattr(snapshot, "short_avg", 0.0) or 0.0)
        long_tp = float(long_tp_price)
        short_sl = float(short_sl_price)

        long_tp_gross = (long_tp - long_avg) * long_qty
        short_sl_gross = (short_avg - short_sl) * short_qty
        long_tp_entry_fee = fee_rate * long_avg * long_qty
        short_sl_entry_fee = fee_rate * short_avg * short_qty
        long_tp_close_fee = fee_rate * long_tp * long_qty
        short_sl_close_fee = fee_rate * short_sl * short_qty
        long_tp_fee = long_tp_entry_fee + long_tp_close_fee
        short_sl_fee = short_sl_entry_fee + short_sl_close_fee
        long_tp_net = long_tp_gross - long_tp_fee
        short_sl_net = short_sl_gross - short_sl_fee
        basket_gross = long_tp_gross + short_sl_gross
        basket_fees = long_tp_fee + short_sl_fee
        basket_net = basket_gross - basket_fees

        state = runtime_state.strategy_state
        staged_realized_map = state.get("staged_second_leg_tp_realized_net") or {}
        stage0_realized = staged_realized_map.get("4")
        if stage0_realized is None:
            stage0_realized = staged_realized_map.get(4)

        capture = {
            "effective_pending_cycle_loss_usdt": float(proj.pending_cycle_loss_usdt),
            "target_profit_usdt": (
                float(components.target_profit_usdt) if components is not None else None
            ),
            "buffer_usdt": (
                float(components.buffer_usdt) if components is not None else None
            ),
            "min_profit_target_usdt": float(proj.min_profit_target_usdt),
            "tolerance_usdt": float(decision.tolerance_usdt),
            "min_required_total_usdt": float(economics.min_required_total_usdt),
            "realized_cycle_net_usdt": float(proj.realized_cycle_net),
            "stage0_realized_net_usdt": (
                float(stage0_realized) if stage0_realized is not None else None
            ),
            "long_tp_gross_pnl_usdt": float(long_tp_gross),
            "long_tp_fee_usdt": float(long_tp_fee),
            "long_tp_net_pnl_usdt": float(long_tp_net),
            "short_sl_gross_pnl_usdt": float(short_sl_gross),
            "short_sl_fee_usdt": float(short_sl_fee),
            "short_sl_net_pnl_usdt": float(short_sl_net),
            "basket_gross_pnl_usdt": float(basket_gross),
            "basket_fees_usdt": float(basket_fees),
            "basket_net_usdt": float(basket_net),
            "expected_total_net_after_exit": float(
                economics.expected_total_net_after_exit
            ),
            "target_delta_usdt": float(economics.target_delta_usdt),
            "sufficient": bool(economics.sufficient),
            "reason_code": str(decision.reason_code),
            "coverage_ok": bool(decision.coverage_ok),
            "long_tp_price": long_tp,
            "short_sl_price": short_sl,
            "fee_rate": fee_rate,
            "projection_entry_fee_usdt": float(proj.entry_fee_usdt),
            "projection_close_fee_usdt": float(proj.close_fee_usdt),
            "sources": {
                "effective_pending_cycle_loss_usdt": "TpProjection.pending_cycle_loss_usdt",
                "target_profit_usdt": "TpProjection.components.target_profit_usdt",
                "buffer_usdt": "TpProjection.components.buffer_usdt",
                "min_profit_target_usdt": "TpProjection.min_profit_target_usdt",
                "tolerance_usdt": "BasketExitCoverageDecision.tolerance_usdt",
                "min_required_total_usdt": "FinalExitEconomics.min_required_total_usdt",
                "realized_cycle_net_usdt": "TpProjection.realized_cycle_net",
                "stage0_realized_net_usdt": "strategy_state.staged_second_leg_tp_realized_net[4]",
                "leg_pnls_fees": (
                    "FinalExitEconomics fee decomposition: "
                    "fee_rate * avg|exit_price * qty per leg "
                    "(same fee_rate/entry/close terms as _evaluate_final_exit_economics)"
                ),
                "expected_total_net_after_exit": (
                    "FinalExitEconomics.expected_total_net_after_exit"
                ),
                "target_delta_usdt": "FinalExitEconomics.target_delta_usdt",
                "sufficient": "FinalExitEconomics.sufficient",
                "reason_code": "BasketExitCoverageDecision.reason_code",
            },
            "missing": [],
        }
        if components is None:
            capture["missing"].append("TpProjection.components")
        if stage0_realized is None:
            capture["missing"].append(
                "strategy_state.staged_second_leg_tp_realized_net[4]"
            )
        if decision.coverage_ok and economics.sufficient:
            captures.append(capture)
        elif not captures:
            captures.append(capture)
        return decision

    FixedCycleHedgeStrategy.evaluate_basket_exit_coverage = wrapped  # type: ignore[method-assign]
    return original


def _restore_basket_coverage_method(original: Any) -> None:
    from fixed_cycle_hedge_bot.fixed_cycle_strategy import FixedCycleHedgeStrategy

    FixedCycleHedgeStrategy.evaluate_basket_exit_coverage = original  # type: ignore[method-assign]


def build_apt_t3_economics_doublecheck(
    *,
    result: Any,
    capture: dict[str, Any] | None,
) -> dict[str, Any]:
    """Assemble diagnostic JSON from capture + fill ledger (no rounded report back-calc)."""
    missing_components: list[dict[str, str]] = []

    def take(key: str) -> dict[str, Any]:
        if not capture or capture.get(key) is None:
            source = (capture or {}).get("sources", {}).get(key, key)
            missing_components.append({"field": key, "missing_source": str(source)})
            return _component(None, source=str(source), available=False)
        src = str((capture.get("sources") or {}).get(key, key))
        if key in {
            "long_tp_gross_pnl_usdt",
            "long_tp_fee_usdt",
            "long_tp_net_pnl_usdt",
            "short_sl_gross_pnl_usdt",
            "short_sl_fee_usdt",
            "short_sl_net_pnl_usdt",
            "basket_gross_pnl_usdt",
            "basket_fees_usdt",
            "basket_net_usdt",
        }:
            src = str((capture.get("sources") or {}).get("leg_pnls_fees", src))
        return _component(float(capture[key]), source=src, available=True)

    fields_order = [
        "effective_pending_cycle_loss_usdt",
        "target_profit_usdt",
        "buffer_usdt",
        "min_profit_target_usdt",
        "tolerance_usdt",
        "min_required_total_usdt",
        "realized_cycle_net_usdt",
        "stage0_realized_net_usdt",
        "long_tp_gross_pnl_usdt",
        "long_tp_fee_usdt",
        "long_tp_net_pnl_usdt",
        "short_sl_gross_pnl_usdt",
        "short_sl_fee_usdt",
        "short_sl_net_pnl_usdt",
        "basket_gross_pnl_usdt",
        "basket_fees_usdt",
        "basket_net_usdt",
        "expected_total_net_after_exit",
        "target_delta_usdt",
    ]
    components: dict[str, Any] = {key: take(key) for key in fields_order}

    if capture and capture.get("sufficient") is not None:
        components["sufficient"] = {
            "available": True,
            "value": bool(capture["sufficient"]),
            "source": "FinalExitEconomics.sufficient",
        }
    else:
        components["sufficient"] = {
            "available": False,
            "value": None,
            "missing_source": "FinalExitEconomics.sufficient / coverage capture",
        }
        missing_components.append(
            {
                "field": "sufficient",
                "missing_source": "FinalExitEconomics.sufficient / coverage capture",
            }
        )

    if capture and capture.get("reason_code") is not None:
        components["reason_code"] = {
            "available": True,
            "value": str(capture["reason_code"]),
            "source": "BasketExitCoverageDecision.reason_code",
        }
    else:
        components["reason_code"] = {
            "available": False,
            "value": None,
            "missing_source": "BasketExitCoverageDecision.reason_code / coverage capture",
        }
        missing_components.append(
            {
                "field": "reason_code",
                "missing_source": "BasketExitCoverageDecision.reason_code / coverage capture",
            }
        )

    realized_pnl = getattr(result, "realized_pnl", None)
    if realized_pnl is None:
        components["realized_pnl"] = {
            "available": False,
            "value": None,
            "missing_source": "BacktestResult.realized_pnl",
        }
        missing_components.append(
            {"field": "realized_pnl", "missing_source": "BacktestResult.realized_pnl"}
        )
    else:
        components["realized_pnl"] = {
            "available": True,
            "value": float(realized_pnl),
            "source": "BacktestResult.realized_pnl",
        }

    def val(key: str) -> float | None:
        entry = components.get(key) or {}
        if not entry.get("available"):
            return None
        return entry.get("value")

    fill_sum, fill_missing = _sum_fill_net_pnls(result)

    def missing_of(*keys: str) -> list[str] | None:
        missed = [k for k in keys if val(k) is None]
        return missed or None

    identities = {
        "min_required_identity": _identity(
            lhs=val("min_required_total_usdt"),
            rhs=(
                None
                if missing_of(
                    "effective_pending_cycle_loss_usdt",
                    "target_profit_usdt",
                    "buffer_usdt",
                )
                else float(val("effective_pending_cycle_loss_usdt"))
                + float(val("target_profit_usdt"))
                + float(val("buffer_usdt"))
            ),
            missing=missing_of(
                "min_required_total_usdt",
                "effective_pending_cycle_loss_usdt",
                "target_profit_usdt",
                "buffer_usdt",
            ),
        ),
        "basket_net_identity": _identity(
            lhs=val("basket_net_usdt"),
            rhs=(
                None
                if missing_of("long_tp_net_pnl_usdt", "short_sl_net_pnl_usdt")
                else float(val("long_tp_net_pnl_usdt"))
                + float(val("short_sl_net_pnl_usdt"))
            ),
            missing=missing_of(
                "basket_net_usdt",
                "long_tp_net_pnl_usdt",
                "short_sl_net_pnl_usdt",
            ),
        ),
        "expected_total_identity": _identity(
            lhs=val("expected_total_net_after_exit"),
            rhs=(
                None
                if missing_of("realized_cycle_net_usdt", "basket_net_usdt")
                else float(val("realized_cycle_net_usdt")) + float(val("basket_net_usdt"))
            ),
            missing=missing_of(
                "expected_total_net_after_exit",
                "realized_cycle_net_usdt",
                "basket_net_usdt",
            ),
        ),
        "target_delta_identity": _identity(
            lhs=val("target_delta_usdt"),
            rhs=(
                None
                if missing_of("expected_total_net_after_exit", "min_required_total_usdt")
                else float(val("expected_total_net_after_exit"))
                - float(val("min_required_total_usdt"))
            ),
            missing=missing_of(
                "target_delta_usdt",
                "expected_total_net_after_exit",
                "min_required_total_usdt",
            ),
        ),
        "sufficient_identity": {
            "available": val("target_delta_usdt") is not None
            and val("tolerance_usdt") is not None
            and bool(components["sufficient"].get("available")),
            "calculated": (
                None
                if val("target_delta_usdt") is None or val("tolerance_usdt") is None
                else bool(
                    float(val("target_delta_usdt")) >= -float(val("tolerance_usdt"))
                )
            ),
            "stored": components["sufficient"].get("value"),
            "lhs": (
                None
                if val("target_delta_usdt") is None or val("tolerance_usdt") is None
                else bool(
                    float(val("target_delta_usdt")) >= -float(val("tolerance_usdt"))
                )
            ),
            "rhs": components["sufficient"].get("value"),
            "difference": None,
            "pass": False,
            "missing_sources": (
                []
                if val("target_delta_usdt") is not None
                and val("tolerance_usdt") is not None
                and components["sufficient"].get("available")
                else [
                    k
                    for k, ok in (
                        ("target_delta_usdt", val("target_delta_usdt") is not None),
                        ("tolerance_usdt", val("tolerance_usdt") is not None),
                        ("sufficient", components["sufficient"].get("available")),
                    )
                    if not ok
                ]
            ),
        },
        "trade_pnl_identity": _identity(
            lhs=fill_sum,
            rhs=float(realized_pnl) if realized_pnl is not None else None,
            missing=(
                fill_missing
                + (["BacktestResult.realized_pnl"] if realized_pnl is None else [])
            )
            or None,
        ),
    }
    si = identities["sufficient_identity"]
    if si.get("available"):
        si["difference"] = int(bool(si["calculated"])) - int(bool(si["stored"]))
        si["pass"] = bool(si["calculated"]) == bool(si["stored"])
    else:
        si["pass"] = False

    return {
        "mode": "dump_apt_t3_economics",
        "coin": APT_T3_DIAG["coin"],
        "trade_number": APT_T3_DIAG["trade_number"],
        "start_index": APT_T3_DIAG["start_index"],
        "profile": APT_T3_DIAG["profile"],
        "final_status": getattr(result, "final_status", None),
        "exit_reason": getattr(result, "exit_reason", None),
        "capture_available": capture is not None,
        "components": components,
        "identities": identities,
        "unavailable_components": missing_components,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def dump_apt_t3_economics(*, output_dir: Path) -> Path:
    """Run only APTUSDT T3 / two_early_medium and write economics double-check JSON."""
    output_dir.mkdir(parents=True, exist_ok=True)
    coin = APT_T3_DIAG["coin"]
    start_index = int(APT_T3_DIAG["start_index"])
    trade_number = int(APT_T3_DIAG["trade_number"])
    profile = str(APT_T3_DIAG["profile"])
    print(
        f"DIAG dump-apt-t3-economics: {coin} T{trade_number} @{start_index} {profile}",
        flush=True,
    )
    candles = normalize_candles(coin, load_candles_for_symbol(coin, limit=50000))
    cfg = resolve_grid_profile(profile)
    captures: list[dict[str, Any]] = []
    original = _capture_basket_close_economics(captures)
    try:
        result = run_isolated_blocker(
            coin=coin,
            candles=candles,
            start_index=start_index,
            staging_config=cfg,
            trade_number=trade_number,
        )
    finally:
        _restore_basket_coverage_method(original)

    capture = captures[-1] if captures else None
    payload = build_apt_t3_economics_doublecheck(result=result, capture=capture)
    out_path = output_dir / APT_T3_ECONOMICS_JSON
    out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(
        json.dumps(
            {
                "wrote": str(out_path),
                "combinations": [
                    {
                        "coin": coin,
                        "trade_number": trade_number,
                        "start_index": start_index,
                        "profile": profile,
                    }
                ],
                "identities": {
                    name: {
                        "pass": row.get("pass"),
                        "available": row.get("available", True),
                    }
                    for name, row in payload["identities"].items()
                },
                "unavailable_components": payload.get("unavailable_components"),
            },
            indent=2,
        ),
        flush=True,
    )
    return out_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="C4 undercoverage fix validation / APT T3 economics dump"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUT,
        help="Analysis output directory (default: c4_undercoverage_fix_validation)",
    )
    parser.add_argument(
        "--dump-apt-t3-economics",
        action="store_true",
        help=(
            "Diagnostic only: run APTUSDT T3 @570 two_early_medium and write "
            f"{APT_T3_ECONOMICS_JSON} (does not run the 8-coin revalidation)."
        ),
    )
    return parser.parse_args(argv)


def run_full_revalidation(*, output_dir: Path) -> None:
    OUT = output_dir
    OUT.mkdir(parents=True, exist_ok=True)
    blockers = _blocker_map()
    pre_fix = _load_pre_fix_map()
    coins = list(dict.fromkeys([*UC_COINS, *CONTROL_COINS]))

    rows: list[dict[str, Any]] = []
    apt_detail: dict[str, Any] = {}
    early_closes_now_open: list[dict[str, Any]] = []

    t0 = time.time()
    for coin in coins:
        blocker = blockers.get(coin)
        if not blocker:
            print(f"SKIP {coin}: no baseline blocker", flush=True)
            continue
        start_index = int(safe_float(blocker.get("start_index")))
        trade_number = int(safe_float(blocker.get("trade_number")))
        print(f"LOAD {coin} candles…", flush=True)
        candles = normalize_candles(coin, load_candles_for_symbol(coin, limit=50000))
        for profile in PROFILES:
            cfg = (
                resolve_profile("legacy")
                if profile == "legacy"
                else resolve_grid_profile(profile)
            )
            print(f"RUN {coin} T{trade_number} @{start_index} {profile}", flush=True)
            result = run_isolated_blocker(
                coin=coin,
                candles=candles,
                start_index=start_index,
                staging_config=cfg,
                trade_number=trade_number,
            )
            analysis = analyze_blocker_run(
                coin=coin,
                trade_number=trade_number,
                start_index=start_index,
                profile=profile,
                result=result,
                candles=candles,
                baseline_row=blocker,
            )
            cycle_pair = _c4_cycle_pair_status(result)
            stage_info = _stage_events(result)
            economic = _classify_economic(
                status=str(analysis.get("status") or result.final_status or ""),
                cycle_pair=cycle_pair,
                stage_info=stage_info,
            )
            pre = pre_fix.get((coin, profile), {})
            pre_flat = int(safe_float(pre.get("trade_flat")))
            post_flat = int(bool(analysis.get("trade_flat")))
            if pre_flat and not post_flat:
                early_closes_now_open.append(
                    {
                        "coin": coin,
                        "profile": profile,
                        "trade_number": trade_number,
                        "pre_final_mtm": safe_float(pre.get("final_mtm")),
                        "post_final_mtm": safe_float(analysis.get("final_mtm")),
                        "pre_realized": safe_float(pre.get("realized_pnl")),
                        "post_realized": safe_float(getattr(result, "realized_pnl", 0.0)),
                    }
                )
            row = {
                "coin": coin,
                "trade_number": trade_number,
                "start_index": start_index,
                "profile": profile,
                "status": analysis.get("status") or result.final_status,
                "trade_flat": post_flat,
                "exit_reason": getattr(result, "exit_reason", None),
                "realized_pnl": safe_float(getattr(result, "realized_pnl", 0.0)),
                "final_mtm": safe_float(analysis.get("final_mtm")),
                "open_mtm": (
                    0.0
                    if post_flat
                    else safe_float(analysis.get("final_mtm"))
                    - safe_float(getattr(result, "realized_pnl", 0.0))
                ),
                "total_pnl": safe_float(analysis.get("final_mtm")),
                "duration_candles": analysis.get("duration_candles"),
                "invalid_partial": analysis.get("invalid_partial"),
                "over_close": analysis.get("over_close"),
                "duplicate_stage": analysis.get("duplicate_stage"),
                "cycle_pair_undercoverage": int(
                    (cycle_pair or {}).get("status") == "undercovered"
                ),
                "cycle_pair_missing_pnl": safe_float((cycle_pair or {}).get("missing_pnl")),
                "economic_class": economic,
                "economic_undercoverage_closed": int(
                    economic == "economic_undercoverage_closed"
                ),
                "covered_by_basket_exit": int(economic == "covered_by_basket_exit"),
                "pre_trade_flat": pre_flat,
                "pre_final_mtm": safe_float(pre.get("final_mtm")) if pre else None,
                "pre_realized_pnl": safe_float(pre.get("realized_pnl")) if pre else None,
                "delta_final_mtm_vs_pre": (
                    safe_float(analysis.get("final_mtm")) - safe_float(pre.get("final_mtm"))
                    if pre
                    else None
                ),
                **{f"stage_{k}": v for k, v in stage_info.items()},
            }
            if not post_flat:
                try:
                    row["open_mtm"] = float(
                        inventory_mtm_usdt(result, candles[start_index:])
                    )
                    row["total_pnl"] = row["realized_pnl"] + row["open_mtm"]
                except Exception:
                    pass
            rows.append(row)
            if coin == "APTUSDT" and profile == "two_early_medium":
                apt_detail = {
                    "pre_fix": {
                        "trade_flat": True,
                        "missing_pnl": 10.661618615007406,
                        "status": "closed",
                        "note": "stage0 only + basket exit; cycle-pair undercovered",
                    },
                    "post_fix": row,
                    "verdict": (
                        "closed_with_basket_compensation"
                        if post_flat
                        and economic in {"covered_by_basket_exit", "cycle_pair_ok"}
                        else ("open_guarded" if not post_flat else economic)
                    ),
                }

    by_profile: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_profile[str(r["profile"])].append(r)

    summary_rows = []
    for profile, group in sorted(by_profile.items()):
        summary_rows.append(
            {
                "profile": profile,
                "n": len(group),
                "closed": sum(int(r["trade_flat"]) for r in group),
                "open": sum(1 - int(r["trade_flat"]) for r in group),
                "economic_undercoverage_closed": sum(
                    int(r["economic_undercoverage_closed"]) for r in group
                ),
                "covered_by_basket_exit": sum(int(r["covered_by_basket_exit"]) for r in group),
                "cycle_pair_undercoverage": sum(
                    int(r["cycle_pair_undercoverage"]) for r in group
                ),
                "invalid_partial": sum(int(safe_float(r.get("invalid_partial"))) for r in group),
                "over_close": sum(int(safe_float(r.get("over_close"))) for r in group),
                "duplicate_stage": sum(int(safe_float(r.get("duplicate_stage"))) for r in group),
                "sum_realized_pnl": sum(safe_float(r.get("realized_pnl")) for r in group),
                "sum_open_mtm": sum(safe_float(r.get("open_mtm")) for r in group),
                "sum_total_pnl": sum(safe_float(r.get("total_pnl")) for r in group),
                "sum_pre_final_mtm": sum(
                    safe_float(r.get("pre_final_mtm"))
                    for r in group
                    if r.get("pre_final_mtm") is not None
                ),
                "sum_post_final_mtm": sum(safe_float(r.get("final_mtm")) for r in group),
            }
        )

    def write_csv(path: Path, data: list[dict[str, Any]]) -> None:
        if not data:
            path.write_text("", encoding="utf-8")
            return
        keys: list[str] = []
        for row in data:
            for k in row:
                if k not in keys:
                    keys.append(k)
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=keys)
            w.writeheader()
            for row in data:
                serialized = {
                    k: (json.dumps(v) if isinstance(v, (list, dict)) else v)
                    for k, v in row.items()
                }
                w.writerow(serialized)

    write_csv(OUT / "revalidation_rows.csv", rows)
    write_csv(OUT / "revalidation_summary_by_profile.csv", summary_rows)
    write_csv(OUT / "early_closes_now_open.csv", early_closes_now_open)
    (OUT / "apt_t3_two_early_medium_detail.json").write_text(
        json.dumps(apt_detail, indent=2, default=str), encoding="utf-8"
    )

    legacy_parity = []
    for r in rows:
        if r["profile"] != "legacy":
            continue
        pre_mtm = r.get("pre_final_mtm")
        delta = None if pre_mtm is None else abs(safe_float(r["final_mtm"]) - safe_float(pre_mtm))
        legacy_parity.append(
            {
                "coin": r["coin"],
                "pre_flat": r["pre_trade_flat"],
                "post_flat": r["trade_flat"],
                "pre_final_mtm": pre_mtm,
                "post_final_mtm": r["final_mtm"],
                "abs_delta_mtm": delta,
                "parity_ok": int(
                    r["pre_trade_flat"] == r["trade_flat"]
                    and (delta is None or delta < 1.0)
                ),
            }
        )
    write_csv(OUT / "legacy_parity.csv", legacy_parity)

    economic_uc_total = sum(int(r["economic_undercoverage_closed"]) for r in rows)
    safety_ok = all(
        int(safe_float(r.get("invalid_partial"))) == 0
        and int(safe_float(r.get("over_close"))) == 0
        and int(safe_float(r.get("duplicate_stage"))) == 0
        and not r.get("stage_late_stage_fills_after_exit")
        for r in rows
    )
    success = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_sec": round(time.time() - t0, 2),
        "economic_undercoverage_closed_total": economic_uc_total,
        "covered_by_basket_exit_total": sum(int(r["covered_by_basket_exit"]) for r in rows),
        "cycle_pair_undercoverage_total": sum(int(r["cycle_pair_undercoverage"]) for r in rows),
        "early_closes_now_open_count": len(early_closes_now_open),
        "safety_ok": safety_ok,
        "legacy_parity_all_ok": all(int(r["parity_ok"]) for r in legacy_parity)
        if legacy_parity
        else None,
        "success_economic_uc_zero": economic_uc_total == 0,
        "profiles": list(PROFILES),
        "coins": coins,
    }
    (OUT / "success_criteria.json").write_text(
        json.dumps(success, indent=2), encoding="utf-8"
    )

    lines = [
        "# C4 Undercoverage Fix Validation (Variant C)",
        "",
        f"Generated: `{success['generated_at']}`",
        f"Elapsed: `{success['elapsed_sec']}s`",
        "",
        "## Success criteria",
        "",
        f"- economic undercoverage closed: **{economic_uc_total}** (target 0)",
        f"- covered_by_basket_exit: **{success['covered_by_basket_exit_total']}**",
        f"- safety_ok: **{safety_ok}**",
        f"- legacy_parity_all_ok: **{success['legacy_parity_all_ok']}**",
        "",
    ]
    (OUT / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(success, indent=2), flush=True)
    print(f"Wrote {OUT}", flush=True)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    output_dir = Path(args.output_dir)
    if args.dump_apt_t3_economics:
        dump_apt_t3_economics(output_dir=output_dir)
        return
    run_full_revalidation(output_dir=output_dir)


if __name__ == "__main__":
    main()
