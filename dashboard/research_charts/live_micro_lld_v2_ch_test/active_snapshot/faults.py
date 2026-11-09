"""Test-only fault injection for crash/resume simulation (isolated runner)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

FaultPoint = Literal[
    "before_write",
    "after_members_before_meta",
    "after_meta_before_publish",
    "before_publish_insert",
    "after_publish",
]


class SimulatedCrash(Exception):
    def __init__(self, point: FaultPoint) -> None:
        self.point = point
        super().__init__(f"simulated crash at {point}")


@dataclass
class FaultInjector:
    """Raise SimulatedCrash at configured points for a single close processing attempt."""

    points: set[FaultPoint] = field(default_factory=set)

    def maybe_raise(self, point: FaultPoint) -> None:
        if point in self.points:
            raise SimulatedCrash(point)

    @classmethod
    def for_scenario(cls, name: str) -> "FaultInjector":
        mapping: dict[str, set[FaultPoint]] = {
            "before_write": {"before_write"},
            "after_members": {"after_members_before_meta"},
            "before_publish": {"after_meta_before_publish"},
            "after_publish": {"after_publish"},
        }
        return cls(points=mapping.get(name, set()))
