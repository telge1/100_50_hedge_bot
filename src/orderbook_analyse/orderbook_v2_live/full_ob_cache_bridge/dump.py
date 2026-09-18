"""Atomic dump writer for freeze payloads."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import orjson

from orderbook_analyse.orderbook_v2_live.full_ob_cache_bridge.checkpoints import BookCheckpoint
from orderbook_analyse.orderbook_v2_live.full_ob_cache_bridge.freeze import (
    FreezeBundle,
    canonical_manifest_hash,
    manifest_from_bundle,
    sha256_bytes,
)
from orderbook_analyse.orderbook_v2_live.full_ob_cache_bridge.paths import (
    UnsafeDumpPath,
    ensure_dump_root,
    resolve_dump_dir,
)
from orderbook_analyse.orderbook_v2_live.full_ob_cache_bridge.protocol import PROTOCOL_VERSION


def _write_atomic(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    tmp = path.with_name(path.name + ".tmp")
    if tmp.exists():
        tmp.unlink()
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    os.replace(tmp, path)
    os.chmod(path, mode)


def build_payload_bytes(
    *,
    bundle: FreezeBundle,
    symbol: str,
) -> bytes:
    payload = {
        "protocol_version": PROTOCOL_VERSION,
        "symbol": symbol,
        "t0_ns": bundle.t0_ns,
        "feature_cutoff_ns": bundle.feature_cutoff_ns,
        "analysis_start_ns": getattr(bundle, "analysis_start_ns", None),
        "payload_replay_start_ns": getattr(bundle, "payload_replay_start_ns", None),
        "anchor": bundle.anchor.to_dict() if bundle.anchor else None,
        "epoch_markers": list(getattr(bundle, "epoch_markers", ()) or ()),
        "deltas": list(bundle.deltas),
        "t0_book": bundle.t0_book,
        "event_time_fields": ["ts", "cts", "event_ts_ms"],
        "receive_time_fields": ["local_receive_time_ns", "_bridge_receive_time_ns"],
    }
    return orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)


def write_freeze_dump(
    *,
    dump_root: Path,
    request_id: str,
    forecast_id_seed: str,
    symbol: str,
    collector_instance_id: str,
    bundle: FreezeBundle,
    max_payload_bytes: int,
) -> dict[str, Any]:
    root = ensure_dump_root(dump_root)
    target = resolve_dump_dir(root, request_id)
    if target.exists():
        raise UnsafeDumpPath("dump_exists")
    target.mkdir(mode=0o700, parents=False)
    os.chmod(target, 0o700)

    payload = build_payload_bytes(bundle=bundle, symbol=symbol)
    if len(payload) > max_payload_bytes:
        # cleanup empty dir
        try:
            target.rmdir()
        except OSError:
            pass
        raise UnsafeDumpPath("payload_too_large")

    payload_hash = sha256_bytes(payload)
    _write_atomic(target / "payload.json", payload)
    manifest = manifest_from_bundle(
        bundle=bundle,
        protocol_version=PROTOCOL_VERSION,
        request_id=request_id,
        forecast_id_seed=forecast_id_seed,
        symbol=symbol,
        collector_instance_id=collector_instance_id,
        payload_sha256=payload_hash,
        dump_relpath=request_id,
    )
    # Fix manifest_sha256 to canonical definition
    manifest["manifest_sha256"] = canonical_manifest_hash(manifest)
    _write_atomic(
        target / "manifest.json",
        orjson.dumps(manifest, option=orjson.OPT_SORT_KEYS | orjson.OPT_INDENT_2),
    )
    return {
        "ok": True,
        "dump_dir": str(target),
        "manifest": manifest,
        "payload_sha256": payload_hash,
        "payload_bytes": len(payload),
    }
