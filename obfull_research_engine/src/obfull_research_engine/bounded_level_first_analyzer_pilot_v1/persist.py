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

# Writes must never land in the frozen main checkout research tree.
FORBIDDEN_WRITE_ROOT = Path("/home/telgenbuescher/projects/orderbook_analyse").resolve()
WORKTREE_SAFE_ROOT = Path("/home/telgenbuescher/projects/orderbook_analyse_btc30m_v1").resolve()


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


def assert_safe_results_root(results_root: Path | str) -> Path:
    """
    Resolve and validate an explicit LF1 output root.

    Refuses paths that resolve into the frozen main checkout
    (/home/telgenbuescher/projects/orderbook_analyse), including via symlinks.
    """
    root = Path(results_root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    resolved = root.resolve()
    forbidden = FORBIDDEN_WRITE_ROOT
    if resolved == forbidden or forbidden in resolved.parents or str(resolved).startswith(str(forbidden) + os.sep):
        raise ValueError(
            f"results_root resolves into forbidden checkout {forbidden}: {resolved}. "
            "Use an isolated path under the btc30m worktree "
            "(e.g. .../obfull_research_engine/runs/full_inputs/...)."
        )
    if WORKTREE_SAFE_ROOT.exists():
        if not (
            resolved == WORKTREE_SAFE_ROOT
            or WORKTREE_SAFE_ROOT in resolved.parents
            or str(resolved).startswith(str(WORKTREE_SAFE_ROOT) + os.sep)
        ):
            raise ValueError(
                f"results_root must stay under worktree {WORKTREE_SAFE_ROOT}, got {resolved}"
            )
    default_resolved = RESULTS_ROOT.resolve()
    if resolved == default_resolved or default_resolved in resolved.parents:
        if RESULTS_ROOT.is_symlink() or str(default_resolved).startswith(str(forbidden)):
            raise ValueError(
                f"refusing default/symlinked RESULTS_ROOT that resolves to {default_resolved}; "
                "pass an explicit isolated --results-root"
            )
    return resolved


def resolve_results_root(results_root: Path | str | None) -> Path:
    """Return writable results root; require explicit root when default is unsafe."""
    if results_root is not None:
        return assert_safe_results_root(results_root)
    if RESULTS_ROOT.exists() or RESULTS_ROOT.is_symlink():
        resolved = RESULTS_ROOT.resolve()
        if str(resolved).startswith(str(FORBIDDEN_WRITE_ROOT)):
            raise ValueError(
                "default RESULTS_ROOT is unsafe (symlink/path into "
                f"{FORBIDDEN_WRITE_ROOT}). Pass --results-root pointing at an "
                "isolated worktree directory."
            )
    return assert_safe_results_root(RESULTS_ROOT)


def run_dir(symbol: str, run_key: str, *, results_root: Path | str | None = None) -> Path:
    root = resolve_results_root(results_root)
    return root / symbol.upper() / run_key


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
