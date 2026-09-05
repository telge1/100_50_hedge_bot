"""Declarative frozen expansion / audit case contract for Entry Contract V2."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from orderbook_analyse.liquidity_pool_entry_contract_v2 import VALID_COMBINATIONS


class InvalidPoolApproachCombination(ValueError):
    verdict = "INVALID_POOL_APPROACH_COMBINATION"


CASE_SPEC_FIELDS = (
    "expansion_case_id",
    "source_candidate_id",
    "symbol",
    "reference_ts",
    "pool_id",
    "pool_side",
    "approach",
    "pool_timeframe",
    "pool_lower",
    "pool_upper",
    "pool_first_available_ts",
    "event_family_id",
    "exposure_status",
)


@dataclass(frozen=True)
class CaseSpec:
    expansion_case_id: str
    source_candidate_id: str
    symbol: str
    reference_ts: str
    pool_id: str
    pool_side: str
    approach: str
    pool_timeframe: str
    pool_lower: float
    pool_upper: float
    pool_first_available_ts: str
    event_family_id: str
    exposure_status: str

    def __post_init__(self) -> None:
        side = str(self.pool_side).upper()
        approach = str(self.approach).upper()
        object.__setattr__(self, "pool_side", side)
        object.__setattr__(self, "approach", approach)
        if (side, approach) not in VALID_COMBINATIONS:
            raise InvalidPoolApproachCombination(
                f"INVALID_POOL_APPROACH_COMBINATION: {side}/{approach}"
            )
        if float(self.pool_upper) <= float(self.pool_lower):
            raise InvalidPoolApproachCombination(
                f"INVALID_POOL_APPROACH_COMBINATION: upper<=lower "
                f"{self.pool_lower}/{self.pool_upper}"
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def case_spec_from_frozen_expansion_case(row: dict[str, Any]) -> CaseSpec:
    """Load exclusively from a frozen expansion case row (no CASE_XX logic)."""
    lower = row.get("pool_lower", row.get("pool_lower_edge"))
    upper = row.get("pool_upper", row.get("pool_upper_edge"))
    return CaseSpec(
        expansion_case_id=str(row["expansion_case_id"]),
        source_candidate_id=str(row["source_candidate_id"]),
        symbol=str(row["symbol"]).upper(),
        reference_ts=str(row["reference_ts"]),
        pool_id=str(row["pool_id"]),
        pool_side=str(row["pool_side"]),
        approach=str(row["approach"]),
        pool_timeframe=str(row["pool_timeframe"]),
        pool_lower=float(lower),
        pool_upper=float(upper),
        pool_first_available_ts=str(row["pool_first_available_ts"]),
        event_family_id=str(row["event_family_id"]),
        exposure_status=str(row["exposure_status"]),
    )


def case_spec_from_mapping(row: dict[str, Any]) -> CaseSpec:
    """Generic mapping loader (tests / regression adapters)."""
    return case_spec_from_frozen_expansion_case(row)
