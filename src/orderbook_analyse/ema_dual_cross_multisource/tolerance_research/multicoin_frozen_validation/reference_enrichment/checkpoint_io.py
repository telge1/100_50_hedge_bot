"""Atomic enrichment checkpoints with hash-gated resume."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from . import constants as C
from .hashes import all_hashes


def ensure_dirs(output_dir: Path) -> dict[str, Path]:
    paths = {
        "root": output_dir,
        "checkpoints": output_dir / "checkpoints",
        "failures": output_dir / "failures",
    }
    for p in paths.values():
        p.mkdir(parents=True, exist_ok=True)
    return paths


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, default=str)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def checkpoint_path(checkpoints_dir: Path, symbol: str) -> Path:
    return checkpoints_dir / f"{symbol.upper()}.json"


def write_enrichment_checkpoint(
    checkpoints_dir: Path,
    *,
    symbol: str,
    status: str,
    candidate_ids: list[str],
    feature_rows: list[dict[str, Any]],
    coverage_summary: dict[str, Any],
    parity_summary: dict[str, Any],
    extra: dict[str, Any] | None = None,
) -> Path:
    hashes = all_hashes()
    body = {
        **hashes,
        "symbol": symbol.upper(),
        "status": status,
        "entry_rule": C.ENTRY_RULE,
        "candidate_ids": candidate_ids,
        "feature_rows": feature_rows,
        "coverage_summary": coverage_summary,
        "parity_summary": parity_summary,
        **(extra or {}),
    }
    path = checkpoint_path(checkpoints_dir, symbol)
    atomic_write_json(path, body)
    return path


def load_checkpoint(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def should_skip_complete(rec: dict[str, Any] | None) -> bool:
    """Skip only COMPLETE with identical schema + three content hashes."""
    if not rec or rec.get("status") != "COMPLETE":
        return False
    cur = all_hashes()
    for key in ("schema_version", "feature_definition_hash", "reference_strategy_hash", "source_schema_hash"):
        if str(rec.get(key)) != str(cur[key]):
            return False
    return True


def hash_mismatch(rec: dict[str, Any] | None) -> bool:
    if not rec:
        return False
    cur = all_hashes()
    for key in ("feature_definition_hash", "reference_strategy_hash", "source_schema_hash"):
        if key in rec and str(rec.get(key)) != str(cur[key]):
            return True
    if "schema_version" in rec and str(rec.get("schema_version")) != str(cur["schema_version"]):
        return True
    return False
