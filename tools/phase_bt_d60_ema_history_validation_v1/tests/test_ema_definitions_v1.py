from ema_definitions_v1 import ema_bt_d59_first_close, ema_liquidity_context, ema_trp_sma_seed


def test_trp_matches_liquidity_context() -> None:
    closes = [float(i) for i in range(1, 250)]
    for period in (9, 20, 59, 200):
        assert ema_trp_sma_seed(closes, period) == ema_liquidity_context(closes, period)


def test_bt_d59_differs_from_trp_for_ema200() -> None:
    closes = [0.08 + 0.0001 * (i % 17) for i in range(300)]
    trp = ema_trp_sma_seed(closes, 200)[-1]
    d59 = ema_bt_d59_first_close(closes, 200)[-1]
    assert trp is not None and d59 is not None
    assert abs(trp - d59) > 1e-9
