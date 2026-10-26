"""Build cache entries from ResearchDensityRunV1.collect_at_close (no alternate math)."""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

TOOLS_D4 = Path(__file__).resolve().parents[1] / "phase_bt_d4_enforced_depth2_backtester_v1"
if str(TOOLS_D4) not in sys.path:
    sys.path.insert(0, str(TOOLS_D4))

from research_backtester_v1 import ResearchDensityRunV1  # noqa: E402

from cache_contract_v1 import ADAPTER_CONTRACT_VERSION, CACHE_BUNDLE_VERSION, CACHE_CONTRACT_VERSION
from cache_store_v1 import (
    CacheBundleProvenance,
    cache_key_for_close,
    finalize_staging_bundle,
    write_bundle_manifest,
    write_cache_entry_atomic,
)


def iter_5m_closes_inclusive(start_utc: str, end_utc: str) -> list[str]:
    start = datetime.fromisoformat(start_utc.replace("Z", "+00:00"))
    end = datetime.fromisoformat(end_utc.replace("Z", "+00:00"))
    if end < start:
        return []
    cur = start
    out: list[str] = []
    while cur <= end:
        out.append(cur.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        cur += timedelta(minutes=5)
    return out


def build_cache_bundle(
    *,
    bundle_dir: Path,
    decision_closes: list[str],
    symbol: str = "DOGEUSDT",
    collect_fn: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Cold-compute each close via canonical provider; write immutable bundle."""
    session = ResearchDensityRunV1.from_config_path()
    if session.config.symbol != symbol:
        raise ValueError(f"research config symbol {session.config.symbol} != {symbol}")

    if collect_fn is None:
        def collect_fn(close: str) -> dict[str, Any]:
            return session.collect_at_close(close)

    staging = bundle_dir.parent / f".staging_{bundle_dir.name}"
    if staging.exists():
        import shutil

        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    (staging / "entries").mkdir()

    timings: list[float] = []
    entry_meta: list[dict[str, Any]] = []
    first_snap: dict[str, Any] | None = None

    for close in decision_closes:
        t0 = time.perf_counter()
        snap = collect_fn(close)
        timings.append(time.perf_counter() - t0)
        if first_snap is None:
            first_snap = snap
        pipeline = str(snap["pipeline_line_id"])
        phash = str(snap["config_hash"])
        prov = CacheBundleProvenance(
            symbol=symbol,
            timeframe=session.config.timeframe,
            pool_detail=session.config.pool_detail,
            min_unique_origins=session.config.min_unique_origins,
            density_method=session.config.density_method,
            required_pipeline_line_id=pipeline,
            required_config_hash=phash,
        )
        key = cache_key_for_close(prov, close)
        meta = write_cache_entry_atomic(staging, key, snap)
        entry_meta.append(meta)

    assert first_snap is not None
    manifest = {
        "cache_bundle_version": CACHE_BUNDLE_VERSION,
        "cache_contract_version": CACHE_CONTRACT_VERSION,
        "adapter_contract_version": ADAPTER_CONTRACT_VERSION,
        "symbol": symbol,
        "timeframe": session.config.timeframe,
        "pool_detail": session.config.pool_detail,
        "min_unique_origins": session.config.min_unique_origins,
        "density_method": session.config.density_method,
        "required_pipeline_line_id": first_snap["pipeline_line_id"],
        "required_config_hash": first_snap["config_hash"],
        "data_provenance": first_snap.get("data_provenance"),
        "density_source_build": "ResearchDensityRunV1.collect_at_close",
        "decision_close_first": decision_closes[0],
        "decision_close_last": decision_closes[-1],
        "entry_count": len(entry_meta),
        "entries": entry_meta,
        "build_timing": {
            "cold_compute_total_s": round(sum(timings), 4),
            "cold_compute_mean_s": round(sum(timings) / len(timings), 4) if timings else 0.0,
            "per_close_s": {decision_closes[i]: round(timings[i], 4) for i in range(len(timings))},
        },
    }
    write_bundle_manifest(staging, manifest)
    finalize_staging_bundle(staging, bundle_dir)

    sizes = []
    for ent in entry_meta:
        p = bundle_dir / "entries" / f"{ent['cache_key']}.json"
        sizes.append(p.stat().st_size)
    return {
        "bundle_dir": str(bundle_dir),
        "entry_count": len(entry_meta),
        "cold_compute_total_s": manifest["build_timing"]["cold_compute_total_s"],
        "cold_compute_mean_s": manifest["build_timing"]["cold_compute_mean_s"],
        "mean_entry_bytes": int(sum(sizes) / len(sizes)) if sizes else 0,
        "total_bytes": sum(sizes),
        "extrapolate_44064_closes": {
            "compute_hours_at_mean": round((manifest["build_timing"]["cold_compute_mean_s"] * 44064) / 3600, 2),
            "storage_gb_at_mean": round((sum(sizes) / len(sizes) * 44064) / (1024**3), 3) if sizes else 0,
        },
    }
