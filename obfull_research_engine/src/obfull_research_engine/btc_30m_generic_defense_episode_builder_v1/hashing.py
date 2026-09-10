"""SHA-256 helpers and source-manifest hashing."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_json(obj: Any) -> str:
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return sha256_text(payload)


def source_manifest_hash(manifest: dict[str, Any]) -> str:
    """Stable hash of read-only source path descriptors (not raw payloads)."""
    slim = {
        k: v
        for k, v in sorted(manifest.items())
        if k
        not in {
            "created_at",
            "run_key",
            "out_dir",
        }
    }
    return sha256_json(slim)


def attack_cluster_id(*, zone_id: str, cluster_start_iso: str) -> str:
    return "ac_" + sha256_text(f"{zone_id}|{cluster_start_iso}")[:16]
