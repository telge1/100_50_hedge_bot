from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

_dash = Path(__file__).resolve().parents[1]
_repo = _dash.parents[2]
for p in (_repo, _dash):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from research_charts.chart_live_micro_lld_v2_ch_test import DEFAULT_RUN_ID
from research_charts.live_micro_lld_v2_ch_test.compare_guard import evaluate_compare_guard
from research_charts.live_micro_lld_v2_ch_test.compare_guard_types import CompareVerdict
from research_charts.live_micro_lld_v2_ch_test.errors import ConfigNotAvailableError
from research_charts.live_micro_lld_v2_ch_test.identity import (
    all_preset_matrix,
    build_live_micro_lld_v2_config,
    config_hash,
)
from research_charts.live_micro_lld_v2_ch_test.run_resolve import RunSelector
from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

FROM = "2026-09-08T11:20:00+00:00"
TO = "2026-10-08T11:20:00+00:00"


def test_preset_matrix_pd5_pd10_differ():
    m = all_preset_matrix()
    assert m[5]["config_hash"] != m[10]["config_hash"]
    assert m[5]["preset_swing_len"] == 12
    assert m[10]["preset_swing_len"] == 2


def test_config_hash_stable():
    h1 = config_hash(build_live_micro_lld_v2_config(10))
    h2 = config_hash(build_live_micro_lld_v2_config(10))
    assert h1 == h2
    assert h1 != config_hash(build_live_micro_lld_v2_config(5))


def test_compare_guard_pd5_vs_pd10_ch_run():
    v = evaluate_compare_guard(
        symbol="TUTUSDT",
        timeframe="5m",
        pool_detail=5,
        from_close=FROM,
        to_close=TO,
        run_id=DEFAULT_RUN_ID,
    )
    assert v["status"] == CompareVerdict.CONFIG_NOT_AVAILABLE


def test_compare_guard_pd10_legacy_may_be_not_verified_or_pass():
    v = evaluate_compare_guard(
        symbol="TUTUSDT",
        timeframe="5m",
        pool_detail=10,
        from_close=FROM,
        to_close=TO,
        run_id=DEFAULT_RUN_ID,
    )
    assert v["status"] in (
        CompareVerdict.PASS,
        CompareVerdict.NOT_VERIFIED,
        CompareVerdict.INPUT_MISMATCH,
    )


def test_resolve_pd10_run():
    meta = RunSelector().resolve_run("TUTUSDT", "5m", 10, run_id=DEFAULT_RUN_ID)
    assert str(meta["run_id"]) == DEFAULT_RUN_ID
    assert int(meta["pool_detail"]) == 10


def test_resolve_pd4_not_available_without_run():
    try:
        RunSelector().resolve_run("TUTUSDT", "5m", 4)
        assert False, "expected ConfigNotAvailableError"
    except ConfigNotAvailableError:
        pass


def test_resolve_pd5_run_after_optional_batch():
    runs = RunSelector().list_runs("TUTUSDT", "5m", 5)
    if not runs:
        return
    meta = RunSelector().resolve_run("TUTUSDT", "5m", 5)
    assert int(meta["pool_detail"]) == 5
