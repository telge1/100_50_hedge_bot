from scripts.simulate_psrh_midtrade import build_strategy, FakeOrderManager, OrderIntent


def test_emergency_intent_bypasses_market_block(tmp_path):
    strategy = build_strategy(FakeOrderManager())
    intent = OrderIntent(side="short", qty=500.0, price=99.0, purpose="HEDGE_RECOVER", order_type="Market")

    def fail_market(*args, **kwargs):
        return False

    strategy.executor._place_market_order_on_exchange = fail_market
    result = strategy.executor.execute_intent(intent)
    assert result is True, "Emergency intent should bypass market_execution_blocked"


def test_non_emergency_market_intent_still_blocked(tmp_path):
    strategy = build_strategy(FakeOrderManager())
    intent = OrderIntent(side="short", qty=1.0, price=99.0, purpose="TP_SHORT", order_type="Market")

    def fail_market(*args, **kwargs):
        return False

    strategy.executor._place_market_order_on_exchange = fail_market
    result = strategy.executor.execute_intent(intent)
    assert result is False, "Non-emergency market intent should still respect market_execution_blocked"
