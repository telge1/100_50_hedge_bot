"""Immutable OB200 source-file identity and manifest validation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .contracts import stable_hash


@dataclass(frozen=True)
class SourceFile:
    path: Path
    relative_path: str
    fingerprint: str
    source_file_id: str
    size: int
    manifest: dict[str, Any]
    segment_start: datetime
    segment_end: datetime


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_source_file(path: Path, root: Path) -> SourceFile:
    resolved = path.resolve(strict=True)
    root_resolved = root.resolve(strict=True)
    if not resolved.is_relative_to(root_resolved):
        raise PermissionError(f"source outside OB200 root: {path}")
    if resolved.suffix not in {".zst", ".ndjson"}:
        raise ValueError(f"unsupported source format: {resolved.suffix}")
    manifest_path = resolved.with_suffix(".manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format_version") != "ob200_v3_live_archive/v1":
        raise ValueError("unexpected OB200 format_version")
    if int(manifest.get("depth", 0)) != 200:
        raise ValueError("unexpected OB200 depth")
    relative = resolved.relative_to(root_resolved).as_posix()
    fingerprint = sha256_file(resolved)
    # Stable identity is the safe relative path; a changed fingerprint for the
    # same identity is therefore a hard conflict rather than a new file.
    source_file_id = stable_hash({"relative_path": relative})
    return SourceFile(
        path=resolved,
        relative_path=relative,
        fingerprint=fingerprint,
        source_file_id=source_file_id,
        size=resolved.stat().st_size,
        manifest=manifest,
        segment_start=datetime.fromisoformat(
            str(manifest["start_utc"]).replace("Z", "+00:00")
        ),
        segment_end=datetime.fromisoformat(
            str(manifest["end_utc"]).replace("Z", "+00:00")
        ),
    )
