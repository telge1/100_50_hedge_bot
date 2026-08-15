from __future__ import annotations

from typing import Any

try:
    from pydantic import BaseModel, ConfigDict

    class FrozenFadeEvalBody(BaseModel):
        model_config = ConfigDict(extra="forbid")
        source_job_id: str

except Exception:  # pragma: no cover

    class FrozenFadeEvalBody:  # type: ignore[no-redef]
        def __init__(self, **kwargs: Any) -> None:
            extra = set(kwargs) - {"source_job_id"}
            if extra:
                raise ValueError("UNKNOWN_FIELDS")
            self.source_job_id = kwargs["source_job_id"]
