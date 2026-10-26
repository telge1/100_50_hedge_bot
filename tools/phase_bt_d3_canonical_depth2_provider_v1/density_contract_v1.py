"""Fail-closed validation for BT-D3 density contract."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONTRACT_PATH = Path(__file__).resolve().parent / "density_contract_v1.json"


class DensityContractError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass(frozen=True)
class DensityContractV1:
    contract_version: str
    density_method: str
    timeframe: str
    pool_detail: int
    min_unique_origins: int
    warmup_hours: int
    symbol_default: str

    @classmethod
    def load(cls, path: Path | None = None) -> DensityContractV1:
        raw = json.loads((path or CONTRACT_PATH).read_text(encoding="utf-8"))
        return cls(
            contract_version=str(raw["contract_version"]),
            density_method=str(raw["density_method"]),
            timeframe=str(raw["timeframe"]),
            pool_detail=int(raw["pool_detail"]),
            min_unique_origins=int(raw["min_unique_origins"]),
            warmup_hours=int(raw["warmup_hours"]),
            symbol_default=str(raw["symbol_default"]),
        )

    def to_manifest_dict(self) -> dict[str, Any]:
        return {
            "density_contract_version": self.contract_version,
            "density_method": self.density_method,
            "timeframe": self.timeframe,
            "pool_detail": self.pool_detail,
            "pool_pd": "PD1",
            "min_unique_origins": self.min_unique_origins,
            "warmup_hours": self.warmup_hours,
        }


def validate_request(
    *,
    symbol: str,
    timeframe: str,
    pool_detail: int,
    min_unique_origins: int,
    density_method: str | None = None,
    contract: DensityContractV1 | None = None,
) -> DensityContractV1:
    c = contract or DensityContractV1.load()
    if density_method is not None and density_method != c.density_method:
        raise DensityContractError(
            "DENSITY_METHOD_MISMATCH",
            f"expected {c.density_method}, got {density_method}",
        )
    if timeframe != c.timeframe:
        raise DensityContractError(
            "TIMEFRAME_NOT_SUPPORTED",
            f"BT-D3 provider supports only {c.timeframe}",
            {"requested": timeframe},
        )
    if pool_detail != c.pool_detail:
        raise DensityContractError(
            "POOL_DETAIL_NOT_SUPPORTED",
            f"BT-D3 provider supports only pool_detail={c.pool_detail}",
            {"requested": pool_detail},
        )
    if min_unique_origins != c.min_unique_origins:
        raise DensityContractError(
            "MIN_UNIQUE_ORIGINS_MISMATCH",
            f"fail-closed: min_unique_origins must be {c.min_unique_origins}",
            {"requested": min_unique_origins},
        )
    if symbol != c.symbol_default:
        raise DensityContractError(
            "SYMBOL_NOT_SUPPORTED",
            f"BT-D3 v1 pilot scope: {c.symbol_default}",
            {"requested": symbol},
        )
    return c
