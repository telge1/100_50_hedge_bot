from __future__ import annotations

import json
from pathlib import Path

import pytest

from cache_contract_v1 import DensityCacheError, make_cache_key, snapshot_content_sha256
from cache_store_v1 import (
    CacheBundleProvenance,
    cache_key_for_close,
    finalize_staging_bundle,
    load_bundle_manifest,
    read_cache_entry,
    write_bundle_manifest,
    write_cache_entry_atomic,
)

ROOT = Path(__file__).resolve().parents[3]
GOLDEN = ROOT / "results/phase_bt_d3_canonical_depth2_provider_v1/golden_20260925_1100_chart_path.json"


def _golden_snapshot() -> dict:
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    meta = golden["meta"]
    close = meta["decision_close_utc"]
    return {
        "decision_close_utc": close,
        "as_of_utc": close,
        "upper_bands": golden["upper"],
        "lower_bands": golden["lower"],
        "upper_band_count": meta["upper_band_count"],
        "lower_band_count": meta["lower_band_count"],
        "total_band_count": meta["upper_band_count"] + meta["lower_band_count"],
        "provider_manifest": meta,
        "data_provenance": meta.get("data_provenance"),
        "pipeline_line_id": meta["pipeline_line_id"],
        "config_hash": meta["config_hash"],
        "adapter_path": meta.get("adapter_path"),
    }


def _write_min_bundle(tmp_path: Path) -> tuple[Path, CacheBundleProvenance, str]:
    snap = _golden_snapshot()
    close = snap["decision_close_utc"]
    prov = CacheBundleProvenance(
        symbol="DOGEUSDT",
        timeframe="5m",
        pool_detail=1,
        min_unique_origins=2,
        density_method="exact_unique_origin",
        required_pipeline_line_id=snap["pipeline_line_id"],
        required_config_hash=snap["config_hash"],
    )
    key = cache_key_for_close(prov, close)
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "entries").mkdir()
    write_cache_entry_atomic(staging, key, snap)
    manifest = {
        "cache_bundle_version": "bt_d8_density_cache_bundle_v1",
        "cache_contract_version": "bt_d8_canonical_density_cache_v1",
        "adapter_contract_version": "bt_d4_snapshot_payload_v1",
        "symbol": "DOGEUSDT",
        "timeframe": "5m",
        "pool_detail": 1,
        "min_unique_origins": 2,
        "density_method": "exact_unique_origin",
        "required_pipeline_line_id": snap["pipeline_line_id"],
        "required_config_hash": snap["config_hash"],
        "entry_count": 1,
        "entries": [{"cache_key": key, "decision_close_utc": close, "content_sha256": snapshot_content_sha256(snap)}],
        "decision_close_first": close,
        "decision_close_last": close,
    }
    write_bundle_manifest(staging, manifest)
    bundle = tmp_path / "bundle"
    finalize_staging_bundle(staging, bundle)
    return bundle, prov, close


def test_cache_key_includes_contract_fields():
    k = make_cache_key(
        symbol="DOGEUSDT",
        timeframe="5m",
        pool_detail=1,
        min_unique_origins=2,
        decision_close_utc="2026-09-25T11:00:00Z",
        pipeline_line_id="pipe_v1",
        provider_config_hash="abc",
    )
    assert "DOGEUSDT" in k
    assert "20260925T110000Z" in k


def test_read_roundtrip_golden(tmp_path):
    bundle, prov, close = _write_min_bundle(tmp_path)
    key = cache_key_for_close(prov, close)
    snap = read_cache_entry(bundle, key, prov, decision_close_utc=close)
    assert snap["total_band_count"] == 215
    assert len(snap["upper_bands"]) == 39


def test_missing_entry_fails(tmp_path):
    bundle, prov, close = _write_min_bundle(tmp_path)
    key = cache_key_for_close(prov, close)
    with pytest.raises(DensityCacheError) as exc:
        read_cache_entry(bundle, key + "_x", prov, decision_close_utc=close)
    assert exc.value.code == "CACHE_ENTRY_MISSING"


def test_corrupt_checksum_fails(tmp_path):
    bundle, prov, close = _write_min_bundle(tmp_path)
    key = cache_key_for_close(prov, close)
    path = bundle / "entries" / f"{key}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["content_sha256"] = "0" * 64
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(DensityCacheError) as exc:
        read_cache_entry(bundle, key, prov, decision_close_utc=close)
    assert exc.value.code == "CACHE_CHECKSUM_FAILED"


def test_wrong_config_hash_in_snapshot_fails(tmp_path):
    bundle, prov, close = _write_min_bundle(tmp_path)
    key = cache_key_for_close(prov, close)
    path = bundle / "entries" / f"{key}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["snapshot"]["config_hash"] = "deadbeef"
    record["content_sha256"] = snapshot_content_sha256(record["snapshot"])
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(DensityCacheError) as exc:
        read_cache_entry(bundle, key, prov, decision_close_utc=close)
    assert exc.value.code == "CONFIG_HASH_MISMATCH"


def test_manifest_loads(tmp_path):
    bundle, _, _ = _write_min_bundle(tmp_path)
    m = load_bundle_manifest(bundle)
    assert m["entry_count"] == 1
