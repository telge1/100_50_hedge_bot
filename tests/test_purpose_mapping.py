"""Basic sanity checks for the new purpose helpers."""

from fixed_cycle_hedge_bot import purpose_mapping


def test_cycle_helpers():
    assert purpose_mapping.cycle_long_add(1) == "CYCLE_1_LONG_ADD"
    assert purpose_mapping.cycle_long_add(4) == "CYCLE_4_LONG_ADD"
    assert purpose_mapping.cycle_short_reduce(1) == "CYCLE_1_SHORT_REDUCE"


def test_cycle_detection():
    assert purpose_mapping.is_cycle_long_add("CYCLE_2_LONG_ADD") is True
    assert purpose_mapping.is_cycle_short_reduce("CYCLE_2_SHORT_REDUCE") is True
    assert purpose_mapping.is_cycle_long_add("something_else") is False
    assert purpose_mapping.is_cycle_short_reduce("something_else") is False


def test_refill_and_exit_helpers():
    assert purpose_mapping.is_refill_long("REFILL_LONG") is True
    assert purpose_mapping.is_refill_short("REFILL_SHORT") is True
    assert purpose_mapping.is_refill_long("REFILL_SHORT") is False
    assert purpose_mapping.is_refill_short("REFILL_LONG") is False

    assert purpose_mapping.is_long_exit("LONG_TP_EXIT") is True
    assert purpose_mapping.is_short_exit("SHORT_SL_EXIT") is True
    assert purpose_mapping.is_long_exit("LONG_SL_EXIT") is False
    assert purpose_mapping.is_short_exit("SHORT_TP_EXIT") is False
