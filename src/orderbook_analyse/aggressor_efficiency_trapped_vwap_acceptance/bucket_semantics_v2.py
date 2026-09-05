"""Bucket / coverage semantics for Acceptance Contract Fix Refreeze V2.

Distinguishes VALID_EMPTY_BUCKET from SOURCE_GAP using dual evidence:
1. Successful CH trade-query window membership (canonical tape coverage).
2. Concurrent Raw-OB200 sample presence in the same 1s floor (book observed).

outcome_used_for_* = false for all design choices here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Iterable, Optional

from orderbook_analyse.aggressor_efficiency_flip.models import SecondBucket
from orderbook_analyse.aggressor_efficiency_flip.timeutil import ensure_utc, floor_second, iso_z
from orderbook_analyse.ob200_v3_raw_discovery.reconstruct import SampleRow


class BucketDataStatus(str, Enum):
    TRADE_PRESENT = "TRADE_PRESENT"
    VALID_EMPTY_BUCKET = "VALID_EMPTY_BUCKET"
    SOURCE_GAP = "SOURCE_GAP"
    QUERY_BOUNDARY = "QUERY_BOUNDARY"
    WARMUP_INCOMPLETE = "WARMUP_INCOMPLETE"
    INVALID_BUCKET = "INVALID_BUCKET"


@dataclass(frozen=True)
class CoverageWindow:
    """Half-open trade-query window that was successfully loaded."""

    load_start: datetime
    load_end: datetime
    query_ok: bool
    rows_loaded: int


def build_ob200_second_index(samples: Iterable[SampleRow]) -> set[datetime]:
    """Seconds with ≥1 OB200 sample (book observation present)."""
    out: set[datetime] = set()
    for s in samples:
        t = datetime.fromtimestamp(s.ts_ms / 1000.0, tz=timezone.utc)
        out.add(floor_second(t))
    return out


def classify_second(
    *,
    sec: datetime,
    buckets: dict[datetime, SecondBucket],
    coverage: CoverageWindow,
    ob200_seconds: set[datetime],
    decision_ts: datetime,
    warmup_before_decision_s: float = 0.0,
) -> dict[str, Any]:
    """Classify one floor-second under the V2 dual-evidence contract.

    Rules (ex ante, no outcomes):
    - Outside successful load window → QUERY_BOUNDARY
    - query_ok false → SOURCE_GAP
    - sec < decision_ts - warmup → WARMUP_INCOMPLETE (if warmup>0)
    - trade bucket with last_price → TRADE_PRESENT
    - no trades + OB200 sample in sec → VALID_EMPTY_BUCKET
    - no trades + no OB200 in sec → SOURCE_GAP
      (book not observed; cannot assert zero-trade vs missing tape)
    """
    sec = floor_second(ensure_utc(sec))
    load_start = ensure_utc(coverage.load_start)
    load_end = ensure_utc(coverage.load_end)
    decision_ts = ensure_utc(decision_ts)

    if not coverage.query_ok:
        return _row(sec, BucketDataStatus.SOURCE_GAP, False, 0, False, "trade_query_not_ok")

    if not (load_start <= sec < load_end):
        return _row(sec, BucketDataStatus.QUERY_BOUNDARY, False, 0, False, "outside_loaded_query_window")

    if warmup_before_decision_s > 0 and sec < decision_ts - timedelta(seconds=warmup_before_decision_s):
        return _row(sec, BucketDataStatus.WARMUP_INCOMPLETE, False, 0, False, "before_warmup")

    b = buckets.get(sec)
    trade_present = b is not None and b.last_price is not None and b.trade_count > 0
    trade_count = int(b.trade_count) if b is not None else 0
    book_obs = sec in ob200_seconds

    if trade_present:
        return _row(sec, BucketDataStatus.TRADE_PRESENT, True, trade_count, book_obs, "trades_in_second")

    if book_obs:
        return _row(
            sec,
            BucketDataStatus.VALID_EMPTY_BUCKET,
            False,
            0,
            True,
            "ob200_observed_zero_trades_in_loaded_window",
        )

    return _row(
        sec,
        BucketDataStatus.SOURCE_GAP,
        False,
        0,
        False,
        "no_trades_and_no_ob200_sample_in_second",
    )


def _row(
    sec: datetime,
    status: BucketDataStatus,
    trade_bucket_present: bool,
    trade_count: int,
    book_observation_present: bool,
    reason: str,
) -> dict[str, Any]:
    eligible_for_state_update = status in {
        BucketDataStatus.TRADE_PRESENT,
        BucketDataStatus.VALID_EMPTY_BUCKET,
    }
    return {
        "sec": iso_z(sec),
        "data_status": status.value,
        "trade_bucket_present": trade_bucket_present,
        "trade_count": trade_count,
        "book_observation_present": book_observation_present,
        "checkpoint_eligible": eligible_for_state_update,
        "reason": reason,
    }


BUCKET_SEMANTICS_CONTRACT = {
    "version": "FROZEN_HIGH_ACCEPTED_BUCKET_SEMANTICS_V2",
    "outcome_used_for_checkpoint_contract": False,
    "dual_evidence": [
        "successful_ch_public_trades_canonical_query_window",
        "raw_ob200_sample_present_in_same_1s_floor",
    ],
    "VALID_EMPTY_BUCKET": (
        "Second lies inside a successfully loaded CH trade query [load_start, load_end), "
        "has zero trades after dedupe, AND has ≥1 Raw-OB200 sample in the same 1s floor "
        "(book observed). Zero trades is then a valid observation."
    ),
    "SOURCE_GAP": (
        "Second has no trades AND no OB200 sample in that 1s floor (cannot prove "
        "zero-trade vs missing observation), OR trade query was not ok."
    ),
    "QUERY_BOUNDARY": "Second outside the successfully loaded trade-query window.",
    "WARMUP_INCOMPLETE": "Second before configured causal warmup relative to decision_ts.",
    "INVALID_BUCKET": "Reserved for contradictory/corrupt inputs.",
    "TRADE_PRESENT": "≥1 trade with last_price in the 1s bucket.",
    "residual_risk": (
        "Silent CH collector outages that still leave OB200 samples cannot be ruled out "
        "without a trade-feed heartbeat table; V2 requires OB200 concurrence for empty "
        "seconds to avoid treating unobserved tape as valid quiet."
    ),
}
