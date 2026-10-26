"""Phase 4X-AK optimized readback gate (read-only helpers)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
_AK = _ROOT / "tools/phase4xak_density_readback_optimization_v1/run_phase4xak.py"


def _load_ak():
    name = "phase4xak_mod"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _AK)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def parity_index() -> dict[str, dict[str, str]]:
    return _load_ak()._parity_index()


def optimized_readback_batch(
    hcfg: Any,
    stems: list[str],
    density_ref: dict[str, dict[str, str]],
    cache: Any,
    client: Any,
    database: str,
    reader: Any,
) -> list[dict[str, Any]]:
    return _load_ak().optimized_batch(hcfg, stems, density_ref, cache, client, database, reader)


def gate_ok(rows: list[dict[str, Any]]) -> bool:
    return all(
        r.get("json_ch_ok") and r.get("reader_ok") and r.get("active_ok") and r.get("density_ok") for r in rows
    )
