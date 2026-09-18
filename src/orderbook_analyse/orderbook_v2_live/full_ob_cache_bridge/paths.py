"""Safe dump path resolution — no traversal, no symlink escape, fixed root."""

from __future__ import annotations

import os
import re
from pathlib import Path

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class UnsafeDumpPath(ValueError):
    pass


def assert_safe_request_id(request_id: str) -> str:
    rid = str(request_id or "").strip()
    if not _SAFE_ID.match(rid):
        raise UnsafeDumpPath("invalid_request_id")
    if ".." in rid or "/" in rid or "\\" in rid:
        raise UnsafeDumpPath("invalid_request_id")
    return rid


def resolve_dump_dir(root: Path, request_id: str) -> Path:
    """Return root/request_id resolved under root; reject escapes."""
    rid = assert_safe_request_id(request_id)
    root_resolved = root.resolve(strict=False)
    target = (root_resolved / rid).resolve(strict=False)
    try:
        target.relative_to(root_resolved)
    except ValueError as exc:
        raise UnsafeDumpPath("path_escape") from exc
    if target.exists() and target.is_symlink():
        raise UnsafeDumpPath("symlink_dump_dir")
    # Parent components must not be symlinks escaping root.
    cur = target
    while True:
        parent = cur.parent
        if parent == cur:
            break
        if parent.exists() and parent.is_symlink():
            real = parent.resolve()
            try:
                real.relative_to(root_resolved)
            except ValueError as exc:
                raise UnsafeDumpPath("symlink_escape") from exc
        if parent == root_resolved:
            break
        cur = parent
    return target


def ensure_dump_root(root: Path, *, mode: int = 0o700) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, mode)
    if root.is_symlink():
        raise UnsafeDumpPath("dump_root_is_symlink")
    return root.resolve()
