"""Wall-migration proxy — same epoch + continuous coverage only. Not calibrated."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..drilldown.aggregation_100ms import _as_dt
from . import ORIGINAL_WALL_PRICE, TICK_SIZE, WALL_MIGRATION_STATUS
from .generations import AskWallGeneration


def _ms(a: datetime, b: datetime) -> int:
    return int(round((b - a).total_seconds() * 1000.0))


def build_migration_proxies(
    gens: list[AskWallGeneration],
    *,
    breach_iso: str | None,
    replay_epoch: int,
) -> list[dict[str, Any]]:
    """Pair consecutive generations by start time within one epoch.

    No threshold tuned on Episode-1. Output status always WALL_MIGRATION_NOT_CALIBRATED.
    """
    same = [g for g in gens if int(g.replay_epoch) == int(replay_epoch) and g.coverage_ok]
    same = sorted(same, key=lambda g: (_as_dt(g.start_time), g.price))
    breach = _as_dt(breach_iso) if breach_iso else None
    out: list[dict[str, Any]] = []
    for i in range(len(same) - 1):
        a = same[i]
        b = same[i + 1]
        if a.replay_epoch != b.replay_epoch:
            continue  # never cross epochs
        if a.end_time is None:
            continue
        t_end = _as_dt(a.end_time)
        t_start = _as_dt(b.start_time)
        gap = _ms(t_end, t_start)
        # Only consider proxies where next appears after previous ends (or small overlap)
        if gap < -1000:
            continue
        shift_ticks = (b.price - a.price) / TICK_SIZE
        mid_ref = a.mid_at_start or ORIGINAL_WALL_PRICE
        shift_bps = (b.price - a.price) / mid_ref * 10000.0 if mid_ref else None
        init_ratio = (b.initial_qty / a.initial_qty) if a.initial_qty > 0 else None
        peak_ratio = (b.peak_qty / a.peak_qty) if a.peak_qty > 0 else None
        # toward price: for ask wall, higher price is away from bid / with attack
        direction = "away_from_bid_up" if shift_ticks > 0 else ("toward_bid_down" if shift_ticks < 0 else "same_price")
        fill_den = a.cumulative_attributed_fills + a.cumulative_residual_pulls
        fill_share = a.cumulative_attributed_fills / fill_den if fill_den > 0 else None
        pull_share = a.cumulative_residual_pulls / fill_den if fill_den > 0 else None
        if breach is None:
            appear = "unknown"
        elif t_start < breach:
            appear = "before_breach"
        else:
            appear = "after_breach"
        # price follow delay left to caller enrichment; placeholder None here
        conf = "LOW"
        if a.price != b.price and gap <= 5000 and (init_ratio or 0) > 0:
            conf = "MEDIUM"
        out.append(
            {
                "previous_wall_generation_id": a.wall_generation_id,
                "next_wall_generation_id": b.wall_generation_id,
                "previous_price": a.price,
                "next_price": b.price,
                "time_gap_ms": gap,
                "price_shift_ticks": shift_ticks,
                "price_shift_bps": shift_bps,
                "initial_mass_ratio": init_ratio,
                "peak_mass_ratio": peak_ratio,
                "direction_toward_or_away_from_price": direction,
                "old_wall_fill_share": fill_share,
                "old_wall_pull_share": pull_share,
                "new_wall_appearance_before_or_after_breach": appear,
                "price_follow_delay_ms": None,
                "confidence": conf,
                "wall_migration_status": WALL_MIGRATION_STATUS,
                "replay_epoch": replay_epoch,
                "note": (
                    "Proxy only — Full L2 cannot prove same-order migration. "
                    "No Episode-1 calibrated threshold."
                ),
            }
        )
    return out


def enrich_price_follow(
    proxies: list[dict[str, Any]],
    *,
    best_ask_timeline: list[tuple[datetime, float]],
) -> list[dict[str, Any]]:
    """Set price_follow_delay_ms when best ask reaches next wall price after it appears."""
    out = []
    for p in proxies:
        item = dict(p)
        next_px = float(p["next_price"])
        # find appearance time from gens already encoded via gap; use timeline
        # delay = first time best_ask >= next_px after previous end — approximate via next gen start
        # Caller should pass timeline; we search first best_ask >= next_px
        hit = None
        for t, ba in best_ask_timeline:
            if ba + 1e-12 >= next_px:
                hit = t
                break
        if hit is not None and p.get("time_gap_ms") is not None:
            # relative to next wall start unknown here; leave as time from first BA touch of next price
            item["price_follow_delay_ms"] = None  # filled in pipeline with gen start
        out.append(item)
    return out
