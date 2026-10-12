"""Compact per-path provenance index (read-only, cached)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .errors import ProvenanceMissingError, ProvenanceMismatchError, Sha256MismatchError

_LOOKUP_CACHE: dict[str, "ProvenanceLookup"] = {}


@dataclass(frozen=True)
class ProvenanceEntry:
    source_frozen_id: str
    dest_sha256: str


@dataclass(frozen=True)
class ProvenanceLookup:
    """Maps production-relative snapshot paths to expected frozen id + file hash."""

    entries: dict[str, ProvenanceEntry]

    def get(self, relative_dest: str) -> ProvenanceEntry:
        key = relative_dest.replace("\\", "/")
        entry = self.entries.get(key)
        if entry is None:
            raise ProvenanceMissingError(f"no provenance for {key}")
        return entry

    def verify_file(self, relative_dest: str, path: Path, payload_frozen_id: str) -> None:
        entry = self.get(relative_dest)
        if payload_frozen_id != entry.source_frozen_id:
            raise ProvenanceMismatchError(
                f"frozen_input {payload_frozen_id!r} != provenance {entry.source_frozen_id!r} for {relative_dest}"
            )
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != entry.dest_sha256:
            raise Sha256MismatchError(f"sha256 mismatch for {relative_dest}")


def _relative_dest(production_root: Path, snapshot_path: Path) -> str:
    rel = snapshot_path.relative_to(production_root)
    return str(rel).replace("\\", "/")


def relative_dest_for(production_root: Path, snapshot_path: Path) -> str:
    return _relative_dest(production_root, snapshot_path)


def build_provenance_lookup_entries(production_root: Path) -> dict[str, ProvenanceEntry]:
    root = production_root.resolve()
    out: dict[str, ProvenanceEntry] = {}
    index_path = root / "production" / "provenance_index.json"
    if index_path.is_file():
        data = json.loads(index_path.read_text(encoding="utf-8"))
        for row in data.get("entries") or []:
            dest = str(row["dest"]).replace("\\", "/")
            fid = str(row.get("source_frozen_id") or row.get("source_frozen_id_expected") or "")
            sha = str(row.get("dest_sha256") or "")
            if not fid or not sha:
                continue
            out[dest] = ProvenanceEntry(source_frozen_id=fid, dest_sha256=sha)

    results = root / "production" / "results"
    if results.is_dir():
        for path in results.rglob("*.json"):
            if path.name.endswith(".tmp"):
                continue
            rel = relative_dest_for(root, path)
            if rel in out:
                continue
            frozen_id = str(json.loads(path.read_text(encoding="utf-8")).get("frozen_input", {}).get("snapshot_id") or "")
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
            out[rel] = ProvenanceEntry(source_frozen_id=frozen_id, dest_sha256=sha)
    return out


def write_provenance_lookup(production_root: Path, *, rel_path: str = "production/provenance_lookup.json") -> Path:
    root = production_root.resolve()
    entries = build_provenance_lookup_entries(root)
    payload = {
        "format_version": "provenance_lookup_v1",
        "entry_count": len(entries),
        "entries": {k: {"source_frozen_id": v.source_frozen_id, "dest_sha256": v.dest_sha256} for k, v in sorted(entries.items())},
    }
    dest = root / rel_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return dest


def load_provenance_lookup(production_root: Path, rel_path: str) -> ProvenanceLookup:
    cache_key = f"{production_root.resolve()}:{rel_path}"
    if cache_key in _LOOKUP_CACHE:
        return _LOOKUP_CACHE[cache_key]

    path = production_root / rel_path
    if not path.is_file():
        write_provenance_lookup(production_root, rel_path=rel_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    entries: dict[str, ProvenanceEntry] = {}
    for key, row in (data.get("entries") or {}).items():
        entries[str(key).replace("\\", "/")] = ProvenanceEntry(
            source_frozen_id=str(row["source_frozen_id"]),
            dest_sha256=str(row["dest_sha256"]),
        )
    lookup = ProvenanceLookup(entries=entries)
    _LOOKUP_CACHE[cache_key] = lookup
    return lookup
