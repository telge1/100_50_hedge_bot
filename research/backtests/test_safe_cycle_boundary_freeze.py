"""Unit tests for Safe Cycle Boundary Freeze (research-only)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from fixed_cycle_hedge_bot.fixed_cycle_strategy import TpProjection
from fixed_cycle_hedge_bot.models import StrategyIntent

from research.backtests.inventory_mtm_freeze import (
    InventoryMtmFreezeConfig,
    is_new_cycle_open_purpose,
)
from research.backtests.inventory_mtm_freeze_shim import install_inventory_mtm_freeze
from research.backtests.safe_cycle_boundary_freeze import (
    FREEZE_ACTIVE,
    FREEZE_PENDING,
    detect_invalid_partial_cycle,
    is_direction_aware_cycle_opener,
    is_direction_aware_second_leg,
    is_next_cycle_first_leg_opener,
    legacy_short_opener_bug_would_match,
    resolve_requested_cycle_at_trigger,
    safe_boundary_ready,
)
from research.backtests.safe_cycle_boundary_policy import build_s0_s3_specs


class _FakeBook:
    def __init__(self) -> None:
        self.long_qty = 0.0
        self.long_avg = 0.0
        self.short_qty = 0.0
        self.short_avg = 0.0


class _FakeCandle:
    def __init__(self, close: float) -> None:
        self.close = close


class _FakeFill:
    def __init__(self, purpose: str, *, closed_pnl: float = 0.0) -> None:
        self.purpose = purpose
        self.metadata = {"confirmed_closed_pnl": closed_pnl}


class _FakeStrategy:
    def __init__(self, *, primary_side: str = "long") -> None:
        self._primary_side = primary_side
        self.LONG_TP_EXIT_PURPOSE = "LONG_TP_EXIT"
        self.SHORT_SL_EXIT_PURPOSE = "SHORT_SL_EXIT"

    def _get_primary_position_side(self) -> str:
        return self._primary_side

    def _calculate_tp_projection(
        self, break_even_price: float, snapshot: Any = None, runtime_state: Any = None
    ) -> TpProjection:
        return TpProjection(
            tp_price=float(break_even_price or 100.0),
            target_delta_usdt=0.0,
            expected_total_net_after_exit=0.0,
            target_total_profit_usdt=0.0,
            required_profit_to_cover_loss=0.0,
            min_profit_target_usdt=0.0,
            min_required_total_usdt=0.0,
            components=None,
            fee_rate=0.00055,
            entry_fee_usdt=0.0,
            close_fee_usdt=0.0,
            pending_cycle_loss_usdt=0.0,
            realized_cycle_net=0.0,
        )


class _FakeRuntimeState:
    def __init__(self) -> None:
        self.strategy_state: dict[str, Any] = {
            "active_cycle_index": 1,
            "cycle_completed_count": 0,
            "cycle_step": "WAITING_FOR_PAIR_FIRST_LEG",
            "cycle_states": {},
            "last_exit_signature": None,
        }
        self.last_snapshot = None


class _FakeProcessCandleResult:
    def __init__(self, *, candle: _FakeCandle, candle_fills: list[_FakeFill] | None = None) -> None:
        self.candle = candle
        self.candle_fills = list(candle_fills or [])


class _FakeSim:
    def __init__(self, *, primary_side: str = "long") -> None:
        self.book = _FakeBook()
        self.strategy = _FakeStrategy(primary_side=primary_side)
        self.runtime_state = _FakeRuntimeState()
        self.intent_filter = None
        self.candle_index = 0
        self.candle = _FakeCandle(100.0)
        self._queued_entry_fills: list[_FakeFill] = []
        self._queued_candle_fills: list[_FakeFill] = []
        self.submit_calls: list[list[Any]] = []

    def run_entry_smoke(self):
        return SimpleNamespace(entry_fills=list(self._queued_entry_fills))

    def process_candle(self, candle: _FakeCandle, **kwargs: Any) -> _FakeProcessCandleResult:
        self.candle = candle
        result = _FakeProcessCandleResult(candle=candle, candle_fills=list(self._queued_candle_fills))
        self._queued_candle_fills = []
        return result

    def submit_intents_to_book(self, intents: list[Any], **kwargs: Any) -> list[Any]:
        self.submit_calls.append(list(intents))
        kept = []
        for intent in intents:
            if self.intent_filter is not None and not self.intent_filter(intent):
                continue
            kept.append(intent)
        return kept

    def _refresh_snapshot_from_book(self, *, source: str) -> None:
        return None


def _s1_config() -> InventoryMtmFreezeConfig:
    return InventoryMtmFreezeConfig(
        variant="A1",
        threshold_usdt=-0.50,
        safe_cycle_boundary=True,
        safe_boundary_arm_mode="mtm",
        safe_boundary_variant="S1",
    )


def _fire_mtm_trigger(sim: _FakeSim, *, candle_index: int = 1, mark: float = 99.0) -> None:
    # long 10 @ 100 with mark 99 → mtm = -10 < -0.50
    sim.book.long_qty = 10.0
    sim.book.long_avg = 100.0
    sim.book.short_qty = 0.0
    sim.book.short_avg = 100.0
    sim.candle_index = candle_index
    sim.runtime_state.strategy_state.update(
        {
            "active_cycle_index": 2,
            "cycle_completed_count": 1,
            "cycle_step": "WAITING_FOR_PAIR_SECOND_LEG",
            "cycle_states": {
                1: {"complete": True},
                2: {"complete": False, "long_add_status": "FILLED"},
            },
            "last_exit_signature": None,
        }
    )
    sim.process_candle(_FakeCandle(mark))


def test_specs_contain_s0_s3() -> None:
    names = [s.name for s in build_s0_s3_specs()]
    assert names == ["S0", "S1", "S2", "S3"]
    by = {s.name: s for s in build_s0_s3_specs()}
    assert by["S0"].freeze_config.safe_cycle_boundary is False
    assert by["S1"].freeze_config.safe_cycle_boundary is True
    assert by["S2"].freeze_config.stop_after_cycle == 1
    assert by["S3"].freeze_config.stop_after_cycle == 2


def test_s1_trigger_sets_pending_only() -> None:
    sim = _FakeSim()
    install_inventory_mtm_freeze(sim, _s1_config())
    _fire_mtm_trigger(sim)
    sb = sim.strategy._backtest_inventory_mtm_freeze_state.safe_boundary
    assert sb.freeze_state == FREEZE_PENDING
    assert sim.strategy._backtest_inventory_mtm_freeze_state.cycle_freeze_enabled is False
    # Opener of next cycle still allowed while pending.
    opener = StrategyIntent(
        side="long", qty=1.0, purpose="CYCLE_3_LONG_ADD", order_type="Limit", reduce_only=True
    )
    assert sim.intent_filter(opener) is True


def test_second_leg_allowed_while_pending() -> None:
    sim = _FakeSim()
    install_inventory_mtm_freeze(sim, _s1_config())
    _fire_mtm_trigger(sim)
    intent = StrategyIntent(
        side="short", qty=1.0, purpose="CYCLE_2_SHORT_REDUCE", order_type="Limit", reduce_only=True
    )
    assert sim.intent_filter(intent) is True
    actions = [a["action"] for a in sim.strategy._backtest_inventory_mtm_policy_actions]
    assert "second_leg_allowed_while_pending" in actions or "freeze_pending_entered" in actions


def test_refill_exit_coverage_allowed_while_pending() -> None:
    sim = _FakeSim()
    install_inventory_mtm_freeze(sim, _s1_config())
    _fire_mtm_trigger(sim)
    for purpose in ("REFILL_LONG", "LONG_TP_EXIT", "SHORT_SL_EXIT"):
        intent = StrategyIntent(
            side="long", qty=1.0, purpose=purpose, order_type="Limit", reduce_only=True
        )
        assert sim.intent_filter(intent) is True


def test_activation_requires_complete_and_exit_signature() -> None:
    sim = _FakeSim()
    install_inventory_mtm_freeze(sim, _s1_config())
    _fire_mtm_trigger(sim)
    sb = sim.strategy._backtest_inventory_mtm_freeze_state.safe_boundary
    assert sb.freeze_requested_cycle == 2

    # Incomplete → stay pending
    sim.runtime_state.strategy_state["cycle_states"][2] = {
        "complete": False,
        "long_add_status": "FILLED",
    }
    sim.runtime_state.strategy_state["cycle_step"] = "WAITING_FOR_PAIR_SECOND_LEG"
    sim.runtime_state.strategy_state["last_exit_signature"] = None
    sim.submit_intents_to_book([])
    assert sb.freeze_state == FREEZE_PENDING

    # Complete but missing exit signature with open inventory → stay pending
    sim.runtime_state.strategy_state["cycle_states"][2] = {"complete": True}
    sim.runtime_state.strategy_state["cycle_completed_count"] = 2
    sim.runtime_state.strategy_state["cycle_step"] = "WAITING_FOR_PAIR_FIRST_LEG"
    sim.runtime_state.strategy_state["last_exit_signature"] = None
    sim.book.long_qty = 5.0
    sim.submit_intents_to_book([])
    assert sb.freeze_state == FREEZE_PENDING

    # Ready
    sim.runtime_state.strategy_state["last_exit_signature"] = ("sig", 5.0, 5.0)
    sim.submit_intents_to_book([])
    assert sb.freeze_state == FREEZE_ACTIVE
    assert sb.freeze_activated_after_cycle == 2


def test_active_blocks_only_next_first_leg_opener_long() -> None:
    sim = _FakeSim()
    install_inventory_mtm_freeze(sim, _s1_config())
    _fire_mtm_trigger(sim)
    sb = sim.strategy._backtest_inventory_mtm_freeze_state.safe_boundary
    sim.runtime_state.strategy_state.update(
        {
            "cycle_states": {1: {"complete": True}, 2: {"complete": True}},
            "cycle_completed_count": 2,
            "cycle_step": "WAITING_FOR_PAIR_FIRST_LEG",
            "last_exit_signature": "ok",
        }
    )
    sim.book.long_qty = 1.0
    sim.submit_intents_to_book([])
    assert sb.freeze_state == FREEZE_ACTIVE

    next_opener = StrategyIntent(
        side="long", qty=1.0, purpose="CYCLE_3_LONG_ADD", order_type="Limit", reduce_only=True
    )
    second = StrategyIntent(
        side="short", qty=1.0, purpose="CYCLE_2_SHORT_REDUCE", order_type="Limit", reduce_only=True
    )
    refill = StrategyIntent(
        side="long", qty=1.0, purpose="REFILL_LONG", order_type="Market", reduce_only=False
    )
    assert sim.intent_filter(next_opener) is False
    assert sim.intent_filter(second) is True
    assert sim.intent_filter(refill) is True
    assert "CYCLE_3_LONG_ADD" in sb.blocked_opener_purposes


def test_long_primary_opener_via_direction_config() -> None:
    assert is_direction_aware_cycle_opener("CYCLE_2_LONG_ADD", primary_side="long") is True
    assert is_direction_aware_cycle_opener("CYCLE_2_SHORT_REDUCE", primary_side="long") is False
    assert is_direction_aware_second_leg("CYCLE_2_SHORT_REDUCE", primary_side="long") is True


def test_short_primary_opener_is_short_reduce_not_short_add() -> None:
    assert is_direction_aware_cycle_opener("CYCLE_2_SHORT_REDUCE", primary_side="short") is True
    assert is_direction_aware_cycle_opener("CYCLE_2_SHORT_ADD", primary_side="short") is False
    assert legacy_short_opener_bug_would_match("CYCLE_2_SHORT_ADD") is True
    # Legacy helper incorrectly treats SHORT_ADD as opener:
    assert is_new_cycle_open_purpose("CYCLE_2_SHORT_ADD", primary_side="short") is True
    assert is_new_cycle_open_purpose("CYCLE_2_SHORT_REDUCE", primary_side="short") is False


def test_short_primary_active_blocks_short_reduce_opener() -> None:
    sim = _FakeSim(primary_side="short")
    install_inventory_mtm_freeze(sim, _s1_config())
    _fire_mtm_trigger(sim)
    sb = sim.strategy._backtest_inventory_mtm_freeze_state.safe_boundary
    sim.runtime_state.strategy_state.update(
        {
            "cycle_states": {2: {"complete": True}},
            "cycle_completed_count": 2,
            "cycle_step": "WAITING_FOR_PAIR_FIRST_LEG",
            "last_exit_signature": "ok",
        }
    )
    sim.submit_intents_to_book([])
    assert sb.freeze_state == FREEZE_ACTIVE
    opener = StrategyIntent(
        side="short", qty=1.0, purpose="CYCLE_3_SHORT_REDUCE", order_type="Limit", reduce_only=True
    )
    false_opener = StrategyIntent(
        side="short", qty=1.0, purpose="CYCLE_3_SHORT_ADD", order_type="Limit", reduce_only=False
    )
    second = StrategyIntent(
        side="long", qty=1.0, purpose="CYCLE_2_LONG_REDUCE", order_type="Limit", reduce_only=True
    )
    assert sim.intent_filter(opener) is False
    assert sim.intent_filter(false_opener) is True  # not a DirectionConfig opener
    assert sim.intent_filter(second) is True


def test_long_path_second_leg_short_reduce_never_blocked_as_opener() -> None:
    assert is_next_cycle_first_leg_opener(
        "CYCLE_2_SHORT_REDUCE", primary_side="long", activated_after_cycle=1
    ) is False


def test_s2_arms_pending_and_activates_after_cycle_1() -> None:
    sim = _FakeSim()
    cfg = InventoryMtmFreezeConfig(
        variant="A1",
        use_mtm_trigger=False,
        safe_cycle_boundary=True,
        safe_boundary_arm_mode="stop_after_cycle",
        stop_after_cycle=1,
        safe_boundary_variant="S2",
    )
    install_inventory_mtm_freeze(sim, cfg)
    sim.book.long_qty = 2.0
    sim.book.long_avg = 100.0
    sim.candle_index = 0
    sim.runtime_state.strategy_state.update(
        {
            "active_cycle_index": 1,
            "cycle_completed_count": 0,
            "cycle_step": "WAITING_FOR_PAIR_FIRST_LEG",
            "cycle_states": {1: {"complete": False}},
            "last_exit_signature": None,
        }
    )
    sim.process_candle(_FakeCandle(100.0))
    sb = sim.strategy._backtest_inventory_mtm_freeze_state.safe_boundary
    assert sb.freeze_state == FREEZE_PENDING
    assert sb.freeze_requested_cycle == 1

    sim.runtime_state.strategy_state.update(
        {
            "cycle_states": {1: {"complete": True}},
            "cycle_completed_count": 1,
            "cycle_step": "WAITING_FOR_PAIR_FIRST_LEG",
            "last_exit_signature": "exit1",
        }
    )
    sim.submit_intents_to_book(
        [
            StrategyIntent(
                side="long", qty=1.0, purpose="CYCLE_2_LONG_ADD", order_type="Limit", reduce_only=True
            )
        ]
    )
    assert sb.freeze_state == FREEZE_ACTIVE
    assert sb.blocked_opener_count >= 1


def test_s3_blocks_only_after_cycle_2() -> None:
    sim = _FakeSim()
    cfg = InventoryMtmFreezeConfig(
        variant="A1",
        use_mtm_trigger=False,
        safe_cycle_boundary=True,
        safe_boundary_arm_mode="stop_after_cycle",
        stop_after_cycle=2,
        safe_boundary_variant="S3",
    )
    install_inventory_mtm_freeze(sim, cfg)
    sim.book.long_qty = 2.0
    sim.book.long_avg = 100.0
    sim.process_candle(_FakeCandle(100.0))
    sb = sim.strategy._backtest_inventory_mtm_freeze_state.safe_boundary
    assert sb.freeze_requested_cycle == 2
    # Cycle 1 complete is not enough
    sim.runtime_state.strategy_state.update(
        {
            "cycle_states": {1: {"complete": True}, 2: {"complete": False}},
            "cycle_completed_count": 1,
            "cycle_step": "WAITING_FOR_PAIR_SECOND_LEG",
            "last_exit_signature": "early",
        }
    )
    sim.submit_intents_to_book([])
    assert sb.freeze_state == FREEZE_PENDING


def test_safe_boundary_ready_helpers() -> None:
    ready, reason = safe_boundary_ready(
        {
            "cycle_states": {1: {"complete": True}},
            "cycle_completed_count": 1,
            "cycle_step": "WAITING_FOR_PAIR_FIRST_LEG",
            "last_exit_signature": "x",
        },
        requested_cycle=1,
        long_qty=1.0,
        short_qty=1.0,
    )
    assert ready is True
    assert reason == "ready"

    ready2, reason2 = safe_boundary_ready(
        {
            "cycle_states": {1: {"complete": False}},
            "cycle_completed_count": 0,
            "cycle_step": "WAITING_FOR_PAIR_SECOND_LEG",
            "last_exit_signature": "x",
        },
        requested_cycle=1,
        long_qty=1.0,
        short_qty=1.0,
    )
    assert ready2 is False


def test_resolve_requested_cycle_at_trigger() -> None:
    assert (
        resolve_requested_cycle_at_trigger(
            {
                "active_cycle_index": 2,
                "cycle_step": "WAITING_FOR_PAIR_SECOND_LEG",
                "cycle_completed_count": 1,
            }
        )
        == 2
    )
    assert (
        resolve_requested_cycle_at_trigger(
            {
                "active_cycle_index": 2,
                "cycle_step": "WAITING_FOR_PAIR_FIRST_LEG",
                "cycle_completed_count": 1,
            }
        )
        == 1
    )


def test_invalid_partial_cycle_detector() -> None:
    assert (
        detect_invalid_partial_cycle(
            {
                "cycle_step": "WAITING_FOR_PAIR_FIRST_LEG",
                "active_cycle_index": 3,
                "cycle_states": {
                    2: {"complete": False, "long_add_status": "FILLED", "short_reduce_status": "OPEN"}
                },
            }
        )
        is True
    )
    assert (
        detect_invalid_partial_cycle(
            {
                "cycle_step": "WAITING_FOR_PAIR_FIRST_LEG",
                "cycle_states": {1: {"complete": True, "long_add_status": "FILLED"}},
            }
        )
        is False
    )


def test_policy_disabled_a0_noop() -> None:
    sim = _FakeSim()
    original = sim.process_candle
    install_inventory_mtm_freeze(sim, InventoryMtmFreezeConfig(variant="A0"))
    assert sim.process_candle == original
    assert sim.intent_filter is None


def test_s0_spec_matches_c1a_threshold() -> None:
    s0 = next(s for s in build_s0_s3_specs() if s.name == "S0")
    assert s0.freeze_config.threshold_usdt == -0.50
    assert s0.freeze_config.variant == "A1"
    assert s0.freeze_config.safe_cycle_boundary is False


def test_injusdt_marker_helper_still_separate() -> None:
    from research.backtests.inventory_mtm_freeze import is_injusdt_trade8_undercoverage

    assert is_injusdt_trade8_undercoverage(coin="INJUSDT", trade_number=8) is True
    assert is_injusdt_trade8_undercoverage(coin="INJUSDT", trade_number=7) is False


def test_deterministic_pending_activation_repeat() -> None:
    def once() -> str:
        sim = _FakeSim()
        install_inventory_mtm_freeze(sim, _s1_config())
        _fire_mtm_trigger(sim)
        sim.runtime_state.strategy_state.update(
            {
                "cycle_states": {2: {"complete": True}},
                "cycle_completed_count": 2,
                "cycle_step": "WAITING_FOR_PAIR_FIRST_LEG",
                "last_exit_signature": "sig",
            }
        )
        sim.submit_intents_to_book([])
        sb = sim.strategy._backtest_inventory_mtm_freeze_state.safe_boundary
        return f"{sb.freeze_state}:{sb.freeze_activated_after_cycle}"

    assert once() == once() == f"{FREEZE_ACTIVE}:2"
