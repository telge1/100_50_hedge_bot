"""Compare two independent e2e output directories. Run metadata may differ; table bodies may not."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .io_zst import body_rows, read_jsonl_zst
from ..market_profile_lld_shared_event_materialization_v1.hashing import file_sha256

COMPARE_TABLES = (
    "states_100ms",
    "initial_book",
    "level_changes",
    "book_resets",
    "walls",
    "level_removals",
    "refill_candidates",
    "confirmed_refills",
    "touches",
    "detections",
)

IGNORE_BODY_FIELDS = {
    "config_hash",
    "input_hash",
    "run_key",
    "created_at",
    "elapsed_s",
    "peak_ram_gb",
}


def uncompressed_canonical_sha256(path: Path) -> tuple[str, int]:
    raw = __import__("zstandard").ZstdDecompressor().decompress(path.read_bytes())
    return hashlib.sha256(raw).hexdigest(), len(raw)


def _semantic_key(table: str, row: dict[str, Any]) -> tuple:
    if table == "states_100ms":
        return ("state", row.get("available_at") or row.get("bucket_end_exclusive"))
    if table == "initial_book":
        return ("initial", row.get("window_start"))
    if table == "level_changes":
        return (
            "lc",
            row.get("source_event_id"),
            row.get("event_time"),
            row.get("side"),
            row.get("price"),
            row.get("u"),
            row.get("seq"),
        )
    if table == "book_resets":
        return ("reset", row.get("source_event_id") or row.get("event_time"))
    if table == "walls":
        return ("wall", row.get("side"), row.get("price"))
    if table == "level_removals":
        return ("rem", row.get("source_event_id"), row.get("event_time"), row.get("side"), row.get("price"))
    if table in {"refill_candidates", "confirmed_refills"}:
        return ("ref", row.get("removal_event_id"), row.get("add_event_id"), row.get("side"), row.get("price"))
    if table == "touches":
        return ("touch", row.get("event_time"))
    if table == "detections":
        return ("det", row.get("event_time"))
    return ("row", json.dumps(row, sort_keys=True, default=str))


def _strip(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k not in IGNORE_BODY_FIELDS}


def compare_tables(dir_a: Path, dir_b: Path) -> dict[str, Any]:
    files: dict[str, Any] = {}
    semantic_diff_count = 0
    content_hash_equal = True
    for name in COMPARE_TABLES:
        pa = dir_a / f"{name}.jsonl.zst"
        pb = dir_b / f"{name}.jsonl.zst"
        rec: dict[str, Any] = {
            "exists_a": pa.is_file(),
            "exists_b": pb.is_file(),
            "n_a": 0,
            "n_b": 0,
            "compressed_sha256_a": None,
            "compressed_sha256_b": None,
            "uncompressed_sha256_a": None,
            "uncompressed_sha256_b": None,
            "content_hash_equal": False,
            "semantic_diff_count": 0,
            "first_divergence": None,
        }
        if pa.is_file() and pb.is_file():
            rec["compressed_sha256_a"] = file_sha256(pa)
            rec["compressed_sha256_b"] = file_sha256(pb)
            rec["uncompressed_sha256_a"], _ = uncompressed_canonical_sha256(pa)
            rec["uncompressed_sha256_b"], _ = uncompressed_canonical_sha256(pb)
            rec["content_hash_equal"] = rec["uncompressed_sha256_a"] == rec["uncompressed_sha256_b"]
            if not rec["content_hash_equal"]:
                content_hash_equal = False
            rows_a = [_strip(r) for r in body_rows(read_jsonl_zst(pa))]
            rows_b = [_strip(r) for r in body_rows(read_jsonl_zst(pb))]
            rec["n_a"] = len(rows_a)
            rec["n_b"] = len(rows_b)
            by_a = {_semantic_key(name, r): r for r in rows_a}
            by_b = {_semantic_key(name, r): r for r in rows_b}
            keys = sorted(set(by_a) | set(by_b), key=str)
            diffs = 0
            first = None
            for k in keys:
                a = by_a.get(k)
                b = by_b.get(k)
                if a != b:
                    diffs += 1
                    if first is None:
                        first = {"key": list(k), "a": a, "b": b}
            rec["semantic_diff_count"] = diffs
            rec["first_divergence"] = first
            semantic_diff_count += diffs
            if rec["compressed_sha256_a"] != rec["compressed_sha256_b"] and rec["content_hash_equal"]:
                rec["compressed_hash_note"] = (
                    "Compressed container hashes differ while uncompressed canonical bytes match."
                )
        elif pa.is_file() or pb.is_file():
            content_hash_equal = False
            semantic_diff_count += 1
            rec["first_divergence"] = {"key": name, "a": pa.is_file(), "b": pb.is_file()}
        files[name] = rec
    return {
        "semantic_diff_count": semantic_diff_count,
        "content_hash_equal": content_hash_equal,
        "files": files,
    }
