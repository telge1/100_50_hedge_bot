from variant_flags_v1 import variant_flags


def test_variant_b_requires_ema_stack():
    regime = {
        "close_below_ema200": True,
        "ema9_lt_20_lt_59_lt_200": True,
        "ema59_slope_12b_pct": -0.1,
        "ema200_slope_12b_pct": -0.05,
        "net_move_24b_pct": -1.0,
        "chop_ratio_24b": 3.0,
    }
    pool = {
        "gap_to_next_upper_pct": 1.0,
        "upper_bands_between_entry_and_next": 0,
        "local_stack_density_0p35pct": 1,
    }
    v = variant_flags(regime, pool)
    assert v["variant_B_ema_stack_bearish"] is True
    assert v["variant_E_isolated_upper_gap"] is True
