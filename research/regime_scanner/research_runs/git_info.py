"""Git revision metadata for research runs (no secrets)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GitInfo:
    commit: str | None
    branch: str | None
    working_tree_dirty: bool


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def collect_git_info(*, repo_root: Path | None = None) -> GitInfo:
    root = repo_root or _repo_root()
    commit = _run_git(["rev-parse", "HEAD"], root)
    branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], root)
    dirty_out = _run_git(["status", "--porcelain"], root)
    dirty = bool(dirty_out and dirty_out.strip())
    return GitInfo(commit=commit, branch=branch, working_tree_dirty=dirty)


def _run_git(args: list[str], root: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    text = (proc.stdout or "").strip()
    return text or None
