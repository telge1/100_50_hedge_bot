"""Stable identifier value objects for StrategySpec V2."""

from __future__ import annotations

import re
from dataclasses import dataclass

_STABLE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_CONTRACT_VERSION_PATTERN = re.compile(r"^[a-z][a-z0-9_]+/v[0-9]+$")


@dataclass(frozen=True, slots=True, kw_only=True)
class StableIdentifier:
    """Machine-readable identifier with frozen lowercase_snake_case syntax."""

    value: str

    def __post_init__(self) -> None:
        if type(self.value) is not str:
            raise TypeError("StableIdentifier.value must be exact str")
        if not self.value or self.value != self.value.strip():
            raise ValueError("StableIdentifier.value must be non-empty without padding")
        if not _STABLE_ID_PATTERN.fullmatch(self.value):
            raise ValueError(
                f"StableIdentifier.value has invalid syntax: {self.value!r}"
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class ContractVersion:
    """Version string for catalog or schema contracts, e.g. catalog/v1."""

    value: str

    def __post_init__(self) -> None:
        if type(self.value) is not str:
            raise TypeError("ContractVersion.value must be exact str")
        if not self.value or self.value != self.value.strip():
            raise ValueError("ContractVersion.value must be non-empty without padding")
        if not _CONTRACT_VERSION_PATTERN.fullmatch(self.value):
            raise ValueError(
                f"ContractVersion.value has invalid syntax: {self.value!r}"
            )
