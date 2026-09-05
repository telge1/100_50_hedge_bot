"""Acceptance checkpoint scanner V2 — causal as-of second scan.

Does NOT mutate V1 edge_acceptance.py (old freeze source hash unchanged).
Acceptance evidence thresholds identical to V1 TrapAcceptConfig
(accept_min_consecutive_buckets / accept_min_seconds / edge band).

outcome_used_for_checkpoint_contract = false
outcome_used_for_entry_timestamp = false
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from orderbook_analyse.aggressor_efficiency_flip.models import SecondBucket, Trade
from orderbook_analyse.aggressor_efficiency_flip.timeutil import ensure_utc, floor_second, iso_z
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.bucket_semantics_v2 import (
    BucketDataStatus,
    CoverageWindow,
    classify_second,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.contracts import TrapAcceptConfig
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.edge_acceptance import (
    _relation_to_edge,
    _state_at,
    edge_band,
)
from orderbook_analyse.l2_wall_attack_discovery.models import tick_size


def evaluate_edge_acceptance_v2(
    *,
    buckets: dict[datetime, SecondBucket],
    trades: list[Trade],
    symbol: str,
    wall_side: Optional[str],
    edge_price: Optional[float],
    edge_confidence: str,
    decision_ts: datetime,
    aggressor_side: str,
    cfg: TrapAcceptConfig,
    coverage: CoverageWindow,
    ob200_seconds: set[datetime],
    as_of: Optional[datetime] = None,
    scan_horizon_s: int = 60,
) -> dict[str, Any]:
    """Scan every 1s from decision floor through horizon; emit causal checkpoints.

    Timestamp convention (manifested):
    - Trade bucket key `sec` covers half-open [sec, sec+1s).
    - Bucket fully observed at `bucket_close = sec+1s`.
    - Checkpoint at bucket_close is complete only if data_status is TRADE_PRESENT
      or VALID_EMPTY_BUCKET (checkpoint_eligible).
    - earliest_causal_entry_ts_v2 = acceptance_first_available_ts_v2
      (bucket_close of first eligible ACCEPTED) — execution not earlier than
      full availability of that closed second.
    """
    if edge_price is None or wall_side is None or edge_confidence in {"none", "low"} and edge_price is None:
        return _unknown("UNKNOWN_EDGE")
    if edge_confidence == "none":
        return _unknown("UNKNOWN_EDGE", note="edge_confidence=none")

    wall = wall_side.upper()
    lo, hi, tick = edge_band(float(edge_price), symbol, cfg)
    decision_ts = ensure_utc(decision_ts)
    horizon = as_of if as_of is not None else decision_ts + timedelta(seconds=scan_horizon_s)
    horizon = ensure_utc(horizon)

    first_break_ts = None
    consecutive_beyond = 0
    time_beyond = 0
    closed_beyond = 0
    max_ext = 0.0
    max_retrace = 0.0
    first_retest_ts = None
    retest_count = 0
    first_reclaim_ts = None
    was_beyond = False
    accepted = False
    reclaimed = False
    chop = False
    first_accepted_ts: Optional[datetime] = None
    acceptance_first_available_ts_v2: Optional[datetime] = None
    source_gap_hit = False

    second_rows: list[dict[str, Any]] = []
    cur = floor_second(decision_ts)
    scan_end = min(decision_ts + timedelta(seconds=scan_horizon_s), horizon)

    while cur + timedelta(seconds=1) <= scan_end:
        bucket_close = cur + timedelta(seconds=1)
        meta = classify_second(
            sec=cur,
            buckets=buckets,
            coverage=coverage,
            ob200_seconds=ob200_seconds,
            decision_ts=decision_ts,
        )
        status = BucketDataStatus(meta["data_status"])
        incomplete = False
        eligible = bool(meta["checkpoint_eligible"])

        if status in {BucketDataStatus.SOURCE_GAP, BucketDataStatus.INVALID_BUCKET}:
            source_gap_hit = True
            incomplete = True
            eligible = False
            # Do not forward-fill acceptance across unobserved gap.
            consecutive_beyond = 0
        elif status == BucketDataStatus.QUERY_BOUNDARY:
            incomplete = True
            eligible = False
            consecutive_beyond = 0
        elif status == BucketDataStatus.WARMUP_INCOMPLETE:
            incomplete = True
            eligible = False
        elif status == BucketDataStatus.VALID_EMPTY_BUCKET:
            # Valid quiet second: emit checkpoint; no new trade evidence.
            # Cannot count as a beyond closed bucket → consecutive streak breaks.
            consecutive_beyond = 0
        elif status == BucketDataStatus.TRADE_PRESENT:
            b = buckets[cur]
            px = b.last_price
            assert px is not None
            hi_px = b.high_price if b.high_price is not None else px
            lo_px = b.low_price if b.low_price is not None else px
            if wall == "ASK":
                ext = max(0.0, (hi_px - float(edge_price)) / float(edge_price) * 1e4)
                retr = max(0.0, (float(edge_price) - lo_px) / float(edge_price) * 1e4)
                beyond = (
                    _relation_to_edge(
                        wall_side=wall,
                        price=px,
                        edge=float(edge_price),
                        lo=lo,
                        hi=hi,
                        policy=cfg.exact_on_edge_policy,
                    )
                    == "BEYOND"
                )
            else:
                ext = max(0.0, (float(edge_price) - lo_px) / float(edge_price) * 1e4)
                retr = max(0.0, (hi_px - float(edge_price)) / float(edge_price) * 1e4)
                beyond = (
                    _relation_to_edge(
                        wall_side=wall,
                        price=px,
                        edge=float(edge_price),
                        lo=lo,
                        hi=hi,
                        policy=cfg.exact_on_edge_policy,
                    )
                    == "BEYOND"
                )
            max_ext = max(max_ext, ext)
            if was_beyond:
                max_retrace = max(max_retrace, retr)
            if beyond:
                if first_break_ts is None:
                    first_break_ts = bucket_close
                consecutive_beyond += 1
                time_beyond += 1
                closed_beyond += 1
                was_beyond = True
                if (
                    consecutive_beyond >= cfg.accept_min_consecutive_buckets
                    and time_beyond >= cfg.accept_min_seconds
                ):
                    accepted = True
                    if first_accepted_ts is None:
                        first_accepted_ts = bucket_close
            else:
                if was_beyond and not beyond:
                    if first_retest_ts is None:
                        first_retest_ts = bucket_close
                    retest_count += 1
                    if consecutive_beyond > 0 or was_beyond:
                        if first_reclaim_ts is None:
                            first_reclaim_ts = bucket_close
                        if not accepted:
                            reclaimed = True
                        else:
                            chop = True
                consecutive_beyond = 0

        state = _state_at(
            wall=wall,
            first_break_ts=first_break_ts,
            accepted=accepted,
            reclaimed=reclaimed,
            chop=chop,
            time_beyond=time_beyond,
        )
        entry_eligible = (
            eligible
            and not incomplete
            and state in {"ACCEPTED_ABOVE", "ACCEPTED_BELOW"}
        )
        if entry_eligible and acceptance_first_available_ts_v2 is None:
            acceptance_first_available_ts_v2 = bucket_close

        second_rows.append(
            {
                "checkpoint_ts": iso_z(bucket_close),
                "bucket_sec": iso_z(cur),
                "data_status": status.value,
                "trade_bucket_present": meta["trade_bucket_present"],
                "trade_count": meta["trade_count"],
                "book_observation_present": meta["book_observation_present"],
                "acceptance_state_at_ts": state,
                "acceptance_reason": meta["reason"],
                "checkpoint_eligible": eligible and not incomplete,
                "incomplete_scan": incomplete,
                "entry_eligible": entry_eligible,
                "first_break_ts": iso_z(first_break_ts),
                "first_accepted_lock_ts": iso_z(first_accepted_ts),
                "time_beyond_edge_seconds": time_beyond,
                "consecutive_buckets_beyond_edge": consecutive_beyond,
                "max_extension_bps": max_ext,
                "max_retrace_through_edge_bps": max_retrace,
                "source_gap_seen": source_gap_hit,
            }
        )
        cur += timedelta(seconds=1)

    final = _state_at(
        wall=wall,
        first_break_ts=first_break_ts,
        accepted=accepted,
        reclaimed=reclaimed,
        chop=chop,
        time_beyond=time_beyond,
    )

    # Hard invariant: final ACCEPTED without eligible ACCEPTED checkpoint ⇒ not tradable
    entry_eligible_event = acceptance_first_available_ts_v2 is not None
    if final in {"ACCEPTED_ABOVE", "ACCEPTED_BELOW"} and not entry_eligible_event:
        final_state_only = True
    else:
        final_state_only = False

    earliest_causal_entry_ts_v2 = acceptance_first_available_ts_v2  # == bucket_close availability

    # Discrete legacy-style checkpoints for comparison (5/10/30/60)
    discrete: dict[str, Any] = {}
    for cp in (5, 10, 30, 60):
        need = decision_ts + timedelta(seconds=cp)
        hit = next((r for r in second_rows if r["checkpoint_ts"] == iso_z(need)), None)
        if hit is None:
            discrete[f"cp_{cp}s"] = {
                "state": "UNKNOWN_DATA",
                "reason": "checkpoint_beyond_as_of_or_scan",
                "checkpoint_eligible": False,
            }
        else:
            discrete[f"cp_{cp}s"] = {
                "state": hit["acceptance_state_at_ts"],
                "checkpoint_ts": hit["checkpoint_ts"],
                "data_status": hit["data_status"],
                "checkpoint_eligible": hit["checkpoint_eligible"],
                "incomplete_scan": hit["incomplete_scan"],
                "entry_eligible": hit["entry_eligible"],
            }

    return {
        "acceptance_status": "OK",
        "edge_price": edge_price,
        "wall_side": wall,
        "tick_size": tick,
        "final_acceptance_state": final,
        "final_state_only_not_tradable": final_state_only,
        "entry_eligible": entry_eligible_event,
        "acceptance_first_available_ts_v2": iso_z(acceptance_first_available_ts_v2),
        "earliest_causal_entry_ts_v2": iso_z(earliest_causal_entry_ts_v2),
        "first_accepted_lock_ts": iso_z(first_accepted_ts),
        "first_break_ts": iso_z(first_break_ts),
        "second_checkpoints": second_rows,
        "checkpoints_discrete": discrete,
        "acceptance_state_at_5s": (discrete.get("cp_5s") or {}).get("state"),
        "acceptance_state_at_10s": (discrete.get("cp_10s") or {}).get("state"),
        "acceptance_state_at_30s": (discrete.get("cp_30s") or {}).get("state"),
        "acceptance_state_at_60s": (discrete.get("cp_60s") or {}).get("state"),
        "source_gap_seen": source_gap_hit,
        "n_seconds_scanned": len(second_rows),
        "n_entry_eligible_seconds": sum(1 for r in second_rows if r["entry_eligible"]),
        "contract_version": "acceptance_checkpoint_v2",
    }


def assert_final_accepted_has_checkpoint(result: dict[str, Any]) -> None:
    """Hard validator: final ACCEPTED_* without eligible checkpoint aborts tradability path."""
    final = result.get("final_acceptance_state")
    if final in {"ACCEPTED_ABOVE", "ACCEPTED_BELOW"}:
        if result.get("entry_eligible") and not result.get("acceptance_first_available_ts_v2"):
            raise AssertionError("entry_eligible without acceptance_first_available_ts_v2")
        if result.get("final_state_only_not_tradable") and result.get("entry_eligible"):
            raise AssertionError("final_state_only cannot be entry_eligible")
        # Violations of silent tradability: entry_eligible must imply a real checkpoint
        if result.get("entry_eligible"):
            rows = result.get("second_checkpoints") or []
            ok = any(
                r.get("entry_eligible")
                and r.get("acceptance_state_at_ts") == final
                and r.get("checkpoint_eligible")
                and not r.get("incomplete_scan")
                for r in rows
            )
            if not ok:
                raise AssertionError(
                    "INVARIANT_VIOLATION: entry_eligible without matching ACCEPTED checkpoint row"
                )


def _unknown(state: str, note: str | None = None) -> dict[str, Any]:
    out = {
        "acceptance_status": state,
        "final_acceptance_state": state,
        "final_state_only_not_tradable": False,
        "entry_eligible": False,
        "acceptance_first_available_ts_v2": None,
        "earliest_causal_entry_ts_v2": None,
        "second_checkpoints": [],
        "checkpoints_discrete": {},
        "contract_version": "acceptance_checkpoint_v2",
    }
    if note:
        out["note"] = note
    return out


CHECKPOINT_CONTRACT_V2 = {
    "version": "FROZEN_HIGH_ACCEPTED_CHECKPOINT_CONTRACT_V2",
    "outcome_used_for_checkpoint_contract": False,
    "outcome_used_for_entry_timestamp": False,
    "acceptance_evidence_thresholds_changed": False,
    "accept_min_consecutive_buckets": 3,
    "accept_min_seconds": 3,
    "scan": "every_1s_from_decision_floor",
    "VALID_EMPTY_emits_checkpoint": True,
    "SOURCE_GAP_forward_fill_forbidden": True,
    "entry_rule": (
        "acceptance_state_at_ts in {ACCEPTED_ABOVE, ACCEPTED_BELOW} "
        "AND checkpoint_eligible AND NOT incomplete_scan"
    ),
    "acceptance_first_available_ts_v2": "earliest bucket_close satisfying entry_rule",
    "earliest_causal_entry_ts_v2": (
        "equal to acceptance_first_available_ts_v2 (= bucket_close); "
        "no same-bucket execution before closed-second availability"
    ),
    "final_acceptance_state": "ex_post_scan_summary_not_an_entry_signal",
    "invariant": "final ACCEPTED without eligible ACCEPTED checkpoint => entry_eligible=false",
}
