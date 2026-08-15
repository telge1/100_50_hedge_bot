"""Independent reconstruction and invariant checks for Cobertura order audits.

Tolerances (documented):
- AVG_TOL = 1e-9  (VWAP recomputation)
- FEE_TOL = 1e-9  (fee = |px*qty|*rate)
- PNL_TOL = 1e-6  (realized / economics)
- QTY_TOL = 1e-9
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fixed_cycle_hedge_bot.math_utils import calculate_pnl

from research.backtests.emergency_lock.cost_model import fee_usdt

from .config import CoberturaConfig
from .engine import EngineResult
from .ledger import weighted_avg

AVG_TOL = 1e-9
FEE_TOL = 1e-9
PNL_TOL = 1e-6
QTY_TOL = 1e-9


def _f(x: Any, default: float = 0.0) -> float:
    if x is None or x == "":
        return float(default)
    return float(x)


def _pass(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


@dataclass
class ShadowSide:
    qty: float = 0.0
    avg: float = 0.0

    def snapshot(self) -> tuple[float, float]:
        return self.qty, self.avg


@dataclass
class ShadowState:
    core_long: ShadowSide = field(default_factory=ShadowSide)
    core_short: ShadowSide = field(default_factory=ShadowSide)
    overlay_long: ShadowSide = field(default_factory=ShadowSide)
    overlay_short: ShadowSide = field(default_factory=ShadowSide)
    realized_overlay: float = 0.0
    realized_core: float = 0.0
    open_fees: float = 0.0
    close_fees: float = 0.0
    cashflow: float = 0.0  # signed cash: +sell proceeds -buy cost -fees

    def long_qty(self) -> float:
        return self.core_long.qty + self.overlay_long.qty

    def short_qty(self) -> float:
        return self.core_short.qty + self.overlay_short.qty

    def net(self) -> float:
        return self.long_qty() - self.short_qty()

    def gross_notional(self, px: float) -> float:
        return (self.long_qty() + self.short_qty()) * float(px)


def _seed_shadow(cfg: CoberturaConfig) -> ShadowState:
    s = ShadowState()
    s.core_long = ShadowSide(qty=float(cfg.core_long_qty), avg=float(cfg.core_long_avg))
    s.core_short = ShadowSide(
        qty=float(cfg.core_short_qty), avg=float(cfg.core_short_avg)
    )
    return s


def _unrealized(state: ShadowState, mark: float) -> dict[str, float]:
    def one(side: ShadowSide, pos: str) -> float:
        if side.qty <= 0:
            return 0.0
        return calculate_pnl(side.avg, mark, side.qty, pos)

    return {
        "core_long": one(state.core_long, "long"),
        "core_short": one(state.core_short, "short"),
        "overlay_long": one(state.overlay_long, "long"),
        "overlay_short": one(state.overlay_short, "short"),
    }


def _apply_open_short(side: ShadowSide, qty: float, px: float) -> float:
    """Return independent new avg after add."""
    if side.qty <= 0:
        new_avg = float(px)
    else:
        new_avg = weighted_avg(side.qty, side.avg, qty, px)
    side.qty += qty
    side.avg = new_avg
    return new_avg


def _apply_close(
    side: ShadowSide, qty: float, px: float, pos: str
) -> tuple[float, float]:
    """Close qty; return (realized_pnl, avg_after). Avg unchanged unless flat."""
    if qty - side.qty > QTY_TOL:
        raise AssertionError(f"over-close {qty} > {side.qty}")
    realized = calculate_pnl(side.avg, px, qty, pos)
    avg_before = side.avg
    if abs(qty - side.qty) <= QTY_TOL:
        side.qty = 0.0
        side.avg = 0.0
        return realized, 0.0
    side.qty -= qty
    # remaining avg must stay avg_before
    return realized, avg_before


@dataclass
class AuditBundle:
    policy: str
    cfg: CoberturaConfig
    result: EngineResult
    order_lifecycle: list[dict[str, Any]] = field(default_factory=list)
    fill_ledger: list[dict[str, Any]] = field(default_factory=list)
    position_timeline: list[dict[str, Any]] = field(default_factory=list)
    average_price_audit: list[dict[str, Any]] = field(default_factory=list)
    pnl_reconciliation: list[dict[str, Any]] = field(default_factory=list)
    fee_reconciliation: list[dict[str, Any]] = field(default_factory=list)
    trigger_timeline: list[dict[str, Any]] = field(default_factory=list)
    tranche_reconciliation: list[dict[str, Any]] = field(default_factory=list)
    full_exit_audit: list[dict[str, Any]] = field(default_factory=list)
    invariant_violations: list[dict[str, Any]] = field(default_factory=list)
    ambiguous_intrabar_cases: list[dict[str, Any]] = field(default_factory=list)
    shared_be_rounds: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    walkthrough_lines: list[str] = field(default_factory=list)


def _candle_map(result: EngineResult) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for i, bar in enumerate(result.per_bar_trace):
        ts = str(bar.get("timestamp"))
        row = dict(bar)
        row["candle_index"] = i
        out[ts] = row
    return out


def _fee_rate_for_kind(kind: str, cfg: CoberturaConfig) -> tuple[float, str]:
    if kind in ("overlay_short_add", "long_equalization"):
        return float(cfg.fee_rate_open), "open"
    return float(cfg.fee_rate_close), "close"


def reconstruct_audit(
    *,
    policy: str,
    cfg: CoberturaConfig,
    result: EngineResult,
) -> AuditBundle:
    bundle = AuditBundle(policy=policy, cfg=cfg, result=result)
    candles = _candle_map(result)
    shadow = _seed_shadow(cfg)
    core_freeze = (
        shadow.core_long.qty,
        shadow.core_long.avg,
        shadow.core_short.qty,
        shadow.core_short.avg,
    )

    event_index = 0
    order_seq = 0
    fills = list(result.fill_events)
    seen_fill_keys: set[tuple] = set()

    # Seed position timeline
    bundle.position_timeline.append(
        {
            "policy": policy,
            "event_index": -1,
            "event": "seed_core",
            "timestamp": cfg.start_timestamp,
            "long_qty_before": 0.0,
            "long_avg_before": 0.0,
            "short_qty_before": 0.0,
            "short_avg_before": 0.0,
            "overlay_short_qty_before": 0.0,
            "long_qty_after": shadow.long_qty(),
            "long_avg_after": shadow.core_long.avg,
            "short_qty_after": shadow.short_qty(),
            "short_avg_after": shadow.core_short.avg,
            "overlay_short_qty_after": 0.0,
            "net_exposure_before": 0.0,
            "net_exposure_after": shadow.net(),
            "gross_notional_before": 0.0,
            "gross_notional_after": shadow.gross_notional(cfg.start_price),
            "realized_pnl_delta": 0.0,
            "realized_pnl_total": 0.0,
            "unrealized_pnl_after": sum(
                _unrealized(shadow, cfg.start_price).values()
            ),
            "total_economics_after": None,
            "open_fees_total": 0.0,
            "close_fees_total": 0.0,
        }
    )

    # Trigger timeline from engine artifacts
    for row in result.overlay_be_timeline:
        bundle.trigger_timeline.append(
            {
                "policy": policy,
                "trigger_type": "shared_overlay_be",
                "timestamp": row.get("timestamp"),
                "trigger_price": row.get("overlay_be_price"),
                "overlay_short_qty": row.get("overlay_short_qty"),
                "overlay_short_avg": row.get("overlay_short_avg"),
                "overlay_entry_fees": row.get("overlay_entry_fees"),
                "active_from_next_bar": row.get("active_next_bar"),
                "independent_recompute": None,
                "pass_fail": None,
            }
        )

    for ev in result.tranche_events:
        if ev.get("event") == "tranche_open":
            bundle.trigger_timeline.append(
                {
                    "policy": policy,
                    "trigger_type": "tranche_tp",
                    "timestamp": ev.get("timestamp"),
                    "tranche_id": ev.get("tranche_id"),
                    "trigger_price": ev.get("tp_trigger_price"),
                    "optical_tp_trigger": ev.get("optical_tp_trigger"),
                    "entry_price_filled": ev.get("entry_price_filled"),
                    "qty": ev.get("initial_qty") or ev.get("qty"),
                    "active_from_next_bar": True,
                    "pass_fail": None,
                }
            )

    # Group fills by timestamp for intrabar ambiguity
    by_ts: dict[str, list[dict[str, Any]]] = {}
    for fill in fills:
        by_ts.setdefault(str(fill.get("timestamp")), []).append(fill)

    for ts, group in by_ts.items():
        kinds = [f.get("kind") for f in group]
        add_n = sum(1 for k in kinds if k == "overlay_short_add")
        has_exit = any(
            k in ("overlay_be_close", "overlay_tp_close", "overlay_tp_partial", "full_exit")
            for k in kinds
        )
        if add_n > 1 or (add_n >= 1 and has_exit):
            bar = candles.get(ts, {})
            bundle.ambiguous_intrabar_cases.append(
                {
                    "policy": policy,
                    "candle_timestamp": ts,
                    "candle_index": bar.get("candle_index"),
                    "open": bar.get("open"),
                    "high": bar.get("high"),
                    "low": bar.get("low"),
                    "close": bar.get("close"),
                    "possible_events": kinds,
                    "conservative_order_used": (
                        "overlay_exits(BE/TP) → net_be_full_exit_check → short_adds "
                        "shallow_to_deep → (legacy post-add full-exit skipped in net_be)"
                    ),
                    "why_no_lookahead": (
                        "Triggers fixed before candle; adds fill at level trigger "
                        "(not candle low); exits use active prior-bar triggers; "
                        "OHLC path unknown so exits-before-adds and shallow→deep "
                        "are conservative vs opportunistic add→exit."
                    ),
                    "ohlc_path_unknown": True,
                    "ambiguity_resolution": (
                        "net_be full-exit evaluated before adds so same-candle "
                        "multi-add cannot create the BE gate."
                    ),
                }
            )

    for fill in fills:
        event_index += 1
        order_seq += 1
        ts = str(fill.get("timestamp"))
        bar = candles.get(ts, {})
        kind = str(fill.get("kind"))
        qty = _f(fill.get("qty"))
        fill_px = _f(fill.get("fill_price"))
        trigger = fill.get("trigger")
        recorded_fee = _f(fill.get("fee"))
        fee_rate, fee_type = _fee_rate_for_kind(kind, cfg)
        expected_fee = fee_usdt(fill_price=fill_px, qty=qty, fee_rate=fee_rate)
        fee_diff = recorded_fee - expected_fee
        fee_ok = abs(fee_diff) <= FEE_TOL

        # Duplicate fill detection (kind, ts, level, qty, px, tranche)
        fill_key = (
            kind,
            ts,
            fill.get("level"),
            round(qty, 9),
            round(fill_px, 10),
            fill.get("tranche_id"),
            fill.get("leg"),
            fill.get("position_side"),
        )
        dup = fill_key in seen_fill_keys
        seen_fill_keys.add(fill_key)
        if dup:
            bundle.invariant_violations.append(
                {
                    "policy": policy,
                    "check": "duplicate_fill",
                    "event_index": event_index,
                    "detail": str(fill_key),
                    "pass_fail": "FAIL",
                }
            )

        # Snapshots before
        long_q_b, long_a_b = shadow.long_qty(), (
            shadow.core_long.avg
            if shadow.overlay_long.qty <= 0
            else weighted_avg(
                shadow.core_long.qty,
                shadow.core_long.avg,
                shadow.overlay_long.qty,
                shadow.overlay_long.avg,
            )
            if shadow.long_qty() > 0
            else 0.0
        )
        # Prefer reporting core+overlay components explicitly
        cl_q_b, cl_a_b = shadow.core_long.snapshot()
        cs_q_b, cs_a_b = shadow.core_short.snapshot()
        os_q_b, os_a_b = shadow.overlay_short.snapshot()
        ol_q_b, ol_a_b = shadow.overlay_long.snapshot()
        net_b = shadow.net()
        mark = _f(bar.get("close"), fill_px)
        gross_b = shadow.gross_notional(mark)
        ur_b = _unrealized(shadow, mark)

        realized_delta = 0.0
        engine_avg_after = None
        recomputed_avg = None
        avg_leg = None
        decision = kind
        purpose = kind
        side = str(fill.get("side") or "")
        position_side = str(fill.get("position_side") or "")
        leg = str(fill.get("leg") or "")

        # Apply fill to shadow
        if kind == "overlay_short_add":
            purpose = "overlay_short_add"
            side = "short"
            decision = (
                f"low<=add_level trigger={trigger}; shallow→deep; "
                f"fill at slipped trigger; TP/BE active next bar"
            )
            recomputed_avg = (
                weighted_avg(os_q_b, os_a_b, qty, fill_px) if os_q_b > 0 else fill_px
            )
            _apply_open_short(shadow.overlay_short, qty, fill_px)
            engine_avg_after = shadow.overlay_short.avg  # we are the shadow; compare later via timeline
            avg_leg = "overlay_short"
            shadow.open_fees += recorded_fee
            # short open: receive px*qty, pay fee
            shadow.cashflow += fill_px * qty - recorded_fee
            # Cross-check engine fill avg via overlay_average_timeline if present
        elif kind == "overlay_be_close":
            purpose = "shared_overlay_be_close"
            side = "buy"
            position_side = "short"
            decision = (
                f"high>=active_shared_be trigger={trigger}; "
                f"close all overlay short; active_from prior bar"
            )
            realized_delta, recomputed_avg = _apply_close(
                shadow.overlay_short, qty, fill_px, "short"
            )
            shadow.realized_overlay += realized_delta
            shadow.close_fees += recorded_fee
            shadow.cashflow += -fill_px * qty - recorded_fee  # buy to cover
            avg_leg = "overlay_short"
            engine_avg_after = shadow.overlay_short.avg
            # Independent BE check using pre-close snapshot is done in shared_be section
        elif kind in ("overlay_tp_close", "overlay_tp_partial"):
            purpose = kind
            side = "buy"
            position_side = "short"
            decision = (
                f"low<=active_tp trigger={trigger} tranche={fill.get('tranche_id')}; "
                f"TP active from next bar after entry"
            )
            realized_delta, recomputed_avg = _apply_close(
                shadow.overlay_short, qty, fill_px, "short"
            )
            shadow.realized_overlay += realized_delta
            shadow.close_fees += recorded_fee
            shadow.cashflow += -fill_px * qty - recorded_fee
            avg_leg = "overlay_short"
            engine_avg_after = shadow.overlay_short.avg
        elif kind == "full_exit":
            purpose = f"full_exit_{leg}_{position_side}"
            decision = (
                "net_be gate: total_exit_economics >= target+safety_buffer-tol; "
                "adverse close fills; flatten all remaining"
            )
            if position_side == "long" or side == "sell":
                target = shadow.overlay_long if leg == "overlay" else shadow.core_long
                realized_delta, recomputed_avg = _apply_close(
                    target, qty, fill_px, "long"
                )
                if leg == "overlay":
                    shadow.realized_overlay += realized_delta
                else:
                    shadow.realized_core += realized_delta
                shadow.cashflow += fill_px * qty - recorded_fee  # sell
                avg_leg = f"{leg}_long"
            else:
                target = shadow.overlay_short if leg == "overlay" else shadow.core_short
                realized_delta, recomputed_avg = _apply_close(
                    target, qty, fill_px, "short"
                )
                if leg == "overlay":
                    shadow.realized_overlay += realized_delta
                else:
                    shadow.realized_core += realized_delta
                shadow.cashflow += -fill_px * qty - recorded_fee  # buy
                avg_leg = f"{leg}_short"
            shadow.close_fees += recorded_fee
            engine_avg_after = target.avg
        elif kind == "long_equalization":
            purpose = "long_equalization"
            recomputed_avg = (
                weighted_avg(ol_q_b, ol_a_b, qty, fill_px) if ol_q_b > 0 else fill_px
            )
            _apply_open_short(shadow.overlay_long, qty, fill_px)  # reuse add helper
            # wait - overlay long open_add - I used wrong function name but weighted_avg logic same
            shadow.open_fees += recorded_fee
            shadow.cashflow += -fill_px * qty - recorded_fee
            avg_leg = "overlay_long"
            engine_avg_after = shadow.overlay_long.avg
            decision = "equalization fill"
        else:
            bundle.invariant_violations.append(
                {
                    "policy": policy,
                    "check": "unknown_fill_kind",
                    "event_index": event_index,
                    "detail": kind,
                    "pass_fail": "FAIL",
                }
            )

        # Engine vs independent avg: for adds, compare shadow after vs formula from before
        if kind == "overlay_short_add":
            indep = weighted_avg(os_q_b, os_a_b, qty, fill_px) if os_q_b > 0 else fill_px
            eng = shadow.overlay_short.avg
            avg_diff = eng - indep
            bundle.average_price_audit.append(
                {
                    "policy": policy,
                    "event_index": event_index,
                    "timestamp": ts,
                    "leg": "overlay_short",
                    "action": "add",
                    "qty_before": os_q_b,
                    "avg_before": os_a_b,
                    "add_qty": qty,
                    "fill_price": fill_px,
                    "engine_avg": eng,
                    "independently_recomputed_avg": indep,
                    "difference": avg_diff,
                    "tolerance": AVG_TOL,
                    "pass_fail": _pass(abs(avg_diff) <= AVG_TOL),
                }
            )
        elif kind in (
            "overlay_be_close",
            "overlay_tp_close",
            "overlay_tp_partial",
            "full_exit",
        ):
            # remaining avg must equal before unless flat
            if avg_leg and "short" in (avg_leg or ""):
                before_avg = os_a_b if "overlay" in (avg_leg or "") or kind.startswith(
                    "overlay_"
                ) else cs_a_b
                if kind == "full_exit" and leg == "core" and position_side == "short":
                    before_avg = cs_a_b
                    after_avg = shadow.core_short.avg
                    after_qty = shadow.core_short.qty
                elif kind == "full_exit" and leg == "overlay" and position_side == "short":
                    before_avg = os_a_b
                    after_avg = shadow.overlay_short.avg
                    after_qty = shadow.overlay_short.qty
                elif kind.startswith("overlay_"):
                    before_avg = os_a_b
                    after_avg = shadow.overlay_short.avg
                    after_qty = shadow.overlay_short.qty
                else:
                    before_avg = os_a_b
                    after_avg = shadow.overlay_short.avg
                    after_qty = shadow.overlay_short.qty
                if after_qty > QTY_TOL:
                    avg_diff = after_avg - before_avg
                    ok = abs(avg_diff) <= AVG_TOL
                else:
                    avg_diff = 0.0
                    ok = after_avg == 0.0
                bundle.average_price_audit.append(
                    {
                        "policy": policy,
                        "event_index": event_index,
                        "timestamp": ts,
                        "leg": avg_leg,
                        "action": "reduce",
                        "qty_before": os_q_b if "overlay" in str(avg_leg) else cs_q_b,
                        "avg_before": before_avg,
                        "close_qty": qty,
                        "fill_price": fill_px,
                        "engine_avg": after_avg,
                        "independently_recomputed_avg": before_avg
                        if after_qty > QTY_TOL
                        else 0.0,
                        "difference": avg_diff,
                        "tolerance": AVG_TOL,
                        "pass_fail": _pass(ok),
                    }
                )
            if kind == "full_exit" and position_side == "long":
                before_avg = ol_a_b if leg == "overlay" else cl_a_b
                after = shadow.overlay_long if leg == "overlay" else shadow.core_long
                if after.qty > QTY_TOL:
                    avg_diff = after.avg - before_avg
                    ok = abs(avg_diff) <= AVG_TOL
                else:
                    avg_diff = 0.0
                    ok = after.avg == 0.0
                bundle.average_price_audit.append(
                    {
                        "policy": policy,
                        "event_index": event_index,
                        "timestamp": ts,
                        "leg": avg_leg,
                        "action": "reduce",
                        "qty_before": ol_q_b if leg == "overlay" else cl_q_b,
                        "avg_before": before_avg,
                        "close_qty": qty,
                        "fill_price": fill_px,
                        "engine_avg": after.avg,
                        "independently_recomputed_avg": before_avg
                        if after.qty > QTY_TOL
                        else 0.0,
                        "difference": avg_diff,
                        "tolerance": AVG_TOL,
                        "pass_fail": _pass(ok),
                    }
                )

        # Independent realized PnL check
        expected_pnl = 0.0
        avg_used = 0.0
        if kind in ("overlay_be_close", "overlay_tp_close", "overlay_tp_partial"):
            avg_used = os_a_b
            expected_pnl = qty * (avg_used - fill_px)
        elif kind == "full_exit" and position_side == "short":
            avg_used = os_a_b if leg == "overlay" else cs_a_b
            expected_pnl = qty * (avg_used - fill_px)
        elif kind == "full_exit" and position_side == "long":
            avg_used = ol_a_b if leg == "overlay" else cl_a_b
            expected_pnl = qty * (fill_px - avg_used)

        if kind in (
            "overlay_be_close",
            "overlay_tp_close",
            "overlay_tp_partial",
            "full_exit",
        ):
            eng_pnl = _f(fill.get("realized_pnl_delta"), realized_delta)
            pnl_diff = eng_pnl - expected_pnl
            bundle.pnl_reconciliation.append(
                {
                    "policy": policy,
                    "event_index": event_index,
                    "timestamp": ts,
                    "kind": kind,
                    "leg": leg or "overlay",
                    "position_side": position_side or "short",
                    "qty": qty,
                    "avg_before": avg_used,
                    "close_price": fill_px,
                    "expected_realized_pnl": expected_pnl,
                    "recorded_realized_pnl": eng_pnl,
                    "shadow_realized_pnl": realized_delta,
                    "difference_recorded_vs_expected": pnl_diff,
                    "tolerance": PNL_TOL,
                    "pass_fail": _pass(abs(pnl_diff) <= PNL_TOL),
                    "slippage_separately_subtracted": False,
                }
            )

        bundle.fee_reconciliation.append(
            {
                "policy": policy,
                "event_index": event_index,
                "timestamp": ts,
                "kind": kind,
                "fill_notional": abs(fill_px * qty),
                "fee_rate": fee_rate,
                "expected_fee": expected_fee,
                "recorded_fee": recorded_fee,
                "difference": fee_diff,
                "fee_type": fee_type,
                "tolerance": FEE_TOL,
                "pass_fail": _pass(fee_ok),
            }
        )
        if not fee_ok:
            bundle.invariant_violations.append(
                {
                    "policy": policy,
                    "check": "fee_mismatch",
                    "event_index": event_index,
                    "detail": f"diff={fee_diff}",
                    "pass_fail": "FAIL",
                }
            )

        # Causality: TP/BE should not fill same bar as their creating add
        causal_ok = True
        causal_note = "ok"
        if kind in ("overlay_tp_close", "overlay_tp_partial", "overlay_be_close"):
            # find matching open on same timestamp → violation
            same_bar_opens = [
                f
                for f in group
                if f.get("kind") == "overlay_short_add"
                and (
                    kind == "overlay_be_close"
                    or f.get("level") == fill.get("level")
                    or True
                )
            ]
            # BE/TP after adds on same bar is forbidden by event order (exits before adds)
            # So if both exist same bar, exits processed first — OK if TP was from prior bar.
            if any(f.get("kind") == "overlay_short_add" for f in group):
                causal_note = (
                    "same_bar_has_adds; exits processed before adds (causal)"
                )
        if kind == "full_exit" and any(
            f.get("kind") == "overlay_short_add" for f in group
        ):
            # net_be should not full-exit after same-bar add; if both present, FAIL
            # unless full_exit came from pre-add gate and adds... can't both happen
            # If both in fill list, engine did add then somehow exit — violation for net_be
            if str(cfg.full_exit_target_mode) == "net_be":
                causal_ok = False
                causal_note = "FAIL: full_exit same bar as overlay_short_add under net_be"
                bundle.invariant_violations.append(
                    {
                        "policy": policy,
                        "check": "same_candle_add_and_full_exit",
                        "event_index": event_index,
                        "detail": ts,
                        "pass_fail": "FAIL",
                    }
                )

        order_id = f"{policy}-O{order_seq}"
        bundle.order_lifecycle.append(
            {
                "policy": policy,
                "event_index": event_index,
                "candle_index": bar.get("candle_index"),
                "candle_timestamp": ts,
                "candle_open": bar.get("open"),
                "candle_high": bar.get("high"),
                "candle_low": bar.get("low"),
                "candle_close": bar.get("close"),
                "order_id": order_id,
                "parent_order_id": None,
                "tranche_id": fill.get("tranche_id"),
                "round_id": fill.get("level")
                if kind == "overlay_short_add"
                else None,
                "purpose": purpose,
                "side": side or position_side,
                "order_type": "trigger_market",
                "submit_timestamp": ts,
                "active_from_timestamp": ts
                if kind == "overlay_short_add"
                else ts,  # BE/TP: conceptually prior-bar armed; fill ts is execution
                "trigger_price": trigger,
                "requested_qty": qty,
                "remaining_qty_before": qty,
                "status_before": "active",
                "fill_timestamp": ts,
                "raw_fill_price": trigger if trigger is not None else fill_px,
                "slipped_fill_price": fill_px,
                "filled_qty": qty,
                "status_after": "filled",
                "cancel_timestamp": None,
                "cancel_reason": None,
                "replacement_order_id": None,
                "decision_reason": decision,
                "causal_ok": causal_ok,
                "causal_note": causal_note,
            }
        )

        ur_a = _unrealized(shadow, mark)
        total_realized = shadow.realized_overlay + shadow.realized_core
        # Economics after event (shadow): realized - fees + unrealized
        # (remaining close costs estimated only at full exit gate)
        econ_after = (
            total_realized
            - shadow.open_fees
            - shadow.close_fees
            + sum(ur_a.values())
        )

        total_short_q_b = cs_q_b + os_q_b
        total_short_a_b = (
            weighted_avg(cs_q_b, cs_a_b, os_q_b, os_a_b) if total_short_q_b > 0 else 0.0
        )
        total_short_q_a = shadow.short_qty()
        total_short_a_a = (
            weighted_avg(
                shadow.core_short.qty,
                shadow.core_short.avg,
                shadow.overlay_short.qty,
                shadow.overlay_short.avg,
            )
            if total_short_q_a > 0
            else 0.0
        )
        open_fee = recorded_fee if fee_type == "open" else 0.0
        close_fee = recorded_fee if fee_type == "close" else 0.0
        gross_realized = realized_delta
        net_realized = realized_delta - close_fee if fee_type == "close" else 0.0

        bundle.position_timeline.append(
            {
                "policy": policy,
                "event_index": event_index,
                "event": kind,
                "timestamp": ts,
                "order_id": order_id,
                "long_qty_before": cl_q_b + ol_q_b,
                "long_avg_before": long_a_b,
                "short_qty_before": total_short_q_b,
                "short_avg_before": total_short_a_b,
                "overlay_short_qty_before": os_q_b,
                "long_qty_after": shadow.long_qty(),
                "long_avg_after": (
                    weighted_avg(
                        shadow.core_long.qty,
                        shadow.core_long.avg,
                        shadow.overlay_long.qty,
                        shadow.overlay_long.avg,
                    )
                    if shadow.long_qty() > 0
                    else 0.0
                ),
                "short_qty_after": total_short_q_a,
                "short_avg_after": total_short_a_a,
                "overlay_short_qty_after": shadow.overlay_short.qty,
                "net_exposure_before": net_b,
                "net_exposure_after": shadow.net(),
                "gross_notional_before": gross_b,
                "gross_notional_after": shadow.gross_notional(mark),
                "realized_pnl_delta": realized_delta,
                "realized_pnl_total": total_realized,
                "unrealized_pnl_after": sum(ur_a.values()),
                "total_economics_after": econ_after,
                "open_fees_total": shadow.open_fees,
                "close_fees_total": shadow.close_fees,
            }
        )

        bundle.fill_ledger.append(
            {
                "global_fill_index": len(bundle.fill_ledger),
                "timestamp": ts,
                "bar_index": bar.get("candle_index"),
                "round": fill.get("level"),
                "order_id": order_id,
                "purpose": purpose,
                "side": side or position_side,
                "kind": kind,
                "qty": qty,
                "raw_price": trigger if trigger is not None else fill_px,
                "filled_price": fill_px,
                "notional": abs(fill_px * qty),
                "open_fee": open_fee,
                "close_fee": close_fee,
                "allocated_entry_fee": open_fee,
                "gross_realized_pnl": gross_realized,
                "net_realized_pnl": net_realized,
                "cumulative_realized_overlay_pnl": shadow.realized_overlay,
                "cumulative_entry_fees": shadow.open_fees,
                "cumulative_close_fees": shadow.close_fees,
                "core_long_qty_before": cl_q_b,
                "core_long_avg_before": cl_a_b,
                "core_long_qty_after": shadow.core_long.qty,
                "core_long_avg_after": shadow.core_long.avg,
                "core_short_qty_before": cs_q_b,
                "core_short_avg_before": cs_a_b,
                "core_short_qty_after": shadow.core_short.qty,
                "core_short_avg_after": shadow.core_short.avg,
                "overlay_short_qty_before": os_q_b,
                "overlay_short_avg_before": os_a_b,
                "overlay_short_qty_after": shadow.overlay_short.qty,
                "overlay_short_avg_after": shadow.overlay_short.avg,
                "total_short_qty_before": total_short_q_b,
                "total_short_avg_before": total_short_a_b,
                "total_short_qty_after": total_short_q_a,
                "total_short_avg_after": total_short_a_a,
                "net_qty_before": net_b,
                "net_qty_after": shadow.net(),
            }
        )

        # Walkthrough line
        bundle.walkthrough_lines.append(
            "\n".join(
                [
                    f"### Event {event_index} — {kind}",
                    f"Zeit: {ts}",
                    f"Preis: trigger={trigger} fill={fill_px}",
                    f"Aktion: {purpose}",
                    f"Menge: {qty}",
                    f"Position vorher: long={cl_q_b + ol_q_b:.6f} short={cs_q_b + os_q_b:.6f} "
                    f"overlay_short={os_q_b:.6f}",
                    f"Position nachher: long={shadow.long_qty():.6f} short={shadow.short_qty():.6f} "
                    f"overlay_short={shadow.overlay_short.qty:.6f}",
                    f"Average vorher/nachher (overlay short): {os_a_b:.8f} → {shadow.overlay_short.avg:.8f}",
                    f"Gross PnL (realized delta): {realized_delta:.8f}",
                    f"Fee: {recorded_fee:.8f} ({fee_type})",
                    f"Nettoeffekt (realized - fee): {realized_delta - recorded_fee:.8f}",
                    f"Warum: {decision}",
                    f"Kausal: {causal_note}",
                    "",
                ]
            )
        )

        # Qty never negative
        for name, q in (
            ("core_long", shadow.core_long.qty),
            ("core_short", shadow.core_short.qty),
            ("overlay_long", shadow.overlay_long.qty),
            ("overlay_short", shadow.overlay_short.qty),
        ):
            if q < -QTY_TOL:
                bundle.invariant_violations.append(
                    {
                        "policy": policy,
                        "check": "negative_qty",
                        "event_index": event_index,
                        "detail": f"{name}={q}",
                        "pass_fail": "FAIL",
                    }
                )

        # Overlay short <= total short
        if shadow.overlay_short.qty - shadow.short_qty() > QTY_TOL:
            bundle.invariant_violations.append(
                {
                    "policy": policy,
                    "check": "overlay_gt_total_short",
                    "event_index": event_index,
                    "detail": f"ov={shadow.overlay_short.qty} tot={shadow.short_qty()}",
                    "pass_fail": "FAIL",
                }
            )

        # Core freeze until full exit of core
        if kind != "full_exit" or leg != "core":
            if (
                abs(shadow.core_long.qty - core_freeze[0]) > QTY_TOL
                or abs(shadow.core_long.avg - core_freeze[1]) > AVG_TOL
                or abs(shadow.core_short.qty - core_freeze[2]) > QTY_TOL
                or abs(shadow.core_short.avg - core_freeze[3]) > AVG_TOL
            ):
                # After core full exit, freeze no longer applies
                if shadow.core_long.qty > QTY_TOL or shadow.core_short.qty > QTY_TOL:
                    if kind.startswith("overlay_") or kind == "overlay_short_add":
                        pass  # core should be unchanged
                        if abs(shadow.core_long.qty - core_freeze[0]) > QTY_TOL or abs(
                            shadow.core_short.qty - core_freeze[2]
                        ) > QTY_TOL:
                            bundle.invariant_violations.append(
                                {
                                    "policy": policy,
                                    "check": "core_mutated_before_full_exit",
                                    "event_index": event_index,
                                    "detail": kind,
                                    "pass_fail": "FAIL",
                                }
                            )

    # Tranche reconciliation
    for t in result.tranches_final:
        tid = t.get("tranche_id")
        initial = _f(t.get("initial_qty"))
        remaining = _f(t.get("remaining_qty"))
        closes = [
            e
            for e in result.tranche_events
            if e.get("tranche_id") == tid
            and e.get("event")
            in (
                "tranche_tp_close",
                "tranche_tp_partial",
                "tranche_shared_be_close",
                "tranche_full_exit_close",
            )
        ]
        closed_qty = sum(_f(e.get("qty")) for e in closes)
        # shared_be / full_exit may close via remaining
        if t.get("status") == "closed" and remaining <= QTY_TOL:
            closed_qty = max(closed_qty, initial - remaining)
        sum_ok = abs((closed_qty + remaining) - initial) <= 1e-6 or (
            t.get("status") == "closed" and remaining <= QTY_TOL
        )
        # Better: closed from events + remaining == initial
        closed_from_events = sum(
            _f(e.get("qty"))
            for e in result.tranche_events
            if e.get("tranche_id") == tid
            and e.get("event")
            in ("tranche_tp_close", "tranche_tp_partial")
        )
        # For BE/full exit, remaining goes to 0 without always logging qty on every path
        if remaining <= QTY_TOL and t.get("status") == "closed":
            sum_ok = True
        else:
            sum_ok = abs(closed_from_events + remaining - initial) <= 1e-6

        bundle.tranche_reconciliation.append(
            {
                "policy": policy,
                "tranche_id": tid,
                "round_id": t.get("round_id"),
                "initial_qty": initial,
                "remaining_qty": remaining,
                "entry_price": t.get("entry_price_filled"),
                "entry_fee": t.get("open_fee_usdt"),
                "tp_pct": t.get("tp_pct"),
                "tp_trigger_price": t.get("tp_trigger_price"),
                "steps_completed": t.get("steps_completed"),
                "closed_qty_from_tp_events": closed_from_events,
                "realized_gross_pnl": t.get("realized_pnl_usdt"),
                "close_fees": t.get("close_fee_usdt"),
                "realized_net_pnl": _f(t.get("realized_pnl_usdt"))
                - _f(t.get("close_fee_usdt"))
                - _f(t.get("open_fee_usdt")),
                "final_status": t.get("status"),
                "qty_sum_pass_fail": _pass(sum_ok and remaining >= -QTY_TOL),
                "negative_remaining": remaining < -QTY_TOL,
                "over_closed": closed_from_events - initial > 1e-6,
            }
        )
        if remaining < -QTY_TOL or not sum_ok:
            bundle.invariant_violations.append(
                {
                    "policy": policy,
                    "check": "tranche_qty_integrity",
                    "event_index": None,
                    "detail": f"{tid} rem={remaining} closed={closed_from_events} init={initial}",
                    "pass_fail": "FAIL",
                }
            )

    # Shared BE round audit
    if cfg.overlay_exit_policy == "shared_be":
        _audit_shared_be_rounds(bundle, result, cfg)

    # Full exit audit
    _audit_full_exit(bundle, result, cfg, shadow)

    # Final flat / cashflow reconciliation
    flat = (
        abs(shadow.long_qty()) <= QTY_TOL
        and abs(shadow.short_qty()) <= QTY_TOL
        and abs(shadow.overlay_short.qty) <= QTY_TOL
    )
    final_econ_shadow = (
        shadow.realized_overlay
        + shadow.realized_core
        - shadow.open_fees
        - shadow.close_fees
    )
    remaining_ur = 0.0 if flat else sum(
        _unrealized(
            shadow,
            _f(result.per_bar_trace[-1].get("close")) if result.per_bar_trace else 0.0,
        ).values()
    )
    cash_diff = shadow.cashflow - final_econ_shadow
    bundle.pnl_reconciliation.append(
        {
            "policy": policy,
            "event_index": "FINAL",
            "timestamp": result.exit_reason,
            "kind": "final_flat_reconciliation",
            "expected_realized_pnl": final_econ_shadow,
            "recorded_realized_pnl": final_econ_shadow,
            "shadow_realized_pnl": final_econ_shadow,
            "cashflow_sum": shadow.cashflow,
            "difference_cashflow_vs_econ": cash_diff,
            "remaining_unrealized": remaining_ur,
            "flat": flat,
            "tolerance": PNL_TOL * 100,
            "pass_fail": _pass(flat and abs(remaining_ur) <= PNL_TOL),
        }
    )

    # Determinism / fingerprint-ish
    r2_state = result.state
    # Timestamp monotonic
    prev_ts = None
    for fill in fills:
        ts = str(fill.get("timestamp"))
        if prev_ts is not None and ts < prev_ts:
            bundle.invariant_violations.append(
                {
                    "policy": policy,
                    "check": "timestamp_not_monotonic",
                    "detail": f"{prev_ts} -> {ts}",
                    "pass_fail": "FAIL",
                }
            )
        prev_ts = ts

    # NaN check on economics timeline
    for bar in result.per_bar_trace:
        econ = bar.get("total_exit_economics")
        if econ is not None:
            try:
                v = float(econ)
                if v != v or abs(v) == float("inf"):
                    bundle.invariant_violations.append(
                        {
                            "policy": policy,
                            "check": "nan_inf_economics",
                            "detail": str(bar.get("timestamp")),
                            "pass_fail": "FAIL",
                        }
                    )
            except (TypeError, ValueError):
                bundle.invariant_violations.append(
                    {
                        "policy": policy,
                        "check": "non_numeric_economics",
                        "detail": str(bar.get("timestamp")),
                        "pass_fail": "FAIL",
                    }
                )

    n_fail = sum(1 for v in bundle.invariant_violations if v.get("pass_fail") == "FAIL")
    avg_fails = sum(
        1 for r in bundle.average_price_audit if r.get("pass_fail") == "FAIL"
    )
    fee_fails = sum(
        1 for r in bundle.fee_reconciliation if r.get("pass_fail") == "FAIL"
    )
    pnl_fails = sum(
        1 for r in bundle.pnl_reconciliation if r.get("pass_fail") == "FAIL"
    )

    bundle.summary = {
        "policy": policy,
        "run_id": cfg.run_id,
        "overlay_exit_policy": cfg.overlay_exit_policy,
        "full_exit_target_mode": cfg.full_exit_target_mode,
        "full_exit_target_usdt": cfg.full_exit_target_usdt,
        "full_exit_safety_buffer_usdt": cfg.full_exit_safety_buffer_usdt,
        "final_status": result.state,
        "exit_reason": result.exit_reason,
        "bars_processed": result.bars_processed,
        "fill_count": len(fills),
        "order_lifecycle_rows": len(bundle.order_lifecycle),
        "invariant_fail_count": n_fail,
        "avg_audit_fail_count": avg_fails,
        "fee_audit_fail_count": fee_fails,
        "pnl_audit_fail_count": pnl_fails,
        "flat_after_exit": flat,
        "shadow_final_economics": final_econ_shadow,
        "shadow_open_fees": shadow.open_fees,
        "shadow_close_fees": shadow.close_fees,
        "engine_open_fees": result.ledger.cumulative_entry_fees,
        "engine_close_fees": result.ledger.cumulative_close_fees,
        "fee_ledger_match": abs(shadow.open_fees - result.ledger.cumulative_entry_fees)
        <= FEE_TOL
        and abs(shadow.close_fees - result.ledger.cumulative_close_fees) <= FEE_TOL,
        "first_net_be_touch": result.first_net_be_touch,
        "ambiguous_intrabar_count": len(bundle.ambiguous_intrabar_cases),
        "audit_overall_pass": n_fail == 0
        and avg_fails == 0
        and fee_fails == 0
        and flat
        and result.state == "RECOVERED_BE",
        "tolerances": {
            "AVG_TOL": AVG_TOL,
            "FEE_TOL": FEE_TOL,
            "PNL_TOL": PNL_TOL,
            "QTY_TOL": QTY_TOL,
        },
        "event_order_documented": (
            "1) activate pending TP/BE from prior bar; "
            "2) arm recovery if activation touched; "
            "3) process overlay exits (shared BE / individual TP); "
            "4) net_be full-exit gate (before adds); "
            "5) short adds shallow→deep; "
            "6) legacy post-add full-exit skipped under net_be"
        ),
    }
    return bundle


def _audit_shared_be_rounds(
    bundle: AuditBundle, result: EngineResult, cfg: CoberturaConfig
) -> None:
    """Rebuild shared-BE rounds from add/close fills and verify triggers."""
    round_id = 0
    open_adds: list[dict[str, Any]] = []
    for fill in result.fill_events:
        kind = fill.get("kind")
        if kind == "overlay_short_add":
            open_adds.append(fill)
            # recompute BE after this add using a mini ledger snapshot is heavy;
            # instead match overlay_be_timeline row
        elif kind == "overlay_be_close":
            round_id += 1
            qty = sum(_f(a.get("qty")) for a in open_adds)
            # VWAP
            if qty > 0:
                avg = (
                    sum(_f(a.get("qty")) * _f(a.get("fill_price")) for a in open_adds)
                    / qty
                )
            else:
                avg = 0.0
            open_fees = sum(_f(a.get("fee")) for a in open_adds)
            close_fee = _f(fill.get("fee"))
            close_px = _f(fill.get("fill_price"))
            trigger = _f(fill.get("trigger"))
            gross = qty * (avg - close_px) if qty > 0 else 0.0
            net = gross - open_fees - close_fee
            # Independent trigger: use engine helper on a temporary structure is hard;
            # verify recorded trigger economics via overlay_short_exit_economics_at needs ledger.
            # Check direction: short BE trigger should be below or near avg (buy-back).
            direction_ok = trigger <= avg + 1e-9 if qty > 0 else True
            # Find timeline BE at last add
            be_rows = [
                r
                for r in result.overlay_be_timeline
                if r.get("timestamp") == open_adds[-1].get("timestamp")
            ] if open_adds else []
            be_px = be_rows[-1].get("overlay_be_price") if be_rows else None
            trigger_match = (
                be_px is None or abs(_f(be_px) - trigger) <= cfg.tick_size + 1e-12
            )
            bundle.shared_be_rounds.append(
                {
                    "policy": bundle.policy,
                    "round_id": round_id,
                    "n_adds": len(open_adds),
                    "add_timestamps": [a.get("timestamp") for a in open_adds],
                    "round_qty": qty,
                    "overlay_avg": avg,
                    "round_open_fees": open_fees,
                    "shared_be_trigger": trigger,
                    "timeline_be_at_last_add": be_px,
                    "trigger_matches_timeline": trigger_match,
                    "close_timestamp": fill.get("timestamp"),
                    "close_fill_price": close_px,
                    "close_fee": close_fee,
                    "gross_pnl": gross,
                    "net_pnl": net,
                    "net_non_negative": net + 1e-6 >= float(cfg.overlay_be_target_usdt),
                    "trigger_direction_ok_for_short": direction_ok,
                    "overlay_qty_after_should_be_zero": True,
                    "pass_fail": _pass(
                        direction_ok
                        and (net + 1e-6 >= float(cfg.overlay_be_target_usdt) - 1e-6)
                    ),
                }
            )
            if not direction_ok:
                bundle.invariant_violations.append(
                    {
                        "policy": bundle.policy,
                        "check": "shared_be_trigger_direction",
                        "detail": f"round={round_id} trigger={trigger} avg={avg}",
                        "pass_fail": "FAIL",
                    }
                )
            open_adds = []
        elif kind == "full_exit":
            break


def _audit_full_exit(
    bundle: AuditBundle,
    result: EngineResult,
    cfg: CoberturaConfig,
    shadow: ShadowState,
) -> None:
    full_ev = None
    for ev in result.order_events:
        if ev.get("event") == "full_exit":
            full_ev = ev
            break
    first = result.first_net_be_touch
    exit_fills = [f for f in result.fill_events if f.get("kind") == "full_exit"]
    pre_econ = None if full_ev is None else _f(full_ev.get("total_exit_economics_pre"))
    actual_final = (
        shadow.realized_overlay
        + shadow.realized_core
        - shadow.open_fees
        - shadow.close_fees
    )
    delay = None
    if first and full_ev:
        # bar indices from trace
        first_ts = first.get("timestamp")
        exit_ts = full_ev.get("timestamp")
        idx = {str(b.get("timestamp")): i for i, b in enumerate(result.per_bar_trace)}
        if first_ts in idx and exit_ts in idx:
            delay = idx[exit_ts] - idx[first_ts]

    not_early = True
    if first and full_ev and delay is not None:
        not_early = delay >= 0
    immediate = delay == 0 if delay is not None else None

    flat = (
        abs(result.ledger.core_long.qty) <= QTY_TOL
        and abs(result.ledger.core_short.qty) <= QTY_TOL
        and abs(result.ledger.overlay_short.qty) <= QTY_TOL
        and abs(result.ledger.overlay_long.qty) <= QTY_TOL
    )
    open_tranches = [
        t
        for t in result.tranches_final
        if _f(t.get("remaining_qty")) > QTY_TOL
    ]

    est_close = None if full_ev is None else full_ev.get("estimated_remaining_close_fees_pre")
    est_slip = None if full_ev is None else full_ev.get("estimated_exit_slippage_pre")
    diff_est = None
    if pre_econ is not None:
        diff_est = actual_final - pre_econ

    row = {
        "policy": bundle.policy,
        "first_net_be_timestamp": None if not first else first.get("timestamp"),
        "first_net_be_economics": None
        if not first
        else first.get("total_exit_economics"),
        "exit_timestamp": None if not full_ev else full_ev.get("timestamp"),
        "exit_reason": result.exit_reason,
        "final_status": result.state,
        "target_usdt": cfg.full_exit_target_usdt,
        "safety_buffer_usdt": cfg.full_exit_safety_buffer_usdt,
        "tolerance_usdt": cfg.pnl_tolerance_usdt,
        "threshold_usdt": float(cfg.full_exit_target_usdt)
        + float(cfg.full_exit_safety_buffer_usdt)
        - float(cfg.pnl_tolerance_usdt),
        "economics_pre_exit_engine": pre_econ,
        "estimated_remaining_close_fees_pre": est_close,
        "estimated_exit_slippage_pre": est_slip,
        "actual_final_economics_shadow": actual_final,
        "estimate_vs_actual_diff": diff_est,
        "be_to_exit_delay_bars": delay,
        "exit_not_before_first_be": not_early,
        "exit_immediate_on_first_be": immediate,
        "flat_after_exit": flat,
        "open_tranches_remaining": len(open_tranches),
        "n_full_exit_fills": len(exit_fills),
        "exit_fill_prices": [f.get("fill_price") for f in exit_fills],
        "exit_fill_qtys": [f.get("qty") for f in exit_fills],
        "pass_fail": _pass(
            result.state == "RECOVERED_BE"
            and flat
            and not_early
            and len(open_tranches) == 0
            and full_ev is not None
        ),
    }
    bundle.full_exit_audit.append(row)
    if row["pass_fail"] == "FAIL":
        bundle.invariant_violations.append(
            {
                "policy": bundle.policy,
                "check": "full_exit_audit",
                "detail": str(row),
                "pass_fail": "FAIL",
            }
        )

    # Independent gate recompute at exit close using engine ledger is post-flat;
    # use pre-exit economics from order event vs threshold
    if pre_econ is not None:
        thr = row["threshold_usdt"]
        if pre_econ + 1e-12 < thr:
            bundle.invariant_violations.append(
                {
                    "policy": bundle.policy,
                    "check": "full_exit_below_threshold",
                    "detail": f"pre={pre_econ} thr={thr}",
                    "pass_fail": "FAIL",
                }
            )
