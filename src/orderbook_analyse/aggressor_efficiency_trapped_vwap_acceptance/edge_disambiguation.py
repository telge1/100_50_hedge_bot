"""Causal disambiguation of MULTIPLE_EDGE_AMBIGUOUS candidates.

Uses only as-of / in-flow information (public trades during flow + directional
reachability). Never uses forward outcomes, future wall size, or future persistence.

outcome_used_for_matching = False
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from orderbook_analyse.aggressor_efficiency_flip.contracts import aggressor_side
from orderbook_analyse.aggressor_efficiency_flip.models import Trade
from orderbook_analyse.aggressor_efficiency_flip.timeutil import ensure_utc, iso_z
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.edge_match import (
    EdgeJoinResult,
    JoinThresholds,
    required_wall_side_for_event,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.models import InputEvent
from orderbook_analyse.l2_wall_attack_discovery.models import tick_size

# Research parameters — NOT fitted on forward outcomes.
CLUSTER_MAX_TICKS = 2.0  # adjacent levels within this form a causal cluster
ZONE_TICKS = 1.0  # tick-normalized trade zone around edge
NOTIONAL_TIE_REL = 0.05  # relative notional gap required to break trade ties
FRONT_GAP_TICKS = 1.0  # unique front if next competing edge farther than this


@dataclass
class DisambiguationThresholds:
    cluster_max_ticks: float = CLUSTER_MAX_TICKS
    zone_ticks: float = ZONE_TICKS
    notional_tie_rel: float = NOTIONAL_TIE_REL
    front_gap_ticks: float = FRONT_GAP_TICKS
    # Standard acceptance: HIGH only. MEDIUM = sensitivity cohort (not applied).
    accept_confidence: tuple[str, ...] = ("HIGH",)
    outcome_used_for_matching: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "cluster_max_ticks": self.cluster_max_ticks,
            "zone_ticks": self.zone_ticks,
            "notional_tie_rel": self.notional_tie_rel,
            "front_gap_ticks": self.front_gap_ticks,
            "accept_confidence": list(self.accept_confidence),
            "outcome_used_for_matching": self.outcome_used_for_matching,
            "source": "causal trade-touch + directional front-edge; unfitted",
        }


def _tick_eq(price: float, edge: float, tick: float) -> bool:
    return abs(price - edge) <= tick * 0.51


def _in_zone(price: float, edge: float, tick: float, zone_ticks: float) -> bool:
    return abs(price - edge) <= tick * zone_ticks * 1.01


def aggressor_trades_in_flow(
    trades: list[Trade],
    *,
    flow_start: datetime,
    flow_end: datetime,
    side: str,
) -> list[Trade]:
    t0, t1 = ensure_utc(flow_start), ensure_utc(flow_end)
    out: list[Trade] = []
    for tr in trades:
        ts = ensure_utc(tr.trade_ts)
        if t0 <= ts <= t1 and tr.side == side:
            out.append(tr)
    return out


def directional_reached(
    *,
    wall_side: str,
    edge_price: float,
    flow_low: float,
    flow_high: float,
    tick: float,
) -> tuple[bool, bool]:
    """Return (reached, behind_market).

    BUY→ASK: price moves up; ASK edge reached iff flow_high >= edge.
    SELL→BID: price moves down; BID edge reached iff flow_low <= edge.
    Behind: ASK below flow_low, or BID above flow_high.
    """
    if wall_side == "ASK":
        behind = edge_price < flow_low - tick * 0.51
        reached = (not behind) and (flow_high + tick * 0.51 >= edge_price)
        return reached, behind
    # BID
    behind = edge_price > flow_high + tick * 0.51
    reached = (not behind) and (flow_low - tick * 0.51 <= edge_price)
    return reached, behind


def enrich_candidate_with_trades(
    cand: dict[str, Any],
    *,
    event: InputEvent,
    atrades: list[Trade],
    flow_start_price: Optional[float],
    flow_vwap: Optional[float],
    flow_low: Optional[float],
    flow_high: Optional[float],
    dthr: DisambiguationThresholds,
) -> dict[str, Any]:
    """Augment a plausible candidate row with causal trade / reach features."""
    row = dict(cand)
    tick = tick_size(event.symbol)
    ep = float(cand["edge_price"])
    wall = str(cand["wall_side"])
    side = aggressor_side(event.direction) if event.direction in {"LONG", "SHORT"} else None

    flo = flow_low
    fhi = flow_high
    if flo is None or fhi is None:
        row["reached_in_directional_path"] = False
        row["book_coverage_status"] = "DATA_INCOMPLETE"
        row["exact_trade_at_edge"] = False
        row["aggressive_notional_at_edge"] = 0.0
        row["aggressive_trade_count_at_edge"] = 0
        row["aggressive_notional_near_edge"] = 0.0
        row["first_touch_ts"] = None
        row["touch_count_during_flow"] = 0
        row["flow_range_overlap"] = bool(cand.get("overlap_with_flow_price_range"))
        row["cluster_role"] = None
        return row

    reached, behind = directional_reached(
        wall_side=wall, edge_price=ep, flow_low=float(flo), flow_high=float(fhi), tick=tick
    )
    row["reached_in_directional_path"] = reached
    row["behind_market"] = behind
    row["flow_range_overlap"] = bool(flo <= ep <= fhi) or bool(cand.get("overlap_with_flow_price_range"))

    n_exact = 0
    notional_exact = 0.0
    first_touch: Optional[datetime] = None
    n_zone = 0
    notional_zone = 0.0
    for tr in atrades:
        if _tick_eq(tr.price, ep, tick):
            n_exact += 1
            notional_exact += float(tr.notional)
            ts = ensure_utc(tr.trade_ts)
            if first_touch is None or ts < first_touch:
                first_touch = ts
        if _in_zone(tr.price, ep, tick, dthr.zone_ticks):
            n_zone += 1
            notional_zone += float(tr.notional)

    row["exact_trade_at_edge"] = n_exact > 0
    row["aggressive_trade_count_at_edge"] = n_exact
    row["aggressive_notional_at_edge"] = notional_exact
    row["aggressive_notional_near_edge"] = notional_zone
    row["touch_count_during_flow"] = n_zone
    row["first_touch_ts"] = iso_z(first_touch) if first_touch else None
    row["first_aggressor_trade_price"] = atrades[0].price if atrades else None
    row["last_aggressor_trade_price"] = atrades[-1].price if atrades else None
    row["edge_age_seconds"] = cand.get("last_seen_age_seconds")
    row["edge_notional_asof_flow_start"] = cand.get("notional_asof_attack")
    row["edge_persistence_seconds_asof_flow_start"] = cand.get("persistence_seconds_asof_attack")
    row["edge_last_seen_asof_flow_start"] = None  # filled by runner if sample ts known
    row["distance_from_flow_start_bps"] = cand.get("distance_to_flow_start_price_bps")
    row["distance_from_flow_vwap_bps"] = cand.get("distance_to_flow_vwap_bps")
    row["distance_from_flow_extreme_bps"] = cand.get("distance_to_flow_extreme_bps")
    row["book_coverage_status"] = "OK" if atrades else "NO_AGGRESSOR_TRADES"
    row["flow_side"] = side
    row["flow_start_price"] = flow_start_price
    row["flow_vwap"] = flow_vwap
    row["flow_low"] = flo
    row["flow_high"] = fhi
    row["symbol"] = event.symbol.upper()
    row["flow_start_ts"] = iso_z(event.flow_start_ts)
    row["flow_end_ts"] = iso_z(event.flow_end_ts)
    return row


def assign_cluster_roles(
    enriched: list[dict[str, Any]],
    *,
    wall_side: str,
    symbol: str,
    dthr: DisambiguationThresholds,
) -> list[dict[str, Any]]:
    """Label FRONT / INNER / BACK / SEPARATE among reached candidates."""
    tick = tick_size(symbol)
    reached = [c for c in enriched if c.get("reached_in_directional_path")]
    if not reached:
        for c in enriched:
            c["cluster_role"] = None
            c["cluster_id"] = None
            c["nearer_competing_edge_count"] = 0
        return enriched

    # sort attack order: ASK ascending, BID descending
    if wall_side == "ASK":
        ordered = sorted(reached, key=lambda c: float(c["edge_price"]))
    else:
        ordered = sorted(reached, key=lambda c: -float(c["edge_price"]))

    # cluster by consecutive tick gaps
    clusters: list[list[dict[str, Any]]] = []
    cur: list[dict[str, Any]] = [ordered[0]]
    for prev, nxt in zip(ordered, ordered[1:]):
        gap_ticks = abs(float(nxt["edge_price"]) - float(prev["edge_price"])) / tick
        if gap_ticks <= dthr.cluster_max_ticks:
            cur.append(nxt)
        else:
            clusters.append(cur)
            cur = [nxt]
    clusters.append(cur)

    id_to_role: dict[str, tuple[str, str, int]] = {}
    cluster_rows: list[dict[str, Any]] = []
    for i, cl in enumerate(clusters):
        cid = f"cl_{wall_side}_{i}"
        if wall_side == "ASK":
            front_px = min(float(c["edge_price"]) for c in cl)
            back_px = max(float(c["edge_price"]) for c in cl)
        else:
            front_px = max(float(c["edge_price"]) for c in cl)
            back_px = min(float(c["edge_price"]) for c in cl)
        for j, c in enumerate(cl):
            if len(cl) == 1:
                role = "SEPARATE_EDGE"
            elif j == 0:
                role = "FRONT_EDGE"
            elif j == len(cl) - 1:
                role = "BACK_EDGE"
            else:
                role = "INNER_CLUSTER_EDGE"
            id_to_role[c["edge_id"]] = (role, cid, j)
        cluster_rows.append(
            {
                "cluster_id": cid,
                "wall_side": wall_side,
                "cluster_front_price": front_px,
                "cluster_back_price": back_px,
                "cluster_total_notional_asof": sum(float(c.get("notional_asof_attack") or 0) for c in cl),
                "attacked_level_count": len(cl),
                "member_edge_ids": [c["edge_id"] for c in cl],
                "primary_touched_edge": next(
                    (c["edge_id"] for c in cl if c.get("exact_trade_at_edge")),
                    cl[0]["edge_id"],
                ),
            }
        )

    for c in enriched:
        meta = id_to_role.get(c["edge_id"])
        if meta:
            c["cluster_role"], c["cluster_id"], c["cluster_rank"] = meta[0], meta[1], meta[2]
        else:
            c["cluster_role"] = "NOT_REACHED"
            c["cluster_id"] = None
            c["cluster_rank"] = None
        # nearer competing: how many reached edges are strictly in front
        if c.get("reached_in_directional_path"):
            if wall_side == "ASK":
                c["nearer_competing_edge_count"] = sum(
                    1
                    for o in reached
                    if float(o["edge_price"]) < float(c["edge_price"]) - tick * 0.51
                )
            else:
                c["nearer_competing_edge_count"] = sum(
                    1
                    for o in reached
                    if float(o["edge_price"]) > float(c["edge_price"]) + tick * 0.51
                )
        else:
            c["nearer_competing_edge_count"] = None

    # stash cluster summary on first row for runner pickup
    if enriched:
        enriched[0]["_cluster_summaries"] = cluster_rows
    return enriched


def _lex_key(c: dict[str, Any], wall_side: str) -> tuple:
    """Lower is better. No wall size / persistence over reach/trade."""
    exact = 0 if c.get("exact_trade_at_edge") else 1
    zone = 0 if float(c.get("aggressive_notional_near_edge") or 0) > 0 else 1
    # first touch: earlier better; missing → large
    ft = c.get("first_touch_ts")
    if ft:
        try:
            touch_ord = datetime.fromisoformat(ft.replace("Z", "+00:00")).timestamp()
        except ValueError:
            touch_ord = 1e18
    else:
        touch_ord = 1e18
    notional = -float(c.get("aggressive_notional_at_edge") or 0.0)
    # front rank: ASK lower price, BID higher price
    px = float(c["edge_price"])
    front = px if wall_side == "ASK" else -px
    dist = float(c.get("distance_from_flow_extreme_bps") or c.get("distance_to_flow_extreme_bps") or 1e9)
    return (exact, zone, touch_ord, notional, front, dist)


def _practically_tied(a: dict[str, Any], b: dict[str, Any], *, wall_side: str, tick: float, dthr: DisambiguationThresholds) -> bool:
    """True if a and b are not distinguishable by causal rules."""
    if bool(a.get("exact_trade_at_edge")) != bool(b.get("exact_trade_at_edge")):
        return False
    if a.get("exact_trade_at_edge"):
        n0 = float(a.get("aggressive_notional_at_edge") or 0)
        n1 = float(b.get("aggressive_notional_at_edge") or 0)
        if max(n0, n1) <= 0:
            return True
        if abs(n0 - n1) / max(n0, n1) > dthr.notional_tie_rel:
            return False
        # first touch different?
        if a.get("first_touch_ts") and b.get("first_touch_ts") and a["first_touch_ts"] != b["first_touch_ts"]:
            return False
        # same price cluster?
        if abs(float(a["edge_price"]) - float(b["edge_price"])) <= tick * dthr.front_gap_ticks:
            return True
        return True  # equal notional + same touch → ambiguous
    # both no exact trade: unique front if price gap > front_gap
    gap = abs(float(a["edge_price"]) - float(b["edge_price"])) / tick
    if gap > dthr.front_gap_ticks:
        return False
    # zone notional
    z0 = float(a.get("aggressive_notional_near_edge") or 0)
    z1 = float(b.get("aggressive_notional_near_edge") or 0)
    if max(z0, z1) > 0 and abs(z0 - z1) / max(z0, z1) > dthr.notional_tie_rel:
        return False
    return True


def select_disambiguated_match(
    event: InputEvent,
    candidate_rows: list[dict[str, Any]],
    *,
    trades: list[Trade],
    flow_start_price: Optional[float],
    flow_vwap: Optional[float],
    flow_low: Optional[float],
    flow_high: Optional[float],
    thr: JoinThresholds,
    dthr: Optional[DisambiguationThresholds] = None,
) -> tuple[EdgeJoinResult, list[dict[str, Any]], list[dict[str, Any]]]:
    """Disambiguate among causal candidates using trades + directional path.

    Returns (join_result, enriched_candidate_rows, cluster_summaries).
    """
    dthr = dthr or DisambiguationThresholds()
    want_side = required_wall_side_for_event(event)
    side = aggressor_side(event.direction) if event.direction in {"LONG", "SHORT"} else None
    tick = tick_size(event.symbol)

    # Start from join-plausible rows (already side/causal/distance filtered)
    plausible = [
        c for c in candidate_rows if c.get("candidate_rejection_reason") is None and c.get("match_class")
    ]

    atrades = (
        aggressor_trades_in_flow(
            trades, flow_start=event.flow_start_ts, flow_end=event.flow_end_ts, side=side or ""
        )
        if side
        else []
    )

    enriched: list[dict[str, Any]] = []
    for c in plausible:
        enriched.append(
            enrich_candidate_with_trades(
                c,
                event=event,
                atrades=atrades,
                flow_start_price=flow_start_price,
                flow_vwap=flow_vwap,
                flow_low=flow_low,
                flow_high=flow_high,
                dthr=dthr,
            )
        )
    # also keep rejected rows for audit (unenriched)
    rejected = [c for c in candidate_rows if c not in plausible]

    cluster_summaries: list[dict[str, Any]] = []
    if want_side and enriched:
        enriched = assign_cluster_roles(
            enriched, wall_side=want_side, symbol=event.symbol, dthr=dthr
        )
        cluster_summaries = list(enriched[0].pop("_cluster_summaries", []) or [])

    # Mark nearer counts on all enriched already done inside assign_cluster_roles

    if flow_low is None or flow_high is None or want_side is None:
        join = EdgeJoinResult(
            aef_event_id=event.event_id,
            edge_join_status="DATA_INCOMPLETE",
            edge_match_explanation_codes=["DATA_INCOMPLETE"],
            edge_match_confidence_class="NONE",
            edge_match_candidate_count=len(plausible),
            candidates=candidate_rows,
        )
        return join, enriched + rejected, cluster_summaries

    reached = [c for c in enriched if c.get("reached_in_directional_path")]
    if not reached:
        # No wall actually attacked — do not keep MULTIPLE_EDGE_AMBIGUOUS
        join = EdgeJoinResult(
            aef_event_id=event.event_id,
            edge_join_status="EDGE_NOT_REACHED",
            edge_match_explanation_codes=["EDGE_NOT_REACHED", "NO_DIRECTIONAL_TOUCH"],
            edge_match_confidence_class="NONE",
            edge_match_candidate_count=len(plausible),
            candidates=candidate_rows,
        )
        return join, enriched + rejected, cluster_summaries

    ranked = sorted(reached, key=lambda c: _lex_key(c, want_side))
    best = ranked[0]

    # Ambiguity vs second
    ambiguous = False
    if len(ranked) > 1:
        second = ranked[1]
        if _practically_tied(best, second, wall_side=want_side, tick=tick, dthr=dthr):
            ambiguous = True

    if ambiguous:
        join = EdgeJoinResult(
            aef_event_id=event.event_id,
            edge_join_status="MULTIPLE_EDGE_AMBIGUOUS",
            matched_edge_id=best["edge_id"],
            matched_edge_price=best["edge_price"],
            matched_edge_source="raw_ob200_wall_lifecycle",
            matched_edge_available_ts=best.get("edge_available_ts"),
            matched_edge_distance_bps=best.get("distance_from_flow_start_bps")
            or best.get("distance_to_flow_start_price_bps"),
            matched_edge_age_seconds=best.get("last_seen_age_seconds"),
            matched_edge_persistence_seconds=best.get("persistence_seconds_asof_attack"),
            matched_edge_relative_size=best.get("relative_size_asof_attack"),
            matched_edge_notional_asof=best.get("notional_asof_attack"),
            edge_match_explanation_codes=[
                "MULTIPLE_EDGE_AMBIGUOUS",
                "BEST_CANDIDATE_ONLY",
                str(best.get("cluster_role") or ""),
            ],
            edge_match_confidence_class="LOW",
            edge_match_candidate_count=len(reached),
            candidates=candidate_rows,
        )
        return join, enriched + rejected, cluster_summaries

    # Determine status + confidence
    codes: list[str] = []
    if best.get("exact_trade_at_edge"):
        status = "EXACT_TRADED_EDGE"
        conf = "HIGH"
        codes.append("EXACT_TRADE")
    elif float(best.get("aggressive_notional_near_edge") or 0) > 0:
        status = "EDGE_ZONE_TRADED"
        conf = "MEDIUM"
        codes.append("ZONE_TRADE")
    elif best.get("cluster_role") in {"FRONT_EDGE", "SEPARATE_EDGE"} or (
        best.get("nearer_competing_edge_count") == 0
    ):
        if best.get("cluster_role") == "FRONT_EDGE":
            status = "CLUSTER_FRONT_EDGE_REACHED"
        else:
            status = "FRONT_EDGE_REACHED"
        conf = "HIGH"
        codes.append("FRONT_REACHED")
    elif best.get("flow_range_overlap") or best.get("overlap_with_flow_price_range"):
        status = "EDGE_RANGE_OVERLAP"
        conf = "MEDIUM"
        codes.append("RANGE_OVERLAP")
    else:
        status = "EDGE_NOT_REACHED"
        conf = "NONE"
        codes.append("WEAK_SPATIAL_ONLY")

    # Back edge alone without trade must not be HIGH
    if best.get("cluster_role") == "BACK_EDGE" and not best.get("exact_trade_at_edge"):
        if conf == "HIGH":
            conf = "MEDIUM"
            status = "EDGE_ZONE_TRADED" if float(best.get("aggressive_notional_near_edge") or 0) > 0 else "EDGE_RANGE_OVERLAP"
            codes.append("BACK_EDGE_DOWNGRADED")

    codes.append(f"CONFIDENCE_{conf}")
    if best.get("cluster_role"):
        codes.append(str(best["cluster_role"]))

    join = EdgeJoinResult(
        aef_event_id=event.event_id,
        edge_join_status=status,
        matched_edge_id=best["edge_id"],
        matched_edge_price=best["edge_price"],
        matched_edge_source="raw_ob200_wall_lifecycle",
        matched_edge_available_ts=best.get("edge_available_ts"),
        matched_edge_distance_bps=best.get("distance_from_flow_start_bps")
        or best.get("distance_to_flow_start_price_bps"),
        matched_edge_age_seconds=best.get("last_seen_age_seconds"),
        matched_edge_persistence_seconds=best.get("persistence_seconds_asof_attack"),
        matched_edge_relative_size=best.get("relative_size_asof_attack"),
        matched_edge_notional_asof=best.get("notional_asof_attack"),
        edge_match_explanation_codes=codes,
        edge_match_confidence_class=conf,
        edge_match_candidate_count=len(reached),
        candidates=candidate_rows,
    )
    return join, enriched + rejected, cluster_summaries
