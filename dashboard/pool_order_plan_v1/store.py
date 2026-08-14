"""Atomic artifact IO. latest is updated only after a complete successful ClickHouse run."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable

from .config import artifacts_dir, production_artifacts_dir
from .schema import is_clickhouse_candle_source, is_test_fixture_only


class SourceRejected(RuntimeError):
    pass


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, default=str) + "\n")
    os.replace(tmp, path)


def _under_production_artifacts(root: Path) -> bool:
    prod = production_artifacts_dir().resolve()
    try:
        root.resolve().relative_to(prod)
        return True
    except ValueError:
        return False


def assert_publishable_clickhouse_manifest(manifest: dict) -> None:
    if is_test_fixture_only(manifest):
        raise SourceRejected("TEST_FIXTURE_ONLY plans cannot be published")
    if not is_clickhouse_candle_source(manifest):
        raise SourceRejected(
            "publish/latest aborted: pool_candle_source must be clickhouse "
            "with database=signal_generator table=candles_1m exchange=bybit "
            "interval=1m final=true is_closed=1"
        )


def write_run(run_id: str, *, manifest: dict, preflight: dict, coverage: dict, plans: list, outcomes: list, ignored: list) -> Path:
    root = artifacts_dir() / run_id
    if _under_production_artifacts(root) and not is_clickhouse_candle_source(manifest):
        raise SourceRejected(
            "cannot write non-ClickHouse pool plan under results/pool_order_plan_v1/"
        )
    root.mkdir(parents=True, exist_ok=True)
    _write_json(root / "preflight.json", preflight)
    _write_json(root / "coverage.json", coverage)
    _write_jsonl(root / "plans.jsonl", plans)
    _write_jsonl(root / "outcomes.jsonl", outcomes)
    _write_jsonl(root / "ignored_duplicates.jsonl", ignored)
    index = {}
    for row in outcomes:
        sid = str(row.get("signal_id") or "")
        if sid:
            index[sid] = row
    for row in ignored:
        sid = str(row.get("signal_id") or "")
        if sid:
            index[sid] = row
    _write_json(root / "index_by_signal_id.json", index)
    _write_json(root / "manifest.json", manifest)
    return root


def _run_manifest(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "manifest.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def run_is_aborted(run_dir: Path) -> bool:
    return (Path(run_dir) / "ABORTED.json").is_file()


def publish_latest(run_dir: Path) -> None:
    if run_is_aborted(run_dir):
        raise SourceRejected("aborted run cannot update latest")
    manifest = _run_manifest(run_dir)
    assert_publishable_clickhouse_manifest(manifest)
    latest = artifacts_dir() / "latest"
    tmp = artifacts_dir() / ".latest.tmp"
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    tmp.symlink_to(run_dir.name)
    os.replace(tmp, latest)


def load_latest_manifest() -> dict[str, Any]:
    latest = artifacts_dir() / "latest"
    path = latest / "manifest.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def load_latest_index() -> dict[str, Any]:
    if not is_clickhouse_candle_source(load_latest_manifest()):
        return {}
    latest = artifacts_dir() / "latest"
    index_path = latest / "index_by_signal_id.json"
    if not index_path.is_file():
        return {}
    return json.loads(index_path.read_text(encoding="utf-8"))


def abort_run(run_id: str, *, reason: str = "aborted") -> Path:
    """Mark an incomplete run. Never updates latest."""
    root = artifacts_dir() / run_id
    root.mkdir(parents=True, exist_ok=True)
    _write_json(
        root / "ABORTED.json",
        {"status": "aborted", "reason": reason, "run_id": run_id, "published": False},
    )
    return root


def artifact_available() -> bool:
    latest = artifacts_dir() / "latest"
    if not (latest.is_symlink() or latest.is_dir()):
        return False
    if run_is_aborted(latest):
        return False
    if not (latest / "manifest.json").is_file():
        return False
    return is_clickhouse_candle_source(load_latest_manifest())
