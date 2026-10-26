"""Immutable config experiment layout (no parameter sweeps)."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from manifest_v1 import build_run_manifest, config_content_hash, write_manifest

ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "results" / "phase_bt_d6_config_experiments_v1"


def prepare_experiment(
    config: dict[str, Any],
    experiment_name: str,
    *,
    source_config_path: Path | None = None,
) -> Path:
    """Copy config immutably into results/<name>_<hash>/"""
    ch = config_content_hash(config)[:12]
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in experiment_name)[:48]
    out_dir = RESULTS / f"{safe}_{ch}_{ts}"
    out_dir.mkdir(parents=True, exist_ok=False)
    cfg_copy = out_dir / "config.immutable.json"
    cfg_copy.write_text(json.dumps(config, indent=2), encoding="utf-8")
    if source_config_path and source_config_path.is_file():
        shutil.copy2(source_config_path, out_dir / "config.source_path.txt")
        (out_dir / "config.source_path.txt").write_text(str(source_config_path), encoding="utf-8")
    manifest = build_run_manifest(
        config,
        run_kind="config_experiment_stub",
        config_path=str(cfg_copy),
        extra={"experiment_name": experiment_name},
    )
    write_manifest(out_dir / "run_manifest.json", manifest)
    return out_dir
