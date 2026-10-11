"""Read-only historical V2 pool snapshot loader (no engine, no CH pool compute)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from .catalog import ProductionCatalog, assert_month_allowed
from .errors import FrozenInputMismatchError, SnapshotMissingError, SnapshotValidationError
from .legacy_map import legacy_pools_from_snapshot_payload

if TYPE_CHECKING:
    from .mixed_catalog import MixedFrozenProductionCatalog


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def decision_close_to_stem(decision_close: datetime) -> str:
    return _utc(decision_close).strftime("%Y%m%dT%H%M%SZ")


def combo_folder(timeframe: str, pool_detail: int) -> str:
    return f"{timeframe}_PD{int(pool_detail)}"


def _is_mixed(catalog: ProductionCatalog) -> bool:
    return bool(getattr(catalog, "mixed_frozen", False))


def _assert_combo_allowed(catalog: ProductionCatalog, month: str, combo: str) -> None:
    if _is_mixed(catalog):
        from .mixed_catalog import assert_mixed_coverage

        assert_mixed_coverage(catalog, month, combo)  # type: ignore[arg-type]
        return
    assert_month_allowed(catalog, month, combo)


def validate_frozen_input(payload: dict, catalog: ProductionCatalog) -> None:
    frozen = payload.get("frozen_input") or {}
    sid = str(frozen.get("snapshot_id") or "")
    if _is_mixed(catalog):
        authorized = getattr(catalog, "authorized_frozen_input_ids", frozenset())
        if sid not in authorized:
            raise FrozenInputMismatchError(
                f"frozen snapshot_id {sid!r} not in catalog authorized set"
            )
        return
    if sid != catalog.frozen_input_id:
        raise FrozenInputMismatchError(
            f"frozen snapshot_id {sid!r} != catalog {catalog.frozen_input_id!r}"
        )


def validate_snapshot_payload(payload: dict, *, timeframe: str, pool_detail: int, stem: str) -> None:
    if str(payload.get("timeframe")) != timeframe:
        raise SnapshotValidationError("timeframe mismatch")
    if int(payload.get("pool_detail")) != int(pool_detail):
        raise SnapshotValidationError("pool_detail mismatch")
    dc = str(payload.get("decision_close") or "")
    if decision_close_to_stem(_utc(datetime.fromisoformat(dc.replace("Z", "+00:00")))) != stem:
        raise SnapshotValidationError("decision_close vs filename stem")


def _validate_active_fingerprint(payload: dict) -> None:
    from .decision_snapshot import validate_snapshot_active_state

    dc = _utc(datetime.fromisoformat(str(payload["decision_close"]).replace("Z", "+00:00")))
    validate_snapshot_active_state(
        payload,
        symbol=str(payload.get("symbol") or "DOGEUSDT"),
        timeframe=str(payload["timeframe"]),
        pool_detail=int(payload["pool_detail"]),
        decision_close=dc,
    )


class HistoricalLmV2PoolStore:
    """Loads legacy pool dicts from on-disk production JSON."""

    def __init__(self, catalog: ProductionCatalog) -> None:
        self.catalog = catalog
        self._path_cache: dict[tuple[str, int, str], Path] = {}

    def snapshot_path(self, timeframe: str, pool_detail: int, decision_close: datetime) -> Path:
        decision_close = _utc(decision_close)
        stem = decision_close_to_stem(decision_close)
        key = (timeframe, int(pool_detail), stem)
        if key in self._path_cache:
            return self._path_cache[key]

        month = decision_close.strftime("%Y-%m")
        combo = combo_folder(timeframe, pool_detail)
        _assert_combo_allowed(self.catalog, month, combo)
        path = self.catalog.results_root / month / combo / f"{stem}.json"
        self._path_cache[key] = path
        return path

    def load_payload(self, timeframe: str, pool_detail: int, decision_close: datetime) -> dict:
        path = self.snapshot_path(timeframe, pool_detail, decision_close)
        if not path.is_file():
            raise SnapshotMissingError(f"missing snapshot {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        stem = decision_close_to_stem(decision_close)
        validate_snapshot_payload(payload, timeframe=timeframe, pool_detail=pool_detail, stem=stem)
        _validate_active_fingerprint(payload)
        if _is_mixed(self.catalog):
            from .provenance_lookup import relative_dest_for

            rel = relative_dest_for(self.catalog.production_root, path)
            frozen_id = str((payload.get("frozen_input") or {}).get("snapshot_id") or "")
            self.catalog.provenance.verify_file(rel, path, frozen_id)  # type: ignore[union-attr]
        validate_frozen_input(payload, self.catalog)
        return payload

    def load_legacy_pools(self, timeframe: str, pool_detail: int, decision_close: datetime) -> list[dict]:
        payload = self.load_payload(timeframe, pool_detail, decision_close)
        return legacy_pools_from_snapshot_payload(payload, decision_close=_utc(decision_close))
