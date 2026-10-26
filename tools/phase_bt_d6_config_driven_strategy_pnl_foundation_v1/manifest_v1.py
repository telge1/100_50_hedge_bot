"""Deterministic run manifest with config hash."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def config_content_hash(config: dict[str, Any]) -> str:
    blob = json.dumps(config, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def build_run_manifest(
    config: dict[str, Any],
    *,
    run_kind: str,
    config_path: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ch = config_content_hash(config)
    manifest: dict[str, Any] = {
        "manifest_version": "bt_d6_run_manifest_v1",
        "run_kind": run_kind,
        "config_hash": ch,
        "config_version": config.get("config_version"),
        "strategy_id": config["strategy_identity"]["strategy_id"],
        "strategy_version": config["strategy_identity"]["strategy_version"],
        "frozen_source_reference": config["strategy_identity"]["frozen_source_reference"],
        "density_contract": config["density"],
        "upper_gap_min_pct": config["short_entry"]["upper_gap_min_pct"],
        "tp_selection_mode": config["take_profit"]["selection_mode"],
        "initial_equity_usdt": config["pnl"]["initial_equity_usdt"],
        "config_path": config_path,
        "created_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if extra:
        manifest.update(extra)
    manifest["manifest_id"] = hashlib.sha256(
        json.dumps({k: manifest[k] for k in sorted(manifest) if k != "created_at_utc"}, sort_keys=True).encode()
    ).hexdigest()[:16]
    return manifest


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
