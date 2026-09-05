"""Atomic JSON file writes with inter-process locking."""

from __future__ import annotations

import fcntl
import json
import os
import threading
from pathlib import Path
from typing import Any

from .contracts import sanitize_json

_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_GUARD = threading.Lock()


def _thread_lock(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _THREAD_GUARD:
        if key not in _THREAD_LOCKS:
            _THREAD_LOCKS[key] = threading.Lock()
        return _THREAD_LOCKS[key]


def _lock_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".lock")


def atomic_write_json(path: Path, payload: Any, *, indent: int = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sanitized = sanitize_json(payload)
    text = json.dumps(sanitized, indent=indent, sort_keys=True, allow_nan=False) + "\n"
    json.loads(text)
    lock_file = _lock_path(path)
    with _thread_lock(path):
        with lock_file.open("a+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            try:
                with tmp_path.open("w", encoding="utf-8") as handle:
                    handle.write(text)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_path, path)
                dir_fd = os.open(path.parent, os.O_DIRECTORY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            finally:
                if tmp_path.exists():
                    tmp_path.unlink(missing_ok=True)
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def read_json(path: Path, *, default: Any = None) -> Any:
    if not path.is_file():
        return default if default is not None else {}
    lock_file = _lock_path(path)
    with _thread_lock(path):
        with lock_file.open("a+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_SH)
            try:
                text = path.read_text(encoding="utf-8")
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise json.JSONDecodeError(
            f"invalid JSON in {path}: {exc.msg}",
            exc.doc,
            exc.pos,
        ) from exc


def read_json_lenient(path: Path, *, default: Any = None) -> tuple[Any, bool]:
    if not path.is_file():
        return (default if default is not None else {}, False)
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text), False
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        obj, end = decoder.raw_decode(text.lstrip())
        corrupted = end < len(text.strip())
        return obj, corrupted
