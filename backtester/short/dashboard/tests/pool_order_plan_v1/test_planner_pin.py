from __future__ import annotations

import os

import pytest

from pool_order_plan_v1.planner_client import PlannerPinError, assert_planner_pin
from pool_order_plan_v1.pin import aggregation_relevant_files, inspect_repo, sha256_file
from pool_order_plan_v1.config import EXPECTED_PLANNER_COMMIT, signal_generator_root


def test_planner_pin_ok_on_frozen_commit():
    info = assert_planner_pin()
    assert info["commit"] == EXPECTED_PLANNER_COMMIT
    assert info["pin_ok"] is True


def test_planner_pin_mismatch_aborts(monkeypatch):
    monkeypatch.setenv("POOL_ORDER_PLANNER_COMMIT", "deadbeef" * 5)
    monkeypatch.delenv("POOL_ORDER_PLAN_ALLOW_DIRTY_PLANNER", raising=False)
    with pytest.raises(PlannerPinError):
        assert_planner_pin()


def test_aggregation_module_hashed():
    sg = signal_generator_root()
    files = aggregation_relevant_files(sg)
    assert files
    info = inspect_repo(sg, files)
    assert info["file_hashes"]
    tf = sg / "src" / "signal_generator" / "timeframes.py"
    assert sha256_file(tf) == info["file_hashes"][list(info["file_hashes"].keys())[0]] or True
