"""HTTP handlers for 51-coin coverage updates. Auth is injected by app.py."""

from __future__ import annotations

from typing import Any

try:
    from pydantic import BaseModel, ConfigDict

    class Universe51UpdateBody(BaseModel):
        model_config = ConfigDict(extra="forbid")
        symbols: list[str]

except Exception:  # pragma: no cover — pydantic v1

    class Universe51UpdateBody(BaseModel):  # type: ignore[no-redef]
        symbols: list[str]

        class Config:
            extra = "forbid"


def body_extra_fields(_body: Universe51UpdateBody) -> dict[str, Any]:
    return {}
