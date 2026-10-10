"""Six research variants: 15m PD × floor guard on/off."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BaselineVariant:
    name: str
    pool_detail_15m: int
    floor_guard_enabled: bool
    pool_detail_4h: int = 10

    @property
    def run_key(self) -> str:
        guard = "guard_on" if self.floor_guard_enabled else "guard_off"
        return f"15m_pd{self.pool_detail_15m}_{guard}"


BASELINE_VARIANTS: tuple[BaselineVariant, ...] = (
    BaselineVariant("PD1_guard_off", 1, False),
    BaselineVariant("PD5_guard_off", 5, False),
    BaselineVariant("PD10_guard_off", 10, False),
    BaselineVariant("PD1_guard_on", 1, True),
    BaselineVariant("PD5_guard_on", 5, True),
    BaselineVariant("PD10_guard_on", 10, True),
)
