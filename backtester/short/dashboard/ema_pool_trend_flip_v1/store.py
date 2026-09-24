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


def write_run(
    run_id: str,
    *,
    manifest: dict,
    preflight: dict,
    coverage: dict,
    trades: list,
    blocked: list,
    ignored: list,
    summary: dict,
) -> Path:
    root = artifacts_dir() / run_id
    if is_test_fixture_only(manifest) and "results/ema_pool_trend_flip_v1" in str(root):
        if not str(root).endswith("fixtures"):
            pass
    if not is_clickhouse_candle_source(manifest) and production_artifacts_dir() in root.parents:
        raise SourceRejected("cannot write non-ClickHouse run under results/ema_pool_trend_flip_v1/")
    root.mkdir(parents=True, exist_ok=True)
    _write_json(root / "preflight.json", preflight)
    _write_json(root / "coverage.json", coverage)
    _write_jsonl(root / "trades.jsonl", trades)
    _write_jsonl(root / "blocked_signals.jsonl", blocked)
    _write_jsonl(root / "ignored_duplicates.jsonl", ignored)
    index = {}
    for row in trades + blocked + ignored:
        sid = str(row.get("signal_id") or "")
        if sid:
            index.setdefault(sid, row)
    _write_json(root / "index_by_signal_id.json", index)
    _write_json(root / "summary.json", summary)
    _write_json(root / "manifest.json", manifest)
    return root


def publish_latest(run_dir: Path, manifest: dict) -> None:
    if is_test_fixture_only(manifest):
        raise SourceRejected("TEST_FIXTURE_ONLY cannot become latest")
    if not is_clickhouse_candle_source(manifest):
        raise SourceRejected("non-ClickHouse cannot become latest")
    if manifest.get("complete") is not True:
        raise SourceRejected("incomplete run cannot become latest")
    if (run_dir / "ABORTED.json").is_file():
        raise SourceRejected("aborted run cannot become latest")
    latest = artifacts_dir() / "latest"
    payload = {
        "run_id": manifest.get("run_id"),
        "artifact_dir": str(run_dir),
        "complete": True,
    }
    _write_json(latest / "pointer.json", payload)


def abort_run(run_id: str, reason: str) -> Path:
    root = artifacts_dir() / run_id
    root.mkdir(parents=True, exist_ok=True)
    _write_json(root / "ABORTED.json", {"run_id": run_id, "reason": reason})
    return root
