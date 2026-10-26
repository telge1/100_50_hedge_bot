"""USER_OCO_PENDING_V1 — dual pending limits; first fill cancels opposite side."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TOOLS4D = Path(__file__).resolve().parents[1] / "phase4d_pool_landscape_backtester_v2"
sys.path.insert(0, str(TOOLS4D))

from exit_simulator import ExitResult, simulate_exit  # noqa: E402
from pool_zone_tracker import LifecycleIndex, anchor_from_signal  # noqa: E402
from signal_provider import RawSignal  # noqa: E402
from strategy_rules import plan_long, plan_short  # noqa: E402

from oco_bar_logic import BarOrderState, eval_bar  # noqa: E402


@dataclass
class PendingOrder:
    order_id: str
    candidate_id: str
    direction: str
    limit_price: float
    pool_lower: float
    pool_upper: float
    pool_identity: str
    active_from_utc: str
    signal_close_utc: str
    zone_active: bool | None = None
    status: str = "ACTIVE"
    anchor: Any = None

    def __post_init__(self) -> None:
        if self.anchor is None:
            self.anchor = anchor_from_signal(
                self.direction, self.pool_lower, self.pool_upper, self.pool_identity
            )


@dataclass
class OpenPosition:
    trade_id: str
    order_id: str
    candidate_id: str
    direction: str
    fill_utc: str
    entry_price: float
    sl_price: float
    tp_price: float
    exit_utc: str | None = None
    exit_reason: str = ""


def _emit(events: list[dict], **kwargs: Any) -> None:
    events.append(kwargs)


def run_oco_backtest(
    cfg: dict[str, Any],
    signals: list[RawSignal],
    prices: dict[str, dict],
    lifecycle: LifecycleIndex | None = None,
) -> dict[str, list[dict]]:
    if lifecycle is None:
        lifecycle = LifecycleIndex.load()
    pilot_from = cfg["pilot_from_utc"]
    pilot_to = cfg["pilot_to_utc"]
    fees = float(cfg["roundtrip_fees_pct"])
    times = [t for t in sorted(prices.keys()) if pilot_from <= t <= pilot_to]

    events: list[dict] = []
    fill_audit: list[dict] = []
    ambiguous_cases: list[dict] = []
    position_checks: list[dict] = []

    sorted_signals = sorted(
        signals, key=lambda s: (s.order_active_from_utc, s.signal_close_utc, s.candidate_id)
    )
    signal_by_active: dict[str, list[RawSignal]] = {}
    for s in sorted_signals:
        signal_by_active.setdefault(s.order_active_from_utc, []).append(s)
    placed_signal_ids: set[str] = set()

    pendings: list[PendingOrder] = []
    position: OpenPosition | None = None
    trade_seq = 0
    order_seq = 0

    def position_blocks_entries(at: str) -> bool:
        if not position:
            return False
        if position.exit_utc and at >= position.exit_utc:
            return False
        return True

    def cancel_order(po: PendingOrder, bar_utc: str, event_type: str, rule_id: str, note: str) -> None:
        if po.status != "ACTIVE":
            return
        po.status = "CANCELLED"
        _emit(
            events,
            event_utc=bar_utc,
            event_type=event_type,
            order_id=po.order_id,
            candidate_id=po.candidate_id,
            direction=po.direction,
            rule_id=rule_id,
            note=note,
        )

    def try_close_position(bar_utc: str) -> None:
        nonlocal position
        if not position or position.exit_utc:
            return
        ex: ExitResult = simulate_exit(
            position.direction,
            position.fill_utc,
            position.entry_price,
            position.sl_price,
            position.tp_price,
            prices,
            pilot_to,
            fees,
        )
        if ex.exit_utc == bar_utc and ex.exit_reason in ("SL", "TP"):
            position.exit_utc = ex.exit_utc
            position.exit_reason = ex.exit_reason
            _emit(
                events,
                event_utc=bar_utc,
                event_type="POSITION_CLOSED",
                trade_id=position.trade_id,
                candidate_id=position.candidate_id,
                direction=position.direction,
                rule_id=f"EXIT-{ex.exit_reason}",
                note=str(ex.exit_price),
            )
            fill_audit.append(
                {
                    "trade_id": position.trade_id,
                    "candidate_id": position.candidate_id,
                    "direction": position.direction,
                    "fill_utc": position.fill_utc,
                    "exit_utc": ex.exit_utc,
                    "exit_reason": ex.exit_reason,
                    "net_pnl_pct": ex.net_pnl_pct,
                }
            )
            position = None

    def activate_signals(bar_utc: str) -> None:
        nonlocal order_seq
        for sig in signal_by_active.get(bar_utc, []):
            if sig.candidate_id in placed_signal_ids:
                continue
            placed_signal_ids.add(sig.candidate_id)
            _emit(
                events,
                event_utc=bar_utc,
                event_type="SIGNAL_ELIGIBLE",
                candidate_id=sig.candidate_id,
                direction=sig.direction,
                signal_close_utc=sig.signal_close_utc,
                rule_id="SIGNAL-PROVIDER-01",
                note=sig.signal_source,
            )
            if position_blocks_entries(bar_utc):
                _emit(
                    events,
                    event_utc=bar_utc,
                    event_type="SIGNAL_ELIGIBLE",
                    candidate_id=sig.candidate_id,
                    direction=sig.direction,
                    rule_id="EXEC-POSITION-BLOCK-01",
                    note="No new entry order while position open",
                )
                continue
            order_seq += 1
            oid = f"OCO_{order_seq:04d}"
            limit = sig.pool_lower if sig.direction == "SHORT" else sig.pool_upper
            po = PendingOrder(
                order_id=oid,
                candidate_id=sig.candidate_id,
                direction=sig.direction,
                limit_price=limit,
                pool_lower=sig.pool_lower,
                pool_upper=sig.pool_upper,
                pool_identity=sig.pool_identity,
                active_from_utc=sig.order_active_from_utc,
                signal_close_utc=sig.signal_close_utc,
            )
            pendings.append(po)
            _emit(
                events,
                event_utc=bar_utc,
                event_type="ORDER_PENDING",
                order_id=oid,
                candidate_id=sig.candidate_id,
                direction=sig.direction,
                limit_price=limit,
                rule_id="EXEC-USER-OCO-PENDING-V1",
                note="Limit placed at pool edge",
            )

    def process_bar(bar_utc: str) -> None:
        nonlocal position, trade_seq
        activate_signals(bar_utc)
        try_close_position(bar_utc)

        active = [p for p in pendings if p.status == "ACTIVE" and p.active_from_utc <= bar_utc]
        if not active or position_blocks_entries(bar_utc):
            return

        bar = prices[bar_utc]
        bar_states: dict[str, BarOrderState] = {}

        for po in active:
            st, po.zone_active = eval_bar(
                po.anchor, po.limit_price, bar_utc, bar, lifecycle, po.zone_active
            )
            bar_states[po.order_id] = st
            if st.identity_ambiguous:
                cancel_order(
                    po,
                    bar_utc,
                    "POOL_ZONE_INVALIDATED",
                    "EXEC-POOL-IDENTITY-01",
                    "Multiple NOT_VERIFIED depth-2 matches",
                )
            elif st.ambiguous_touch_disappear:
                cancel_order(
                    po,
                    bar_utc,
                    "AMBIGUOUS_OCO_FILL_SEQUENCE",
                    "EXEC-AMBIG-01",
                    "Limit touch and POOL_DISAPPEARED same bar",
                )
                ambiguous_cases.append(
                    {
                        "bar_utc": bar_utc,
                        "order_id": po.order_id,
                        "candidate_id": po.candidate_id,
                        "kind": "SINGLE_ORDER_TOUCH_DISAPPEAR",
                    }
                )

        active = [p for p in pendings if p.status == "ACTIVE" and p.active_from_utc <= bar_utc]
        for po in active:
            st = bar_states[po.order_id]
            if st.zone_active is False and not st.touch:
                cancel_order(
                    po,
                    bar_utc,
                    "POOL_ZONE_INVALIDATED",
                    "EXEC-POOL-DEACTIVATED-01",
                    "Entry depth-2 zone lost before fill",
                )

        active = [p for p in pendings if p.status == "ACTIVE" and p.active_from_utc <= bar_utc]
        fill_ready = [
            p
            for p in active
            if bar_states[p.order_id].touch and bar_states[p.order_id].zone_active
        ]
        shorts = [p for p in fill_ready if p.direction == "SHORT"]
        longs = [p for p in fill_ready if p.direction == "LONG"]

        if shorts and longs:
            for po in shorts + longs:
                cancel_order(
                    po,
                    bar_utc,
                    "AMBIGUOUS_OCO_FILL_SEQUENCE",
                    "EXEC-OCO-AMBIG-01",
                    "Both SHORT and LONG limits touched same 5m bar",
                )
            ambiguous_cases.append(
                {
                    "bar_utc": bar_utc,
                    "order_ids": "|".join(p.order_id for p in shorts + longs),
                    "kind": "OCO_BOTH_SIDES_SAME_BAR",
                }
            )
            return

        if len(shorts) > 1 or len(longs) > 1:
            for po in fill_ready:
                cancel_order(
                    po,
                    bar_utc,
                    "AMBIGUOUS_OCO_FILL_SEQUENCE",
                    "EXEC-OCO-AMBIG-02",
                    "Multiple same-direction limits touched same bar",
                )
            ambiguous_cases.append(
                {
                    "bar_utc": bar_utc,
                    "order_ids": "|".join(p.order_id for p in fill_ready),
                    "kind": "OCO_MULTI_SAME_SIDE",
                }
            )
            return

        if len(fill_ready) != 1:
            return

        winner = fill_ready[0]
        winner.status = "FILLED"
        sig = next(s for s in sorted_signals if s.candidate_id == winner.candidate_id)

        _emit(
            events,
            event_utc=bar_utc,
            event_type="ORDER_FILLED",
            order_id=winner.order_id,
            candidate_id=winner.candidate_id,
            direction=winner.direction,
            limit_price=winner.limit_price,
            rule_id="EXEC-USER-OCO-PENDING-V1",
            note="First fill wins OCO pair",
        )

        for po in pendings:
            if po.status != "ACTIVE" or po.order_id == winner.order_id:
                continue
            if po.direction != winner.direction:
                cancel_order(
                    po,
                    bar_utc,
                    "OCO_CANCELLED",
                    "EXEC-OCO-CANCEL-01",
                    f"Opposite pending cancelled after {winner.direction} fill",
                )
            else:
                cancel_order(
                    po,
                    bar_utc,
                    "OCO_CANCELLED",
                    "EXEC-OCO-CANCEL-02",
                    "Same-direction pending cancelled after first fill",
                )

        if sig.direction == "SHORT":
            plan = plan_short(
                sig.pool_lower,
                sig.pool_upper,
                sig.landscape_intervals,
                winner.limit_price,
                cfg,
                quality_gate=False,
            )
        else:
            plan = plan_long(
                sig.pool_lower,
                sig.pool_upper,
                sig.upper_tp_rows,
                winner.limit_price,
                cfg,
                quality_gate=False,
            )

        trade_seq += 1
        tid = f"T4H_{trade_seq:03d}"
        position = OpenPosition(
            trade_id=tid,
            order_id=winner.order_id,
            candidate_id=winner.candidate_id,
            direction=winner.direction,
            fill_utc=bar_utc,
            entry_price=winner.limit_price,
            sl_price=plan.sl_price,
            tp_price=plan.tp_price or 0.0,
        )
        _emit(
            events,
            event_utc=bar_utc,
            event_type="POSITION_OPEN",
            trade_id=tid,
            order_id=winner.order_id,
            candidate_id=winner.candidate_id,
            direction=winner.direction,
            rule_id="POS-MAX-ONE-01",
            note=f"sl={plan.sl_price} tp={plan.tp_price}",
        )
        position_checks.append(
            {
                "bar_utc": bar_utc,
                "check": "SINGLE_POSITION_AFTER_FILL",
                "trade_id": tid,
                "pending_remaining_active": sum(1 for p in pendings if p.status == "ACTIVE"),
                "ok": sum(1 for p in pendings if p.status == "ACTIVE") == 0,
            }
        )
        try_close_position(bar_utc)

    for t in times:
        process_bar(t)

    if position and not position.exit_utc:
        position_checks.append(
            {
                "bar_utc": pilot_to,
                "check": "POSITION_OPEN_AT_DATA_END",
                "trade_id": position.trade_id,
                "pending_remaining_active": sum(1 for p in pendings if p.status == "ACTIVE"),
                "ok": sum(1 for p in pendings if p.status == "ACTIVE") == 0,
            }
        )

    return {
        "oco_order_events": events,
        "oco_fill_audit": fill_audit,
        "oco_ambiguous_cases": ambiguous_cases,
        "oco_single_position_checks": position_checks,
    }
