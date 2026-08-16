"""Persistent desired_state for live collector supervisor (RUNNING | STOPPED)."""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

DesiredStateValue = Literal["RUNNING", "STOPPED"]
DEFAULT_DESIRED = "STOPPED"


def default_desired_state_path() -> Path:
    return (
        Path(__file__).resolve().parents[4]
        / "results"
        / "live_collector"
        / "desired_state.json"
    )


@dataclass(slots=True)
class DesiredStateStore:
    path: Path
    default: DesiredStateValue = DEFAULT_DESIRED

    def __post_init__(self) -> None:
        self.path = Path(self.path)

    def read(self) -> DesiredStateValue:
        if not self.path.is_file():
            return self.default
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        value = str(raw.get("desired_state") or self.default).upper()
        if value not in ("RUNNING", "STOPPED"):
            raise ValueError(f"invalid desired_state in {self.path}: {value!r}")
        return value  # type: ignore[return-value]

    def write(self, desired_state: DesiredStateValue, *, reason: str = "") -> dict[str, Any]:
        value = str(desired_state).upper()
        if value not in ("RUNNING", "STOPPED"):
            raise ValueError(f"desired_state must be RUNNING or STOPPED, got {desired_state!r}")
        payload = {
            "desired_state": value,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "reason": reason or None,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(self.path.parent),
            delete=False,
            prefix=".desired_",
            suffix=".tmp",
        ) as tmp:
            tmp.write(text)
            tmp_path = Path(tmp.name)
        tmp_path.replace(self.path)
        return payload

    def to_dict(self) -> dict[str, Any]:
        if self.path.is_file():
            return json.loads(self.path.read_text(encoding="utf-8"))
        return {
            "desired_state": self.default,
            "updated_at": None,
            "reason": "default_unset",
        }
