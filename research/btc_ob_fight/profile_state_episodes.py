"""Profile-state episodes from chronological public trades (post-anchor observation)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .config import iso_z, utc
from .profile_edge_state import (
    STATE_BETWEEN_LOWER,
    STATE_BETWEEN_UPPER,
    STATE_INSIDE_BOTH,
    STATE_INVALID,
    STATE_OUTSIDE_ABOVE,
    STATE_OUTSIDE_BELOW,
    classify_price_state,
    distance_bps_from_edge,
)

PROFILE_STATE_EPISODES_CONTRACT = "profile_state_episodes_v1"
END_REASON_STATE_CHANGE = "STATE_CHANGE"
END_REASON_WINDOW_END = "WINDOW_END"


def build_profile_state_episodes(
    trades: list[dict[str, Any]],
    edges: dict[str, Any],
    *,
    anchor: datetime,
    window_end: datetime,
) -> dict[str, Any]:
    """Chronological profile-state episodes for ``anchor <= ts < window_end``."""
    anchor = utc(anchor)
    window_end = utc(window_end)
    obs = sorted(
        [t for t in trades if anchor <= t["ts"] < window_end],
        key=lambda t: (t["ts"], t["trade_id"]),
    )

    if edges.get("profile_state") != "VALID":
        return {
            "contract_version": PROFILE_STATE_EPISODES_CONTRACT,
            "profile_state": STATE_INVALID,
            "transitions": [],
            "episodes": [],
            "episode_count": 0,
        }

    transitions: list[dict[str, Any]] = []
    episodes: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    seq = 0

    for t in obs:
        cls = classify_price_state(float(t["price"]), edges)
        st = cls["state"]
        if current is None:
            current = _open_episode(seq, st, cls, t, edges)
            seq += 1
            continue
        if st != current["state"]:
            transitions.append(
                {
                    "transition_index": len(transitions),
                    "from_state": current["state"],
                    "to_state": st,
                    "transition_ts": iso_z(t["ts"]),
                    "trade_id": t["trade_id"],
                    "price": t["price"],
                }
            )
            current = _close_episode(current, t, END_REASON_STATE_CHANGE)
            episodes.append(current)
            current = _open_episode(seq, st, cls, t, edges)
            seq += 1
            continue
        _accumulate(current, t, edges)

    if current is not None:
        if obs:
            current = _close_episode(current, obs[-1], END_REASON_WINDOW_END, closed=False)
        episodes.append(current)

    return {
        "contract_version": PROFILE_STATE_EPISODES_CONTRACT,
        "profile_state": "VALID",
        "observation_start_utc": iso_z(anchor),
        "observation_end_utc": iso_z(window_end),
        "trade_count_observed": len(obs),
        "transitions": transitions,
        "episodes": episodes,
        "episode_count": len(episodes),
    }


def _open_episode(
    index: int,
    state: str,
    cls: dict[str, Any],
    trade: dict[str, Any],
    edges: dict[str, Any],
) -> dict[str, Any]:
    eid = f"pstate_{index:04d}_{state.lower()}"
    ref = cls.get("relevant_edge_price")
    dist = cls.get("distance_bps_from_relevant_edge") or 0.0
    buy_q = trade["notional"] if trade["side"] == "Buy" else 0.0
    sell_q = trade["notional"] if trade["side"] == "Sell" else 0.0
    return {
        "episode_id": eid,
        "episode_index": index,
        "state": state,
        "state_group": cls["state_group"],
        "direction": cls["direction"],
        "start_ts": iso_z(trade["ts"]),
        "end_ts": None,
        "duration_seconds": None,
        "start_price": trade["price"],
        "end_price": None,
        "min_price": trade["price"],
        "max_price": trade["price"],
        "price_change_bps": None,
        "trade_count": 1,
        "base_volume": trade["size"],
        "quote_notional": trade["notional"],
        "taker_buy_quote": buy_q,
        "taker_sell_quote": sell_q,
        "taker_delta_quote": buy_q - sell_q,
        "max_distance_bps_from_relevant_edge": dist,
        "closed": False,
        "end_reason": None,
        "relevant_edge_price": ref,
        "upper_outer_edge": edges.get("upper_outer_edge"),
        "lower_outer_edge": edges.get("lower_outer_edge"),
    }


def _accumulate(ep: dict[str, Any], trade: dict[str, Any], edges: dict[str, Any]) -> None:
    from .profile_edge_state import classify_price_state

    ep["trade_count"] += 1
    ep["base_volume"] += trade["size"]
    ep["quote_notional"] += trade["notional"]
    if trade["side"] == "Buy":
        ep["taker_buy_quote"] += trade["notional"]
        ep["taker_delta_quote"] += trade["notional"]
    else:
        ep["taker_sell_quote"] += trade["notional"]
        ep["taker_delta_quote"] -= trade["notional"]
    ep["min_price"] = min(ep["min_price"], trade["price"])
    ep["max_price"] = max(ep["max_price"], trade["price"])
    cls = classify_price_state(float(trade["price"]), edges)
    dist = cls.get("distance_bps_from_relevant_edge") or 0.0
    ep["max_distance_bps_from_relevant_edge"] = max(ep["max_distance_bps_from_relevant_edge"], dist)


def _close_episode(
    ep: dict[str, Any],
    trade: dict[str, Any],
    reason: str,
    *,
    closed: bool = True,
) -> dict[str, Any]:
    ep["end_ts"] = iso_z(trade["ts"])
    ep["end_price"] = trade["price"]
    ep["closed"] = closed
    ep["end_reason"] = reason
    start = datetime.fromisoformat(ep["start_ts"].replace("Z", "+00:00"))
    ep["duration_seconds"] = (trade["ts"] - start).total_seconds()
    if ep["start_price"]:
        ep["price_change_bps"] = (ep["end_price"] - ep["start_price"]) / ep["start_price"] * 10000.0
    return ep


def episodes_by_state(episodes: list[dict[str, Any]], *states: str) -> list[dict[str, Any]]:
    want = set(states)
    return [e for e in episodes if e.get("state") in want]


def episode_time_span(ep: dict[str, Any]) -> tuple[datetime, datetime]:
    start = datetime.fromisoformat(ep["start_ts"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(ep["end_ts"].replace("Z", "+00:00")) if ep.get("end_ts") else start
    return start, end
