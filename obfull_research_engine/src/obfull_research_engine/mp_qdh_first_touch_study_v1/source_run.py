"""Resolve read-only historical MP batch run directories for First-Touch study.

Priority:
1. explicit ``source_run_dir`` argument / CLI ``--source-run-dir``
2. environment ``OBFULL_RESEARCH_SOURCE_RUN_DIR``
3. repo-relative default ``BATCH_RUN_REL`` only if it exists
4. otherwise clear error (or skip for optional integration tests)

Absolute server paths are run metadata only; content SHA256 is the content identity.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import BATCH_RUN_REL

SOURCE_RUN_ENV = "OBFULL_RESEARCH_SOURCE_RUN_DIR"

REQUIRED_BATCH_FILES: tuple[str, ...] = (
    "events_all.csv",
    "episodes.csv",
    "batch_windows.csv",
)


class SourceRunResolutionError(FileNotFoundError):
    """Raised when no usable historical batch directory can be resolved."""


@dataclass(frozen=True)
class ResolvedSourceRun:
    source_run_dir: Path
    source_manifest_path: Path | None
    source_event_file: Path
    source_episode_file: Path
    source_windows_file: Path
    source_event_file_sha256: str
    source_episode_file_sha256: str
    source_windows_file_sha256: str
    source_is_external: bool
    source_read_only_expected: bool
    resolution_source: str  # cli | env | default_repo_relative
    batch_run_rel_default: str

    def to_manifest_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k, v in list(d.items()):
            if isinstance(v, Path):
                d[k] = str(v)
        # Content identity used for parity (path-independent).
        d["source_content_id_sha256"] = content_id_sha256(
            self.source_event_file_sha256,
            self.source_episode_file_sha256,
            self.source_windows_file_sha256,
        )
        return d


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def content_id_sha256(event_sha: str, episode_sha: str, windows_sha: str) -> str:
    blob = f"events={event_sha}\nepisodes={episode_sha}\nwindows={windows_sha}\n".encode()
    return hashlib.sha256(blob).hexdigest()


def _validate_batch_dir(path: Path) -> Path:
    root = path.expanduser().resolve()
    if not root.is_dir():
        raise SourceRunResolutionError(f"source_run_dir is not a directory: {root}")
    missing = [name for name in REQUIRED_BATCH_FILES if not (root / name).is_file()]
    if missing:
        raise SourceRunResolutionError(
            f"source_run_dir {root} missing required files: {', '.join(missing)}"
        )
    return root


def resolve_source_run_dir(
    *,
    source_run_dir: str | Path | None = None,
    repo_root: str | Path | None = None,
    env: dict[str, str] | None = None,
    allow_missing_default: bool = False,
) -> ResolvedSourceRun | None:
    """Resolve batch dir. If ``allow_missing_default`` and only default missing, return None."""
    environ = env if env is not None else os.environ
    repo = Path(repo_root).resolve() if repo_root is not None else None

    chosen: Path | None = None
    how = ""
    if source_run_dir is not None and str(source_run_dir).strip():
        chosen = Path(str(source_run_dir))
        how = "cli"
    else:
        env_val = (environ.get(SOURCE_RUN_ENV) or "").strip()
        if env_val:
            chosen = Path(env_val)
            how = "env"
        elif repo is not None:
            default = (repo / BATCH_RUN_REL).resolve()
            if default.is_dir():
                chosen = default
                how = "default_repo_relative"
            elif allow_missing_default:
                return None
            else:
                raise SourceRunResolutionError(
                    f"No --source-run-dir / {SOURCE_RUN_ENV} and default missing: {default}"
                )
        else:
            raise SourceRunResolutionError(
                f"No source_run_dir, no {SOURCE_RUN_ENV}, and no repo_root for default path"
            )

    root = _validate_batch_dir(chosen)
    event = root / "events_all.csv"
    episode = root / "episodes.csv"
    windows = root / "batch_windows.csv"
    manifest = root / "run_manifest.json"
    is_external = True
    if repo is not None:
        try:
            root.relative_to(repo.resolve())
            is_external = how != "default_repo_relative"
        except ValueError:
            is_external = True
        if how == "default_repo_relative":
            is_external = False

    return ResolvedSourceRun(
        source_run_dir=root,
        source_manifest_path=manifest if manifest.is_file() else None,
        source_event_file=event,
        source_episode_file=episode,
        source_windows_file=windows,
        source_event_file_sha256=_sha256_file(event),
        source_episode_file_sha256=_sha256_file(episode),
        source_windows_file_sha256=_sha256_file(windows),
        source_is_external=bool(is_external),
        source_read_only_expected=True,
        resolution_source=how,
        batch_run_rel_default=BATCH_RUN_REL,
    )
