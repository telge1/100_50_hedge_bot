"""Validated cache density session for BT-D7 (fail-closed, no recompute)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cache_contract_v1 import DensityCacheError
from cache_store_v1 import (
    cache_key_for_close,
    load_bundle_manifest,
    provenance_from_manifest,
    read_cache_entry,
)


class ValidatedDensityCacheSessionV1:
    def __init__(self, bundle_dir: Path, *, strategy_config: dict[str, Any] | None = None) -> None:
        self._bundle_dir = bundle_dir.resolve()
        self._strategy_config = strategy_config
        self._manifest = load_bundle_manifest(self._bundle_dir)
        self._provenance = provenance_from_manifest(self._manifest)
        self._validate_strategy_density_locks()

    def _validate_strategy_density_locks(self) -> None:
        if not self._strategy_config:
            return
        d = self._strategy_config.get("density") or {}
        if d.get("timeframe") != self._provenance.timeframe:
            raise DensityCacheError("DENSITY_TIMEFRAME_MISMATCH", "strategy vs cache timeframe")
        if int(d.get("pool_detail", -1)) != self._provenance.pool_detail:
            raise DensityCacheError("DENSITY_PD_MISMATCH", "strategy vs cache pool_detail")
        if int(d.get("min_unique_origins", -1)) != self._provenance.min_unique_origins:
            raise DensityCacheError("DENSITY_DEPTH_MISMATCH", "strategy vs cache depth")
        if d.get("method") != self._provenance.density_method:
            raise DensityCacheError("DENSITY_METHOD_MISMATCH", "strategy vs cache method")
        vref = d.get("validated_cache") or {}
        exp_hash = vref.get("required_config_hash")
        if exp_hash and exp_hash != self._provenance.required_config_hash:
            raise DensityCacheError("CONFIG_HASH_MISMATCH", "strategy required_config_hash vs bundle")

    def collect_at_close(self, decision_close_utc: str) -> dict[str, Any]:
        key = cache_key_for_close(self._provenance, decision_close_utc)
        return read_cache_entry(
            self._bundle_dir,
            key,
            self._provenance,
            decision_close_utc=decision_close_utc,
        )

    @property
    def manifest(self) -> dict[str, Any]:
        return self._manifest
