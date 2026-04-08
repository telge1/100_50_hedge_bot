from __future__ import annotations

from dataclasses import dataclass, field
import logging
import sys
import threading
from collections import deque
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from strategy.config import StrategyConfig
from strategy.execution.order_executor import OrderExecutor, OrderIntent
from strategy.position_manager import PositionManager
from strategy.psrh_strategy import PSRHStrategy
from strategy.risk_manager import RiskManager
from strategy.state_machine import StateMachine, StrategyState


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FakeOrderManager:
    def __init__(self) -> None:
        self.open_orders: list[dict[str, Any]] = []
        self.positions: list[dict[str, Any]] = []
        self.events: list[str] = []
        self.simulated_recovery_heal_market_fill = True

    def set_positions(
        self,
        long_size: float,
        long_avg: float,
        short_size: float,
        short_avg: float,
    ) -> None:
        self.positions = []
        if long_size > 0:
            self.positions.append(
                {
                    "symbol": "BTCUSDT",
                    "side": "long",
                    "size": long_size,
                    "avgPrice": long_avg,
                }
            )
        if short_size > 0:
            self.positions.append(
                {
                    "symbol": "BTCUSDT",
                    "side": "short",
                    "size": short_size,
                    "avgPrice": short_avg,
                }
            )

    def _update_position(self, side: str, qty: float, price: float | None) -> None:
        if qty <= 0 or price is None:
            return
        symbol = "BTCUSDT"
        pos = next((p for p in self.positions if p.get("side") == side), None)
        if not pos:
            pos = {"symbol": symbol, "side": side, "size": 0.0, "avgPrice": 0.0}
            self.positions.append(pos)
        existing_size = float(pos.get("size") or 0.0)
        existing_avg = float(pos.get("avgPrice") or 0.0)
        total_cost = existing_avg * existing_size + price * qty
        new_size = existing_size + qty
        pos["size"] = new_size
        pos["avgPrice"] = total_cost / new_size if new_size else 0.0

    def normalize_qty(self, symbol: str, qty: float, category: str) -> float:
        return qty

    def fetch_instrument_info(self, symbol: str, category: str) -> dict[str, Any]:
        return {"lotSizeFilter": {"qtyStep": "0.001"}}

    def fetch_open_orders(self, symbol: str, category: str):
        self.events.append(f"fetch_open_orders(symbol={symbol}, category={category})")
        return list(self.open_orders)

    def fetch_positions(self, symbol: str | None, category: str, settle_coin: str | None = None):
        self.events.append(
            f"fetch_positions(symbol={symbol}, category={category}, settle_coin={settle_coin})"
        )
        return list(self.positions)

    def place_limit_order(self, payload) -> dict[str, Any]:
        order = {
            "orderLinkId": payload.order_link_id,
            "orderId": f"ex-{payload.order_link_id}",
            "orderStatus": "New",
            "cumExecQty": "0.0",
            "qty": f"{payload.qty}",
        }
        self.open_orders.append(order)
        self.events.append(
            f"place_limit_order(side={payload.side}, qty={payload.qty}, price={payload.price}, purpose_link={payload.order_link_id})"
        )
        return {"result": {"orderId": order["orderId"]}}

    def place_market_order(self, **kwargs) -> dict[str, Any]:
        self.events.append(
            f"place_market_order(side={kwargs['side']}, qty={kwargs['qty']}, order_link_id={kwargs['order_link_id']})"
        )
        price = kwargs.get("price")
        side = kwargs["side"]
        qty = float(kwargs["qty"])
        if price is not None:
            if side == "Buy":
                self._update_position("long", qty, price)
            else:
                self._update_position("short", qty, price)
        return {"result": {"orderId": f"ex-{kwargs['order_link_id']}"}}

    def set_long_take_profit(
        self,
        *,
        symbol: str,
        tp_price: float,
        position_size: float,
        position_idx: int = 1,
        category: str = "linear",
    ) -> dict[str, Any]:
        self.events.append(
            "set_long_take_profit("
            f"symbol={symbol}, position_idx={position_idx}, "
            f"tp_price={tp_price}, position_size={position_size})"
        )
        return {"result": {"symbol": symbol, "positionIdx": position_idx}}

    def set_short_stop_loss(
        self,
        *,
        symbol: str,
        sl_price: float,
        position_size: float,
        position_idx: int = 2,
        category: str = "linear",
    ) -> dict[str, Any]:
        self.events.append(
            "set_short_stop_loss("
            f"symbol={symbol}, position_idx={position_idx}, "
            f"sl_price={sl_price}, position_size={position_size})"
        )
        return {"result": {"symbol": symbol, "positionIdx": position_idx}}

    def place_reduce_market_order(self, **kwargs) -> dict[str, Any]:
        self.events.append(
            f"place_reduce_market_order(side={kwargs['side']}, qty={kwargs['qty']}, order_link_id={kwargs.get('order_link_id')})"
        )
        return {"result": {"orderId": f"ex-{kwargs.get('order_link_id', 'market')}"}}

    def cancel_order(
        self,
        order_id: str,
        *,
        symbol: str | None = None,
        category: str = "linear",
    ) -> bool:
        self.events.append(
            f"cancel_order(order_id={order_id}, symbol={symbol}, category={category})"
        )
        self.open_orders = [
            order for order in self.open_orders if order.get("orderId") != order_id
        ]
        return True

    def ensure_hedge_mode(self, symbol: str, category: str = "linear") -> bool:
        self.events.append(f"ensure_hedge_mode(symbol={symbol}, category={category})")
        return True

    def set_leverage(
        self, symbol: str, buy_leverage: int, sell_leverage: int
    ) -> bool:
        self.events.append(
            f"set_leverage(symbol={symbol}, buy={buy_leverage}, sell={sell_leverage})"
        )
        return True


class ListLoggingHandler(logging.Handler):
    def __init__(self, collector: list[str]) -> None:
        super().__init__(level=logging.INFO)
        self.collector = collector

    def emit(self, record: logging.LogRecord) -> None:
        self.collector.append(self.format(record))


def build_strategy(order_manager: FakeOrderManager) -> PSRHStrategy:
    strategy = PSRHStrategy.__new__(PSRHStrategy)
    config = StrategyConfig(
        api_key="",
        secret_key="",
        initial_balance=10_000_000_000.0,
        max_total_exposure_pct=1.0,
        max_total_notional=10_000_000_000.0,
        min_order_value=1.0,
        max_short_deviation=0.03,
    )

    logger = logging.getLogger("psrh.midtrade.simulator")
    logger.handlers = []
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    logger.setLevel(logging.INFO)
    debug_logs: list[str] = []
    debug_handler = ListLoggingHandler(debug_logs)
    debug_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(debug_handler)

    strategy.config = config
    strategy.position_manager = PositionManager()
    strategy.state_machine = StateMachine()
    strategy.risk_manager = RiskManager(config)
    strategy.orders = []
    strategy.dca_steps = 0
    strategy.last_price = None
    strategy.last_rebuy_time = None
    strategy.initialized = True
    strategy.order_manager = order_manager
    strategy._exchange_ready = True
    strategy.logger = logger
    strategy._simulator_debug_logs = debug_logs
    strategy.last_rebuy_price = None
    strategy._extend_requested = False
    strategy._submitted_orders = set()
    strategy.active_orders = {}
    strategy._order_lock = threading.Lock()
    strategy._recent_orders = deque(maxlen=20)
    strategy._reconcile_thread = None
    strategy._fast_poll_thread = None
    strategy._position_sync_queue = deque()
    strategy._reconcile_stop = threading.Event()
    strategy._fast_poll_stop = threading.Event()
    strategy._exchange_lock = threading.Lock()
    strategy._position_sync_lock = threading.Lock()
    strategy._init_lock = threading.Lock()
    strategy._recovery_lock = threading.RLock()
    strategy._has_recovered = True
    strategy._last_status_log = None
    strategy._last_mismatch_log = None
    strategy._last_hedge_time = None
    strategy._startup_waiting_logged = False
    strategy._exchange_to_client_id = {}
    strategy._initial_hedge_checked = True
    strategy._post_rebuy_exit_target = None
    strategy._last_rebuy_attempted = False
    strategy._last_rebuy_intent_created = False
    strategy._last_tp_short_suppressed = False
    strategy._last_priority_state_before = None
    strategy._last_priority_state_after = None
    strategy._recovery_heal_cooldown_ticks = 0
    strategy._pending_recovery_heal_key = None
    strategy._last_recovery_heal_spread = None
    strategy._last_recovery_heal_price = None

    strategy.executor = OrderExecutor(
        config=strategy.config,
        logger=strategy.logger,
        order_manager=strategy.order_manager,
        position_manager=strategy.position_manager,
        risk_manager=strategy.risk_manager,
        state_machine=strategy.state_machine,
        active_orders=strategy.active_orders,
        submitted_orders=strategy._submitted_orders,
        recent_orders=strategy._recent_orders,
        exchange_to_client_id=strategy._exchange_to_client_id,
        order_lock=strategy._order_lock,
        normalize_order_qty=strategy._normalize_order_qty,
        current_qty_step=strategy._current_qty_step,
        has_active_intent=strategy._has_active_intent,
        generate_client_order_id=strategy._generate_client_order_id,
        log_slippage_check=strategy._log_slippage_check,
        safe_update_order=strategy.safe_update_order,
        mark_order_filled=strategy.mark_order_filled,
        handle_order_finalized_locked=strategy._handle_order_finalized_locked,
        sync_positions_with_exchange=strategy.sync_positions_with_exchange,
        get_position_snapshot=strategy._get_position_snapshot,
        verify_order_on_exchange=strategy.verify_order_on_exchange,
        get_last_price=strategy._get_last_price,
        set_dca_steps=strategy._set_dca_steps,
        on_intent_executed=strategy._on_intent_executed,
    )

    return strategy


def set_midtrade_state(
    strategy: PSRHStrategy,
    *,
    long_size: float,
    long_avg: float,
    short_size: float,
    short_avg: float,
    state: StrategyState,
    last_rebuy_price: float,
    dca_steps: int,
    last_price: float,
    last_rebuy_time: datetime | None,
) -> None:
    strategy.position_manager.sync_positions(
        long_size=long_size,
        long_avg=long_avg,
        short_size=short_size,
        short_avg=short_avg,
    )
    strategy.state_machine.transition(state)
    strategy.last_rebuy_price = last_rebuy_price
    strategy.dca_steps = dca_steps
    strategy.last_price = last_price
    strategy.last_rebuy_time = last_rebuy_time
    strategy._set_initialized(True)


def format_float(value: float | None) -> str:
    if value is None:
        return "None"
    return f"{value:.4f}"


def snapshot(strategy: PSRHStrategy) -> dict[str, Any]:
    long_size, short_size, long_avg, short_avg = strategy._get_position_snapshot()
    return {
        "state": strategy.state_machine.state.value,
        "dca_steps": strategy.dca_steps,
        "last_rebuy_price": strategy.last_rebuy_price,
        "long_size": long_size,
        "long_avg": long_avg,
        "short_size": short_size,
        "short_avg": short_avg,
    }


@dataclass
class ScenarioCase:
    name: str
    expected_branch: str
    long_size: float
    long_avg: float
    short_size: float
    short_avg: float
    state: StrategyState
    last_rebuy_price: float
    dca_steps: int
    last_price: float
    prices: list[float]
    bridge_cooldown: bool = False
    last_rebuy_age_seconds: float | None = None
    expectations: dict[str, Any] = field(default_factory=dict)


def print_tick_result(
    tick: int,
    price: float,
    state: dict[str, Any],
    debug_events: list[str],
    actions: list[str],
    execution_summary: dict[str, Any],
) -> None:
    print(f"Tick {tick} | Preis {price:.2f}")
    print(f"  state: {state['state']}")
    print(f"  dca_steps: {state['dca_steps']}")
    print(f"  last_rebuy_price: {format_float(state['last_rebuy_price'])}")
    print(f"  long_size: {format_float(state['long_size'])}")
    print(f"  long_avg: {format_float(state['long_avg'])}")
    print(f"  short_size: {format_float(state['short_size'])}")
    print(f"  short_avg: {format_float(state['short_avg'])}")
    print("  exec_summary:")
    for key, value in execution_summary.items():
        print(f"    - {key}: {value}")
    if debug_events:
        print("  debug:")
        for event in debug_events:
            print(f"    - {event}")
    if actions:
        print("  actions:")
        for action in actions:
            print(f"    - {action}")
    else:
        print("  actions: none")
    print()


def install_debug_hooks(strategy: PSRHStrategy, debug_events: list[str]) -> None:
    original_update_state = strategy.update_state
    original_ensure_hedge_integrity = strategy.ensure_hedge_integrity
    original_place_long_rebuy = strategy.place_long_rebuy
    original_execute_take_profit = strategy.execute_take_profit

    def tracked_update_state(price: float, spread: float):
        state_before = strategy.state_machine.state.value
        debug_events.append(
            "update_state_called("
            f"price={price:.2f}, "
            f"spread={spread:.6f}, "
            f"threshold={strategy.config.spread_threshold:.6f}, "
            f"recovery_low={strategy.config.recovery_low:.4f}, "
            f"state_before={state_before}"
            ")"
        )
        result = original_update_state(price, spread)
        state_after = strategy.state_machine.state.value
        debug_events.append(
            f"update_state_result(state_after={state_after}, intents={len(result)})"
        )
        if state_before != state_after:
            debug_events.append(f"state_transition({state_before}->{state_after})")
        return result

    def tracked_ensure_hedge_integrity(current_price: float | None = None):
        debug_events.append(
            f"ensure_hedge_integrity_called(price={format_float(current_price)})"
        )
        result = original_ensure_hedge_integrity(current_price)
        debug_events.append(
            "ensure_hedge_integrity_result("
            f"{result.purpose if result else 'None'}"
            ")"
        )
        return result

    def tracked_place_long_rebuy(price: float, spread: float):
        debug_events.append(
            "place_long_rebuy_called("
            f"price={price:.2f}, "
            f"spread={spread:.6f}, "
            f"dca_steps={strategy.dca_steps}, "
            f"last_rebuy_price={format_float(strategy.last_rebuy_price)}"
            ")"
        )
        result = original_place_long_rebuy(price, spread)
        debug_events.append(
            "place_long_rebuy_result("
            f"{result.purpose if result else 'None'}"
            ")"
        )
        return result

    def tracked_execute_take_profit(price: float, allow_tp_short: bool = True):
        debug_events.append(f"execute_take_profit_called(price={price:.2f})")
        result = original_execute_take_profit(price, allow_tp_short=allow_tp_short)
        debug_events.append(f"execute_take_profit_result(intents={len(result)})")
        return result

    strategy.update_state = tracked_update_state
    strategy.ensure_hedge_integrity = tracked_ensure_hedge_integrity
    strategy.place_long_rebuy = tracked_place_long_rebuy
    strategy.execute_take_profit = tracked_execute_take_profit


def build_execution_summary(tick_logs: list[str], actions: list[str]) -> dict[str, Any]:
    decisions = [line for line in tick_logs if "[EXEC DEBUG] decision=" in line]
    return {
        "generated_intents": [action for action in actions if action.startswith("execute_intent(")],
        "execute_attempts": len(
            [line for line in tick_logs if "[EXEC DEBUG] execute_intent_called" in line]
        ),
        "skipped_by_active_intent": any(
            "decision=SKIP_ACTIVE_INTENT" in line for line in tick_logs
        ),
        "skipped_by_submitted_orders": any(
            "decision=SKIP_ALREADY_SUBMITTED" in line for line in tick_logs
        ),
        "skipped_by_duplicate_guard": any(
            ("decision=SKIP_ACTIVE_INTENT" in line)
            or ("decision=SKIP_ALREADY_SUBMITTED" in line)
            for line in tick_logs
        ),
        "place_order_called": any(
            "[EXEC DEBUG] place_order_called" in line for line in tick_logs
        ),
        "final_execution_decision": decisions[-1] if decisions else "EXECUTE_OR_NO_DECISION_LOG",
    }


def build_scenarios() -> list[ScenarioCase]:
    return [
        ScenarioCase(
            name="scenario_1_threshold_below_normal",
            expected_branch="stay NORMAL, no RECOVERY, no rebuy path",
            long_size=1400.0,
            long_avg=67908.0,
            short_size=700.0,
            short_avg=66920.0,
            state=StrategyState.NORMAL,
            last_rebuy_price=67653.0,
            dca_steps=1,
            last_price=66920.0,
            prices=[66920.0],
            expectations={
                "final_state": "normal",
                "rebuy_called": False,
                "intent_generated": False,
                "forced_rebuy_triggered": False,
            },
        ),
        ScenarioCase(
            name="scenario_2_threshold_above_recovery",
            expected_branch="RECOVERY reached and first normal rebuy intent created",
            long_size=1400.0,
            long_avg=67908.0,
            short_size=700.0,
            short_avg=66800.0,
            state=StrategyState.NORMAL,
            last_rebuy_price=67653.0,
            dca_steps=1,
            last_price=66920.0,
            prices=[66920.0],
            expectations={
                "final_state": "recovery",
                "rebuy_called": True,
                "intent_generated": True,
                "forced_rebuy_triggered": False,
            },
        ),
        ScenarioCase(
            name="scenario_3_recovery_cooldown_blocks",
            expected_branch="RECOVERY active, rebuy function entered, cooldown blocks follow-up",
            long_size=1400.0,
            long_avg=67908.0,
            short_size=700.0,
            short_avg=66800.0,
            state=StrategyState.RECOVERY,
            last_rebuy_price=67314.735,
            dca_steps=2,
            last_price=66920.0,
            prices=[66560.0, 66200.0],
            last_rebuy_age_seconds=0.0,
            expectations={
                "final_state": "recovery",
                "rebuy_called": True,
                "intent_generated": False,
                "cooldown_blocked": True,
            },
        ),
        ScenarioCase(
            name="scenario_4_cooldown_bridged_dedupe",
            expected_branch="RECOVERY + rebuy execution path + dedupe visible after cooldown bridge",
            long_size=1400.0,
            long_avg=67908.0,
            short_size=700.0,
            short_avg=66800.0,
            state=StrategyState.NORMAL,
            last_rebuy_price=67653.0,
            dca_steps=1,
            last_price=66920.0,
            prices=[66920.0, 66560.0, 66200.0],
            bridge_cooldown=True,
            expectations={
                "final_state": "recovery",
                "rebuy_called": True,
                "intent_generated": True,
                "dedupe_blocked": True,
            },
        ),
        ScenarioCase(
            name="scenario_5_forced_rebuy_switchpoint",
            expected_branch="normal rebuy first, then forced rebuy after short-distance threshold",
            long_size=1400.0,
            long_avg=67908.0,
            short_size=700.0,
            short_avg=66800.0,
            state=StrategyState.RECOVERY,
            last_rebuy_price=66869.32717971373,
            dca_steps=3,
            last_price=65300.0,
            prices=[65300.0, 64200.0],
            bridge_cooldown=True,
            last_rebuy_age_seconds=1.0,
            expectations={
                "final_state": "recovery",
                "rebuy_called": True,
                "intent_generated": True,
                "forced_rebuy_triggered": True,
            },
        ),
        ScenarioCase(
            name="scenario_6_realistic_hedge_start",
            expected_branch="hedge start with immediate spread pressure",
            long_size=1000.0,
            long_avg=100.0,
            short_size=500.0,
            short_avg=98.0,
            state=StrategyState.NORMAL,
            last_rebuy_price=100.0,
            dca_steps=1,
            last_price=100.0,
            prices=[100.0, 99.0, 98.0, 96.5, 95.0],
            expectations={
                "final_state": "recovery",
                "rebuy_called": True,
                "intent_generated": True,
                "forced_rebuy_triggered": False,
            },
        ),
        ScenarioCase(
            name="scenario_7_real_numbers_tp_wait_for_hedge",
            expected_branch="hedge unwind via TP_SHORT then wait for hedge",
            long_size=1000.0,
            long_avg=100.0,
            short_size=500.0,
            short_avg=98.0,
            state=StrategyState.NORMAL,
            last_rebuy_price=100.0,
            dca_steps=1,
            last_price=100.0,
            prices=[100.0, 99.0, 98.0, 96.5, 95.0],
            expectations={
                "final_state": "wait_for_hedge",
                "rebuy_called": True,
                "intent_generated": True,
                "forced_rebuy_triggered": False,
            },
        ),
        ScenarioCase(
            name="scenario_8_real_numbers_rebuy_drift",
            expected_branch="drift-triggered rebuy leading into recovery",
            long_size=1000.0,
            long_avg=100.0,
            short_size=500.0,
            short_avg=94.0,
            state=StrategyState.NORMAL,
            last_rebuy_price=100.0,
            dca_steps=1,
            last_price=97.0,
            prices=[97.0, 96.0, 95.0, 94.0, 92.5, 91.0],
            expectations={
                "final_state": "recovery",
                "rebuy_called": True,
                "intent_generated": True,
                "forced_rebuy_triggered": True,
            },
        ),
    ]


def summarize_scenario(
    scenario: ScenarioCase,
    strategy: PSRHStrategy,
    tick_results: list[dict[str, Any]],
) -> dict[str, Any]:
    all_debug = [
        event
        for tick_result in tick_results
        for event in tick_result["debug_events"]
    ]
    all_actions = [
        action
        for tick_result in tick_results
        for action in tick_result["actions"]
    ]
    transitions = [
        event.removeprefix("state_transition(").removesuffix(")")
        for event in all_debug
        if event.startswith("state_transition(")
    ]
    intent_generated = [action for action in all_actions if action.startswith("execute_intent(")]
    summary = {
        "scenario_name": scenario.name,
        "expected_branch": scenario.expected_branch,
        "actual_state_transitions": transitions or ["none"],
        "rebuy_called": any(
            event.startswith("place_long_rebuy_called") for event in all_debug
        ),
        "intent_generated": intent_generated,
        "cooldown_blocked": any("[REBUY DEBUG] skip: cooldown" in event for event in all_debug),
        "dedupe_blocked": any(
            ("decision=SKIP_ALREADY_SUBMITTED" in event)
            or ("decision=SKIP_ACTIVE_INTENT" in event)
            for event in all_debug
        ),
        "forced_rebuy_triggered": any("FORCED REBUY TRIGGERED" in event for event in all_debug),
        "final_state": strategy.state_machine.state.value,
    }

    checks: list[bool] = []
    for key, expected in scenario.expectations.items():
        actual = summary[key]
        if key == "intent_generated":
            checks.append(bool(actual) is bool(expected))
        else:
            checks.append(actual == expected)
    summary["match"] = all(checks)
    summary["actual_summary"] = (
        f"state={summary['final_state']}, "
        f"transitions={summary['actual_state_transitions']}, "
        f"rebuy_called={summary['rebuy_called']}, "
        f"intents={len(summary['intent_generated'])}, "
        f"cooldown_blocked={summary['cooldown_blocked']}, "
        f"dedupe_blocked={summary['dedupe_blocked']}, "
        f"forced_rebuy_triggered={summary['forced_rebuy_triggered']}"
    )
    return summary


def print_scenario_summary(summary: dict[str, Any]) -> None:
    print("Scenario summary:")
    print(f"  - scenario_name: {summary['scenario_name']}")
    print(f"  - expected_branch: {summary['expected_branch']}")
    print(f"  - actual_state_transitions: {summary['actual_state_transitions']}")
    print(f"  - rebuy_called: {summary['rebuy_called']}")
    print(f"  - intent_generated: {summary['intent_generated']}")
    print(f"  - cooldown_blocked: {summary['cooldown_blocked']}")
    print(f"  - dedupe_blocked: {summary['dedupe_blocked']}")
    print(f"  - forced_rebuy_triggered: {summary['forced_rebuy_triggered']}")
    print(f"  - final_state: {summary['final_state']}")
    print(f"  - expected: {summary['expected_branch']}")
    print(f"  - actual: {summary['actual_summary']}")
    print(f"  - match: {'YES' if summary['match'] else 'NO'}")
    print()


def resolve_last_rebuy_time(age_seconds: float | None) -> datetime | None:
    if age_seconds is None:
        return None
    return utcnow() - timedelta(seconds=age_seconds)


def run_scenario(scenario: ScenarioCase) -> dict[str, Any]:
    order_manager = FakeOrderManager()
    strategy = build_strategy(order_manager)
    intent_events: list[str] = []
    debug_events: list[str] = []
    original_execute_intent = strategy.executor.execute_intent

    def tracked_execute_intent(
        intent: OrderIntent,
        enqueue_follow_ups=None,
        allow_tp_short: bool = True,
    ) -> bool:
        intent_events.append(
            f"execute_intent(purpose={intent.purpose}, side={intent.side}, qty={intent.qty}, price={intent.price})"
        )
        executed = original_execute_intent(
            intent,
            enqueue_follow_ups=enqueue_follow_ups,
            allow_tp_short=allow_tp_short,
        )
        if executed and intent.purpose == "HEDGE_RECOVER":
            strategy.sync_positions_with_exchange()
        return executed

    strategy.executor.execute_intent = tracked_execute_intent
    install_debug_hooks(strategy, debug_events)

    set_midtrade_state(
        strategy,
        long_size=scenario.long_size,
        long_avg=scenario.long_avg,
        short_size=scenario.short_size,
        short_avg=scenario.short_avg,
        state=scenario.state,
        last_rebuy_price=scenario.last_rebuy_price,
        dca_steps=scenario.dca_steps,
        last_price=scenario.last_price,
        last_rebuy_time=resolve_last_rebuy_time(scenario.last_rebuy_age_seconds),
    )
    order_manager.set_positions(
        scenario.long_size,
        scenario.long_avg,
        scenario.short_size,
        scenario.short_avg,
    )

    print(f"=== {scenario.name} ===")
    print(f"Expected branch: {scenario.expected_branch}")
    print(
        "Start state: "
        f"long_size={scenario.long_size}, long_avg={scenario.long_avg}, "
        f"short_size={scenario.short_size}, short_avg={scenario.short_avg}, "
        f"state={scenario.state.value}, last_rebuy_price={scenario.last_rebuy_price}, "
        f"dca_steps={scenario.dca_steps}, last_price={scenario.last_price}, "
        f"last_rebuy_time={strategy.last_rebuy_time}"
    )
    print(f"Prices: {scenario.prices}")
    print()

    tick_results: list[dict[str, Any]] = []
    for idx, price in enumerate(scenario.prices, start=1):
        strategy.executor._sim_current_tick = idx
        order_event_offset = len(order_manager.events)
        intent_event_offset = len(intent_events)
        debug_event_offset = len(debug_events)
        log_offset = len(strategy._simulator_debug_logs)

        hedge_spread = abs(strategy.calculate_hedge_spread())
        market_deviation = strategy.calculate_market_deviation(price)
        debug_events.append(
            "precheck("
            f"state_before={strategy.state_machine.state.value}, "
            f"hedge_spread={hedge_spread:.6f}, "
            f"spread_threshold={strategy.config.spread_threshold:.6f}, "
            f"market_deviation={market_deviation:.6f}, "
            f"recovery_low={strategy.config.recovery_low:.4f}, "
            f"price_below_recovery_low={price <= strategy.config.recovery_low}"
            ")"
        )

        if scenario.bridge_cooldown and strategy.last_rebuy_time:
            strategy.last_rebuy_time -= timedelta(
                seconds=strategy.config.min_rebuy_interval + 0.01
            )
        strategy.on_price_update(price)

        tick_debug = (
            debug_events[debug_event_offset:]
            + strategy._simulator_debug_logs[log_offset:]
        )
        if strategy.last_rebuy_time:
            elapsed = (utcnow() - strategy.last_rebuy_time).total_seconds()
        else:
            elapsed = 0.0
        tick_debug.append(
            "cooldown_rt("
            f"last_rebuy_time={strategy.last_rebuy_time}, "
            f"elapsed={elapsed:.3f}, "
            f"min_interval={strategy.config.min_rebuy_interval:.3f}, "
            f"cooldown_ok={elapsed >= strategy.config.min_rebuy_interval}"
            ")"
        )
        actions = (
            intent_events[intent_event_offset:]
            + order_manager.events[order_event_offset:]
        )
        if not any(event.startswith("place_long_rebuy_called") for event in tick_debug):
            debug_events.append(
                "blockade(rebuy_path_not_reached="
                f"{not strategy.state_machine.is_recovery()}, "
                f"state_after={strategy.state_machine.state.value})"
            )
            tick_debug = debug_events[debug_event_offset:]
        execution_summary = build_execution_summary(
            strategy._simulator_debug_logs[log_offset:],
            actions,
        )
        print_tick_result(
            idx,
            price,
            snapshot(strategy),
            tick_debug,
            actions,
            execution_summary,
        )
        tick_results.append(
            {
                "debug_events": tick_debug,
                "actions": actions,
                "execution_summary": execution_summary,
                "priority_info": {
                    "tick": idx,
                    "price": price,
                    "rebuy_attempted": strategy._last_rebuy_attempted,
                    "rebuy_intent_created": strategy._last_rebuy_intent_created,
                    "tp_short_suppressed": strategy._last_tp_short_suppressed,
                    "state_before": strategy._last_priority_state_before,
                    "state_after": strategy._last_priority_state_after,
                    "generated_intents": execution_summary["generated_intents"],
                },
            }
        )

    summary = summarize_scenario(scenario, strategy, tick_results)
    print_scenario_summary(summary)
    summary["tick_priority_history"] = [
        tick["priority_info"] for tick in tick_results
    ]
    return summary


def print_overview_table(summaries: list[dict[str, Any]]) -> None:
    print("Overview")
    for summary in summaries:
        print(
            f"- {summary['scenario_name']} | "
            f"expectation={summary['expected_branch']} | "
            f"observation={summary['actual_summary']} | "
            f"passt={'YES' if summary['match'] else 'NO'}"
        )


def main() -> None:
    selected_name = sys.argv[1] if len(sys.argv) > 1 else None
    scenarios = build_scenarios()
    if selected_name:
        scenarios = [scenario for scenario in scenarios if scenario.name == selected_name]
        if not scenarios:
            available = ", ".join(scenario.name for scenario in build_scenarios())
            raise SystemExit(f"Unknown scenario '{selected_name}'. Available: {available}")

    print("PSRH Mid-Trade Simulator")
    print("This runner uses real strategy code with a local fake order manager.")
    print("Positions do not auto-fill unless you extend the fake manually.\n")

    summaries = [run_scenario(scenario) for scenario in scenarios]
    print_overview_table(summaries)


if __name__ == "__main__":
    main()
