"""Parse historical catalog JSON into ActiveStateSnapshot + catalog rows (no engine)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research_charts.historical_lmv2.chart_adapter import active_pool_records
from research_charts.historical_lmv2.decision_snapshot import parse_decision_close
from research_charts.historical_lmv2.provider import decision_close_to_stem

from ..active_snapshot.extract import ActiveStateSnapshot
from ..active_snapshot.fingerprint import active_state_fingerprint
from ..active_snapshot.writer import snapshot_id_for_state
from .fingerprint_parse import enrich_pool_row_from_json, pool_type_from_pool_id


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_dt(value: str) -> datetime:
    return _utc(parse_decision_close(value))


@dataclass(frozen=True)
class CatalogManifestContext:
    catalog_id: str
    engine_version: str
    history_policy: str
    authorized_frozen_input_ids: frozenset[str]


def load_manifest_context(catalog_root: Path) -> CatalogManifestContext:
    path = catalog_root / "production" / "catalog_manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return CatalogManifestContext(
        catalog_id=str(data.get("catalog_id") or ""),
        engine_version=str(data.get("engine_version") or ""),
        history_policy=str(data.get("history_policy") or ""),
        authorized_frozen_input_ids=frozenset(str(x) for x in (data.get("authorized_frozen_input_ids") or [])),
    )


def load_snapshot_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def json_to_active_state(
    payload: dict[str, Any],
    *,
    config_hash: str,
    engine_version: str,
    history_policy: str,
) -> ActiveStateSnapshot:
    symbol = str(payload["symbol"]).upper()
    timeframe = str(payload["timeframe"])
    pool_detail = int(payload["pool_detail"])
    active_ids = tuple(str(x) for x in (payload.get("active_pool_ids") or []))
    active_sides = tuple(str(x) for x in (payload.get("active_sides") or []))
    upper = sum(1 for s in active_sides if str(s).lower() == "upper")
    lower = sum(1 for s in active_sides if str(s).lower() == "lower")
    known_pool_count = len(payload.get("pools") or {})
    fp_expected = str(payload.get("active_state_fingerprint") or "")
    fp_actual = active_state_fingerprint(active_ids, active_sides)
    if fp_actual != fp_expected:
        raise ValueError("active_state_fingerprint mismatch")

    return ActiveStateSnapshot(
        symbol=symbol,
        timeframe=timeframe,
        pool_detail=pool_detail,
        config_hash=str(config_hash),
        engine_version=str(engine_version),
        engine_input_hash=str(payload.get("engine_input_hash") or ""),
        candle_load_start=_parse_dt(str(payload["candle_load_start"])),
        history_from_close=_parse_dt(str(payload["history_from_close"])),
        decision_close=_parse_dt(str(payload["decision_close"])),
        prefix_candle_count=int(payload.get("candle_count") or 0),
        prefix_candle_fingerprint=str(payload.get("prefix_candle_fingerprint") or ""),
        known_pool_count=int(known_pool_count),
        active_pool_ids=active_ids,
        active_sides=active_sides,
        active_upper_count=int(upper),
        active_lower_count=int(lower),
        active_state_fingerprint=fp_expected,
        history_policy=str(history_policy),
    )


def catalog_rows_from_payload(
    payload: dict[str, Any],
    *,
    snapshot_id: str,
    pipeline_line_id: str,
    history_policy: str,
) -> list[dict[str, Any]]:
    """One row per ACTIVE pool (full membership), matching pool_catalog insert shape."""
    state_dc = _parse_dt(str(payload["decision_close"]))
    symbol = str(payload["symbol"]).upper()
    timeframe = str(payload["timeframe"])
    pool_detail = int(payload["pool_detail"])
    rows: list[dict[str, Any]] = []
    for rec in active_pool_records(payload):
        enriched = enrich_pool_row_from_json(rec, active=True)
        rows.append(
            {
                "snapshot_id": snapshot_id,
                "pipeline_line_id": pipeline_line_id,
                "symbol": symbol,
                "timeframe": timeframe,
                "pool_detail": pool_detail,
                "decision_close": state_dc,
                "history_policy": history_policy,
                "pool_id": str(enriched["pool_id"]),
                "pool_type": pool_type_from_pool_id(str(enriched["pool_id"])),
                "side": str(enriched["side"]),
                "top_price": float(enriched["top_price"]),
                "bottom_price": float(enriched["bottom_price"]),
                "strength": float(enriched["strength"]),
                "strength_class": str(enriched["strength_class"]),
                "known_at": _parse_dt(str(enriched["known_at"])),
                "break_at": _parse_optional_dt(enriched.get("break_at")),
                "reclaim_level": enriched.get("reclaim_level"),
                "candidate_created_at": enriched.get("candidate_created_at"),
                "confirmed_at": enriched.get("confirmed_at"),
                "invalidated_at": enriched.get("invalidated_at"),
                "created_index": int(enriched["created_index"]),
                "row_fingerprint": str(enriched["row_fingerprint"]),
                "source_json_row_fingerprint": str(rec.get("row_fingerprint") or ""),
            }
        )
    return rows


def _parse_optional_dt(value: Any) -> datetime | None:
    if value is None or str(value).strip() in ("", "None"):
        return None
    return _parse_dt(str(value))


def snapshot_path_for_stem(
    catalog_root: Path,
    *,
    month: str,
    combo: str,
    stem: str,
) -> Path:
    return catalog_root / "production" / "results" / month / combo / f"{stem}.json"


def assert_filename_stem(payload: dict[str, Any], path: Path) -> None:
    dc = _parse_dt(str(payload["decision_close"]))
    stem = decision_close_to_stem(dc)
    if path.stem != stem:
        raise ValueError(f"filename stem {path.stem} != decision_close stem {stem}")


def build_import_bundle(
    path: Path,
    *,
    catalog_root: Path,
    config_hash: str,
    manifest: CatalogManifestContext,
    pipeline_line_id: str,
) -> dict[str, Any]:
    payload = load_snapshot_json(path)
    assert_filename_stem(payload, path)
    state = json_to_active_state(
        payload,
        config_hash=config_hash,
        engine_version=manifest.engine_version,
        history_policy=manifest.history_policy,
    )
    sid = snapshot_id_for_state(state)
    catalog = catalog_rows_from_payload(
        payload,
        snapshot_id=sid,
        pipeline_line_id=pipeline_line_id,
        history_policy=manifest.history_policy,
    )
    frozen = payload.get("frozen_input") or {}
    return {
        "path": path,
        "relative_dest": str(path.relative_to(catalog_root)).replace("\\", "/"),
        "payload": payload,
        "state": state,
        "snapshot_id": sid,
        "catalog_rows": catalog,
        "frozen_input_id": str(frozen.get("snapshot_id") or ""),
        "frozen_price_hash": str(frozen.get("price_hash") or ""),
        "frozen_revision_hash": str(frozen.get("revision_hash") or ""),
    }
