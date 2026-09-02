"""Corrected outside excursions and reclaim events with invariant audit."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from .config import iso_z
from .profile_edge_state import (
    STATE_BETWEEN_LOWER,
    STATE_BETWEEN_UPPER,
    STATE_INSIDE_BOTH,
    STATE_OUTSIDE_ABOVE,
    STATE_OUTSIDE_BELOW,
    price_to_tick,
)
from .profile_state_episodes import (
    END_REASON_STATE_CHANGE,
    END_REASON_WINDOW_END,
    episode_time_span,
    episodes_by_state,
)

from .edge_visits import build_edge_visits, assign_episode_to_visit
from .same_timestamp_audit import AMBIGUOUS_MULTI_STATE, ORDERING_NOT_EXCHANGE_PROVEN

OUTSIDE_RECLAIM_CONTRACT = "outside_reclaim_v1"
RECLAIM_EVENT_CONTRACT_V2 = "reclaim_event_contract_v2"
RECLAIM_EVENT_CONTRACT_V3 = "reclaim_event_contract_v3"
INTERPRETATION_NOT_EVALUATED = "NOT_EVALUATED"
ORDERING_EPISODE_SCOPED = "EPISODE_SCOPED_TRANSITION"
ORDERING_SAME_TS_SHARED = "SAME_TIMESTAMP_SHARED_CROSS"
ORDERING_UNAMBIGUOUS = "UNAMBIGUOUS_TIMESTAMP_ORDER"
SEQUENCE_RAW = "RAW_DETERMINISTIC_ORDER_ONLY"
EVENT_CANONICAL = "CANONICAL_RECLAIM_OBSERVED"
EVENT_AMBIGUOUS = "AMBIGUOUS_RECLAIM_CANDIDATE"
INELIG_AMBIGUOUS_MULTI_STATE = "AMBIGUOUS_MULTI_STATE"
INELIG_SAME_TS_NO_SEQUENCE = "SAME_TIMESTAMP_WITHOUT_EXCHANGE_SEQUENCE"
INELIG_LEGACY_TRADE_ID_ORDER = "LEGACY_TRADE_ID_ORDER_ONLY"
RECLAIM_EVENT_RETURNED_BELOW_UPPER = "RETURNED_BELOW_UPPER_OUTER_EDGE"
RECLAIM_EVENT_RETURNED_ABOVE_LOWER = "RETURNED_ABOVE_LOWER_OUTER_EDGE"
RETEST_NO_POST = "NO_POST_RECLAIM_OBSERVATION"


def build_outside_excursions(
    episodes: list[dict[str, Any]],
    transitions: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    edges: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build outside excursions with at most one chronologically valid reclaim each."""
    outside_eps = episodes_by_state(episodes, STATE_OUTSIDE_ABOVE, STATE_OUTSIDE_BELOW)
    excursions: list[dict[str, Any]] = []
    reclaims: list[dict[str, Any]] = []

    for oep in outside_eps:
        start, end = episode_time_span(oep)
        exc_id = f"exc_{uuid.uuid4().hex[:12]}"
        edge_side = "UPPER" if oep["state"] == STATE_OUTSIDE_ABOVE else "LOWER"
        outer = edges.get("upper_outer_edge") if edge_side == "UPPER" else edges.get("lower_outer_edge")

        reclaim = _find_reclaim_for_outside_episode(oep, transitions, trades, edges)
        reclaimed = reclaim is not None
        retest = _retest_status(reclaim, oep, episodes, trades, edges)

        chunk = [t for t in trades if start <= t["ts"] <= end]
        dists = [abs(t["price"] - outer) / t["price"] * 10000.0 for t in chunk] if outer and chunk else []

        excursions.append(
            {
                "outside_excursion_id": exc_id,
                "source_episode_id": oep["episode_id"],
                "edge": edge_side,
                "state": oep["state"],
                "start_ts": oep["start_ts"],
                "end_ts": oep["end_ts"],
                "duration_seconds": oep.get("duration_seconds"),
                "closed": oep.get("closed", True),
                "end_reason": oep.get("end_reason"),
                "reclaimed": reclaimed,
                "reclaim_event_id": reclaim["reclaim_event_id"] if reclaim else None,
                "retest_status": retest,
                "trade_count": oep.get("trade_count"),
                "base_volume": oep.get("base_volume"),
                "quote_notional": oep.get("quote_notional"),
                "taker_delta_quote": oep.get("taker_delta_quote"),
                "taker_buy_quote": oep.get("taker_buy_quote"),
                "taker_sell_quote": oep.get("taker_sell_quote"),
                "mean_distance_bps_from_outer_edge": sum(dists) / len(dists) if dists else None,
                "max_distance_bps_from_outer_edge": max(dists) if dists else oep.get("max_distance_bps_from_relevant_edge"),
                "outer_edge_price": outer,
            }
        )
        if reclaim:
            reclaim["outside_excursion_id"] = exc_id
            reclaims.append(reclaim)

    return excursions, reclaims


def build_canonical_reclaim_pipeline(
    episodes: list[dict[str, Any]],
    transitions: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    edges: dict[str, Any],
    *,
    visits: list[dict[str, Any]] | None = None,
    ambiguous_timestamps: set[str] | None = None,
) -> dict[str, Any]:
    """Phase 2A.3: split raw excursions, ambiguous candidates, canonical reclaims."""
    if visits is None:
        visits = build_edge_visits(episodes)
    ambiguous_ts = ambiguous_timestamps or set()
    raw_excursions, raw_reclaims = build_outside_excursions(episodes, transitions, trades, edges)

    raw_tagged = _tag_raw_excursions(raw_excursions)
    _, canonical_excursions, ambiguous_excursions = _split_excursion_categories(raw_excursions, ambiguous_ts)
    canonical_exc_ids = {e["outside_excursion_id"] for e in canonical_excursions}

    canonical_reclaims: list[dict[str, Any]] = []
    ambiguous_candidates: list[dict[str, Any]] = []

    for reclaim in raw_reclaims:
        exc_id = reclaim.get("outside_excursion_id")
        exc = next((e for e in raw_excursions if e["outside_excursion_id"] == exc_id), {})
        eligible, reason = _assess_reclaim_eligibility(exc, reclaim, ambiguous_ts)

        if eligible and exc_id in canonical_exc_ids:
            canonical_reclaims.append(
                _to_reclaim_v3_canonical(reclaim, exc, visits, edges)
            )
        else:
            ambiguous_candidates.append(
                _to_ambiguous_candidate(reclaim, exc, visits, ambiguous_ts, reason or INELIG_AMBIGUOUS_MULTI_STATE)
            )

    eligibility_summary = {
        "raw_outside_count": len(raw_tagged),
        "canonical_outside_count": len(canonical_excursions),
        "ambiguous_outside_count": len(ambiguous_excursions),
        "canonical_reclaim_count": len(canonical_reclaims),
        "ambiguous_reclaim_candidate_count": len(ambiguous_candidates),
        "invariant_raw_equals_canonical_plus_ambiguous": len(raw_tagged) == len(canonical_excursions) + len(ambiguous_excursions),
        "invariant_canonical_reclaim_lte_canonical_outside": len(canonical_reclaims) <= len(canonical_excursions),
    }

    return {
        "reclaim_events": canonical_reclaims,
        "ambiguous_reclaim_candidates": ambiguous_candidates,
        "outside_excursions": raw_excursions,
        "raw_outside_excursions": raw_tagged,
        "canonical_outside_excursions": canonical_excursions,
        "ambiguous_same_timestamp_excursions": ambiguous_excursions,
        "canonical_eligibility_summary": eligibility_summary,
        "visits": visits,
    }


def _tag_raw_excursions(excursions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            **exc,
            "excursion_category": "RAW",
            "sequence_status": SEQUENCE_RAW,
            "exchange_order_proven": False,
            "canonical_eligible": False,
            "canonical_ineligibility_reason": "RAW_DETERMINISTIC_AUDIT_ONLY",
        }
        for exc in excursions
    ]


def _assess_reclaim_eligibility(
    exc: dict[str, Any],
    reclaim: dict[str, Any],
    ambiguous_ts: set[str],
) -> tuple[bool, str | None]:
    start = exc.get("start_ts") or ""
    cross = reclaim.get("cross_ts") or ""
    duration = float(exc.get("duration_seconds") or 0)

    if start in ambiguous_ts and duration == 0:
        return False, INELIG_AMBIGUOUS_MULTI_STATE
    if cross in ambiguous_ts and start == cross:
        return False, INELIG_SAME_TS_NO_SEQUENCE
    if cross in ambiguous_ts and duration == 0:
        return False, INELIG_AMBIGUOUS_MULTI_STATE
    if start == cross and not reclaim.get("exchange_order_proven"):
        return False, INELIG_SAME_TS_NO_SEQUENCE
    if cross < start:
        return False, INELIG_LEGACY_TRADE_ID_ORDER
    return True, None


def _to_reclaim_v3_canonical(
    reclaim: dict[str, Any],
    exc: dict[str, Any],
    visits: list[dict[str, Any]],
    edges: dict[str, Any],
) -> dict[str, Any]:
    ep_id = reclaim.get("source_outside_episode_id") or exc.get("source_episode_id")
    edge_visit_id = assign_episode_to_visit(ep_id, visits) if ep_id else None
    edge = exc.get("edge") or ("UPPER" if "UPPER" in reclaim.get("event_type", "") else "LOWER")
    outer = edges.get("upper_outer_edge") if edge == "UPPER" else edges.get("lower_outer_edge")
    return {
        "reclaim_event_id": reclaim["reclaim_event_id"],
        "outside_excursion_id": exc["outside_excursion_id"],
        "edge_visit_id": edge_visit_id,
        "edge": edge,
        "outside_start_ts": exc.get("start_ts"),
        "cross_ts": reclaim.get("cross_ts"),
        "outside_duration_seconds": exc.get("duration_seconds"),
        "previous_state": exc.get("state"),
        "new_state": reclaim.get("to_profile_state"),
        "outer_edge_price": outer,
        "cross_price": reclaim.get("cross_price"),
        "event_status": EVENT_CANONICAL,
        "canonical_eligible": True,
        "ordering_quality": ORDERING_UNAMBIGUOUS,
        "source_contract": RECLAIM_EVENT_CONTRACT_V3,
        "interpretation_status": INTERPRETATION_NOT_EVALUATED,
        "exchange_order_proven": False,
        "closed_by_cross": True,
        "end_reason": END_REASON_STATE_CHANGE,
        "cross_trade_id": reclaim.get("cross_trade_id"),
        "source_outside_episode_id": ep_id,
        "seconds_since_outside_start": reclaim.get("seconds_since_outside_start"),
    }


def _to_ambiguous_candidate(
    reclaim: dict[str, Any],
    exc: dict[str, Any],
    visits: list[dict[str, Any]],
    ambiguous_ts: set[str],
    reason: str,
) -> dict[str, Any]:
    ep_id = reclaim.get("source_outside_episode_id") or exc.get("source_episode_id")
    edge_visit_id = assign_episode_to_visit(ep_id, visits) if ep_id else None
    edge = exc.get("edge") or "UPPER"
    ts = exc.get("start_ts") or reclaim.get("cross_ts")
    ordering = ORDERING_SAME_TS_SHARED if ts in ambiguous_ts else ORDERING_EPISODE_SCOPED
    return {
        "candidate_id": f"arc_{uuid.uuid4().hex[:12]}",
        "raw_outside_excursion_id": exc.get("outside_excursion_id"),
        "edge_visit_id": edge_visit_id,
        "edge": edge,
        "timestamp": ts,
        "states_observed": f"{exc.get('state')}|{reclaim.get('to_profile_state')}",
        "min_price": exc.get("min_price"),
        "max_price": exc.get("max_price"),
        "trade_count": exc.get("trade_count"),
        "buy_volume": exc.get("taker_buy_quote"),
        "sell_volume": exc.get("taker_sell_quote"),
        "taker_delta": exc.get("taker_delta_quote"),
        "ambiguity_reason": reason,
        "ordering_quality": ordering,
        "exchange_order_proven": False,
        "canonical_eligible": False,
        "interpretation_status": INTERPRETATION_NOT_EVALUATED,
        "event_status": EVENT_AMBIGUOUS,
        "cross_ts": reclaim.get("cross_ts"),
        "reclaim_event_id_raw": reclaim.get("reclaim_event_id"),
    }


def _split_excursion_categories(
    excursions: list[dict[str, Any]],
    ambiguous_timestamps: set[str] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    ambiguous_ts = ambiguous_timestamps or set()
    ambiguous: list[dict[str, Any]] = []
    canonical: list[dict[str, Any]] = []
    for exc in excursions:
        start = exc.get("start_ts") or ""
        is_amb = start in ambiguous_ts and float(exc.get("duration_seconds") or 0) == 0
        tagged = {
            **exc,
            "excursion_category": "AMBIGUOUS_SAME_TIMESTAMP" if is_amb else "CANONICAL",
            "canonical_eligible": not is_amb,
            "canonical_ineligibility_reason": INELIG_AMBIGUOUS_MULTI_STATE if is_amb else None,
        }
        if is_amb:
            ambiguous.append(tagged)
        else:
            canonical.append(tagged)
    raw = [{**e, "excursion_category": "RAW"} for e in excursions]
    return raw, canonical, ambiguous


def _find_reclaim_for_outside_episode(
    oep: dict[str, Any],
    transitions: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    edges: dict[str, Any],
) -> dict[str, Any] | None:
    """First chronologic transition leaving outside state within episode span."""
    if oep.get("end_reason") == END_REASON_WINDOW_END:
        return None
    if not oep.get("closed", True):
        return None

    start_dt = datetime.fromisoformat(oep["start_ts"].replace("Z", "+00:00"))
    end_dt = datetime.fromisoformat(oep["end_ts"].replace("Z", "+00:00"))
    outside_state = oep["state"]

    for tr in transitions:
        tr_ts = datetime.fromisoformat(tr["transition_ts"].replace("Z", "+00:00"))
        if tr_ts < start_dt or tr_ts > end_dt:
            continue
        if tr.get("from_state") != outside_state:
            continue
        to_st = tr.get("to_state")
        if outside_state == STATE_OUTSIDE_ABOVE:
            if to_st == STATE_OUTSIDE_ABOVE:
                continue
            if to_st not in (STATE_BETWEEN_UPPER, STATE_INSIDE_BOTH, STATE_BETWEEN_LOWER, STATE_OUTSIDE_BELOW):
                continue
            event_type = RECLAIM_EVENT_RETURNED_BELOW_UPPER
        else:
            if to_st == STATE_OUTSIDE_BELOW:
                continue
            if to_st not in (STATE_BETWEEN_LOWER, STATE_INSIDE_BOTH, STATE_BETWEEN_UPPER, STATE_OUTSIDE_ABOVE):
                continue
            event_type = RECLAIM_EVENT_RETURNED_ABOVE_LOWER

        before = [t for t in trades if start_dt <= t["ts"] < tr_ts]
        after = [t for t in trades if t["ts"] >= tr_ts]
        delta_before = sum(t["notional"] if t["side"] == "Buy" else -t["notional"] for t in before)
        delta_after = sum(t["notional"] if t["side"] == "Buy" else -t["notional"] for t in after[:50])
        return {
            "reclaim_event_id": f"rcl_{uuid.uuid4().hex[:12]}",
            "event_type": event_type,
            "source_outside_episode_id": oep["episode_id"],
            "cross_ts": tr["transition_ts"],
            "cross_price": tr.get("price"),
            "cross_trade_id": tr.get("trade_id"),
            "seconds_since_outside_start": (tr_ts - start_dt).total_seconds(),
            "outside_duration_seconds": oep.get("duration_seconds"),
            "taker_delta_quote_before_cross": delta_before,
            "taker_delta_quote_after_cross_sample": delta_after,
            "to_profile_state": to_st,
            "chronological_cross_verified": True,
        }
    return None


def _retest_status(
    reclaim: dict[str, Any] | None,
    oep: dict[str, Any],
    episodes: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    edges: dict[str, Any],
) -> str:
    if not reclaim:
        if oep.get("end_reason") == END_REASON_WINDOW_END:
            return RETEST_NO_POST
        return "NO_RECLAIM"
    cross_ts = datetime.fromisoformat(reclaim["cross_ts"].replace("Z", "+00:00"))
    idx = oep.get("episode_index", -1)
    next_outside = None
    for ep in episodes:
        if ep.get("episode_index", -1) <= idx:
            continue
        if ep["state"] in (STATE_OUTSIDE_ABOVE, STATE_OUTSIDE_BELOW):
            next_outside = ep
            break
    post = [t for t in trades if t["ts"] > cross_ts]
    if not post and next_outside is None:
        return RETEST_NO_POST
    return "UNFROZEN_RETEST_WINDOW"


def run_outside_reclaim_invariant_audit(
    excursions: list[dict[str, Any]],
    reclaims: list[dict[str, Any]],
    phase2a_reclaims: list[dict[str, Any]] | None = None,
    *,
    ambiguous_candidates: list[dict[str, Any]] | None = None,
    canonical_outside_count: int | None = None,
) -> dict[str, Any]:
    """Audit invariants; fail if violations found."""
    violations: list[dict[str, Any]] = []
    reclaim_by_exc: dict[str, list] = {}
    for r in reclaims:
        exc_id = r.get("outside_excursion_id")
        if exc_id:
            reclaim_by_exc.setdefault(exc_id, []).append(r)

    for exc in excursions:
        exc_id = exc["outside_excursion_id"]
        rcls = reclaim_by_exc.get(exc_id, [])
        if len(rcls) > 1:
            violations.append({"type": "MULTIPLE_RECLAIMS", "outside_excursion_id": exc_id, "count": len(rcls)})
        if exc.get("reclaimed") and not rcls and exc.get("canonical_eligible"):
            violations.append({"type": "RECLAIM_FLAG_WITHOUT_EVENT", "outside_excursion_id": exc_id})
        if not exc.get("reclaimed") and rcls:
            violations.append({"type": "RECLAIM_EVENT_WITHOUT_FLAG", "outside_excursion_id": exc_id})
        if exc.get("end_reason") == END_REASON_WINDOW_END and exc.get("reclaimed"):
            violations.append({"type": "WINDOW_END_RECLAIM", "outside_excursion_id": exc_id})
        for r in rcls:
            if r.get("cross_ts", "") < exc.get("start_ts", ""):
                violations.append({"type": "RECLAIM_BEFORE_OUTSIDE_START", "reclaim_event_id": r["reclaim_event_id"]})
            if r.get("source_contract") == RECLAIM_EVENT_CONTRACT_V3 and not r.get("canonical_eligible"):
                violations.append({"type": "INELIGIBLE_IN_CANONICAL_OUTPUT", "reclaim_event_id": r["reclaim_event_id"]})

    if canonical_outside_count is not None and len(reclaims) > canonical_outside_count:
        violations.append(
            {
                "type": "CANONICAL_RECLAIM_EXCEEDS_CANONICAL_OUTSIDE",
                "canonical_reclaim_count": len(reclaims),
                "canonical_outside_count": canonical_outside_count,
            }
        )

    for cand in ambiguous_candidates or []:
        if cand.get("canonical_eligible"):
            violations.append({"type": "AMBIGUOUS_CANDIDATE_MARKED_ELIGIBLE", "candidate_id": cand.get("candidate_id")})

    phase2a_bug = None
    legacy_bug_detected = False
    if phase2a_reclaims:
        cross_ts_set = {r.get("cross_ts") for r in phase2a_reclaims}
        legacy_bug_detected = len(phase2a_reclaims) > 1 and len(cross_ts_set) == 1
        if legacy_bug_detected:
            phase2a_bug = {
                "type": "PHASE_2A_GLOBAL_FIRST_RECLAIM_BUG",
                "description": "All outside episodes assigned same global first reclaim cross_ts",
                "shared_cross_ts": next(iter(cross_ts_set)),
                "affected_count": len(phase2a_reclaims),
            }

    # Detect legacy global-first pattern in current canonical reclaims
    if reclaims and not legacy_bug_detected:
        v2_cross = {r.get("cross_ts") for r in reclaims}
        if len(reclaims) > 1 and len(v2_cross) == 1:
            violations.append(
                {
                    "type": "CANONICAL_GLOBAL_FIRST_RECLAIM_BUG",
                    "shared_cross_ts": next(iter(v2_cross)),
                    "count": len(reclaims),
                }
            )

    passed = len(violations) == 0
    return {
        "contract_version": OUTSIDE_RECLAIM_CONTRACT,
        "canonical_reclaim_contract": RECLAIM_EVENT_CONTRACT_V3,
        "passed": passed,
        "violation_count": len(violations),
        "violations": violations,
        "outside_excursion_count": len(excursions),
        "reclaim_count": len(reclaims),
        "unique_reclaim_cross_timestamps": len({r.get("cross_ts") for r in reclaims}),
        "open_excursion_count": sum(1 for e in excursions if not e.get("reclaimed") and e.get("end_reason") == END_REASON_WINDOW_END),
        "phase2a_reclaim_bug_documented": phase2a_bug,
        "phase2a_reclaim_count": len(phase2a_reclaims or []),
        "legacy_global_first_reclaim_enabled": False,
    }


def assert_invariants_or_raise(audit: dict[str, Any]) -> None:
    if not audit.get("passed"):
        v = audit.get("violations") or []
        raise ValueError(f"outside_reclaim_invariant_audit failed: {v[:3]}")
