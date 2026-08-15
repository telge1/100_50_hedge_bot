"""Strict JSON body. Strategy is never taken from the browser."""

from __future__ import annotations

from typing import Any

try:
    from pydantic import BaseModel, ConfigDict

    class FrozenFadeJobBody(BaseModel):
        model_config = ConfigDict(extra="forbid")
        symbols: list[str]
        signal_start: str
        signal_end_exclusive: str

except Exception:  # pragma: no cover

    class FrozenFadeJobBody:  # type: ignore[no-redef]
        def __init__(self, **kwargs: Any) -> None:
            extra = set(kwargs) - {"symbols", "signal_start", "signal_end_exclusive"}
            if extra:
                raise ValueError("UNKNOWN_FIELDS")
            self.symbols = kwargs["symbols"]
            self.signal_start = kwargs["signal_start"]
            self.signal_end_exclusive = kwargs["signal_end_exclusive"]
