#!/usr/bin/env python3
"""Phase 4X-AT isolated tests (read-only CH except mock paths)."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
AO = ROOT / "tools/phase4xao_jun_sep_nohup_rollout_v1"
POOLS = ROOT / "backtester/short/dashboard"
RESULTS = ROOT / "results/phase4xat_september_mixed_batch_recovery_v1"

PIPELINE = "historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1"
APR_SEP = POOLS / "research_charts/live_micro_lld_v2_ch_test/doge_apr_sep_2026_v1_history"
COMBO = "5m_PD1"
PILOT_LOOKUP = "provenance_lookup_v1:185ddfc1ca31f9bc"
NEW_LOOKUP = "provenance_lookup_v1:5e4b7ee7d37d65ff"
PILOT_SNAPSHOT = "5eda0ea7e408dfeecdbb35c6084de32328c6c046bc2762a20de79f73cec6fb9c"
PILOT_STEM = "20260909T000000Z"
BATCH46_PARTIAL = (
    "20260908T234000Z",
    "20260908T234500Z",
    "20260908T235000Z",
    "20260908T235500Z",
)


def _setup_paths() -> None:
    for p in (ROOT, POOLS, AO, Path("/home/telgenbuescher/projects/trading_research_platform")):
        if p.is_dir() and str(p) not in sys.path:
            sys.path.insert(0, str(p))
    from research_charts.trp_import import ensure_trp_on_path

    ensure_trp_on_path()


def _hcfg():
    _setup_paths()
    from import_path_5m_pd1 import setup_hcfg

    return setup_hcfg()


class TestMixedBatchHelpers(unittest.TestCase):
    def test_stems_to_import_only_missing(self) -> None:
        _setup_paths()
        from mixed_batch_import_v1 import stems_to_import

        batch = ["a", "b", "c"]
        pub = {"b"}
        self.assertEqual(stems_to_import(batch, pub), ["a", "c"])

    def test_provenance_epochs_per_snapshot(self) -> None:
        _setup_paths()
        from mixed_batch_import_v1 import provenance_lookup_epochs_compatible

        self.assertEqual(provenance_lookup_epochs_compatible()["model"], "per_snapshot_sidecar")


class TestPublishMonthImportSlice(unittest.TestCase):
    """Ensure publish_month passes to_import to BatchSpec (static inspection)."""

    def test_runner_uses_to_import_in_spec(self) -> None:
        src = (AO / "run_jun_sep_nohup.py").read_text(encoding="utf-8")
        self.assertIn("stems=tuple(to_import)", src)
        self.assertIn("assert_published_stems_integrity", src)
        self.assertNotIn("run_import_batch(month, stems,", src)


class TestProvenanceGuard(unittest.TestCase):
    def test_mismatch_still_raises(self) -> None:
        _setup_paths()
        from research_charts.live_micro_lld_v2_ch_test.historical_json_import.provenance_identity import (
            assert_canonical_original_match,
        )
        from research_charts.live_micro_lld_v2_ch_test.historical_json_import.provenance_sidecar import (
            ProvenanceSidecarConflictError,
            ProvenanceSidecarRow,
        )

        base = {
            "snapshot_id": PILOT_SNAPSHOT,
            "pipeline_line_id": PIPELINE,
            "import_batch_id": "x",
            "relative_json_path": "p",
            "dest_sha256": "d",
            "frozen_input_id": "f",
            "frozen_price_hash": "a",
            "frozen_revision_hash": "b",
            "catalog_manifest_id": "m",
            "validated_at": "2026-01-01T00:00:00Z",
        }
        stored = ProvenanceSidecarRow(**base, provenance_lookup_version=PILOT_LOOKUP)
        new_pub = ProvenanceSidecarRow(**base, provenance_lookup_version=NEW_LOOKUP)
        with self.assertRaises(ProvenanceSidecarConflictError):
            assert_canonical_original_match(stored, new_pub)


class TestBatch46Preconditions(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.hcfg = _hcfg()
        _setup_paths()
        from run_jun_sep_nohup import build_month_plan, published_stems_month

        cls.plan = build_month_plan("2026-09")
        cls.pub = published_stems_month(cls.hcfg, "2026-09")
        cls.batch46 = next(b for b in cls.plan["batches"] if b["batch_index"] == 46)
        cls.stems = [c["stem"] for c in cls.batch46["closes"]]

    def test_batch46_reconstructable(self) -> None:
        self.assertEqual(len(self.stems), 50)
        self.assertIn(PILOT_STEM, self.stems)

    def test_four_sep8_in_batch46_published(self) -> None:
        for s in BATCH46_PARTIAL:
            self.assertIn(s, self.pub, msg=s)

    def test_pilot_stem_published(self) -> None:
        self.assertIn(PILOT_STEM, self.pub)

    def test_batch46_to_import_empty_after_partial(self) -> None:
        from mixed_batch_import_v1 import stems_to_import

        ti = stems_to_import(self.stems, self.pub)
        self.assertEqual(ti, [], msg=f"unexpected to_import: {ti[:5]}")

    def test_some_later_batch_mixed_skip_and_import(self) -> None:
        from mixed_batch_import_v1 import stems_to_import

        mixed_bi = None
        for b in self.plan["batches"]:
            stems = [c["stem"] for c in b["closes"]]
            ti = stems_to_import(stems, self.pub)
            if 0 < len(ti) < 50:
                mixed_bi = b["batch_index"]
                break
        self.assertIsNotNone(mixed_bi, "expected a batch with both published and missing stems")


class TestSkipIntegrityLive(unittest.TestCase):
    """Read-only CH: pilot + batch46 boundary."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.hcfg = _hcfg()
        _setup_paths()
        from run_jun_sep_nohup import build_month_plan, published_stems_month

        cls.pub = published_stems_month(cls.hcfg, "2026-09")
        plan = build_month_plan("2026-09")
        cls.stems46 = [c["stem"] for c in plan["batches"][46]["closes"]]

    def test_batch46_skip_integrity(self) -> None:
        from mixed_batch_import_v1 import assert_published_stems_integrity

        assert_published_stems_integrity(
            catalog_root=APR_SEP,
            month="2026-09",
            combo_folder=COMBO,
            pipeline_line_id=PIPELINE,
            config_hash=self.hcfg.config_hash,
            published_stems=self.pub,
            batch_stems=self.stems46,
        )

    def test_pilot_sidecar_lookup_unchanged(self) -> None:
        import clickhouse_connect
        from research_charts.clickhouse_config import load_clickhouse_config
        from research_charts.live_micro_lld_v2_ch_test.historical_json_import.provenance_sidecar_store import (
            ClickHouseProvenanceSidecarStore,
        )

        store = ClickHouseProvenanceSidecarStore()
        row = store.read(PILOT_SNAPSHOT, PIPELINE)
        self.assertIsNotNone(row)
        self.assertEqual(row.provenance_lookup_version, PILOT_LOOKUP)

    def test_mixed_lookup_counts(self) -> None:
        import clickhouse_connect
        from research_charts.clickhouse_config import load_clickhouse_config

        cfg = load_clickhouse_config()
        client = clickhouse_connect.get_client(**cfg.connect_kwargs())
        db = cfg.database
        try:
            rows = client.query(
                f"""
                SELECT provenance_lookup_version, count()
                FROM {db}.research_lmv2_snapshot_import_provenance_v1
                WHERE pipeline_line_id = {{lid:String}}
                  AND relative_json_path LIKE '%/2026-09/5m_PD1/%'
                GROUP BY provenance_lookup_version
                """,
                parameters={"lid": PIPELINE},
            ).result_rows
        finally:
            client.close()
        by_ver = {r[0]: int(r[1]) for r in rows}
        self.assertEqual(by_ver.get(PILOT_LOOKUP, 0), 2592)
        self.assertGreaterEqual(by_ver.get(NEW_LOOKUP, 0), 2300)


class TestJunAugRegression(unittest.TestCase):
    def test_june_plan_batches_unchanged(self) -> None:
        _setup_paths()
        from run_jun_sep_nohup import build_month_plan

        j = build_month_plan("2026-06")
        self.assertEqual(j["close_count"], 8640)
        self.assertEqual(len(j["batches"]), 8640 // 50 + (1 if 8640 % 50 else 0))


def run_all() -> dict:
    RESULTS.mkdir(parents=True, exist_ok=True)
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (
        TestMixedBatchHelpers,
        TestPublishMonthImportSlice,
        TestProvenanceGuard,
        TestBatch46Preconditions,
        TestSkipIntegrityLive,
        TestJunAugRegression,
    ):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    summary = {
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "ok": result.wasSuccessful(),
    }
    if not summary["ok"]:
        summary["verdict"] = "BLOCKED_TESTS"
    else:
        summary["verdict"] = "READY_FOR_CONTROLLED_SEPTEMBER_RESUME_APPROVAL"
    (RESULTS / "test_results.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


if __name__ == "__main__":
    out = run_all()
    print(json.dumps(out, indent=2))
    sys.exit(0 if out["ok"] else 1)
