"""Resumable BT-D8/D9 validated cache build (no overwrite of valid entries)."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
TOOLS_D4 = ROOT / "tools/phase_bt_d4_enforced_depth2_backtester_v1"
TOOLS_D8 = ROOT / "tools/phase_bt_d8_canonical_density_cache_v1"
TOOLS_D9 = ROOT / "tools/phase_bt_d9_first_24h_short_pnl_v1"
for p in (TOOLS_D4, TOOLS_D8, TOOLS_D9):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from research_backtester_v1 import ResearchDensityRunV1  # noqa: E402

from cache_contract_v1 import ADAPTER_CONTRACT_VERSION, CACHE_BUNDLE_VERSION, CACHE_CONTRACT_VERSION  # noqa: E402
from cache_merge_v1 import try_import_close  # noqa: E402
from cache_store_v1 import (  # noqa: E402
    CacheBundleProvenance,
    cache_key_for_close,
    load_bundle_manifest,
    provenance_from_manifest,
    read_cache_entry,
    write_bundle_manifest,
    write_cache_entry_atomic,
)


def discover_donor_bundles() -> list[Path]:
    donors: list[Path] = []
    for manifest in sorted(ROOT.glob("results/**/bundle_manifest.json")):
        p = manifest.parent
        if "phase_bt_d73" in str(p):
            continue
        donors.append(p)
    return donors


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _entry_ok(bundle_dir: Path, provenance: CacheBundleProvenance, close: str) -> bool:
    key = cache_key_for_close(provenance, close)
    try:
        read_cache_entry(bundle_dir, key, provenance, decision_close_utc=close)
        return True
    except Exception:
        return False


def _load_progress(progress_path: Path) -> dict[str, Any]:
    if not progress_path.is_file():
        return {"closes": {}, "stats": {}}
    return json.loads(progress_path.read_text(encoding="utf-8"))


def build_or_resume_bundle(
    *,
    bundle_dir: Path,
    decision_closes: list[str],
    progress_path: Path,
    donor_bundle_dirs: list[Path] | None = None,
    max_retries: int = 3,
    progress_cb: Callable[[int, int, str, str], None] | None = None,
) -> dict[str, Any]:
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "entries").mkdir(exist_ok=True)
    donors = donor_bundle_dirs if donor_bundle_dirs is not None else discover_donor_bundles()
    progress = _load_progress(progress_path)
    closes_state: dict[str, Any] = progress.setdefault("closes", {})

    session = ResearchDensityRunV1.from_config_path()
    target_provenance: CacheBundleProvenance | None = None
    if (bundle_dir / "bundle_manifest.json").is_file():
        target_provenance = provenance_from_manifest(load_bundle_manifest(bundle_dir))
    elif donors:
        try:
            target_provenance = provenance_from_manifest(load_bundle_manifest(donors[0]))
        except Exception:
            target_provenance = None

    entry_meta: list[dict[str, Any]] = []
    cold_times: list[float] = []
    reused = 0
    computed = 0
    skipped_existing = 0
    failures: list[dict[str, str]] = []

    for i, close in enumerate(decision_closes):
        if closes_state.get(close, {}).get("status") == "ok":
            skipped_existing += 1
            if progress_cb:
                progress_cb(i + 1, len(decision_closes), close, "resume_ok")
            continue

        if target_provenance is not None and _entry_ok(bundle_dir, target_provenance, close):
            closes_state[close] = {"status": "ok", "source": "existing_entry"}
            skipped_existing += 1
            _atomic_write_json(progress_path, progress)
            if progress_cb:
                progress_cb(i + 1, len(decision_closes), close, "existing")
            continue

        imported = None
        if target_provenance is not None:
            for donor in donors:
                try:
                    imported = try_import_close(donor, close, target_provenance, bundle_dir)
                except Exception:
                    imported = None
                if imported:
                    closes_state[close] = {"status": "ok", "source": "donor", "donor": imported["donor"]}
                    entry_meta.append(
                        {
                            "cache_key": imported["cache_key"],
                            "decision_close_utc": close,
                            "build_source": "donor_reuse",
                            "donor_path": imported["donor"],
                        }
                    )
                    reused += 1
                    _atomic_write_json(progress_path, progress)
                    if progress_cb:
                        progress_cb(i + 1, len(decision_closes), close, "reuse")
                    break

        if closes_state.get(close, {}).get("status") == "ok":
            continue

        last_err = ""
        for attempt in range(max_retries):
            try:
                t0 = time.perf_counter()
                run = ResearchDensityRunV1.from_config_path()
                snap = run.collect_at_close(close)
                cold_times.append(time.perf_counter() - t0)
                if target_provenance is None:
                    target_provenance = CacheBundleProvenance(
                        symbol=session.config.symbol,
                        timeframe=session.config.timeframe,
                        pool_detail=session.config.pool_detail,
                        min_unique_origins=session.config.min_unique_origins,
                        density_method=session.config.density_method,
                        required_pipeline_line_id=str(snap["pipeline_line_id"]),
                        required_config_hash=str(snap["config_hash"]),
                    )
                key = cache_key_for_close(target_provenance, close)
                meta = write_cache_entry_atomic(bundle_dir, key, snap)
                meta["build_source"] = "cold_compute"
                entry_meta.append(meta)
                computed += 1
                closes_state[close] = {"status": "ok", "source": "cold_compute", "attempt": attempt + 1}
                _atomic_write_json(progress_path, progress)
                if progress_cb:
                    progress_cb(i + 1, len(decision_closes), close, "cold")
                break
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)[:300]
                time.sleep(min(2 ** attempt, 8))
        else:
            failures.append({"decision_close_utc": close, "error": last_err})
            closes_state[close] = {"status": "failed", "error": last_err}
            _atomic_write_json(progress_path, progress)

    assert target_provenance is not None, "no provenance established"
    missing = [c for c in decision_closes if closes_state.get(c, {}).get("status") != "ok"]
    if missing:
        progress["stats"] = {
            "reused_entries": reused,
            "cold_computed_entries": computed,
            "skipped_existing": skipped_existing,
            "failures": len(failures),
            "missing_count": len(missing),
        }
        _atomic_write_json(progress_path, progress)
        return {
            "complete": False,
            "missing_count": len(missing),
            "failures": failures[:20],
            "reused_entries": reused,
            "cold_computed_entries": computed,
            "cold_compute_total_s": round(sum(cold_times), 4),
        }

    # Rebuild manifest from on-disk entries (authoritative).
    full_meta: list[dict[str, Any]] = []
    for close in decision_closes:
        key = cache_key_for_close(target_provenance, close)
        snap = read_cache_entry(bundle_dir, key, target_provenance, decision_close_utc=close)
        from cache_store_v1 import snapshot_content_sha256

        full_meta.append(
            {
                "cache_key": key,
                "decision_close_utc": close,
                "content_sha256": snapshot_content_sha256(snap),
                "build_source": closes_state.get(close, {}).get("source", "unknown"),
            }
        )

    ref_snap = read_cache_entry(
        bundle_dir,
        cache_key_for_close(target_provenance, decision_closes[0]),
        target_provenance,
        decision_close_utc=decision_closes[0],
    )
    manifest = {
        "cache_bundle_version": CACHE_BUNDLE_VERSION,
        "cache_contract_version": CACHE_CONTRACT_VERSION,
        "adapter_contract_version": ADAPTER_CONTRACT_VERSION,
        "symbol": session.config.symbol,
        "timeframe": session.config.timeframe,
        "pool_detail": session.config.pool_detail,
        "min_unique_origins": session.config.min_unique_origins,
        "density_method": session.config.density_method,
        "required_pipeline_line_id": target_provenance.required_pipeline_line_id,
        "required_config_hash": target_provenance.required_config_hash,
        "data_provenance": ref_snap.get("data_provenance"),
        "density_source_build": "ResearchDensityRunV1.collect_at_close",
        "bt_d73_window": "inclusive_endpoints",
        "decision_close_first": decision_closes[0],
        "decision_close_last": decision_closes[-1],
        "entry_count": len(full_meta),
        "entries": full_meta,
        "donor_bundles": [str(p) for p in donors],
        "build_stats": {
            "reused_entries": reused,
            "cold_computed_entries": computed,
            "skipped_existing": skipped_existing,
            "cold_compute_total_s": round(sum(cold_times), 4),
            "cold_compute_mean_s": round(sum(cold_times) / len(cold_times), 4) if cold_times else 0.0,
        },
    }
    write_bundle_manifest(bundle_dir, manifest)
    progress["stats"] = manifest["build_stats"]
    progress["manifest_written"] = True
    _atomic_write_json(progress_path, progress)
    return {
        "complete": True,
        "bundle_dir": str(bundle_dir),
        "entry_count": len(full_meta),
        **manifest["build_stats"],
    }
