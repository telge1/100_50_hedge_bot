"""Deterministic causal edge matching for AEF events."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.contracts import (
    aggressor_side_for_wall,
    wall_side_for_aef_direction,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.edge_catalog import (
    CausalEdge,
    sample_at_or_before,
    wall_present_asof,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.models import InputEvent
from orderbook_analyse.l2_wall_attack_discovery.models import tick_size
from orderbook_analyse.ob200_v3_raw_discovery.reconstruct import SampleRow


# Research parameters — NOT fitted on forward outcomes.
# Distances aligned with existing wall detector approach/touch bands.
JOIN_MAX_NEAR_BPS = 5.0  # approach_bps in walls.py
JOIN_EXACT_BPS = 1.5  # touch_bps in walls.py
JOIN_MAX_FAR_BPS = 15.0  # hard reject beyond
JOIN_MAX_STALE_GAP_S = 30.0  # if last visible sample older than this before flow_start
JOIN_BUCKET_ALIGN_TOLERANCE_MS = 250  # sample_ms alignment only


@dataclass
class JoinThresholds:
    max_near_bps: float = JOIN_MAX_NEAR_BPS
    exact_bps: float = JOIN_EXACT_BPS
    max_far_bps: float = JOIN_MAX_FAR_BPS
    max_stale_gap_s: float = JOIN_MAX_STALE_GAP_S
    bucket_align_tolerance_ms: int = JOIN_BUCKET_ALIGN_TOLERANCE_MS
    accept_confidence: tuple[str, ...] = ("HIGH", "MEDIUM")  # LOW does not enable acceptance

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_near_bps": self.max_near_bps,
            "exact_bps": self.exact_bps,
            "max_far_bps": self.max_far_bps,
            "max_stale_gap_s": self.max_stale_gap_s,
            "bucket_align_tolerance_ms": self.bucket_align_tolerance_ms,
            "accept_confidence": list(self.accept_confidence),
            "source": "l2 wall detector approach/touch bands + research stale gap",
            "unfitted_on_outcomes": True,
        }


@dataclass
class EdgeJoinResult:
    aef_event_id: str
    edge_join_status: str
    matched_edge_id: Optional[str] = None
    matched_edge_price: Optional[float] = None
    matched_edge_source: Optional[str] = None
    matched_edge_available_ts: Optional[str] = None
    matched_edge_distance_bps: Optional[float] = None
    matched_edge_age_seconds: Optional[float] = None
    matched_edge_persistence_seconds: Optional[float] = None
    matched_edge_relative_size: Optional[float] = None
    matched_edge_notional_asof: Optional[float] = None
    edge_match_explanation_codes: list[str] = field(default_factory=list)
    edge_match_confidence_class: str = "NONE"
    edge_match_candidate_count: int = 0
    candidates: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "aef_event_id": self.aef_event_id,
            "edge_join_status": self.edge_join_status,
            "matched_edge_id": self.matched_edge_id,
            "matched_edge_price": self.matched_edge_price,
            "matched_edge_source": self.matched_edge_source,
            "matched_edge_available_ts": self.matched_edge_available_ts,
            "matched_edge_distance_bps": self.matched_edge_distance_bps,
            "matched_edge_age_seconds": self.matched_edge_age_seconds,
            "matched_edge_persistence_seconds": self.matched_edge_persistence_seconds,
            "matched_edge_relative_size": self.matched_edge_relative_size,
            "matched_edge_notional_asof": self.matched_edge_notional_asof,
            "edge_match_explanation_codes": list(self.edge_match_explanation_codes),
            "edge_match_confidence_class": self.edge_match_confidence_class,
            "edge_match_candidate_count": self.edge_match_candidate_count,
        }


def required_wall_side_for_event(event: InputEvent) -> Optional[str]:
    if event.direction in {"LONG", "SHORT"}:
        return wall_side_for_aef_direction(event.direction)
    if event.wall_side:
        return str(event.wall_side).upper()
    return None


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _ref_price(event: InputEvent, flow_start_price: Optional[float]) -> Optional[float]:
    if event.reference_price and event.reference_price > 0:
        return float(event.reference_price)
    return flow_start_price


def evaluate_candidates(
    event: InputEvent,
    edges: list[CausalEdge],
    samples: list[SampleRow],
    *,
    flow_start_price: Optional[float],
    flow_vwap: Optional[float],
    flow_low: Optional[float],
    flow_high: Optional[float],
    thr: JoinThresholds,
) -> list[dict[str, Any]]:
    want_side = required_wall_side_for_event(event)
    flow_ms = int(event.flow_start_ts.timestamp() * 1000)
    # allow tiny bucket align: edge available up to flow_start + tolerance is NOT allowed;
    # only edge_available <= flow_start. Tolerance applied when comparing sample timestamps.
    asof = sample_at_or_before(samples, flow_ms)
    mid = asof.mid if asof and asof.mid else _ref_price(event, flow_start_price)
    out: list[dict[str, Any]] = []

    for edge in edges:
        row: dict[str, Any] = {
            "aef_event_id": event.event_id,
            "edge_id": edge.edge_id,
            "wall_side": edge.wall_side,
            "edge_price": edge.edge_price,
            "first_seen_ts": _iso(edge.first_seen_ts),
            "edge_available_ts": _iso(edge.edge_available_ts),
            "wall_side_match": False,
            "edge_visible_before_attack": False,
            "candidate_rejection_reason": None,
            "distance_to_flow_start_price_bps": None,
            "distance_to_flow_vwap_bps": None,
            "distance_to_flow_extreme_bps": None,
            "overlap_with_flow_price_range": False,
            "persistence_seconds_asof_attack": None,
            "relative_size_asof_attack": None,
            "notional_asof_attack": None,
            "last_seen_age_seconds": None,
            "match_class": None,
        }
        if edge.symbol != event.symbol.upper():
            row["candidate_rejection_reason"] = "SYMBOL_MISMATCH"
            out.append(row)
            continue
        if want_side is None:
            row["candidate_rejection_reason"] = "DATA_INCOMPLETE"
            out.append(row)
            continue
        if edge.wall_side != want_side:
            row["candidate_rejection_reason"] = "SIDE_MISMATCH"
            out.append(row)
            continue
        row["wall_side_match"] = True

        # causality: available before/at flow start
        avail_ms = int(edge.edge_available_ts.timestamp() * 1000)
        if avail_ms > flow_ms:
            row["candidate_rejection_reason"] = "EDGE_AFTER_FLOW_START"
            out.append(row)
            continue
        row["edge_visible_before_attack"] = True
        row["persistence_seconds_asof_attack"] = max(0.0, (flow_ms - avail_ms) / 1000.0)

        present, qty, sample_mid = wall_present_asof(
            asof, side=edge.wall_side, edge_price=edge.edge_price, symbol=event.symbol
        )
        if asof is None:
            row["candidate_rejection_reason"] = "DATA_INCOMPLETE"
            out.append(row)
            continue
        age = (flow_ms - asof.ts_ms) / 1000.0
        row["last_seen_age_seconds"] = age
        if age > thr.max_stale_gap_s:
            row["candidate_rejection_reason"] = "EDGE_STALE"
            out.append(row)
            continue
        if not present or qty is None or qty <= 0:
            row["candidate_rejection_reason"] = "EDGE_STALE"
            out.append(row)
            continue

        row["notional_asof_attack"] = float(qty) * float(edge.edge_price)
        # as-of relative size: matched wall qty is the dominant wall by construction
        row["relative_size_asof_attack"] = 1.0

        ref = mid or sample_mid or flow_start_price
        if ref and ref > 0:
            dist_start = abs(edge.edge_price - (flow_start_price or ref)) / ref * 1e4 if flow_start_price else abs(edge.edge_price - ref) / ref * 1e4
            row["distance_to_flow_start_price_bps"] = dist_start
            if flow_vwap:
                row["distance_to_flow_vwap_bps"] = abs(edge.edge_price - flow_vwap) / ref * 1e4
            # extreme: closest of high/low to edge
            ext = None
            if flow_low is not None and flow_high is not None:
                ext = min(abs(edge.edge_price - flow_low), abs(edge.edge_price - flow_high)) / ref * 1e4
                # overlap if edge inside [low, high]
                lo, hi = min(flow_low, flow_high), max(flow_low, flow_high)
                row["overlap_with_flow_price_range"] = lo <= edge.edge_price <= hi
            row["distance_to_flow_extreme_bps"] = ext
            dist = dist_start
        else:
            row["candidate_rejection_reason"] = "DATA_INCOMPLETE"
            out.append(row)
            continue

        if dist > thr.max_far_bps and not row["overlap_with_flow_price_range"]:
            row["candidate_rejection_reason"] = "EDGE_TOO_FAR"
            out.append(row)
            continue

        if row["overlap_with_flow_price_range"] or dist <= thr.exact_bps:
            row["match_class"] = "EXACT_EDGE_TOUCH" if dist <= thr.exact_bps else "EDGE_RANGE_OVERLAP"
        elif dist <= thr.max_near_bps:
            row["match_class"] = "NEAR_EDGE_APPROACH"
        else:
            row["candidate_rejection_reason"] = "EDGE_TOO_FAR"
            out.append(row)
            continue

        row["candidate_rejection_reason"] = None
        out.append(row)
    return out


def select_match(
    event: InputEvent,
    candidate_rows: list[dict[str, Any]],
    *,
    thr: JoinThresholds,
) -> EdgeJoinResult:
    plausible = [c for c in candidate_rows if c.get("candidate_rejection_reason") is None and c.get("match_class")]
    codes: list[str] = []
    if not plausible:
        # classify dominant rejection
        reasons = [c.get("candidate_rejection_reason") for c in candidate_rows if c.get("candidate_rejection_reason")]
        side_only = [c for c in candidate_rows if c.get("wall_side_match")]
        if not side_only and candidate_rows:
            status = "SIDE_MISMATCH"
        elif any(r == "DATA_INCOMPLETE" for r in reasons) and not any(
            r in {"EDGE_TOO_FAR", "EDGE_STALE", "EDGE_AFTER_FLOW_START"} for r in reasons
        ):
            status = "DATA_INCOMPLETE"
        elif any(r == "EDGE_STALE" for r in reasons) and not any(r is None for r in reasons):
            status = "EDGE_STALE"
        elif any(r == "EDGE_TOO_FAR" for r in reasons):
            status = "EDGE_TOO_FAR"
        else:
            status = "NO_CAUSAL_EDGE"
        return EdgeJoinResult(
            aef_event_id=event.event_id,
            edge_join_status=status,
            edge_match_explanation_codes=[status],
            edge_match_confidence_class="NONE",
            edge_match_candidate_count=0,
            candidates=candidate_rows,
        )

    # lex sort: match_class rank, then distance, then persistence desc, then edge_id
    class_rank = {"EXACT_EDGE_TOUCH": 0, "EDGE_RANGE_OVERLAP": 1, "NEAR_EDGE_APPROACH": 2}

    def key(c: dict[str, Any]):
        return (
            class_rank.get(c["match_class"], 9),
            float(c.get("distance_to_flow_start_price_bps") or 1e9),
            -float(c.get("persistence_seconds_asof_attack") or 0.0),
            str(c["edge_id"]),
        )

    plausible_sorted = sorted(plausible, key=key)
    best = plausible_sorted[0]
    # ambiguity: second within 0.5 bps and same class
    ambiguous = False
    if len(plausible_sorted) > 1:
        second = plausible_sorted[1]
        if second["match_class"] == best["match_class"]:
            d0 = float(best.get("distance_to_flow_start_price_bps") or 0)
            d1 = float(second.get("distance_to_flow_start_price_bps") or 0)
            if abs(d0 - d1) <= 0.5:
                ambiguous = True

    if ambiguous:
        codes.append("MULTIPLE_EDGE_AMBIGUOUS")
        return EdgeJoinResult(
            aef_event_id=event.event_id,
            edge_join_status="MULTIPLE_EDGE_AMBIGUOUS",
            matched_edge_id=best["edge_id"],  # best_candidate logged but not accepted
            matched_edge_price=best["edge_price"],
            matched_edge_source="raw_ob200_wall_lifecycle",
            matched_edge_available_ts=best["edge_available_ts"],
            matched_edge_distance_bps=best.get("distance_to_flow_start_price_bps"),
            matched_edge_age_seconds=best.get("last_seen_age_seconds"),
            matched_edge_persistence_seconds=best.get("persistence_seconds_asof_attack"),
            matched_edge_relative_size=best.get("relative_size_asof_attack"),
            matched_edge_notional_asof=best.get("notional_asof_attack"),
            edge_match_explanation_codes=codes + [best["match_class"], "BEST_CANDIDATE_ONLY"],
            edge_match_confidence_class="LOW",
            edge_match_candidate_count=len(plausible),
            candidates=candidate_rows,
        )

    if best["match_class"] in {"EXACT_EDGE_TOUCH", "EDGE_RANGE_OVERLAP"}:
        conf = "HIGH"
    elif best["match_class"] == "NEAR_EDGE_APPROACH":
        conf = "MEDIUM"
    else:
        conf = "LOW"
    codes.append(best["match_class"])
    codes.append(f"CONFIDENCE_{conf}")
    return EdgeJoinResult(
        aef_event_id=event.event_id,
        edge_join_status=best["match_class"],
        matched_edge_id=best["edge_id"],
        matched_edge_price=best["edge_price"],
        matched_edge_source="raw_ob200_wall_lifecycle",
        matched_edge_available_ts=best["edge_available_ts"],
        matched_edge_distance_bps=best.get("distance_to_flow_start_price_bps"),
        matched_edge_age_seconds=best.get("last_seen_age_seconds"),
        matched_edge_persistence_seconds=best.get("persistence_seconds_asof_attack"),
        matched_edge_relative_size=best.get("relative_size_asof_attack"),
        matched_edge_notional_asof=best.get("notional_asof_attack"),
        edge_match_explanation_codes=codes,
        edge_match_confidence_class=conf,
        edge_match_candidate_count=len(plausible),
        candidates=candidate_rows,
    )


def apply_join_to_event(event: InputEvent, join: EdgeJoinResult, thr: JoinThresholds) -> InputEvent:
    """Return a copy-like update: only HIGH/MEDIUM set measurable edge for acceptance."""
    if join.edge_match_confidence_class not in thr.accept_confidence:
        event.edge_price = None
        event.edge_source = "causal_join_no_accept_edge"
        event.edge_confidence = "none" if join.edge_match_confidence_class == "NONE" else "low"
        event.meta = {
            **(event.meta or {}),
            "edge_join": join.to_dict(),
        }
        return event
    event.edge_price = join.matched_edge_price
    event.wall_side = required_wall_side_for_event(event)
    event.edge_source = join.matched_edge_source or "raw_ob200_wall_lifecycle"
    event.edge_confidence = "high" if join.edge_match_confidence_class == "HIGH" else "medium"
    event.meta = {**(event.meta or {}), "edge_join": join.to_dict()}
    return event
