"""Phase 4R: April PD5/PD10 manifests, batched CH scope queries, dry-run."""

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

from research_charts.live_micro_lld_v2_ch_test.historical_json_import.ch_scope_query import (
    iter_chunks,
    scope_layer_counts_batched,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.controlled_monthly_orchestrator import (
    ControlledMonthlyImportOrchestrator,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.controlled_run_manifest import (
    build_phase4r_april_15mpd10_full_manifest,
    build_phase4r_april_15mpd5_full_manifest,
    load_controlled_run_manifest,
    resolve_manifest_stems,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.pilot_config import (
    CATALOG_ROOT,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.post_import_verification import (
    verify_manifest_scope_ch_counts,
)

MANIFEST_PD5 = (
    _dash
    / "live_micro_lld_v2_ch_test/historical_json_import/manifests/phase4r_april_15mpd5_full_v1.json"
)
MANIFEST_PD10 = (
    _dash
    / "live_micro_lld_v2_ch_test/historical_json_import/manifests/phase4r_april_15mpd10_full_v1.json"
)


def test_iter_chunks_limits_payload_size():
    ids = [f"id{i}" for i in range(250)]
    chunks = list(iter_chunks(ids, 100))
    assert len(chunks) == 3
    assert all(len(c) <= 100 for c in chunks)


def test_phase4r_pd5_manifest_2880_58_batches():
    built = build_phase4r_april_15mpd5_full_manifest(CATALOG_ROOT)
    loaded = load_controlled_run_manifest(MANIFEST_PD5)
    assert loaded.scope_fingerprint == built.scope_fingerprint
    stems = resolve_manifest_stems(CATALOG_ROOT, loaded)
    assert len(stems) == 2880
    assert loaded.max_batches == 58
    assert loaded.batch_import_ids[-1] == "phase4r_apr2026_15mpd5_v1_batch58"
    assert loaded.pipeline_line_id.endswith("15mpd5_v1")


def test_phase4r_pd10_manifest_distinct_from_pilot_line():
    loaded = load_controlled_run_manifest(MANIFEST_PD10)
    assert loaded.pipeline_line_id == "historical_mixed_frozen_doge_apr_sep_2026_v1_15mpd10_v1"
    assert "aug2026_pilot" not in loaded.pipeline_line_id


def test_phase4r_dry_runs(tmp_path):
    orch = ControlledMonthlyImportOrchestrator(CATALOG_ROOT, progress_dir=tmp_path / "dry")
    for path in (MANIFEST_PD5, MANIFEST_PD10):
        man = load_controlled_run_manifest(path)
        res = orch.dry_run(man, manifest_path=path)
        assert res.status == "dry_run_ok"
        assert len(res.stems) == 2880
        assert len(res.batch_specs) == 58
        assert res.expected_writes["meta_rows"] == 2880


def test_scope_ch_counts_uses_chunked_queries():
    fake_client = mock.Mock()
    fake_client.query.return_value = mock.Mock(result_rows=[(0,)])

    scope_layer_counts_batched(
        fake_client,
        "signal_generator",
        [f"s{i}" for i in range(250)],
        pipeline_line_id="line",
        chunk_size=100,
    )
    assert fake_client.query.call_count >= 5 * 3  # 3 chunks × 5 table queries


def test_verify_manifest_scope_ch_counts_integration_smoke():
    man = load_controlled_run_manifest(MANIFEST_PD5)
    counts = verify_manifest_scope_ch_counts(CATALOG_ROOT, man, chunk_size=150)
    assert counts["publish_distinct"] == 0
    assert counts["members"] == 0
