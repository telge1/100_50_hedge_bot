"""Local jsonl.zst IO. Does not import the smoke package."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import zstandard as zstd

from ..timeparse import format_utc_z
from . import SCHEMA_VERSION


def _line(obj: Any) -> bytes:
    return (json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str) + "\n").encode("utf-8")


def header(*, config_hash: str, input_hash: str, episode_id: str, table: str, symbol: str = "BTCUSDT") -> dict[str, Any]:
    return {
        "record_type": "header",
        "schema_version": SCHEMA_VERSION,
        "config_hash": config_hash,
        "input_hash": input_hash,
        "episode_id": episode_id,
        "table": table,
        "symbol": symbol,
    }


def atomic_write_jsonl_zst(path: Path, rows: list[dict[str, Any]]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = b"".join(_line(row) for row in rows)
    digest = hashlib.sha256(raw).hexdigest()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(zstd.ZstdCompressor(level=3).compress(raw))
    os.replace(tmp, path)
    return digest


def uncompressed_sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def read_jsonl_zst(path: Path) -> list[dict[str, Any]]:
    text = zstd.ZstdDecompressor().decompress(path.read_bytes()).decode("utf-8")
    rows = []
    for line in text.splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def body_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if r.get("record_type") != "header"]


def ts_cell(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return value
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    return format_utc_z(value)
