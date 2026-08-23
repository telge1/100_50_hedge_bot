"""Signal-timeframe contract for plugin descriptors."""

from __future__ import annotations

from dataclasses import dataclass

from orderbook_analyse.strategy_lab.models.contracts_v2.enums import SignalTimeframeModeV2


@dataclass(frozen=True, slots=True, kw_only=True)
class SignalTimeframeContractV2:
    """Signal-timeframe semantics for a plugin."""

    mode: SignalTimeframeModeV2
    reference_minutes: int
    allowed_minutes: tuple[int, ...]
    notes: str | None

    def __post_init__(self) -> None:
        if type(self.reference_minutes) is not int:
            raise TypeError("reference_minutes must be exact int")
        if type(self.allowed_minutes) is not tuple:
            raise TypeError("allowed_minutes must be a tuple")
