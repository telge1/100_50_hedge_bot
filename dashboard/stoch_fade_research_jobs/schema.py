"""Strict JSON body. Strategy id is optional and server-whitelisted only."""

from __future__ import annotations

from typing import Any

try:
    from pydantic import BaseModel, ConfigDict, field_validator

    class FrozenFadeJobBody(BaseModel):
        model_config = ConfigDict(extra="forbid")
        symbols: list[str]
        signal_start: str
        signal_end_exclusive: str
        strategy_id: str | None = None

        @field_validator("strategy_id")
        @classmethod
        def _strip_strategy(cls, value: str | None) -> str | None:
            if value is None:
                return None
            text = str(value).strip()
            return text or None

except Exception:  # pragma: no cover

    class FrozenFadeJobBody:  # type: ignore[no-redef]
        def __init__(self, **kwargs: Any) -> None:
            allowed = {"symbols", "signal_start", "signal_end_exclusive", "strategy_id"}
            extra = set(kwargs) - allowed
            if extra:
                raise ValueError("UNKNOWN_FIELDS")
            self.symbols = kwargs["symbols"]
            self.signal_start = kwargs["signal_start"]
            self.signal_end_exclusive = kwargs["signal_end_exclusive"]
            raw = kwargs.get("strategy_id")
            if raw is None or raw == "":
                self.strategy_id = None
            else:
                self.strategy_id = str(raw).strip() or None
