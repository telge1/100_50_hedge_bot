#!/usr/bin/env python3
"""Simulate fixed-cycle downside moves using the real bot logic."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

# ensure repo root is importable
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PACKAGE_ROOT = os.path.dirname(SCRIPT_DIR)
REPO_ROOT = os.path.dirname(PACKAGE_ROOT)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from fixed_cycle_hedge_bot.audit_logger import AuditLogger
from fixed_cycle_hedge_bot.base import StrategyContext
from fixed_cycle_hedge_bot.fixed_cycle_strategy import (
    FixedCycleHedgeConfig,
    FixedCycleHedgeStrategy,
)
from fixed_cycle_hedge_bot.models import (
    FillEvent,
    HedgeSnapshot,
    ManagedOrder,
    RuntimeState,
    StrategyIntent,
)
from utils.math_utils import calculate_pnl

logging.basicConfig(level=logging.WARNING, format="%(message)s")
logger = logging.getLogger("fixed_cycle_simulator")


class SimulationOrderManager:
    def __init__(self) -> None:
        self.rows: List[Dict[str, Any]] = []

    def publish_closed_pnl(
        self,
        order_id: str,
        symbol: str,
        category: str,
        closed_pnl: float,
        closed_qty: float,
    ) -> None:
        self.rows.append(
            {
                "orderId": order_id,
                "symbol": symbol.upper(),
                "closedPnl": closed_pnl,
                "closedSize": closed_qty,
                "qty": closed_qty,
            }
        )

    def fetch_closed_pnl(
        self, symbol: str, category: str, limit: int, start_time_ms: Optional[int]
    ) -> List[Dict[str, Any]]:
        return [
            row
            for row in self.rows
            if str(row.get("symbol") or "").upper() == symbol.upper()
        ]


def _current_timestamp() -> str:
    return datetime.utcnow().isoformat()


def _format_ratio(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def _parse_grid(value: str) -> Sequence[float]:
    parts = [part.strip() for part in value.split(",") if part.strip()]
    return [float(part) for part in parts]


def _pct(value: float) -> float:
    return value * 100.0


@dataclass
class PendingOrder:
    managed: ManagedOrder
    trigger_price: Optional[float]


@dataclass
class CycleSnapshot:
    long_qty: float
    short_qty: float
    long_avg: float
    short_avg: float


class CycleTracker:
    def __init__(self) -> None:
        self._entries: Dict[int, Dict[str, Any]] = {}
        self.completed_cycles: List[int] = []
        self.cycle_snapshots: Dict[int, CycleSnapshot] = {}
        self.first_long_add_cycle: Optional[int] = None
        self.first_long_add_price: Optional[float] = None
        self.first_long_add_qty: Optional[float] = None
        self.first_short_tp_price: Optional[float] = None
        self.first_short_tp_qty: Optional[float] = None
        self.post_first_cycle_exposure: Optional[float] = None
        self.long_add_fills: int = 0
        self.short_tp_fills: int = 0
        self.additional_cycles_after_first: int = 0

    def _ensure_entry(self, cycle_index: int) -> Dict[str, Any]:
        return self._entries.setdefault(cycle_index, {})

    def record_long_add(self, cycle_index: int, price: float, qty: float) -> None:
        entry = self._ensure_entry(cycle_index)
        entry["long_add_filled"] = True
        entry["long_add_price"] = price
        entry["long_add_qty"] = qty
        self.long_add_fills += 1
        if self.first_long_add_cycle is None:
            self.first_long_add_cycle = cycle_index
            self.first_long_add_price = price
            self.first_long_add_qty = qty

    def record_short_tp(
        self, cycle_index: int, price: float, qty: float, snapshot: HedgeSnapshot
    ) -> None:
        entry = self._ensure_entry(cycle_index)
        entry["short_tp_filled"] = True
        entry["short_tp_price"] = price
        entry["short_tp_qty"] = qty
        self.short_tp_fills += 1
        if (
            self.first_long_add_cycle is not None
            and cycle_index == self.first_long_add_cycle
            and self.first_short_tp_price is None
        ):
            self.first_short_tp_price = price
            self.first_short_tp_qty = qty
        if (
            entry.get("long_add_filled")
            and entry.get("short_tp_filled")
            and not entry.get("completed")
        ):
            entry["completed"] = True
            self.completed_cycles.append(cycle_index)
            self.cycle_snapshots[cycle_index] = CycleSnapshot(
                long_qty=snapshot.long_qty,
                short_qty=snapshot.short_qty,
                long_avg=snapshot.long_avg,
                short_avg=snapshot.short_avg,
            )
            if (
                self.first_long_add_cycle is not None
                and cycle_index == self.first_long_add_cycle
                and self.post_first_cycle_exposure is None
            ):
                self.post_first_cycle_exposure = snapshot.long_qty + snapshot.short_qty
            if (
                self.first_long_add_cycle is not None
                and cycle_index > self.first_long_add_cycle
            ):
                self.additional_cycles_after_first += 1

    @property
    def completed_cycle_count(self) -> int:
        return len(self.completed_cycles)


@dataclass
class SimulationSummary:
    drop_pct: float
    long_add_distance_pct: float
    base_long_qty: float
    base_short_qty: float
    long_qty: float
    short_qty: float
    long_avg: float
    short_avg: float
    completed_cycle_count: int
    cycle_snapshots: Dict[int, CycleSnapshot]
    first_long_add_price: Optional[float]
    first_long_add_qty: Optional[float]
    first_short_tp_price: Optional[float]
    first_short_tp_qty: Optional[float]
    post_first_cycle_exposure: Optional[float]
    long_add_count: int
    short_tp_count: int
    additional_cycles_after_first: int
    realized_long_pnl_total: float
    realized_short_pnl_total: float
    remaining_orders: int
    last_price: float

    @property
    def remaining_exposure(self) -> float:
        return self.long_qty + self.short_qty

    @property
    def long_multiple_vs_base(self) -> float:
        return _format_ratio(self.long_qty, self.base_long_qty)

    @property
    def short_multiple_vs_base(self) -> float:
        return _format_ratio(self.short_qty, self.base_short_qty)

    def long_pct_of_start(self, cycle_index: int) -> float:
        snapshot = self.cycle_snapshots.get(cycle_index)
        if not snapshot:
            return 0.0
        return _format_ratio(snapshot.long_qty, self.base_long_qty) * 100.0

    def short_pct_of_start(self, cycle_index: int) -> float:
        snapshot = self.cycle_snapshots.get(cycle_index)
        if not snapshot:
            return 0.0
        return _format_ratio(snapshot.short_qty, self.base_short_qty) * 100.0


class SimulationRunner:
    def __init__(
        self,
        config: FixedCycleHedgeConfig,
        start_price: float,
        long_add_distance_pct: float,
        verbose: bool = False,
    ) -> None:
        self.config = config
        self.config.rest_poll_after_fill_ms = 0
        self.config.long_fill_distance_pct = long_add_distance_pct
        self.strategy = FixedCycleHedgeStrategy(self.config)
        self.runtime_state = RuntimeState()
        long_qty = self.strategy._normalize_qty(
            self.config.base_notional_usdt / start_price
        )
        short_qty = self.strategy._normalize_qty(
            (self.config.base_notional_usdt * self.config.hedge_ratio_short) / start_price
        )
        self.base_long_qty = long_qty
        self.base_short_qty = short_qty
        self.snapshot = HedgeSnapshot(
            symbol=self.config.symbol,
            current_price=start_price,
            long_qty=long_qty,
            short_qty=short_qty,
            long_avg=start_price,
            short_avg=start_price,
            realized_long_pnl_total=0.0,
            realized_short_pnl_total=0.0,
        )
        self.runtime_state.last_snapshot = self.snapshot
        self.order_manager = SimulationOrderManager()
        self.context = StrategyContext(
            audit=AuditLogger(logger),
            runtime_name="simulator",
            symbol=self.config.symbol,
            category=self.config.category,
            min_order_value=self.config.min_notional_usdt,
            order_manager=self.order_manager,
            refresh_snapshot=self._refresh_snapshot,
            cancel_open_orders_by_purpose=lambda _: None,
        )
        self.pending_orders: Dict[str, PendingOrder] = {}
        self.order_sequence = 0
        self.cycle_tracker = CycleTracker()
        self.verbose = verbose
        self.event_log: List[str] = []
        self._initialize_state()

    def _refresh_snapshot(self, _: str) -> HedgeSnapshot:
        return self.snapshot

    def _initialize_state(self) -> None:
        intents = self.strategy.on_start(
            self.snapshot, self.runtime_state, self.context
        )
        if intents:
            self._add_intents(intents, reason="initial")
        self._refresh_structure(reason="initial_seed")

    def _refresh_structure(self, *, reason: str) -> None:
        self.runtime_state.strategy_state["last_structure_refresh_ms"] = 0
        intents = self.strategy._maybe_refresh_structure(
            self.snapshot, self.runtime_state, self.context, reason=reason
        )
        if intents:
            self._add_intents(intents, reason=reason)

    def _should_track_intent(self, intent: StrategyIntent) -> bool:
        skip_purposes = {
            self.strategy.LONG_TP_EXIT_PURPOSE,
            self.strategy.LONG_SL_EXIT_PURPOSE,
            self.strategy.SHORT_SL_EXIT_PURPOSE,
            self.strategy.SHORT_TP_EXIT_PURPOSE,
        }
        return intent.purpose not in skip_purposes

    def _sync_active_orders(self) -> None:
        self.snapshot.active_orders = tuple(
            order.managed.to_snapshot() for order in self.pending_orders.values()
        )

    def _add_intents(
        self, intents: Iterable[StrategyIntent], *, reason: str = ""
    ) -> None:
        added = False
        for intent in intents:
            if not self._should_track_intent(intent):
                if self.verbose:
                    self.event_log.append(f"[{reason}] skipping {intent.purpose}")
                continue
            order = self._intent_to_order(intent)
            self.pending_orders[order.managed.client_order_id] = order
            added = True
            if self.verbose:
                self.event_log.append(
                    f"[{reason}] planned {order.managed.purpose} "
                    f"{order.managed.side} qty={order.managed.qty:.4f} "
                    f"trigger={order.trigger_price or 'Mkt'}"
                )
        if added:
            self._sync_active_orders()

    def _intent_to_order(self, intent: StrategyIntent) -> PendingOrder:
        trigger_price = intent.trigger_price or intent.price
        managed = ManagedOrder(
            client_order_id=f"sim-order-{self.order_sequence}",
            side=intent.side,
            qty=intent.qty,
            purpose=intent.purpose,
            price=intent.price,
            order_type=intent.order_type,
            reduce_only=intent.reduce_only,
            metadata=dict(intent.metadata),
        )
        self.order_sequence += 1
        return PendingOrder(managed=managed, trigger_price=trigger_price)

    def _order_triggered(self, order: PendingOrder, price: float) -> bool:
        if order.trigger_price is None:
            return True
        return price <= order.trigger_price

    def _process_pending_orders(self, price: float, label: str) -> None:
        while True:
            triggered = [
                order
                for order in list(self.pending_orders.values())
                if self._order_triggered(order, price)
            ]
            if not triggered:
                break
            for order in triggered:
                self._fill_order(order, price, label)

    def _fill_order(self, pending_order: PendingOrder, price: float, label: str) -> None:
        managed = pending_order.managed
        qty = managed.qty
        if qty <= 0:
            self.pending_orders.pop(managed.client_order_id, None)
            return
        entry_price = (
            self.snapshot.long_avg if managed.side == "long" else self.snapshot.short_avg
        )
        pnl = calculate_pnl(entry_price, price, qty, managed.side)
        if managed.side == "long":
            self.snapshot.long_qty = max(self.snapshot.long_qty - qty, 0.0)
            self.snapshot.realized_long_pnl_total += pnl
            self.runtime_state.realized_long_pnl_total += pnl
        else:
            self.snapshot.short_qty = max(self.snapshot.short_qty - qty, 0.0)
            self.snapshot.realized_short_pnl_total += pnl
            self.runtime_state.realized_short_pnl_total += pnl
        self.snapshot.current_price = price
        self.runtime_state.last_snapshot = self.snapshot
        self.strategy._sync_state_from_snapshot(self.snapshot, self.runtime_state)
        managed.filled_qty = qty
        managed.remaining_qty = 0.0
        managed.status = "FILLED"
        managed.updated_at = datetime.now(timezone.utc)
        fill_event = FillEvent(
            exchange_order_id=f"sim-fill-{self.order_sequence}",
            client_order_id=managed.client_order_id,
            side=managed.side,
            purpose=managed.purpose,
            exec_qty=qty,
            exec_price=price,
            order_type=managed.order_type,
            reduce_only=managed.reduce_only,
            status="FILLED",
            metadata=dict(managed.metadata),
            traces=[],
        )
        self.order_manager.publish_closed_pnl(
            order_id=fill_event.exchange_order_id,
            symbol=self.config.symbol,
            category=self.config.category,
            closed_pnl=pnl,
            closed_qty=qty,
        )
        if "_LONG_" in managed.purpose:
            self.cycle_tracker.record_long_add(
                int(managed.metadata.get("cycle_index") or 0), price, qty
            )
        if "_SHORT_" in managed.purpose:
            self.cycle_tracker.record_short_tp(
                int(managed.metadata.get("cycle_index") or 0), price, qty, self.snapshot
            )
        self.runtime_state.temporary_pnl_by_order[
            managed.client_order_id
        ] = pnl
        if self.verbose:
            cycle = int(self.runtime_state.strategy_state.get("current_long_cycle_index") or 0)
            self.event_log.append(
                f"[{label}] filled {managed.purpose} side={managed.side} qty={qty:.4f} "
                f"price={price:.6f} long={self.snapshot.long_qty:.4f}@{self.snapshot.long_avg:.6f} "
                f"short={self.snapshot.short_qty:.4f}@{self.snapshot.short_avg:.6f} "
                f"completed_cycles={self.cycle_tracker.completed_cycle_count}"
            )
        self.pending_orders.pop(managed.client_order_id, None)
        self._sync_active_orders()
        intents = self.strategy.on_fill(fill_event, self.snapshot, self.runtime_state, self.context)
        if intents:
            self._add_intents(intents, reason="post_fill")

    def run(self, drop_pct: float, step_pct: float) -> SimulationSummary:
        if drop_pct <= 0:
            self.snapshot.current_price = self.snapshot.current_price
        start_price = self.snapshot.current_price
        steps: List[float] = []
        current = step_pct
        while current < drop_pct:
            steps.append(current)
            current += step_pct
        steps.append(drop_pct)
        for drop in steps:
            price = max(start_price * (1 - drop / 100.0), 0.0)
            self.snapshot.current_price = price
            self.runtime_state.last_snapshot = self.snapshot
            self._refresh_structure(reason=f"down_{drop:.2f}")
            self._process_pending_orders(price, f"drop_{drop:.2f}")
        return self._summarize(drop_pct)

    def _summarize(self, drop_pct: float) -> SimulationSummary:
        long_qty = self.snapshot.long_qty
        short_qty = self.snapshot.short_qty
        short_avg = self.snapshot.short_avg
        long_avg = self.snapshot.long_avg
        return SimulationSummary(
            drop_pct=drop_pct,
            long_add_distance_pct=self.config.long_fill_distance_pct,
            base_long_qty=self.base_long_qty,
            base_short_qty=self.base_short_qty,
            long_qty=long_qty,
            short_qty=short_qty,
            long_avg=long_avg,
            short_avg=short_avg,
            completed_cycle_count=self.cycle_tracker.completed_cycle_count,
            cycle_snapshots=self.cycle_tracker.cycle_snapshots.copy(),
            first_long_add_price=self.cycle_tracker.first_long_add_price,
            first_long_add_qty=self.cycle_tracker.first_long_add_qty,
            first_short_tp_price=self.cycle_tracker.first_short_tp_price,
            first_short_tp_qty=self.cycle_tracker.first_short_tp_qty,
            post_first_cycle_exposure=self.cycle_tracker.post_first_cycle_exposure,
            long_add_count=self.cycle_tracker.long_add_fills,
            short_tp_count=self.cycle_tracker.short_tp_fills,
            additional_cycles_after_first=self.cycle_tracker.additional_cycles_after_first,
            realized_long_pnl_total=self.snapshot.realized_long_pnl_total,
            realized_short_pnl_total=self.snapshot.realized_short_pnl_total,
            remaining_orders=len(self.pending_orders),
            last_price=self.snapshot.current_price,
        )

    def active_order_purposes(self) -> List[str]:
        return [order.managed.purpose for order in self.pending_orders.values()]


def _print_single_summary(summary: SimulationSummary, runner: SimulationRunner) -> None:
    print("\n== Single run summary ==")
    print(
        f"Drop {summary.drop_pct:.2f}% with long-add distance {summary.long_add_distance_pct:.2f}%"
    )
    print(
        f"Base sizes: long={summary.base_long_qty:.4f}, short={summary.base_short_qty:.4f}"
    )
    print(
        f"Final sizes: long={summary.long_qty:.4f}, short={summary.short_qty:.4f} "
        f"(long multiple {summary.long_multiple_vs_base:.3f}, "
        f"short multiple {summary.short_multiple_vs_base:.3f})"
    )
    print(
        f"Completed cycles: {summary.completed_cycle_count}, "
        f"long_adds={summary.long_add_count}, short_tp={summary.short_tp_count}"
    )
    if summary.first_long_add_price is not None:
        print(
            f"First LONG_ADD fill at {summary.first_long_add_price:.6f} qty={summary.first_long_add_qty:.4f}"
        )
    if summary.first_short_tp_price is not None:
        print(
            f"Matching LONG_TP fill at {summary.first_short_tp_price:.6f} qty={summary.first_short_tp_qty:.4f}"
        )
    if summary.post_first_cycle_exposure is not None:
        print(
            f"Exposure after first LONG_ADD+SHORT_TP cycle: {summary.post_first_cycle_exposure:.4f}"
        )
    for cycle in sorted(summary.cycle_snapshots):
        snapshot = summary.cycle_snapshots[cycle]
        print(
            f" Cycle {cycle}: long={snapshot.long_qty:.4f} ({summary.long_pct_of_start(cycle):.1f}% of base), "
            f"short={snapshot.short_qty:.4f} ({summary.short_pct_of_start(cycle):.1f}% of base)"
        )
    if runner.verbose:
        print("\nEvent log:")
        for entry in runner.event_log:
            print("  " + entry)


def _print_sweep_table(summaries: List[SimulationSummary]) -> None:
    headers = [
        "long_add_dist",
        "drop_pct",
        "long_qty",
        "short_qty",
        "long_multiple",
        "short_multiple",
        "long_avg",
        "short_avg",
        "cycles",
        "long_adds",
        "short_tps",
        "pending_orders",
        "heuristic",
    ]
    row_format = "{:<12} {:<8} {:<9} {:<9} {:<13} {:<14} {:<9} {:<10} {:<7} {:<10} {:<11} {:<15} {}"
    print("\n== Sweep summary ==")
    print(row_format.format(*headers))
    for summary in summaries:
        long_multiple = summary.long_multiple_vs_base
        short_multiple = summary.short_multiple_vs_base
        heuristic = (
            "conservative"
            if summary.long_add_count <= 1
            else "aggressive"
            if summary.long_add_count >= 3
            else "moderate"
        )
        print(
            row_format.format(
                f"{summary.long_add_distance_pct:.2f}%",
                f"{summary.drop_pct:.1f}%",
                f"{summary.long_qty:.4f}",
                f"{summary.short_qty:.4f}",
                f"{long_multiple:.3f}",
                f"{short_multiple:.3f}",
                f"{summary.long_avg:.6f}",
                f"{summary.short_avg:.6f}",
                summary.completed_cycle_count,
                summary.long_add_count,
                summary.short_tp_count,
                summary.remaining_orders,
                heuristic,
            )
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Simulate fixed-cycle downside moves.")
    parser.add_argument(
        "--mode",
        choices=["single", "sweep"],
        default="single",
        help="Run a single scenario or sweep over grids.",
    )
    parser.add_argument("--start-price", type=float, default=0.05429)
    parser.add_argument("--drop-pct", type=float, default=6.0)
    parser.add_argument("--step-pct", type=float, default=0.1)
    parser.add_argument("--long-add-distance-pct", type=float, default=0.5)
    parser.add_argument(
        "--drop-grid", type=str, default="2,4,6,8", help="Comma-separated drop pct values"
    )
    parser.add_argument(
        "--long-add-grid",
        type=str,
        default="0.25,0.5,0.75,1.0,1.25,1.5",
        help="Comma-separated long-add distance pct values",
    )
    return parser.parse_args()


def _load_config() -> FixedCycleHedgeConfig:
    return FixedCycleHedgeConfig.from_json_file(
        "fixed_cycle_hedge_bot/config/fixed_cycle_config.json"
    )


def run_single(args: argparse.Namespace) -> None:
    config = _load_config()
    runner = SimulationRunner(
        config=config,
        start_price=args.start_price,
        long_add_distance_pct=args.long_add_distance_pct,
        verbose=True,
    )
    summary = runner.run(drop_pct=args.drop_pct, step_pct=args.step_pct)
    _print_single_summary(summary, runner)


def run_sweep(args: argparse.Namespace) -> None:
    config = _load_config()
    drop_values = list(_parse_grid(args.drop_grid))
    long_add_values = list(_parse_grid(args.long_add_grid))
    summaries: List[SimulationSummary] = []
    for long_add in long_add_values:
        for drop in drop_values:
            runner = SimulationRunner(
                config=_load_config(),
                start_price=args.start_price,
                long_add_distance_pct=long_add,
            )
            summaries.append(runner.run(drop_pct=drop, step_pct=args.step_pct))
    _print_sweep_table(summaries)


def main() -> None:
    args = parse_args()
    if args.mode == "single":
        run_single(args)
    else:
        run_sweep(args)


if __name__ == "__main__":
    main()
