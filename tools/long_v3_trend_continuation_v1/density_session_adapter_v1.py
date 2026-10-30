"""Density session adapters (frozen bundle read-only)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Protocol

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/phase_bt_d8_canonical_density_cache_v1"))
from cache_session_v1 import ValidatedDensityCacheSessionV1  # noqa: E402


class DensitySessionProtocol(Protocol):
    def collect_at_close(self, decision_close_utc: str) -> dict[str, Any]: ...


class FrozenBundleDensitySessionV1:
    def __init__(self, bundle_dir: Path) -> None:
        self._inner = ValidatedDensityCacheSessionV1(bundle_dir)
        self.manifest = self._inner.manifest
        self._close_set = {
            e["decision_close_utc"]
            for e in (self.manifest.get("entries") or [])
            if e.get("decision_close_utc")
        }

    def has_close(self, decision_close_utc: str) -> bool:
        return decision_close_utc in self._close_set

    def collect_at_close(self, decision_close_utc: str) -> dict[str, Any]:
        snap = dict(self._inner.collect_at_close(decision_close_utc))
        snap["density_transport"] = "validated_frozen_bundle"
        return snap

    def decision_closes(self) -> list[str]:
        m = self.manifest
        closes = sorted(e["decision_close_utc"] for e in (m.get("entries") or []) if e.get("decision_close_utc"))
        if closes:
            return closes
        f, l = m.get("decision_close_first"), m.get("decision_close_last")
        return [x for x in (f, l) if x]
