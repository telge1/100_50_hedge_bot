"""Canonical density cache contract (BT-D8) — keys, checksums, provenance."""

from __future__ import annotations

import hashlib
import json
from typing import Any

CACHE_CONTRACT_VERSION = "bt_d8_canonical_density_cache_v1"
CACHE_ENTRY_VERSION = "bt_d8_cache_entry_v1"
CACHE_BUNDLE_VERSION = "bt_d8_density_cache_bundle_v1"
ADAPTER_CONTRACT_VERSION = "bt_d4_snapshot_payload_v1"


class DensityCacheError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def canonical_snapshot_json(snapshot: dict[str, Any]) -> str:
    """Stable serialization for checksums (no float rounding)."""
    return json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def snapshot_content_sha256(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_snapshot_json(snapshot).encode("utf-8")).hexdigest()


def make_cache_key(
    *,
    symbol: str,
    timeframe: str,
    pool_detail: int,
    min_unique_origins: int,
    decision_close_utc: str,
    pipeline_line_id: str,
    provider_config_hash: str,
    cache_contract_version: str = CACHE_CONTRACT_VERSION,
    adapter_contract_version: str = ADAPTER_CONTRACT_VERSION,
) -> str:
    """Structured key — not timestamp-only."""
    parts = [
        f"sym={symbol}",
        f"tf={timeframe}",
        f"pd={pool_detail}",
        f"depth={min_unique_origins}",
        f"close={decision_close_utc}",
        f"pipe={pipeline_line_id}",
        f"phash={provider_config_hash}",
        f"ccv={cache_contract_version}",
        f"acv={adapter_contract_version}",
    ]
    raw = "|".join(parts)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"{symbol}_{timeframe}_pd{pool_detail}_d{min_unique_origins}_{decision_close_utc.replace(':', '').replace('-', '')}_{digest}"


def validate_snapshot_shape(snapshot: dict[str, Any], *, decision_close_utc: str) -> None:
    required = (
        "decision_close_utc",
        "as_of_utc",
        "upper_bands",
        "lower_bands",
        "pipeline_line_id",
        "config_hash",
    )
    missing = [k for k in required if k not in snapshot]
    if missing:
        raise DensityCacheError("SNAPSHOT_SHAPE_INVALID", "missing snapshot fields", {"missing": missing})
    if snapshot["decision_close_utc"] != decision_close_utc:
        raise DensityCacheError(
            "SNAPSHOT_CLOSE_MISMATCH",
            "decision_close_utc does not match cache key",
            {"expected": decision_close_utc, "got": snapshot["decision_close_utc"]},
        )
    if snapshot["as_of_utc"] != decision_close_utc:
        raise DensityCacheError("AS_OF_CAUSALITY_VIOLATION", "as_of must equal decision close")
    for side in ("upper_bands", "lower_bands"):
        for b in snapshot.get(side) or []:
            for fld in ("lower_price", "upper_price", "unique_origin_count", "origin_ids", "side"):
                if fld not in b:
                    raise DensityCacheError(
                        "BAND_SHAPE_INVALID",
                        f"band missing {fld}",
                        {"side": side, "band": b},
                    )


def bands_for_parity(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for side_key, side in (("upper_bands", "upper"), ("lower_bands", "lower")):
        for b in snapshot.get(side_key) or []:
            rows.append(
                {
                    "side": side,
                    "lower_price": str(b["lower_price"]),
                    "upper_price": str(b["upper_price"]),
                    "unique_origin_count": int(b["unique_origin_count"]),
                    "origin_ids": str(b["origin_ids"]),
                }
            )
    return rows
