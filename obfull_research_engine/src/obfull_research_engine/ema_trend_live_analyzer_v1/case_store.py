"""MAX_SIGNALS=1 case store (atomic JSON)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import MAX_ACCEPTED_CASES
from .schema import iso_z, utc_now


@dataclass
class CaseStore:
    path: Path
    max_cases: int = MAX_ACCEPTED_CASES
    cases: list[dict[str, Any]] = field(default_factory=list)

    def load(self) -> None:
        if self.path.is_file():
            self.cases = json.loads(self.path.read_text(encoding="utf-8")).get("cases", [])

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        payload = {"cases": self.cases, "updated_at": iso_z(utc_now())}
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def can_accept(self) -> bool:
        return len(self.cases) < self.max_cases

    def accept(self, case: dict[str, Any]) -> None:
        if not self.can_accept():
            raise RuntimeError("MAX_SIGNALS_REACHED")
        self.cases.append(case)
        self.save()
