"""Same-timestamp trade ordering audit (post-anchor observation window)."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from .config import iso_z, utc
from .profile_edge_state import (
    STATE_OUTSIDE_ABOVE,
    STATE_OUTSIDE_BELOW,
    build_frozen_profile_edges,
    classify_price_state,
)
from .profile_state_episodes import END_REASON_STATE_CHANGE

SAME_TIMESTAMP_ORDERING_CONTRACT = "same_timestamp_ordering_audit_v1"
UNAMBIGUOUS_SINGLE_STATE = "UNAMBIGUOUS_SINGLE_STATE"
AMBIGUOUS_MULTI_STATE = "AMBIGUOUS_MULTI_STATE"
ORDERING_TRADE_ID_LEX = "DETERMINISTIC_TRADE_ID_LEXICOGRAPHIC"
ORDERING_NOT_EXCHANGE_PROVEN = "EXCHANGE_EXECUTION_ORDER_NOT_PROVEN"


def build_same_timestamp_ordering_audit(
    trades: list[dict[str, Any]],
    edges: dict[str, Any],
    *,
    anchor: datetime,
    window_end: datetime,
    trades_meta: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Audit duplicate timestamps and profile-state ambiguity in observation trades."""
    anchor = utc(anchor)
    window_end = utc(window_end)
    obs = sorted(
        [t for t in trades if anchor <= t["ts"] < window_end],
        key=lambda t: (t["ts"], t["trade_id"]),
    )

    by_ts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in obs:
        by_ts[iso_z(t["ts"])].append(t)

    multistate_rows: list[dict[str, Any]] = []
    groups_multi = 0
    groups_multi_price = 0
    groups_inner_cross = 0
    groups_outer_cross = 0
    max_per_ts = max((len(v) for v in by_ts.values()), default=0)

    ui = edges.get("upper_inner_edge")
    uo = edges.get("upper_outer_edge")
    li = edges.get("lower_inner_edge")
    lo = edges.get("lower_outer_edge")

    for ts_key, chunk in sorted(by_ts.items()):
        states = set()
        prices = set()
        for t in chunk:
            cls = classify_price_state(float(t["price"]), edges)
            states.add(cls["state"])
            prices.add(float(t["price"]))
        if len(chunk) <= 1:
            continue
        quality = UNAMBIGUOUS_SINGLE_STATE if len(states) == 1 else AMBIGUOUS_MULTI_STATE
        if quality == AMBIGUOUS_MULTI_STATE:
            groups_multi += 1
        if len(prices) > 1:
            groups_multi_price += 1
        inner_hit = outer_hit = False
        if edges.get("profile_state") == "VALID":
            for p in prices:
                if ui is not None and li is not None and (p > ui or p < li):
                    inner_hit = True
                if uo is not None and lo is not None and (p > uo or p < lo):
                    outer_hit = True
            if inner_hit:
                groups_inner_cross += 1
            if outer_hit:
                groups_outer_cross += 1

        buy_q = sum(t["notional"] for t in chunk if t["side"] == "Buy")
        sell_q = sum(t["notional"] for t in chunk if t["side"] == "Sell")
        multistate_rows.append(
            {
                "timestamp": ts_key,
                "trade_count": len(chunk),
                "unique_prices": len(prices),
                "min_price": min(prices),
                "max_price": max(prices),
                "states_touched": "|".join(sorted(states)),
                "state_count": len(states),
                "ordering_quality": quality,
                "taker_buy_quote": buy_q,
                "taker_sell_quote": sell_q,
                "taker_delta_quote": buy_q - sell_q,
                "trade_ids_lex_order": ",".join(t["trade_id"] for t in sorted(chunk, key=lambda x: x["trade_id"])),
            }
        )

    zero_duration = 0
    outside_reclaim_same_ts = 0
    if obs and edges.get("profile_state") == "VALID":
        from .profile_state_episodes import build_profile_state_episodes

        bundle = build_profile_state_episodes(trades, edges, anchor=anchor, window_end=window_end)
        eps = bundle.get("episodes") or []
        trans = bundle.get("transitions") or []
        for ep in eps:
            if float(ep.get("duration_seconds") or 0) == 0:
                zero_duration += 1
        trans_by_ts = {tr["transition_ts"]: tr for tr in trans}
        for ts_key in by_ts:
            if ts_key not in trans_by_ts:
                continue
            tr = trans_by_ts[ts_key]
            if tr.get("from_state") in (STATE_OUTSIDE_ABOVE, STATE_OUTSIDE_BELOW):
                outside_reclaim_same_ts += 1

    meta = trades_meta or {}
    audit = {
        "contract_version": SAME_TIMESTAMP_ORDERING_CONTRACT,
        "observation_start_utc": iso_z(anchor),
        "observation_end_utc": iso_z(window_end),
        "total_trades": len(obs),
        "unique_timestamps": len(by_ts),
        "timestamp_groups_with_multiple_trades": sum(1 for v in by_ts.values() if len(v) > 1),
        "max_trades_per_timestamp": max_per_ts,
        "groups_with_multiple_prices": groups_multi_price,
        "groups_with_multiple_profile_states": groups_multi,
        "groups_with_inner_edge_cross": groups_inner_cross,
        "groups_with_outer_edge_cross": groups_outer_cross,
        "zero_duration_episode_count": zero_duration,
        "outside_reclaim_transitions_at_duplicate_timestamp": outside_reclaim_same_ts,
        "available_ordering_fields": ["trade_ts", "trade_id"],
        "trade_id_semantics": "ClickHouse canonical trade_id string; lexicographic sort is deterministic but not proven exchange execution order",
        "exchange_execution_order_field_present": False,
        "ordering_policy_applied": ORDERING_TRADE_ID_LEX,
        "exchange_order_proven": False,
        "canonical_policy": "AMBIGUOUS_MULTI_STATE groups do not invent intra-group reclaim chronology",
        "source_table": meta.get("table"),
        "deduped_trade_count": meta.get("deduped_count"),
    }
    return audit, multistate_rows


def ambiguous_timestamp_set(multistate_rows: list[dict[str, Any]]) -> set[str]:
    return {r["timestamp"] for r in multistate_rows if r.get("ordering_quality") == AMBIGUOUS_MULTI_STATE}
