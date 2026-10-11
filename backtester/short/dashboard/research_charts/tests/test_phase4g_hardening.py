"""Phase 4G: UUID guards, orchestrator validate-only, import plan."""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from unittest import mock

import pytest

_dash = Path(__file__).resolve().parents[1]
_repo = _dash.parents[2]
for p in (_repo, _dash):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

from research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_orchestrator import (
    BatchOrchestrator,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_runner import (
    first_regulatory_batch_spec,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_write import (
    execute_batch_import,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.full_import_plan import (
    build_full_import_plan,
    estimate_batches_for_sizes,
    iter_all_batch_specs,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.import_close import (
    import_historical_close,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.pilot_config import (
    CATALOG_ROOT,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.write_guards import (
    InvalidTestRunIdError,
    require_valid_test_run_uuid,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.staged_writer import (
    write_active_snapshot_staged,
)


def test_require_valid_test_run_uuid_accepts_uuid():
    u = str(uuid.uuid4())
    assert require_valid_test_run_uuid(u) == u


def test_require_valid_test_run_uuid_rejects_readable_batch_id():
    with pytest.raises(InvalidTestRunIdError):
        require_valid_test_run_uuid("phase4f_first_50_apr2026_4hpd10_v1")


def test_staged_writer_no_ch_on_invalid_uuid():
    state = mock.Mock(active_pool_count=0, active_pool_ids=[], active_sides=[])
    with (
        mock.patch("clickhouse_connect.get_client") as gc,
        pytest.raises(InvalidTestRunIdError),
    ):
        write_active_snapshot_staged(state, test_run_id="not-a-uuid")
    gc.assert_not_called()


def test_execute_batch_import_invalid_uuid_zero_writes(monkeypatch):
    monkeypatch.setenv("HISTORICAL_JSON_IMPORT_ALLOW_WRITE", "1")
    monkeypatch.setenv("HISTORICAL_JSON_IMPORT_ALLOW_SIDECAR_WRITE", "1")
    spec = first_regulatory_batch_spec(CATALOG_ROOT)
    with (
        mock.patch(
            "research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_write._execute_batch_import_locked"
        ) as locked,
        pytest.raises(Exception),
    ):
        execute_batch_import(
            CATALOG_ROOT,
            spec,
            test_run_id="phase4f_first_50_apr2026_4hpd10_v1",
        )
    locked.assert_not_called()


def test_import_close_rejects_invalid_test_run_id():
    state = mock.Mock(active_pool_count=0, active_pool_ids=[])
    line = mock.Mock()
    with pytest.raises(InvalidTestRunIdError):
        import_historical_close(
            state,
            [],
            pipeline_line_id="line",
            test_run_id="bad",
            line=line,
        )


def test_orchestrator_validate_only_no_write():
    spec = first_regulatory_batch_spec(CATALOG_ROOT)
    orch = BatchOrchestrator(CATALOG_ROOT)
    res = orch.run_batch(spec, mode="validate_only")
    assert res.status == "validated"
    assert not res.errors


def test_full_import_plan_batch_counts():
    plan = build_full_import_plan(CATALOG_ROOT, batch_size=50)
    assert plan["catalog_snapshot_count"] == 53802
    assert plan["stems_in_plan"] == 53802
    assert plan["batch_count"] > 0
    sizes = estimate_batches_for_sizes(CATALOG_ROOT)
    assert sizes["50"]["batch_count"] > sizes["200"]["batch_count"]


def test_iter_all_batch_specs_deterministic():
    a = list(iter_all_batch_specs(CATALOG_ROOT, batch_size=50))
    b = list(iter_all_batch_specs(CATALOG_ROOT, batch_size=50))
    assert [s.batch_id for s in a] == [s.batch_id for s in b]
