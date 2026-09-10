"""Immutable research artifacts. No ClickHouse writes."""

from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..market_profile_lld_shared_event_materialization_v1.hashing import file_sha256, sha256_hex
from ..paths import ENGINE_ROOT

RESULTS_ROOT = ENGINE_ROOT / "results" / "bounded_level_first_analyzer_pilot_v1"
PHASE2_ROOT = (
    ENGINE_ROOT
    / "results"
    / "market_profile_lld_shared_event_materialization_v1"
)


def now_z() -> str:
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


def atomic_write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    if not fieldnames:
        keys: list[str] = []
        seen: set[str] = set()
        for row in rows:
            for k in row:
                if k not in seen:
                    seen.add(k)
                    keys.append(k)
        fieldnames = keys
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: _csv_cell(row.get(k)) for k in fieldnames})
    os.replace(tmp, path)


def _csv_cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (list, dict, tuple)):
        return json.dumps(value, sort_keys=True, default=str)
    return value


def compute_run_key(config: dict[str, Any]) -> str:
    return "lf1_" + sha256_hex(config)[:16]


def run_dir(symbol: str, run_key: str) -> Path:
    return RESULTS_ROOT / symbol.upper() / run_key


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file() or path.name.endswith(".tmp"):
        return None
    if path.with_suffix(path.suffix + ".tmp").exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def new_manifest(*, symbol: str, run_key: str, directory: Path, config: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "bounded_level_first_analyzer_pilot_v1",
        "symbol": symbol.upper(),
        "run_key": run_key,
        "run_dir": str(directory),
        "status": "RUNNING",
        "verdict": None,
        "created_at": now_z(),
        "updated_at": now_z(),
        "config_hash": config.get("config_hash"),
        "clickhouse_writes": 0,
        "dashboard_modified": False,
        "outcomes_loaded": False,
    }


def mark(manifest: dict[str, Any], status: str, **extra: Any) -> None:
    manifest["status"] = status
    manifest["updated_at"] = now_z()
    manifest.update(extra)


def file_hashes(directory: Path, names: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for name in names:
        path = directory / name
        if path.is_file():
            out[name] = file_sha256(path)
    return out
