"""Phase 4A batch importer prep: resume, conflicts, dry-run (no CH writes)."""

from __future__ import annotations

import sys
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

from research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_progress import (
    BatchProgressReport,
    write_progress_report,
    load_progress_report,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_runner import (
    BatchSpec,
    classify_close_resume,
    first_regulatory_batch_spec,
    multi_combination_dry_run,
    pilot_overlap_stems,
    validate_batch,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_write import execute_batch_import
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.catalog_inventory import (
    inventory_from_coverage,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.historical_batch_config import (
    STRATEGY_COMBOS,
    StrategyCombo,
    combo_for_folder,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.pilot_config import CATALOG_ROOT
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.provenance_sidecar_design import (
    provenance_row_from_plan,
    sidecar_ddl,
)


def test_classify_resume_publish_skip():
    assert (
        classify_close_resume(
            snapshot_id="s1",
            pipeline_line_id="line",
            published_snapshot_ids={"s1"},
            meta_snapshot_ids=set(),
            pilot_snapshot_ids=set(),
        )
        == "publish_skip"
    )


def test_classify_pilot_overlap_publish_only():
    assert (
        classify_close_resume(
            snapshot_id="s1",
            pipeline_line_id="prod",
            published_snapshot_ids=set(),
            meta_snapshot_ids={"s1"},
            pilot_snapshot_ids={"s1"},
        )
        == "pilot_overlap_publish_only"
    )


def test_classify_ready():
    assert (
        classify_close_resume(
            snapshot_id="s1",
            pipeline_line_id="line",
            published_snapshot_ids=set(),
            meta_snapshot_ids=set(),
            pilot_snapshot_ids=set(),
        )
        == "ready"
    )


def test_batch_validate_fails_on_duplicate_snapshot_in_batch():
    spec = BatchSpec(
        month="2026-04",
        combo_folder="15m_PD10",
        stems=("20260401T000000Z", "20260401T001500Z"),
        pipeline_line_id=STRATEGY_COMBOS[2].pipeline_line_id(),
        import_batch_id="t",
    )
    fake = Path("/fake/snap.json")

    def _bundle(*_a, **_k):
        return {
            "path": fake,
            "snapshot_id": "same",
            "state": mock.Mock(active_pool_count=1),
            "catalog_rows": [],
            "frozen_input_id": "f",
        }

    with (
        mock.patch(
            "research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_runner.stem_path",
            return_value=fake,
        ),
        mock.patch.object(Path, "is_file", return_value=True),
        mock.patch(
            "research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_runner.build_import_bundle",
            side_effect=_bundle,
        ),
        mock.patch(
            "research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_runner.validate_bundle"
        ),
        mock.patch(
            "research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_runner.build_write_plan"
        ) as bwp,
    ):
        bwp.return_value = mock.Mock(member_rows=[], catalog_rows=[])
        out = validate_batch(CATALOG_ROOT, spec)
    assert any("duplicate snapshot_id" in e for e in out["errors"])


def test_execute_batch_import_requires_write_gate(monkeypatch):
    monkeypatch.delenv("HISTORICAL_JSON_IMPORT_ALLOW_WRITE", raising=False)
    monkeypatch.delenv("HISTORICAL_JSON_IMPORT_ALLOW_SIDECAR_WRITE", raising=False)
    spec = first_regulatory_batch_spec(CATALOG_ROOT)
    from research_charts.live_micro_lld_v2_ch_test.historical_json_import.productive_prewrite_gate import (
        ProductivePrewriteGateError,
    )

    with pytest.raises(ProductivePrewriteGateError, match="productive import gates"):
        execute_batch_import(CATALOG_ROOT, spec)


def test_progress_report_roundtrip(tmp_path):
    rep = BatchProgressReport(
        batch_id="b1",
        pipeline_line_id="line",
        combo_folder="15m_PD10",
        month="2026-04",
        import_batch_id="run",
        status="validated",
        closes_planned=1,
    )
    p = tmp_path / "progress.json"
    write_progress_report(p, rep)
    loaded = load_progress_report(p)
    assert loaded.batch_id == "b1"
    assert loaded.status == "validated"


def test_isolated_combo_for_folder_5m_pd5():
    assert combo_for_folder("5m_PD5") == StrategyCombo("5m", 5)
    assert combo_for_folder("5m_PD1") == StrategyCombo("5m", 1)


def test_isolated_combo_for_folder_5m_pd10():
    assert combo_for_folder("5m_PD10") == StrategyCombo("5m", 10)


def test_catalog_inventory_matches_manifest():
    inv = inventory_from_coverage(CATALOG_ROOT)
    # Bulk Apr–Sep manifest + coverage_matrix (15m/4h only; unchanged by 5m file import).
    bulk_snapshot_count = 53802
    sep_5m_pd1_on_disk = 2592
    sep_5m_pd5_on_disk = 2592
    sep_5m_pd10_on_disk = 2592
    assert inv["manifest_snapshot_count"] == bulk_snapshot_count
    assert inv["coverage_total_found"] == bulk_snapshot_count
    isolated_5m_sep = sep_5m_pd1_on_disk + sep_5m_pd5_on_disk + sep_5m_pd10_on_disk
    assert inv["disk_total_json"] == bulk_snapshot_count + isolated_5m_sep
    assert inv["by_combo_disk"].get("5m_PD1") == sep_5m_pd1_on_disk
    assert inv["by_combo_disk"].get("5m_PD5") == sep_5m_pd5_on_disk
    assert inv["by_combo_disk"].get("5m_PD10") == sep_5m_pd10_on_disk
    assert inv["all_slices_pass"] is True
    assert sum(inv["by_combo_coverage"].values()) == bulk_snapshot_count


def test_multi_combination_dry_run_ok():
    body = multi_combination_dry_run(CATALOG_ROOT, samples_per_combo=2)
    assert body["ok"] is True
    assert len(body["combo_batches"]) == 4
    assert body["pilot_revalidate"]["errors"] == []


def test_provenance_sidecar_ddl_and_row_shape():
    ddl = sidecar_ddl("signal_generator")
    assert "research_lmv2_snapshot_import_provenance_v1" in ddl
    row = provenance_row_from_plan(
        {"relative_dest": "a/b.json", "frozen_input_id": "fid"},
        snapshot_id="sid",
        pipeline_line_id="line",
        import_batch_id="batch",
        catalog_manifest_id="doge_apr_sep_2026_v1",
        provenance_lookup_version="v1",
        dest_sha256="abc",
    )
    assert row["dest_sha256"] == "abc"
    assert row["frozen_input_id"] == "fid"


def test_pilot_overlap_stems_count():
    assert len(pilot_overlap_stems()) == 10
