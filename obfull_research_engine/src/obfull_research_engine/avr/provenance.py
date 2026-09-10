"""AVR Dashboard source provenance for OBFULL research adapter."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any

SRH_ROOT = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev")
AVR_DASHBOARD_DIR = SRH_ROOT / "dashboard"
AVR_PACKAGE_DIR = AVR_DASHBOARD_DIR / "footprint_candles"

AVR_SOURCE_FILES = (
    "response_contracts.py",
    "response_baseline.py",
    "response_engine.py",
    "response_service.py",
    "contracts.py",
    "coverage.py",
)


def ensure_avr_import_path() -> Path:
    """Put SRH ``dashboard/`` on sys.path so ``import footprint_candles`` works."""
    p = str(AVR_DASHBOARD_DIR)
    if p not in sys.path:
        sys.path.insert(0, p)
    return AVR_DASHBOARD_DIR


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def combined_source_code_hash(files: dict[str, str]) -> str:
    blob = "|".join(f"{k}:{v}" for k, v in sorted(files.items()))
    return hashlib.sha256(blob.encode()).hexdigest()


def collect_provenance(*, srh_commit: str | None = None, srh_branch: str | None = None) -> dict[str, Any]:
    ensure_avr_import_path()
    from footprint_candles.response_contracts import (  # noqa: WPS433
        RESPONSE_ENGINE_ID,
        RESPONSE_ENGINE_VERSION,
        BASELINE_LOOKBACK_S,
        MIN_BASELINE_VALID_SECONDS,
        PRIMARY_WINDOW_S,
        DEFAULT_THRESHOLDS,
        config_hash,
    )
    from footprint_candles.contracts import TRADES_FQN  # noqa: WPS433

    per_file = {}
    for name in AVR_SOURCE_FILES:
        path = AVR_PACKAGE_DIR / name
        per_file[name] = {
            "path": str(path),
            "sha256": file_sha256(path),
            "bytes": path.stat().st_size,
            "exists": path.exists(),
        }
    code_hash = combined_source_code_hash({k: v["sha256"] for k, v in per_file.items()})
    return {
        "reuse_mode": "DIRECT_IMPORT_DASHBOARD_PURE_FUNCTIONS",
        "avr_source_root": str(AVR_PACKAGE_DIR),
        "srh_root": str(SRH_ROOT),
        "srh_commit": srh_commit,
        "srh_branch": srh_branch,
        "avr_source_contract_version": RESPONSE_ENGINE_VERSION,
        "avr_engine_id": RESPONSE_ENGINE_ID,
        "avr_config_hash": config_hash(DEFAULT_THRESHOLDS),
        "avr_source_code_hash": code_hash,
        "public_trades_fqn": TRADES_FQN,
        "baseline_lookback_s": BASELINE_LOOKBACK_S,
        "min_baseline_valid_seconds": MIN_BASELINE_VALID_SECONDS,
        "primary_window_s": PRIMARY_WINDOW_S,
        "available_at_policy": "second_ts + 1s; features use trade_ts < available_at",
        "source_files": per_file,
        "no_dashboard_http": True,
        "no_canvas_import": True,
        "formula_fork": False,
    }
