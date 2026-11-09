"""Readback gate exceptions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .readback_parity import ParityReport


@dataclass
class ReadbackGateFailure(Exception):
    failures: list

    def __str__(self) -> str:
        lines = []
        for r in self.failures:
            lines.append(f"{r.stem}: " + "; ".join(r.mismatches))
        return "pilot readback gate failed:\n" + "\n".join(lines)
