"""Input event models for trap/acceptance stage 1."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass
class InputEvent:
    event_id: str
    symbol: str
    direction: str  # LONG | SHORT | UNKNOWN
    wall_side: Optional[str]  # BID | ASK | None
    edge_price: Optional[float]
    edge_source: str  # measured_pool | synthetic_fixture | inferred_direction_only | none
    edge_confidence: str  # high | medium | low | none
    flow_start_ts: datetime
    flow_end_ts: datetime
    decision_ts: datetime  # earliest causal decision (typically post-flow close t2)
    reference_price: Optional[float]
    data_quality: str  # OK | DEGRADED | UNKNOWN
    source: str
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("flow_start_ts", "flow_end_ts", "decision_ts"):
            v = getattr(self, k)
            d[k] = v.isoformat().replace("+00:00", "Z") if v is not None else None
        return d
