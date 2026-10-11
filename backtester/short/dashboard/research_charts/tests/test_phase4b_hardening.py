"""Phase 4B: provenance sidecar, pilot overlap, batch readback, resume."""

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

from research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_resume import (
    batch_success_criteria_met,
    reconcile_progress_with_ch,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_runner import (
    dry_run_readback_all_combos,
    first_regulatory_batch_spec,
    validate_batch,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.pilot_config import CATALOG_ROOT
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.pilot_overlap import (
    describe_overlap_behavior,
    pilot_snapshot_ids_from_catalog,
    verify_ch_catalog_matches_import_bundle,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.provenance_sidecar import (
    InMemoryProvenanceRegistry,
    ProvenanceSidecarConflictError,
    ProvenanceSidecarRow,
    build_sidecar_row_from_bundle,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.provenance_sidecar_design import (
    sidecar_ddl,
)


def _row(**kwargs) -> ProvenanceSidecarRow:
    base = {
        "snapshot_id": "s1",
        "pipeline_line_id": "line",
        "import_batch_id": "b1",
        "relative_json_path": "production/results/x.json",
        "dest_sha256": "a",
        "frozen_input_id": "f",
        "frozen_price_hash": "",
        "frozen_revision_hash": "",
        "provenance_lookup_version": "v1",
        "catalog_manifest_id": "doge",
        "validated_at": "2026-01-01T00:00:00+00:00",
    }
    base.update(kwargs)
    return ProvenanceSidecarRow(**base)


def test_sidecar_conflict_same_snapshot_different_sha():
    reg = InMemoryProvenanceRegistry()
    reg.register(_row(dest_sha256="aaa"))
    with pytest.raises(ProvenanceSidecarConflictError):
        reg.register(_row(dest_sha256="bbb"))


def test_sidecar_idempotent_same_batch():
    reg = InMemoryProvenanceRegistry()
    reg.register(_row())
    reg.register(_row())
    assert len(reg.rows) == 1


def test_sidecar_path_bound_to_snapshot():
    reg = InMemoryProvenanceRegistry()
    reg.register(_row(snapshot_id="s1", relative_json_path="p.json"))
    with pytest.raises(ProvenanceSidecarConflictError):
        reg.register(_row(snapshot_id="s2", relative_json_path="p.json"))


def test_sidecar_ddl_has_validated_at():
    assert "validated_at" in sidecar_ddl("signal_generator")


def test_first_regulatory_batch_50_closes():
    spec = first_regulatory_batch_spec(CATALOG_ROOT)
    assert spec.combo_folder == "4h_PD10"
    assert spec.month == "2026-04"
    assert len(spec.stems) == 50
    overlap = pilot_snapshot_ids_from_catalog(CATALOG_ROOT)
    vr = validate_batch(CATALOG_ROOT, spec)
    assert not vr["errors"]
    assert not (set(vr["snapshot_ids"]) & overlap)
    assert vr["provenance_rows_planned"] == 50


def test_dry_run_readback_all_four_combos():
    body = dry_run_readback_all_combos(CATALOG_ROOT, samples_per_combo=1)
    assert body["ok"] is True
    assert len(body["reports"]) == 4


def test_batch_success_requires_provenance():
    ok, errs = batch_success_criteria_met(
        all_published_on_line=True,
        readback_ok=True,
        provenance_registry=None,
        snapshot_ids=["s1"],
        progress_written=True,
    )
    assert not ok
    assert any("provenance" in e for e in errs)


def test_progress_reconcile_ch_authoritative(tmp_path):
    out = reconcile_progress_with_ch(tmp_path / "missing.json", ch_published_count=0, expected_in_batch=50)
    assert out["ch_authoritative"] is True


def test_pilot_overlap_catalog_matches_ch_for_one_snapshot():
    """Read-only CH: pilot catalog fingerprints match JSON import bundle."""
    from research_charts.live_micro_lld_v2_ch_test.historical_json_import.historical_batch_config import (
        PILOT_COMBO_FOLDER,
        PILOT_DECISION_CLOSE_STEMS,
        PILOT_MONTH,
        PILOT_PIPELINE_LINE_ID,
    )
    from research_charts.live_micro_lld_v2_ch_test.historical_json_import.parse import (
        build_import_bundle,
        load_manifest_context,
        snapshot_path_for_stem,
    )
    from research_charts.live_micro_lld_v2_ch_test.identity import build_live_micro_lld_v2_config, config_hash

    stem = PILOT_DECISION_CLOSE_STEMS[0]
    path = snapshot_path_for_stem(
        CATALOG_ROOT, month=PILOT_MONTH, combo=PILOT_COMBO_FOLDER, stem=stem
    )
    manifest = load_manifest_context(CATALOG_ROOT)
    ch = config_hash(build_live_micro_lld_v2_config(10))
    bundle = build_import_bundle(
        path,
        catalog_root=CATALOG_ROOT,
        config_hash=ch,
        manifest=manifest,
        pipeline_line_id=PILOT_PIPELINE_LINE_ID,
    )
    verify_ch_catalog_matches_import_bundle(
        bundle["catalog_rows"],
        snapshot_id=bundle["snapshot_id"],
    )


def test_run_batch_readback_gate_uses_combo_rel():
    from research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_runner import (
        run_batch_readback_gate,
    )

    with mock.patch(
        "research_charts.live_micro_lld_v2_ch_test.historical_json_import.readback_parity.run_pilot_readback",
        return_value=[],
    ) as rp:
        run_batch_readback_gate(
            ["20260401T000000Z"],
            catalog_root=CATALOG_ROOT,
            combo_folder="4h_PD10",
            month="2026-04",
            pipeline_line_id="historical_mixed_frozen_doge_apr_sep_2026_v1_4hpd10_v1",
        )
        assert rp.call_args.kwargs["combo_rel"] == "production/results/2026-04/4h_PD10"


def test_overlap_behavior_documented():
    assert "snapshot_id" in describe_overlap_behavior()["global_snapshot_id"]
