"""Reuse validated BT-D8 entries into a new bundle (provenance-checked)."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

TOOLS_D8 = Path(__file__).resolve().parents[1] / "phase_bt_d8_canonical_density_cache_v1"
if str(TOOLS_D8) not in sys.path:
    sys.path.insert(0, str(TOOLS_D8))

from cache_contract_v1 import CACHE_CONTRACT_VERSION, DensityCacheError  # noqa: E402
from cache_store_v1 import (  # noqa: E402
    CacheBundleProvenance,
    cache_key_for_close,
    load_bundle_manifest,
    provenance_from_manifest,
    read_cache_entry,
)


def assert_donor_compatible(donor_manifest: dict[str, Any], target_provenance: CacheBundleProvenance) -> None:
    if donor_manifest.get("cache_contract_version") != CACHE_CONTRACT_VERSION:
        raise DensityCacheError("DONOR_CONTRACT_MISMATCH", "donor cache contract")
    donor_prov = provenance_from_manifest(donor_manifest)
    if donor_prov.required_pipeline_line_id != target_provenance.required_pipeline_line_id:
        raise DensityCacheError("DONOR_PIPELINE_MISMATCH", "refusing mixed pipeline donors")
    if donor_prov.required_config_hash != target_provenance.required_config_hash:
        raise DensityCacheError("DONOR_CONFIG_HASH_MISMATCH", "refusing mixed config_hash donors")
    if donor_prov.symbol != target_provenance.symbol:
        raise DensityCacheError("DONOR_IDENTITY_MISMATCH", "symbol")
    if donor_prov.timeframe != target_provenance.timeframe:
        raise DensityCacheError("DONOR_IDENTITY_MISMATCH", "timeframe")
    if donor_prov.pool_detail != target_provenance.pool_detail:
        raise DensityCacheError("DONOR_IDENTITY_MISMATCH", "pool_detail")
    if donor_prov.min_unique_origins != target_provenance.min_unique_origins:
        raise DensityCacheError("DONOR_IDENTITY_MISMATCH", "min_unique_origins")
    if donor_prov.density_method != target_provenance.density_method:
        raise DensityCacheError("DONOR_IDENTITY_MISMATCH", "density_method")


def try_import_close(
    donor_dir: Path,
    decision_close_utc: str,
    target_provenance: CacheBundleProvenance,
    staging_dir: Path,
) -> dict[str, Any] | None:
    """Copy verified entry into staging if donor has this close."""
    if not donor_dir.is_dir():
        return None
    manifest = load_bundle_manifest(donor_dir)
    assert_donor_compatible(manifest, target_provenance)
    key = cache_key_for_close(target_provenance, decision_close_utc)
    listed = {e["decision_close_utc"]: e for e in manifest.get("entries") or []}
    if decision_close_utc not in listed:
        return None
    snap = read_cache_entry(donor_dir, key, target_provenance, decision_close_utc=decision_close_utc)
    src = donor_dir / "entries" / f"{key}.json"
    dst = staging_dir / "entries" / f"{key}.json"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    record = json.loads(dst.read_text(encoding="utf-8"))
    if record.get("content_sha256") != listed[decision_close_utc].get("content_sha256"):
        dst.unlink(missing_ok=True)
        raise DensityCacheError("DONOR_CHECKSUM_DRIFT", decision_close_utc)
    return {"cache_key": key, "decision_close_utc": decision_close_utc, "source": "donor", "donor": str(donor_dir)}
