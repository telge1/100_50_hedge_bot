"""Direction-neutral raw book features for one evidence window."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..bounded_level_first_analyzer_pilot_v1.episodes import parse_utc
from ..drilldown.imbalance import SAFE_DIV_EPS, depth_imbalance, row_band_depths, row_imbalance
from ..timeparse import format_utc_z
from .sides import price_in


def _phase(ts: datetime, touch: datetime, detection: datetime) -> str:
    if ts < touch:
        return "PRE_TOUCH"
    if ts == touch:
        return "TOUCH"
    if ts < detection:
        return "POST_TOUCH_TO_DETECTION"
    return "AT_OR_AFTER_DETECTION"


def _as_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return parse_utc(value)


def summarize_states(
    states: list[dict[str, Any]],
    *,
    first_touch: datetime,
    detection: datetime,
) -> dict[str, Any]:
    valid = []
    crossed = 0
    for row in states:
        ts = _as_dt(row["bucket_start"])
        if ts >= detection:
            continue
        if row.get("best_bid") is not None and row.get("best_ask") is not None and row["best_bid"] >= row["best_ask"]:
            crossed += 1
            continue
        if row.get("book_valid") in {1, True} or (
            row.get("best_bid") is not None
            and row.get("best_ask") is not None
            and row["best_bid"] < row["best_ask"]
        ):
            valid.append({**row, "_ts": ts, "_phase": _phase(ts, first_touch, detection)})
    by_phase: dict[str, list[dict[str, Any]]] = {
        "PRE_TOUCH": [],
        "TOUCH": [],
        "POST_TOUCH_TO_DETECTION": [],
    }
    for row in valid:
        by_phase[row["_phase"]].append(row)

    def last(phase: str) -> dict[str, Any] | None:
        rows = by_phase[phase]
        return rows[-1] if rows else None

    def first(phase: str) -> dict[str, Any] | None:
        rows = by_phase[phase]
        return rows[0] if rows else None

    pre = last("PRE_TOUCH")
    post = last("POST_TOUCH_TO_DETECTION") or last("TOUCH")
    touch = first("TOUCH") or last("PRE_TOUCH")

    def pack(row: dict[str, Any] | None) -> dict[str, Any]:
        if row is None:
            return {
                "best_bid": None,
                "best_ask": None,
                "spread": None,
                "mid": None,
                "bid_depth_0_2": None,
                "ask_depth_0_2": None,
                "depth_imbalance": None,
            }
        bid, ask = row_band_depths(row)
        bb, ba = row.get("best_bid"), row.get("best_ask")
        mid = row.get("mid_price")
        spread = (ba - bb) if bb is not None and ba is not None else None
        return {
            "best_bid": bb,
            "best_ask": ba,
            "spread": spread,
            "mid": mid,
            "bid_depth_0_2": bid,
            "ask_depth_0_2": ask,
            "depth_imbalance": row_imbalance(row),
        }

    pre_p, post_p, touch_p = pack(pre), pack(post), pack(touch)
    bid_change = None
    ask_change = None
    if pre_p["bid_depth_0_2"] is not None and post_p["bid_depth_0_2"] is not None:
        bid_change = post_p["bid_depth_0_2"] - pre_p["bid_depth_0_2"]
    if pre_p["ask_depth_0_2"] is not None and post_p["ask_depth_0_2"] is not None:
        ask_change = post_p["ask_depth_0_2"] - pre_p["ask_depth_0_2"]
    imb_change = None
    if pre_p["depth_imbalance"] is not None and post_p["depth_imbalance"] is not None:
        imb_change = post_p["depth_imbalance"] - pre_p["depth_imbalance"]
    return {
        "valid_state_count": len(valid),
        "crossed_bucket_count": crossed,
        "n_pre_touch": len(by_phase["PRE_TOUCH"]),
        "n_touch": len(by_phase["TOUCH"]),
        "n_post_touch": len(by_phase["POST_TOUCH_TO_DETECTION"]),
        "pre_touch": pre_p,
        "touch": touch_p,
        "post_touch_to_detection": post_p,
        "bid_depth_change": bid_change,
        "ask_depth_change": ask_change,
        "depth_imbalance_change": imb_change,
        "first_valid_ts": format_utc_z(valid[0]["_ts"]) if valid else None,
        "last_valid_ts": format_utc_z(valid[-1]["_ts"]) if valid else None,
        "_valid_rows": valid,
        "_pre_row": pre,
        "_post_row": post,
    }


def zone_liquidity(bids: dict[float, float], asks: dict[float, float], band: tuple[float, float]) -> dict[str, float]:
    bid_n = sum(px * qty for px, qty in bids.items() if qty > 0 and price_in(band, px))
    ask_n = sum(px * qty for px, qty in asks.items() if qty > 0 and price_in(band, px))
    return {
        "bid_notional": bid_n,
        "ask_notional": ask_n,
        "imbalance": depth_imbalance(bid_n, ask_n) if (bid_n + ask_n) > SAFE_DIV_EPS else None,
    }


def raw_book_features(
    *,
    bundle: dict[str, Any],
    first_touch: datetime,
    detection: datetime,
    spatial: dict[str, Any],
) -> dict[str, Any]:
    replay = bundle.get("replay") or {}
    states = bundle.get("states_100ms") or []
    summary = summarize_states(states, first_touch=first_touch, detection=detection)
    bids = bundle.get("bids_at_touch") or replay.get("initial_bids") or {}
    asks = bundle.get("asks_at_touch") or replay.get("initial_asks") or {}
    final_bids = replay.get("final_bids") or {}
    final_asks = replay.get("final_asks") or {}
    return {
        "best_bid": summary["post_touch_to_detection"]["best_bid"],
        "best_ask": summary["post_touch_to_detection"]["best_ask"],
        "spread": summary["post_touch_to_detection"]["spread"],
        "mid": summary["post_touch_to_detection"]["mid"],
        "valid_state_count": summary["valid_state_count"],
        "crossed_bucket_count": summary["crossed_bucket_count"],
        "sequence_gaps": bundle.get("sequence_gaps"),
        "replay_epochs": bundle.get("replay_epoch"),
        "ordering_quality": bundle.get("ordering_confidence"),
        "bid_depth_0_2_pre": summary["pre_touch"]["bid_depth_0_2"],
        "ask_depth_0_2_pre": summary["pre_touch"]["ask_depth_0_2"],
        "bid_depth_0_2_post": summary["post_touch_to_detection"]["bid_depth_0_2"],
        "ask_depth_0_2_post": summary["post_touch_to_detection"]["ask_depth_0_2"],
        "depth_imbalance_pre": summary["pre_touch"]["depth_imbalance"],
        "depth_imbalance_post": summary["post_touch_to_detection"]["depth_imbalance"],
        "bid_depth_change": summary["bid_depth_change"],
        "ask_depth_change": summary["ask_depth_change"],
        "depth_imbalance_change": summary["depth_imbalance_change"],
        "liquidity_in_level_at_touch": zone_liquidity(bids, asks, tuple(spatial["level_zone"])),
        "liquidity_in_front_at_touch": zone_liquidity(bids, asks, tuple(spatial["front_zone"])),
        "liquidity_in_back_at_touch": zone_liquidity(bids, asks, tuple(spatial["back_zone"])),
        "liquidity_in_level_at_end": zone_liquidity(final_bids, final_asks, tuple(spatial["level_zone"])),
        "liquidity_in_front_at_end": zone_liquidity(final_bids, final_asks, tuple(spatial["front_zone"])),
        "liquidity_in_back_at_end": zone_liquidity(final_bids, final_asks, tuple(spatial["back_zone"])),
        "dominant_side_pre": _dominant(summary["pre_touch"]["depth_imbalance"]),
        "dominant_side_post": _dominant(summary["post_touch_to_detection"]["depth_imbalance"]),
        "first_valid_ts": summary["first_valid_ts"],
        "last_valid_ts": summary["last_valid_ts"],
        "_summary": summary,
    }


def _dominant(imb: float | None) -> str | None:
    if imb is None:
        return None
    if imb > SAFE_DIV_EPS:
        return "bid"
    if imb < -SAFE_DIV_EPS:
        return "ask"
    return "flat"
