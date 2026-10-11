"""Phase 4D: CH sidecar store (fake client), lock, readback, recovery."""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

_dash = Path(__file__).resolve().parents[1]
_repo = _dash.parents[2]
for p in (_repo, _dash):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

from research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_runner import (
    first_regulatory_batch_spec,
    run_batch_readback_gate,
    validate_batch,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.historical_import_lock import (
    HistoricalImportLockError,
    historical_import_write_lock,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.import_recovery import (
    ImportPartialState,
    classify_partial_import,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.pilot_config import (
    CATALOG_ROOT,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.provenance_identity import (
    evaluate_physical_rows,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.provenance_sidecar import (
    ProvenanceSidecarRow,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.provenance_sidecar_design import (
    TABLE_IMPORT_PROVENANCE,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.provenance_sidecar_store import (
    ClickHouseProvenanceSidecarStore,
    MemoryProvenanceSidecarStore,
    ProvenanceSidecarConflictError,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.sidecar_readback import (
    verify_sidecar_readback_for_batch,
)


def _row(
    sid: str = "s1",
    line: str = "prod_line",
    batch: str = "b1",
    sha: str = "abc",
    path: str = "production/results/x.json",
) -> ProvenanceSidecarRow:
    return ProvenanceSidecarRow(
        snapshot_id=sid,
        pipeline_line_id=line,
        import_batch_id=batch,
        relative_json_path=path,
        dest_sha256=sha,
        frozen_input_id="frozen",
        frozen_price_hash="",
        frozen_revision_hash="",
        provenance_lookup_version="v1",
        catalog_manifest_id="doge",
        validated_at="2026-01-01T00:00:00+00:00",
    )


class FakeCHSidecarClient:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.insert_calls = 0

    def query(self, q: str, parameters: dict[str, str] | None = None) -> Any:
        parameters = parameters or {}
        hits: list[tuple[Any, ...]] = []
        for r in self.rows:
            if "relative_json_path" in q and "snapshot_id = {sid" not in q:
                if (
                    r["relative_json_path"] == parameters.get("path")
                    and r["pipeline_line_id"] == parameters.get("line")
                ):
                    hits.append(self._tuple(r))
            elif (
                r["snapshot_id"] == parameters.get("sid")
                and r["pipeline_line_id"] == parameters.get("line")
            ):
                hits.append(self._tuple(r))
        return type("R", (), {"result_rows": hits})()

    @staticmethod
    def _tuple(r: dict[str, Any]) -> tuple[Any, ...]:
        return (
            r["snapshot_id"],
            r["pipeline_line_id"],
            r["import_batch_id"],
            r["relative_json_path"],
            r["dest_sha256"],
            r["frozen_input_id"],
            r["frozen_price_hash"],
            r["frozen_revision_hash"],
            r["provenance_lookup_version"],
            r["catalog_manifest_id"],
            r["validated_at"],
        )

    def insert(self, table: str, data: list[tuple[Any, ...]], column_names: list[str]) -> None:
        assert TABLE_IMPORT_PROVENANCE in table
        self.insert_calls += 1
        for tup in data:
            self.rows.append(dict(zip(column_names, tup)))

    def close(self) -> None:
        pass


def test_ch_store_write_read_verify(monkeypatch):
    monkeypatch.setenv("HISTORICAL_JSON_IMPORT_ALLOW_SIDECAR_WRITE", "1")
    client = FakeCHSidecarClient()
    store = ClickHouseProvenanceSidecarStore(client=client, database="signal_generator")
    assert store.write_and_verify(_row()) == "written"
    assert store.write_and_verify(_row(batch="b2")) == "skipped_identical"
    assert client.insert_calls == 1


def test_ch_store_blocks_conflicting_sha(monkeypatch):
    monkeypatch.setenv("HISTORICAL_JSON_IMPORT_ALLOW_SIDECAR_WRITE", "1")
    client = FakeCHSidecarClient()
    store = ClickHouseProvenanceSidecarStore(client=client, database="db")
    store.write_and_verify(_row(sha="aaa"))
    with pytest.raises(ProvenanceSidecarConflictError):
        store.write_and_verify(_row(sha="bbb"))


def test_ch_store_blocks_write_without_env(monkeypatch):
    monkeypatch.delenv("HISTORICAL_JSON_IMPORT_ALLOW_SIDECAR_WRITE", raising=False)
    store = ClickHouseProvenanceSidecarStore(
        client=FakeCHSidecarClient(), database="db"
    )
    with pytest.raises(ProvenanceSidecarConflictError, match="blocked"):
        store.write_and_verify(_row())


def test_physical_duplicate_rows_conflict():
    a = _row()
    b = _row(sha="other")
    with pytest.raises(ProvenanceSidecarConflictError):
        evaluate_physical_rows([a, b], snapshot_id="s1", pipeline_line_id="prod_line")


def test_idempotent_resume_different_batch_id():
    store = MemoryProvenanceSidecarStore()
    store.write_and_verify(_row(batch="batch-a"))
    assert store.write_and_verify(_row(batch="batch-b")) == "skipped_identical"


def test_pilot_and_production_same_snapshot_id_different_lines():
    store = MemoryProvenanceSidecarStore()
    store.write_and_verify(_row(line="pilot_line", path="p/a.json"))
    store.write_and_verify(_row(line="prod_line", path="p/b.json"))
    assert store.read("s1", "pilot_line") is not None
    assert store.read("s1", "prod_line") is not None


def test_recovery_classifications():
    assert classify_partial_import(
        ImportPartialState(True, True, False)
    )[0] == "SAFE_RESUME"
    assert classify_partial_import(
        ImportPartialState(True, False, False)
    )[0] == "STOP_MANUAL"
    assert classify_partial_import(
        ImportPartialState(False, True, True)
    )[0] == "STOP_MANUAL"
    assert classify_partial_import(
        ImportPartialState(True, False, True)
    )[0] == "STOP_MANUAL"


def test_sidecar_readback_with_memory_store():
    spec = first_regulatory_batch_spec(CATALOG_ROOT)
    store = MemoryProvenanceSidecarStore()
    stems = list(spec.stems[:3])
    for stem in stems:
        from research_charts.live_micro_lld_v2_ch_test.historical_json_import.sidecar_readback import (
            expected_sidecar_for_stem,
        )

        store.write_and_verify(expected_sidecar_for_stem(CATALOG_ROOT, spec, stem))
    reports = verify_sidecar_readback_for_batch(
        CATALOG_ROOT, spec, store, stems=stems, fail_closed=True
    )
    assert all(r.ok for r in reports)


def test_batch_readback_gate_sidecar_branch():
    spec = first_regulatory_batch_spec(CATALOG_ROOT)
    store = MemoryProvenanceSidecarStore()
    for stem in spec.stems[:2]:
        from research_charts.live_micro_lld_v2_ch_test.historical_json_import.sidecar_readback import (
            expected_sidecar_for_stem,
        )

        store.write_and_verify(expected_sidecar_for_stem(CATALOG_ROOT, spec, stem))
    stems = list(spec.stems[:2])
    with mock.patch(
        "research_charts.live_micro_lld_v2_ch_test.historical_json_import.readback_parity.run_pilot_readback",
        return_value=[],
    ):
        out = run_batch_readback_gate(
            stems,
            catalog_root=CATALOG_ROOT,
            combo_folder=spec.combo_folder,
            month=spec.month,
            pipeline_line_id=spec.pipeline_line_id,
            batch_spec=spec,
            require_sidecar=True,
            sidecar_store=store,
        )
    assert len(out["sidecar"]) == 2


def test_flock_exclusive(tmp_path):
    lock = tmp_path / "test.flock"
    with historical_import_write_lock(lock):
        with pytest.raises(HistoricalImportLockError):
            with historical_import_write_lock(lock, blocking=False):
                pass


def test_flock_released_after_exit(tmp_path):
    lock = tmp_path / "test2.flock"
    with historical_import_write_lock(lock):
        pass
    with historical_import_write_lock(lock, blocking=False):
        pass


def test_first_50_validate_unchanged():
    spec = first_regulatory_batch_spec(CATALOG_ROOT)
    vr = validate_batch(CATALOG_ROOT, spec)
    assert not vr["errors"]
    assert vr["summary"]["member_rows"] == 3903
    assert vr["provenance_rows_planned"] == 50
