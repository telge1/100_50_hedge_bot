"""L1 V3S — always load manual drawings from canonical server JSON (research only)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROBE_DIR = Path(__file__).resolve().parent
CANONICAL_DRAWINGS_PATH = PROBE_DIR.parents[1] / "research_charts" / "user_data" / "drawings.json"

# Static template bundles — not used by the backtester; listed for drift diagnostics only.
STALE_COPY_PATHS: tuple[Path, ...] = (
    PROBE_DIR.parents[1] / "static" / "research_trp" / "user_data" / "drawings.json",
    PROBE_DIR.parents[1] / "static" / "js" / "research" / "user_data" / "drawings.json",
)


@dataclass
class DrawingRefreshResult:
    sha256: str
    text: str
    payload: dict[str, Any]
    drawing_ids: list[str]
    byte_size: int
    backup_path: Path
    snapshot_path: Path
    canonical_path: Path
    refreshed_at_utc: str
    warnings: list[str] = field(default_factory=list)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def validate_payload(payload: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    drawings = payload.get("drawings")
    if not isinstance(drawings, list):
        raise ValueError("drawings.json: missing or invalid 'drawings' array")
    seen: set[str] = set()
    for i, d in enumerate(drawings):
        if not isinstance(d, dict):
            warnings.append(f"row {i}: not an object")
            continue
        did = str(d.get("drawing_id") or "")
        if not did:
            warnings.append(f"row {i}: missing drawing_id")
        elif did in seen:
            warnings.append(f"duplicate drawing_id {did}")
        else:
            seen.add(did)
        if d.get("visible") is False:
            warnings.append(f"{did}: visible=false (excluded by line_from_drawing)")
    return warnings


def diagnose_stale_copies(canonical_sha: str) -> list[str]:
    out: list[str] = []
    for path in STALE_COPY_PATHS:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
            sha = _sha256(text)
            if sha != canonical_sha:
                n = len(json.loads(text).get("drawings") or [])
                out.append(f"stale_copy {path.name}: sha={sha[:16]}… count={n} (canonical {canonical_sha[:16]}…)")
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            out.append(f"stale_copy {path}: unreadable ({exc})")
    return out


class DrawingDriftError(RuntimeError):
    """Backtest blocked: authenticated health reported RAM/file drift."""


def assert_backtest_drawing_health(health: dict[str, Any] | None) -> list[str]:
    """Preflight: require health payload from GET /api/research/drawings/health when enforcing sync."""
    if health is None:
        return [
            "RAM/JSON drift not verified (no authenticated drawings/health); "
            "backtest uses file-only SSOT"
        ]
    if health.get("drift"):
        raise DrawingDriftError(
            f"drawing drift: ram_only={health.get('ids_only_in_ram')} "
            f"file_only={health.get('ids_only_in_file')} "
            f"diffs={len(health.get('ids_with_content_diff') or [])}"
        )
    return []


def refresh_drawings_for_run(
    out_dir: Path,
    *,
    run_id: str,
    prefix: str = "l1_v3s",
) -> DrawingRefreshResult:
    """Load canonical JSON only; backup + immutable run snapshot (never merge old snapshots)."""
    path = CANONICAL_DRAWINGS_PATH
    if not path.is_file():
        raise FileNotFoundError(f"canonical drawings missing: {path}")
    text = path.read_text(encoding="utf-8")
    payload = json.loads(text)
    warnings = validate_payload(payload)
    sha = _sha256(text)
    ids = sorted(str(d.get("drawing_id") or "") for d in payload.get("drawings") or [] if d.get("drawing_id"))

    out_dir.mkdir(parents=True, exist_ok=True)
    backup_path = out_dir / f"{prefix}_drawings_backup_{run_id}.json"
    backup_path.write_text(text, encoding="utf-8")
    snapshot_path = out_dir / f"{prefix}_drawings_snapshot_{sha[:16]}.json"
    snapshot_path.write_text(text, encoding="utf-8")

    warnings.extend(diagnose_stale_copies(sha))

    return DrawingRefreshResult(
        sha256=sha,
        text=text,
        payload=payload,
        drawing_ids=ids,
        byte_size=len(text.encode("utf-8")),
        backup_path=backup_path,
        snapshot_path=snapshot_path,
        canonical_path=path,
        refreshed_at_utc=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        warnings=warnings,
    )
