import unittest

from emergency_100.config import Emergency100Config
from emergency_100.runner import _advance_runtime_state, _merge_runtime_state
from emergency_100.state import Emergency100Mode, Emergency100RuntimeState, HedgeSnapshot, MarketBias
from emergency_100.strategy import Emergency100Strategy


class Emergency100StrategyRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = Emergency100Config()
        self.strategy = Emergency100Strategy(self.config)

    def test_decision_contains_path_for_ping_pong_add_long(self) -> None:
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_size_usdt=100.0,
            short_size_usdt=100.0,
            long_avg=100.0,
            short_avg=98.8,
        )
        runtime = Emergency100RuntimeState(
            mode=Emergency100Mode.PING_PONG,
            cycle_id="cycle-1",
            decision_count=2,
        )

        decision = self.strategy.decide(snapshot=snapshot, runtime=runtime, market_bias=MarketBias.RISING)

        self.assertEqual(decision.reason_code, "ping_pong_add_long")
        self.assertTrue(decision.decision_path)
        self.assertEqual(decision.decision_path[0]["step"], "enter_decide")
        self.assertEqual(decision.decision_path[-1]["step"], "ping_pong_bias_check")

    def test_merge_runtime_uses_loaded_values_until_overridden(self) -> None:
        loaded = Emergency100RuntimeState(
            mode=Emergency100Mode.BRIDGE_TO_NORMAL,
            bridge_step_index=1,
            cycle_id="cycle-1",
            decision_count=4,
            last_decision_id="cycle-1-d0004",
            last_action="reduce_short",
            last_reason="Bridge step",
            notes=["old-note"],
        )

        merged = _merge_runtime_state(
            loaded,
            mode_arg=None,
            bridge_step_index_arg=None,
            cycle_id_arg=None,
            reset_runtime=False,
        )

        self.assertEqual(merged.mode, Emergency100Mode.BRIDGE_TO_NORMAL)
        self.assertEqual(merged.bridge_step_index, 1)
        self.assertEqual(merged.decision_count, 4)
        self.assertEqual(merged.cycle_id, "cycle-1")

    def test_advance_runtime_increments_bridge_step_when_target_satisfied(self) -> None:
        runtime = Emergency100RuntimeState(
            mode=Emergency100Mode.BRIDGE_TO_NORMAL,
            bridge_step_index=1,
            cycle_id="cycle-1",
            decision_count=4,
        )

        next_runtime = _advance_runtime_state(
            runtime,
            decision_id="cycle-1-d0005",
            decision_mode=Emergency100Mode.BRIDGE_TO_NORMAL,
            decision_reason="Bridge target already satisfied.",
            decision_reason_code="bridge_target_satisfied",
            action_kinds=["noop"],
        )

        self.assertEqual(next_runtime.decision_count, 5)
        self.assertEqual(next_runtime.bridge_step_index, 2)
        self.assertEqual(next_runtime.last_decision_id, "cycle-1-d0005")


if __name__ == "__main__":
    unittest.main()
