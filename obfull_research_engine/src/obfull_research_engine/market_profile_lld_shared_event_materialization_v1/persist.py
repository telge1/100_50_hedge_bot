"""Immutable research-artifact persistence. No ClickHouse writes."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..paths import ENGINE_ROOT
from .hashing import file_sha256, sha256_hex

RESULTS_ROOT = ENGINE_ROOT / "results" / "market_profile_lld_shared_event_materialization_v1"


def _now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def atomic_write_json(path: Path, obj: Any) -> None:
    atomic_write_text(path, json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n")


def atomic_write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    text = "".join(json.dumps(r, sort_keys=True, default=str) + "\n" for r in rows)
    atomic_write_text(path, text)


def compute_run_key(config: dict[str, Any]) -> str:
    return "rk_" + sha256_hex(config)[:16]


def run_dir(symbol: str, run_key: str) -> Path:
    return RESULTS_ROOT / symbol.upper() / run_key


def load_manifest(path: Path) -> dict[str, Any] | None:
    if not path.is_file() or path.name.endswith(".tmp"):
        return None
    if path.with_suffix(path.suffix + ".tmp").exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def new_manifest(*, symbol: str, run_key: str, directory: Path, config: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "market_profile_lld_shared_event_materialization_v1",
        "symbol": symbol.upper(),
        "run_key": run_key,
        "run_dir": str(directory),
        "status": "RUNNING",
        "verdict": None,
        "created_at": _now_z(),
        "updated_at": _now_z(),
        "config_hash": config.get("config_hash"),
        "clickhouse_writes": 0,
        "dashboard_modified": False,
    }


def mark(manifest: dict[str, Any], status: str, **extra: Any) -> None:
    manifest["status"] = status
    manifest["updated_at"] = _now_z()
    manifest.update(extra)
