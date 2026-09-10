"""Epoch-4 breach structure Q&A and Epoch-5 independent landscape."""

from __future__ import annotations

from typing import Any

from ..drilldown.aggregation_100ms import _as_dt
from . import (
    ASK_WALL_BREACH,
    EPOCH4_COVERAGE_END,
    LAYER_POST_BREACH,
    LAYER_POST_EPOCH,
    LAYER_PRE_EXISTING,
    ORIGINAL_WALL_GENERATION_ID,
    ORIGINAL_WALL_PRICE,
)
from .generations import AskWallGeneration
from .segments import EpochSegment, book_state_at


def analyze_epoch4_breach_structure(
    *,
    gens: list[AskWallGeneration],
    liquidity_rows: list[dict[str, Any]],
    original_wall_price: float = ORIGINAL_WALL_PRICE,
) -> dict[str, Any]:
    breach = _as_dt(ASK_WALL_BREACH)
    end = _as_dt(EPOCH4_COVERAGE_END)
    ep4 = [g for g in gens if g.replay_epoch == 4]

    original = [
        g
        for g in ep4
        if abs(g.price - original_wall_price) <= 1e-9
        and (
            g.wall_generation_id == ORIGINAL_WALL_GENERATION_ID
            or (_as_dt(g.start_time) < breach and (g.end_time is None or _as_dt(g.end_time) <= breach))
        )
    ]
    # Prefer gen ending near known end
    if not original:
        original = [g for g in ep4 if abs(g.price - original_wall_price) <= 1e-9]
    orig = sorted(original, key=lambda g: _as_dt(g.start_time))[-1] if original else None

    fill_pull = None
    if orig is not None:
        den = orig.cumulative_attributed_fills + orig.cumulative_residual_pulls
        fill_pull = {
            "wall_generation_id": orig.wall_generation_id,
            "fills": orig.cumulative_attributed_fills,
            "pulls": orig.cumulative_residual_pulls,
            "refills": orig.cumulative_refills,
            "termination_reason": orig.termination_reason,
            "mostly": (
                "FILLED"
                if den > 0 and orig.cumulative_attributed_fills / den >= 0.7
                else (
                    "PULLED"
                    if den > 0 and orig.cumulative_residual_pulls / den >= 0.7
                    else ("MIXED" if den > 0 else "UNKNOWN")
                )
            ),
            "fill_share": (orig.cumulative_attributed_fills / den) if den > 0 else None,
            "pull_share": (orig.cumulative_residual_pulls / den) if den > 0 else None,
        }

    pre_above = [
        g.to_dict()
        for g in ep4
        if g.layer_class == LAYER_PRE_EXISTING and g.price > original_wall_price + 1e-9
    ]
    post_new = [
        g.to_dict()
        for g in ep4
        if g.layer_class == LAYER_POST_BREACH
        and _as_dt(g.start_time) >= breach
        and _as_dt(g.start_time) <= end
    ]

    # Centroid movement
    pre_rows = [r for r in liquidity_rows if _as_dt(r["timestamp"]) < breach]
    post_rows = [r for r in liquidity_rows if _as_dt(r["timestamp"]) >= breach]
    cent_pre = next((r["ask_liquidity_centroid"] for r in reversed(pre_rows) if r.get("ask_liquidity_centroid") is not None), None)
    cent_post = next((r["ask_liquidity_centroid"] for r in reversed(post_rows) if r.get("ask_liquidity_centroid") is not None), None)
    cent_end = post_rows[-1]["ask_liquidity_centroid"] if post_rows else None
    centroid_moved_higher = None
    if cent_pre is not None and cent_end is not None:
        centroid_moved_higher = float(cent_end) > float(cent_pre) + 1e-9

    # Price follow nearest ask
    follow = None
    if post_rows:
        last = post_rows[-1]
        ba = last.get("best_ask")
        nr = last.get("nearest_relevant_ask_price")
        follow = {
            "best_ask_at_coverage_end": ba,
            "nearest_relevant_ask_at_coverage_end": nr,
            "price_at_or_near_nearest_ask": (
                ba is not None and nr is not None and abs(float(ba) - float(nr)) <= 0.1000001
            ),
        }

    active_at_end = [
        g.to_dict()
        for g in ep4
        if g.end_time is None
        or _as_dt(g.end_time) > end
        or (g.termination_reason == "STILL_ACTIVE_AT_SEGMENT_END")
    ]

    stacked = len(pre_above) >= 2

    return {
        "replay_epoch": 4,
        "original_wall_fill_vs_pull": fill_pull,
        "pre_existing_ask_walls_above_original": pre_above,
        "n_pre_existing_above": len(pre_above),
        "post_breach_new_generations_before_epoch_end": post_new,
        "n_post_breach_new": len(post_new),
        "ask_centroid_before_breach": cent_pre,
        "ask_centroid_at_coverage_end": cent_end,
        "ask_centroid_moved_higher": centroid_moved_higher,
        "price_follow_nearest_ask": follow,
        "active_walls_at_epoch4_coverage_end": active_at_end,
        "stacked_defense_visible_within_epoch4": stacked,
        "stacked_defense_note": (
            "Multiple PRE_EXISTING ask layers above original wall before breach "
            "indicate stacked defense structure (not participant identity)."
            if stacked
            else "Fewer than two pre-existing layers above original wall."
        ),
    }


def analyze_epoch5_independent_landscape(
    *,
    payload: dict[str, Any],
    segment: EpochSegment,
    gens: list[AskWallGeneration],
) -> dict[str, Any]:
    """Describe epoch-5 walls from checkpoint — no link to epoch-4 generation IDs."""
    from datetime import timedelta

    state = book_state_at(
        payload,
        until=_as_dt(segment.checkpoint_event_time) + timedelta(milliseconds=1),
        require_epoch=None,
    )
    ep5_gens = [g.to_dict() for g in gens if g.replay_epoch == 5 or g.layer_class == LAYER_POST_EPOCH]
    # Hard assert: no epoch4 ids in ep5 set
    for g in ep5_gens:
        if int(g["replay_epoch"]) == 4:
            raise RuntimeError("epoch4 generation leaked into epoch5 landscape")
        if g.get("layer_class") != LAYER_POST_EPOCH and int(g["replay_epoch"]) == 5:
            g["layer_class"] = LAYER_POST_EPOCH

    asks = state.get("asks") or {}
    top = sorted(
        ((float(px), float(q)) for px, q in asks.items() if q and float(q) > 0),
        key=lambda x: x[0],
    )[:15]

    return {
        "replay_epoch": 5,
        "anchor_mode": "checkpoint_reanchor",
        "checkpoint": segment.checkpoint_event_time.isoformat().replace("+00:00", "Z")
        if segment.checkpoint_event_time
        else None,
        "book_at_checkpoint_best_bid": state.get("best_bid"),
        "book_at_checkpoint_best_ask": state.get("best_ask"),
        "book_at_checkpoint_top_asks": [{"price": p, "qty": q} for p, q in top],
        "epoch5_generations": ep5_gens,
        "n_epoch5_generations": len(ep5_gens),
        "continuity_to_epoch4": False,
        "note": (
            "Independent segment only. POST_EPOCH_INDEPENDENT generations must not be "
            "classified as migration of Epoch-4 walls."
        ),
    }
