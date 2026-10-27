from ablation_v1 import ablation_flags, first_variant_drop


def test_ablation_chain() -> None:
    row = {
        "ema9_lt_20_lt_59_lt_200": True,
        "ema59_slope_12b_pct": -0.1,
        "ema200_slope_12b_pct": -0.1,
        "net_move_24b_pct": -1.0,
        "chop_ratio_24b": 3.0,
        "gap_to_next_upper_pct": 1.0,
        "upper_bands_between_entry_and_next": 0,
        "local_stack_density_0p35pct": 1,
        "origin_ids_added": False,
    }
    f = ablation_flags(row)
    assert f["variant_E_plus_no_stack_growth"] is True
    assert first_variant_drop(f) == "PASS_E"
