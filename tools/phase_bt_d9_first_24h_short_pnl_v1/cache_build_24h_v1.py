"""Build immutable 24h cache bundle with donor reuse + cold compute for gaps."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

TOOLS_D4 = Path(__file__).resolve().parents[1] / "phase_bt_d4_enforced_depth2_backtester_v1"
TOOLS_D8 = Path(__file__).resolve().parents[1] / "phase_bt_d8_canonical_density_cache_v1"
if str(TOOLS_D4) not in sys.path:
    sys.path.insert(0, str(TOOLS_D4))
if str(TOOLS_D8) not in sys.path:
    sys.path.insert(0, str(TOOLS_D8))

from research_backtester_v1 import ResearchDensityRunV1  # noqa: E402

from cache_contract_v1 import ADAPTER_CONTRACT_VERSION, CACHE_BUNDLE_VERSION, CACHE_CONTRACT_VERSION  # noqa: E402
from cache_store_v1 import (  # noqa: E402
    CacheBundleProvenance,
    cache_key_for_close,
    finalize_staging_bundle,
    write_bundle_manifest,
    write_cache_entry_atomic,
)

from cache_merge_v1 import try_import_close  # noqa: E402
from cache_store_v1 import load_bundle_manifest, provenance_from_manifest  # noqa: E402


def build_24h_bundle_incremental(
    *,
    bundle_dir: Path,
    decision_closes: list[str],
    donor_bundle_dirs: list[Path],
    symbol: str = "DOGEUSDT",
    progress_cb: Any | None = None,
) -> dict[str, Any]:
    if bundle_dir.exists():
        raise FileExistsError(f"bundle already exists: {bundle_dir}")

    session = ResearchDensityRunV1.from_config_path()
    if session.config.symbol != symbol:
        raise ValueError("symbol mismatch vs BT-D4 research config")

    target_provenance: CacheBundleProvenance | None = None
    if donor_bundle_dirs:
        dm = load_bundle_manifest(donor_bundle_dirs[0])
        target_provenance = provenance_from_manifest(dm)
    staging = bundle_dir.parent / f".staging_{bundle_dir.name}"
    if staging.exists():
        import shutil

        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    (staging / "entries").mkdir()

    entry_meta: list[dict[str, Any]] = []
    cold_times: list[float] = []
    reused = 0
    computed = 0

    for i, close in enumerate(decision_closes):
        imported = None
        if target_provenance is not None:
            for donor in donor_bundle_dirs:
                try:
                    imported = try_import_close(donor, close, target_provenance, staging)
                except Exception:
                    imported = None
                if imported:
                    break
        if imported:
            dm = load_bundle_manifest(Path(imported["donor"]))
            sha = next(
                e.get("content_sha256")
                for e in dm.get("entries") or []
                if e.get("decision_close_utc") == close
            )
            entry_meta.append(
                {
                    "cache_key": imported["cache_key"],
                    "decision_close_utc": close,
                    "content_sha256": sha,
                    "build_source": "donor_reuse",
                    "donor_path": imported["donor"],
                }
            )
            reused += 1
            if progress_cb:
                progress_cb(i + 1, len(decision_closes), close, "reuse")
            continue

        t0 = time.perf_counter()
        run = ResearchDensityRunV1.from_config_path()
        snap = run.collect_at_close(close)
        cold_times.append(time.perf_counter() - t0)
        if target_provenance is None:
            target_provenance = CacheBundleProvenance(
                symbol=symbol,
                timeframe=session.config.timeframe,
                pool_detail=session.config.pool_detail,
                min_unique_origins=session.config.min_unique_origins,
                density_method=session.config.density_method,
                required_pipeline_line_id=str(snap["pipeline_line_id"]),
                required_config_hash=str(snap["config_hash"]),
            )
        key = cache_key_for_close(target_provenance, close)
        meta = write_cache_entry_atomic(staging, key, snap)
        meta["build_source"] = "cold_compute"
        entry_meta.append(meta)
        computed += 1
        if progress_cb:
            progress_cb(i + 1, len(decision_closes), close, "cold")

    assert target_provenance is not None
    first_snap_close = decision_closes[0]
    key0 = cache_key_for_close(target_provenance, first_snap_close)
    from cache_store_v1 import read_cache_entry

    ref_snap = read_cache_entry(staging, key0, target_provenance, decision_close_utc=first_snap_close)

    manifest = {
        "cache_bundle_version": CACHE_BUNDLE_VERSION,
        "cache_contract_version": CACHE_CONTRACT_VERSION,
        "adapter_contract_version": ADAPTER_CONTRACT_VERSION,
        "symbol": symbol,
        "timeframe": session.config.timeframe,
        "pool_detail": session.config.pool_detail,
        "min_unique_origins": session.config.min_unique_origins,
        "density_method": session.config.density_method,
        "required_pipeline_line_id": target_provenance.required_pipeline_line_id,
        "required_config_hash": target_provenance.required_config_hash,
        "data_provenance": ref_snap.get("data_provenance"),
        "density_source_build": "ResearchDensityRunV1.collect_at_close",
        "bt_d9_window": "half_open_end",
        "decision_close_first": decision_closes[0],
        "decision_close_last": decision_closes[-1],
        "entry_count": len(entry_meta),
        "entries": entry_meta,
        "donor_bundles": [str(p) for p in donor_bundle_dirs],
        "build_stats": {
            "reused_entries": reused,
            "cold_computed_entries": computed,
            "cold_compute_total_s": round(sum(cold_times), 4),
            "cold_compute_mean_s": round(sum(cold_times) / len(cold_times), 4) if cold_times else 0.0,
        },
    }
    write_bundle_manifest(staging, manifest)
    finalize_staging_bundle(staging, bundle_dir)
    return {
        "bundle_dir": str(bundle_dir),
        "entry_count": len(entry_meta),
        "reused_entries": reused,
        "cold_computed_entries": computed,
        "cold_compute_total_s": manifest["build_stats"]["cold_compute_total_s"],
    }
