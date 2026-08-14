"""Git / SHA pinning for planner and aggregation modules."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any, Iterable


class PinMismatch(RuntimeError):
    """Planner or aggregation identity does not match the freeze pin."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_git(root: Path, args: list[str]) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def inspect_repo(root: Path, relevant: Iterable[Path]) -> dict[str, Any]:
    root = root.resolve()
    files = [p.resolve() for p in relevant if p.exists()]
    hashes = {str(p.relative_to(root) if root in p.parents or p.parent == root else p): sha256_file(p) for p in files}
    commit = _run_git(root, ["rev-parse", "HEAD"])
    porcelain = _run_git(root, ["status", "--porcelain", "--"] + [str(p) for p in files])
    dirty_files = []
    if porcelain:
        for line in porcelain.splitlines():
            dirty_files.append(line[3:].strip() if len(line) > 3 else line)
    return {
        "root": str(root),
        "commit": commit,
        "is_git": commit is not None,
        "dirty": bool(dirty_files),
        "dirty_files": dirty_files,
        "file_hashes": hashes,
    }


def planner_relevant_files(planner_root: Path) -> list[Path]:
    liq = planner_root / "research" / "liquidity"
    files = sorted(liq.glob("*.py"))
    test = planner_root / "tests" / "test_order_planner.py"
    if test.exists():
        files.append(test)
    return files


def aggregation_relevant_files(sg_root: Path) -> list[Path]:
    out = [sg_root / "src" / "signal_generator" / "timeframes.py"]
    return [p for p in out if p.exists()]
