"""Resumable D8 bundle build via MultiChunkLifetimeSessionV1 (fail-closed)."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

TOOLS_D8 = Path(__file__).resolve().parents[1] / "phase_bt_d8_canonical_density_cache_v1"
TOOLS_D9 = Path(__file__).resolve().parents[1] / "phase_bt_d9_first_24h_short_pnl_v1"
D80 = Path(__file__).resolve().parent
if str(TOOLS_D8) not in sys.path:
    sys.path.insert(0, str(TOOLS_D8))
if str(TOOLS_D9) not in sys.path:
    sys.path.insert(0, str(TOOLS_D9))
if str(D80) not in sys.path:
    sys.path.insert(0, str(D80))

from cache_contract_v1 import (  # noqa: E402
    ADAPTER_CONTRACT_VERSION,
    CACHE_BUNDLE_VERSION,
    CACHE_CONTRACT_VERSION,
)
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
from multichunk_lifetime_session_v1 import MultiChunkLifetimeSessionV1  # noqa: E402


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _entry_ok(bundle_dir: Path, prov: CacheBundleProvenance, close: str) -> bool:
    try:
        key = cache_key_for_close(prov, close)
        read_cache_entry(bundle_dir, key, prov, decision_close_utc=close)
        return True
    except Exception:
        return False


def build_chunk_cache_bundle(
    *,
    bundle_dir: Path,
    decision_closes: list[str],
    day_chunks: list[list[str]],
    progress_path: Path,
    donor_dirs: list[Path],
    multichunk: MultiChunkLifetimeSessionV1,
    progress_cb: Callable[[int, int, str, str], None] | None = None,
) -> dict[str, Any]:
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "entries").mkdir(exist_ok=True)
    progress = json.loads(progress_path.read_text(encoding="utf-8")) if progress_path.is_file() else {"closes": {}}
    closes_state: dict[str, Any] = progress.setdefault("closes", {})

    target_prov: CacheBundleProvenance | None = None
    if (bundle_dir / "bundle_manifest.json").is_file():
        target_prov = provenance_from_manifest(load_bundle_manifest(bundle_dir))
    elif donor_dirs:
        for d in donor_dirs:
            if (d / "bundle_manifest.json").is_file():
                target_prov = provenance_from_manifest(load_bundle_manifest(d))
                break

    reused = 0
    chunk_written = 0
    skipped = 0
    failures: list[dict[str, str]] = []
    t_chunk_compute = 0.0

    for i, close in enumerate(decision_closes):
        if closes_state.get(close, {}).get("status") == "ok":
            skipped += 1
            if progress_cb:
                progress_cb(i + 1, len(decision_closes), close, "resume_ok")
            continue

        if target_prov and _entry_ok(bundle_dir, target_prov, close):
            closes_state[close] = {"status": "ok", "source": "existing_entry"}
            skipped += 1
            _atomic_write_json(progress_path, progress)
            if progress_cb:
                progress_cb(i + 1, len(decision_closes), close, "existing")
            continue

        imported = None
        if target_prov:
            for donor in donor_dirs:
                try:
                    imported = try_import_close(donor, close, target_prov, bundle_dir)
                except Exception:
                    imported = None
                if imported:
                    closes_state[close] = {"status": "ok", "source": "donor", "donor": imported["donor"]}
                    reused += 1
                    _atomic_write_json(progress_path, progress)
                    if progress_cb:
                        progress_cb(i + 1, len(decision_closes), close, "donor")
                    break

        if closes_state.get(close, {}).get("status") == "ok":
            continue

        try:
            t0 = time.perf_counter()
            snap = multichunk.collect_at_close(close)
            t_chunk_compute += time.perf_counter() - t0
            if target_prov is None:
                target_prov = CacheBundleProvenance(
                    symbol="DOGEUSDT",
                    timeframe="5m",
                    pool_detail=1,
                    min_unique_origins=2,
                    density_method="exact_unique_origin",
                    required_pipeline_line_id=str(snap["pipeline_line_id"]),
                    required_config_hash=str(snap["config_hash"]),
                )
            key = cache_key_for_close(target_prov, close)
            write_cache_entry_atomic(bundle_dir, key, snap)
            closes_state[close] = {"status": "ok", "source": "chunk_lifetime"}
            chunk_written += 1
            _atomic_write_json(progress_path, progress)
            if progress_cb:
                progress_cb(i + 1, len(decision_closes), close, "chunk")
        except Exception as exc:  # noqa: BLE001
            failures.append({"decision_close_utc": close, "error": str(exc)[:300]})
            closes_state[close] = {"status": "failed", "error": str(exc)[:200]}
            _atomic_write_json(progress_path, progress)

    assert target_prov is not None, "provenance not established"
    missing = [c for c in decision_closes if closes_state.get(c, {}).get("status") != "ok"]
    if missing:
        return {
            "complete": False,
            "missing_count": len(missing),
            "failures": failures[:30],
            "reused_donor": reused,
            "chunk_written": chunk_written,
            "skipped": skipped,
            "chunk_compute_s": round(t_chunk_compute, 4),
        }

    full_meta = []
    for close in decision_closes:
        key = cache_key_for_close(target_prov, close)
        snap = read_cache_entry(bundle_dir, key, target_prov, decision_close_utc=close)
        from cache_store_v1 import snapshot_content_sha256

        full_meta.append(
            {
                "cache_key": key,
                "decision_close_utc": close,
                "content_sha256": snapshot_content_sha256(snap),
                "build_source": closes_state.get(close, {}).get("source", "unknown"),
            }
        )
    ref = read_cache_entry(
        bundle_dir,
        cache_key_for_close(target_prov, decision_closes[0]),
        target_prov,
        decision_close_utc=decision_closes[0],
    )
    manifest = {
        "cache_bundle_version": CACHE_BUNDLE_VERSION,
        "cache_contract_version": CACHE_CONTRACT_VERSION,
        "adapter_contract_version": ADAPTER_CONTRACT_VERSION,
        "symbol": target_prov.symbol,
        "timeframe": target_prov.timeframe,
        "pool_detail": target_prov.pool_detail,
        "min_unique_origins": target_prov.min_unique_origins,
        "density_method": target_prov.density_method,
        "required_pipeline_line_id": target_prov.required_pipeline_line_id,
        "required_config_hash": target_prov.required_config_hash,
        "data_provenance": ref.get("data_provenance"),
        "density_source_build": "MultiChunkLifetimeSessionV1.chunk_lifetime_amortized",
        "bt_d80_window": "2026-09-23T00:00:00Z_2026-09-25T23:55:00Z",
        "decision_close_first": decision_closes[0],
        "decision_close_last": decision_closes[-1],
        "entry_count": len(full_meta),
        "entries": full_meta,
        "donor_bundles": [str(p) for p in donor_dirs],
        "build_stats": {
            "reused_donor": reused,
            "chunk_written": chunk_written,
            "skipped_existing": skipped,
            "chunk_compute_s": round(t_chunk_compute, 4),
            "multichunk_timing": multichunk.timing,
        },
    }
    write_bundle_manifest(bundle_dir, manifest)
    progress["manifest_written"] = True
    progress["stats"] = manifest["build_stats"]
    _atomic_write_json(progress_path, progress)
    return {
        "complete": True,
        "entry_count": len(full_meta),
        "reused_donor": reused,
        "chunk_written": chunk_written,
        "skipped": skipped,
        "chunk_compute_s": round(t_chunk_compute, 4),
        "multichunk_timing": multichunk.timing,
    }
