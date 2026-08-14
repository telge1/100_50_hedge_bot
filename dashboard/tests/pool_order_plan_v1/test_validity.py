from __future__ import annotations

from pool_order_plan_v1.schema import STATUS_NO_PLAN, STATUS_READY
from pool_order_plan_v1.validity import classify_plan


def test_two_targets_ready():
    plan = {
        "INITIAL_TARGET_MODE": "TWO_VISIBLE_TARGETS",
        "TP1_UPDATED_FROM_DYNAMIC_POOL": False,
        "SL": {"available": True, "SL_PRICE": 1.0, "SL_TOO_WIDE": True},
        "TP1": {"available": True, "TP1_PRICE": 2.0, "TP1_SIZE": 0.5},
        "TP2": {"available": True, "TP2_PRICE": 3.0, "TP2_SIZE": 0.5},
    }
    judged = classify_plan(plan, replay=False)
    assert judged["ready"] is True
    assert judged["status"] == STATUS_READY
    assert judged["sl_too_wide"] is True


def test_empty_gap_one_target():
    plan = {
        "INITIAL_TARGET_MODE": "ONE_VISIBLE_TARGET",
        "tp2_skip_reason": "EMPTY_GAP_NO_TP2",
        "SL": {"available": True, "SL_PRICE": 1.0, "SL_TOO_WIDE": False},
        "TP1": {"available": True, "TP1_PRICE": 2.0, "TP1_SIZE": 1.0},
        "TP2": {"available": False, "TP2_PRICE": None, "TP2_SIZE": None},
    }
    judged = classify_plan(plan)
    assert judged["ready"] is True


def test_dynamic_and_missing_are_no_plan():
    wait = {
        "INITIAL_TARGET_MODE": "ONE_VISIBLE_TARGET_WAIT_FOR_DYNAMIC_TP1",
        "SL": {"available": True, "SL_PRICE": 1.0},
        "TP1": {"available": False, "TP1_PRICE": None, "TP1_SIZE": None},
        "TP2": {"available": False},
    }
    assert classify_plan(wait)["status"] == STATUS_NO_PLAN
    no_sl = {
        "INITIAL_TARGET_MODE": "TWO_VISIBLE_TARGETS",
        "SL": {"available": False, "SL_PRICE": None},
        "TP1": {"available": True, "TP1_PRICE": 2.0, "TP1_SIZE": 0.5},
        "TP2": {"available": True, "TP2_PRICE": 3.0, "TP2_SIZE": 0.5},
    }
    assert classify_plan(no_sl)["reason"] == "NO_SL_AT_ENTRY"
    no_tp = {
        "INITIAL_TARGET_MODE": "TWO_VISIBLE_TARGETS",
        "SL": {"available": True, "SL_PRICE": 1.0},
        "TP1": {"available": False, "TP1_PRICE": None},
        "TP2": {"available": False},
    }
    assert classify_plan(no_tp)["reason"] == "NO_TP1_AT_ENTRY"
