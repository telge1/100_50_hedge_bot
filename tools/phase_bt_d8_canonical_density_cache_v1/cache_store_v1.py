"""Atomic cache bundle read/write with fail-closed integrity checks."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from cache_contract_v1 import (
    ADAPTER_CONTRACT_VERSION,
    CACHE_BUNDLE_VERSION,
    CACHE_CONTRACT_VERSION,
    CACHE_ENTRY_VERSION,
    DensityCacheError,
    canonical_snapshot_json,
    make_cache_key,
    snapshot_content_sha256,
    validate_snapshot_shape,
)


class CacheBundleProvenance:
    def __init__(
        self,
        *,
        symbol: str,
        timeframe: str,
        pool_detail: int,
        min_unique_origins: int,
        density_method: str,
        required_pipeline_line_id: str,
        required_config_hash: str,
        cache_contract_version: str = CACHE_CONTRACT_VERSION,
        adapter_contract_version: str = ADAPTER_CONTRACT_VERSION,
    ) -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self.pool_detail = pool_detail
        self.min_unique_origins = min_unique_origins
        self.density_method = density_method
        self.required_pipeline_line_id = required_pipeline_line_id
        self.required_config_hash = required_config_hash
        self.cache_contract_version = cache_contract_version
        self.adapter_contract_version = adapter_contract_version


def _entry_path(bundle_dir: Path, cache_key: str) -> Path:
    return bundle_dir / "entries" / f"{cache_key}.json"


def write_bundle_manifest(bundle_dir: Path, manifest: dict[str, Any]) -> None:
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "entries").mkdir(exist_ok=True)
    tmp = bundle_dir / "bundle_manifest.json.tmp"
    final = bundle_dir / "bundle_manifest.json"
    body = json.dumps(manifest, indent=2, sort_keys=True)
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, final)


def load_bundle_manifest(bundle_dir: Path) -> dict[str, Any]:
    path = bundle_dir / "bundle_manifest.json"
    if not path.is_file():
        raise DensityCacheError("CACHE_BUNDLE_MISSING", f"no manifest: {bundle_dir}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DensityCacheError("CACHE_BUNDLE_CORRUPT", "manifest JSON invalid", {"error": str(exc)}) from exc
    if manifest.get("cache_bundle_version") != CACHE_BUNDLE_VERSION:
        raise DensityCacheError(
            "CACHE_BUNDLE_VERSION_MISMATCH",
            "unsupported bundle version",
            {"got": manifest.get("cache_bundle_version")},
        )
    return manifest


def provenance_from_manifest(manifest: dict[str, Any]) -> CacheBundleProvenance:
    if manifest.get("cache_contract_version") != CACHE_CONTRACT_VERSION:
        raise DensityCacheError("CACHE_CONTRACT_MISMATCH", "cache contract version mismatch")
    return CacheBundleProvenance(
        symbol=str(manifest["symbol"]),
        timeframe=str(manifest["timeframe"]),
        pool_detail=int(manifest["pool_detail"]),
        min_unique_origins=int(manifest["min_unique_origins"]),
        density_method=str(manifest["density_method"]),
        required_pipeline_line_id=str(manifest["required_pipeline_line_id"]),
        required_config_hash=str(manifest["required_config_hash"]),
        cache_contract_version=str(manifest["cache_contract_version"]),
        adapter_contract_version=str(manifest.get("adapter_contract_version", ADAPTER_CONTRACT_VERSION)),
    )


def write_cache_entry_atomic(bundle_dir: Path, cache_key: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    decision_close = snapshot["decision_close_utc"]
    validate_snapshot_shape(snapshot, decision_close_utc=decision_close)
    sha = snapshot_content_sha256(snapshot)
    record = {
        "cache_entry_version": CACHE_ENTRY_VERSION,
        "cache_key": cache_key,
        "content_sha256": sha,
        "adapter_contract_version": ADAPTER_CONTRACT_VERSION,
        "snapshot": snapshot,
    }
    entries = bundle_dir / "entries"
    entries.mkdir(parents=True, exist_ok=True)
    final = _entry_path(bundle_dir, cache_key)
    tmp = final.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
    os.replace(tmp, final)
    return {"cache_key": cache_key, "decision_close_utc": decision_close, "content_sha256": sha}


def read_cache_entry(
    bundle_dir: Path,
    cache_key: str,
    provenance: CacheBundleProvenance,
    *,
    decision_close_utc: str,
) -> dict[str, Any]:
    path = _entry_path(bundle_dir, cache_key)
    if not path.is_file():
        raise DensityCacheError(
            "CACHE_ENTRY_MISSING",
            "no cache entry for decision close",
            {"decision_close_utc": decision_close_utc, "cache_key": cache_key},
        )
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DensityCacheError("CACHE_ENTRY_CORRUPT", "entry JSON invalid", {"path": str(path)}) from exc
    if record.get("cache_entry_version") != CACHE_ENTRY_VERSION:
        raise DensityCacheError("CACHE_ENTRY_VERSION_MISMATCH", "unsupported entry version")
    if record.get("cache_key") != cache_key:
        raise DensityCacheError("CACHE_KEY_MISMATCH", "entry cache_key does not match request")
    snap = record.get("snapshot")
    if not isinstance(snap, dict):
        raise DensityCacheError("CACHE_ENTRY_CORRUPT", "snapshot missing")
    expected_sha = record.get("content_sha256")
    actual_sha = snapshot_content_sha256(snap)
    if expected_sha != actual_sha:
        raise DensityCacheError(
            "CACHE_CHECKSUM_FAILED",
            "content_sha256 mismatch",
            {"expected": expected_sha, "actual": actual_sha},
        )
    validate_snapshot_shape(snap, decision_close_utc=decision_close_utc)
    if snap.get("pipeline_line_id") != provenance.required_pipeline_line_id:
        raise DensityCacheError(
            "PIPELINE_PROVENANCE_MISMATCH",
            "snapshot pipeline_line_id does not match bundle",
        )
    if snap.get("config_hash") != provenance.required_config_hash:
        raise DensityCacheError(
            "CONFIG_HASH_MISMATCH",
            "snapshot config_hash does not match bundle",
        )
    return snap


def finalize_staging_bundle(staging_dir: Path, bundle_dir: Path) -> Path:
    """Atomically publish a fully written staging directory as an immutable bundle."""
    if not (staging_dir / "bundle_manifest.json").is_file():
        raise DensityCacheError("CACHE_BUILD_INCOMPLETE", "staging missing bundle_manifest.json")
    if bundle_dir.exists():
        raise DensityCacheError("BUNDLE_EXISTS", f"refusing to overwrite {bundle_dir}")
    bundle_dir.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staging_dir, bundle_dir)
    return bundle_dir


def cache_key_for_close(provenance: CacheBundleProvenance, decision_close_utc: str) -> str:
    return make_cache_key(
        symbol=provenance.symbol,
        timeframe=provenance.timeframe,
        pool_detail=provenance.pool_detail,
        min_unique_origins=provenance.min_unique_origins,
        decision_close_utc=decision_close_utc,
        pipeline_line_id=provenance.required_pipeline_line_id,
        provider_config_hash=provenance.required_config_hash,
        cache_contract_version=provenance.cache_contract_version,
        adapter_contract_version=provenance.adapter_contract_version,
    )
