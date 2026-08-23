"""Atomic per-coin checkpoints and resume helpers."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .constants import CHECKPOINT_SCHEMA_VERSION, ENTRY_RULE, LEGACY_ENTRY_RULES


class IncompatibleCheckpointError(ValueError):
    """Resume blocked: checkpoint uses a different/legacy entry rule."""


def ensure_dirs(output_dir: Path) -> dict[str, Path]:
    paths = {
        "root": output_dir,
        "preflight": output_dir / "preflight",
        "checkpoints": output_dir / "checkpoints",
        "failures": output_dir / "failures",
        "reports": output_dir / "reports",
    }
    for p in paths.values():
        p.mkdir(parents=True, exist_ok=True)
    return paths


def checkpoint_path(checkpoints_dir: Path, symbol: str) -> Path:
    return checkpoints_dir / f"{symbol.upper()}.json"


def failure_path(failures_dir: Path, symbol: str) -> Path:
    return failures_dir / f"{symbol.upper()}.json"


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, default=str)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def checkpoint_entry_compatible(rec: dict[str, Any] | None) -> bool:
    if not rec:
        return False
    rule = rec.get("entry_rule")
    if rule is None:
        return False
    if str(rule) in LEGACY_ENTRY_RULES:
        return False
    return str(rule) == ENTRY_RULE


def write_coin_checkpoint(
    checkpoints_dir: Path,
    *,
    symbol: str,
    status: str,
    payload: dict[str, Any],
) -> Path:
    path = checkpoint_path(checkpoints_dir, symbol)
    body = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "symbol": symbol.upper(),
        "status": status,  # COMPLETE | FAILED | FAILED_PARITY | PARTIAL
        "entry_rule": ENTRY_RULE,
        **payload,
    }
    # Ensure payload cannot override canonical fields
    body["status"] = status
    body["entry_rule"] = ENTRY_RULE
    atomic_write_json(path, body)
    return path


def write_coin_failure(failures_dir: Path, *, symbol: str, error: str, detail: dict[str, Any] | None = None) -> Path:
    path = failure_path(failures_dir, symbol)
    atomic_write_json(
        path,
        {
            "symbol": symbol.upper(),
            "status": "FAILED",
            "entry_rule": ENTRY_RULE,
            "error": error,
            "detail": detail or {},
        },
    )
    return path


def load_checkpoint(checkpoints_dir: Path, symbol: str) -> dict[str, Any] | None:
    path = checkpoint_path(checkpoints_dir, symbol)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def is_complete_checkpoint(rec: dict[str, Any] | None) -> bool:
    return bool(rec) and str(rec.get("status")) == "COMPLETE" and checkpoint_entry_compatible(rec)


def list_complete_symbols(checkpoints_dir: Path) -> list[str]:
    out = []
    for p in sorted(checkpoints_dir.glob("*.json")):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if is_complete_checkpoint(rec):
            out.append(str(rec.get("symbol") or p.stem).upper())
    return out


def find_incompatible_checkpoints(
    checkpoints_dir: Path,
    symbols: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Return checkpoints that block resume under the current ENTRY_RULE.

    - Explicit legacy ``>``-rule checkpoints are always incompatible.
    - ``COMPLETE`` checkpoints missing/ mismatched ``entry_rule`` are incompatible.
    - Non-complete checkpoints without a rule may be reprocessed (not blocking).
    """
    bad: list[dict[str, Any]] = []
    paths = (
        [checkpoint_path(checkpoints_dir, s) for s in symbols]
        if symbols is not None
        else sorted(checkpoints_dir.glob("*.json"))
    )
    for path in paths:
        if not path.exists():
            continue
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            bad.append({"path": str(path), "error": f"unreadable:{exc}", "compatible": False})
            continue
        if checkpoint_entry_compatible(rec):
            continue
        rule = rec.get("entry_rule")
        status = str(rec.get("status") or "")
        legacy = str(rule) in LEGACY_ENTRY_RULES if rule is not None else False
        blocks = legacy or status == "COMPLETE"
        if not blocks:
            continue
        bad.append(
            {
                "symbol": rec.get("symbol") or path.stem,
                "path": str(path),
                "status": status,
                "entry_rule": rule,
                "expected_entry_rule": ENTRY_RULE,
                "legacy": legacy,
                "compatible": False,
            }
        )
    return bad


def assert_resume_checkpoints_compatible(checkpoints_dir: Path, eligible: list[str]) -> None:
    """Reject resume when any eligible symbol has an incompatible checkpoint (no migration)."""
    bad = find_incompatible_checkpoints(checkpoints_dir, eligible)
    if not bad:
        return
    raise IncompatibleCheckpointError(
        "Incompatible checkpoints for current entry_rule="
        f"{ENTRY_RULE}; refuse resume without migration. "
        f"details={bad}"
    )


def symbols_to_process(
    eligible: list[str],
    checkpoints_dir: Path,
    *,
    resume: bool,
) -> tuple[list[str], list[str]]:
    """Return (todo, skipped_complete). Resume skips COMPLETE+compatible only."""
    if resume:
        assert_resume_checkpoints_compatible(checkpoints_dir, eligible)
    if not resume:
        return list(eligible), []
    skipped = []
    todo = []
    for sym in eligible:
        rec = load_checkpoint(checkpoints_dir, sym)
        if is_complete_checkpoint(rec):
            skipped.append(sym)
        else:
            todo.append(sym)
    return todo, skipped
