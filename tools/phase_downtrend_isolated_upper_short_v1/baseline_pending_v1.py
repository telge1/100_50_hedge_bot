"""Create D41 pending orders from baseline short signal (unchanged entry/SL/TP rules)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
D41 = ROOT / "tools/phase_bt_d41_pending_lifecycle_v1"
if str(D41) not in sys.path:
    sys.path.insert(0, str(D41))

from candidate_identity_v1 import origin_set_identity_key  # noqa: E402
from pending_lifecycle_v1 import (  # noqa: E402
    STATE_PENDING,
    PendingOrderV1,
    compute_planned_levels,
    next_5m_open_after_close,
)
from signal_types_v1 import pool_key  # noqa: E402


def make_pending_from_baseline_signal(
    *,
    config: dict[str, Any],
    order_id: str,
    decision_close_utc: str,
    baseline_signal: Any,
    snapshot: dict[str, Any],
    selection_reason: str = "BASELINE_SHORT_SIGNAL",
) -> PendingOrderV1 | None:
    lo = float(baseline_signal.pool_lower)
    hi = float(baseline_signal.pool_upper)
    levels = compute_planned_levels(config, pool_lo=lo, pool_hi=hi, snapshot=snapshot)
    if levels.tp is None:
        return None
    origins = str(getattr(baseline_signal, "origin_ids", "") or "")
    pk = str(baseline_signal.pool_key)
    return PendingOrderV1(
        order_id=order_id,
        lifecycle_state=STATE_PENDING,
        decision_close_utc=decision_close_utc,
        intent_created_utc=decision_close_utc,
        earliest_eligible_execution_utc=next_5m_open_after_close(decision_close_utc),
        pool_lower=lo,
        pool_upper=hi,
        pool_key=pk,
        origin_ids=origins,
        origin_set_key=origin_set_identity_key(origins),
        planned_entry=levels.entry,
        planned_sl=levels.sl,
        planned_tp=levels.tp,
        selection_reason=selection_reason,
        scanner_setup_id=None,
    )
