from __future__ import annotations

from typing import Any

try:
    from pydantic import BaseModel, ConfigDict

    class FrozenFadeEvalBody(BaseModel):
        model_config = ConfigDict(extra="forbid")
        source_job_id: str
        outcome_data_end: str | None = None

except Exception:  # pragma: no cover

    class FrozenFadeEvalBody:  # type: ignore[no-redef]
        def __init__(self, **kwargs: Any) -> None:
            extra = set(kwargs) - {"source_job_id", "outcome_data_end"}
            if extra:
                raise ValueError("UNKNOWN_FIELDS")
            self.source_job_id = kwargs["source_job_id"]
            self.outcome_data_end = kwargs.get("outcome_data_end")
