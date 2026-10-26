from __future__ import annotations

import json
from pathlib import Path

import pytest

from result_publish_v1 import ResultPublishError, build_result_document, publish_result, validate_result


def _minimal_doc():
    return {
        "result_format": "bt_d12_backtest_result_v1",
        "run_id": "test_run",
        "config_hash": "abc",
        "symbol": "DOGEUSDT",
        "signals": [],
        "orders": [],
        "trades": [],
        "pnl": {},
        "integration": {},
    }


def test_validate_requires_format():
    assert validate_result({})


def test_publish_atomic(tmp_path):
    doc = _minimal_doc()
    publish_result("run_a", doc, runs_root=tmp_path)
    latest = json.loads((tmp_path / "latest_result.json").read_text(encoding="utf-8"))
    assert latest["run_id"] == "run_a"
    assert (tmp_path / "run_a" / "result.json").is_file()


def test_failed_publish_leaves_latest(tmp_path):
    doc = _minimal_doc()
    publish_result("ok1", doc, runs_root=tmp_path)
    bad = dict(doc)
    bad["result_format"] = "wrong"
    with pytest.raises(ResultPublishError):
        publish_result("ok2", bad, runs_root=tmp_path)
    latest = json.loads((tmp_path / "latest_result.json").read_text(encoding="utf-8"))
    assert latest["run_id"] == "ok1"
