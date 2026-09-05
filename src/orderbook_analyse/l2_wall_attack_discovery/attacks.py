"""Attack episode construction from wall lifecycles + samples + trades."""

from __future__ import annotations

from typing import Any

import pandas as pd

from orderbook_analyse.l2_wall_attack_discovery.models import (
    ATTACK_SIDE_BY_WALL,
    bps_between,
    safe_float,
    tick_size,
    ticks_between,
)
from orderbook_analyse.ob200_v3_raw_discovery.lifecycles_v2 import WallLifecycle
from orderbook_analyse.ob200_v3_raw_discovery.reconstruct import SampleRow


def _sample_at(samples: list[SampleRow], ts_ms: int) -> SampleRow | None:
    if not samples:
        return None
    # samples sorted; last with ts <= ts_ms
    lo, hi = 0, len(samples) - 1
    ans = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if samples[mid].ts_ms <= ts_ms:
            ans = samples[mid]
            lo = mid + 1
        else:
            hi = mid - 1
    return ans


def _wall_qty_at(sample: SampleRow | None, side: str) -> float | None:
    if sample is None:
        return None
    if side == "BID":
        return safe_float(sample.bid_wall_qty)
    return safe_float(sample.ask_wall_qty)


def _wall_price_at(sample: SampleRow | None, side: str) -> float | None:
    if sample is None:
        return None
    if side == "BID":
        return safe_float(sample.bid_wall_price)
    return safe_float(sample.ask_wall_price)


def _first_trade_contact(
    trades: pd.DataFrame,
    *,
    wall_price: float,
    side: str,
    symbol: str,
    start_ms: int,
    end_ms: int,
) -> tuple[int | None, float | None]:
    """First aggressive trade within 3 ticks of wall in [start, end)."""
    if trades.empty or wall_price <= 0:
        return None, None
    attack_side = ATTACK_SIDE_BY_WALL[side]
    tick = tick_size(symbol)
    band = 3 * tick
    sub = trades[(trades["ts_ms"] >= start_ms) & (trades["ts_ms"] < end_ms) & (trades["side"] == attack_side)]
    if sub.empty:
        return None, None
    near = sub[(sub["price"] - wall_price).abs() <= band]
    if near.empty:
        return None, None
    row = near.iloc[0]
    return int(row["ts_ms"]), float(row["price"])


def build_attack_episodes(
    lifecycles: list[WallLifecycle],
    samples_by_symbol: dict[str, list[SampleRow]],
    trades_by_symbol: dict[str, pd.DataFrame],
    *,
    seed: int = 42,
    cooldown_ms: int = 60_000,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """One primary attack per lifecycle physical approach; retests marked secondary."""
    episodes: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    seq = 0

    for lc in sorted(lifecycles, key=lambda x: (x.symbol, x.appear_ts)):
        samples = samples_by_symbol.get(lc.symbol, [])
        trades = trades_by_symbol.get(lc.symbol, pd.DataFrame())
        approach = lc.approach_ts
        touch = lc.touch_ts
        # Episode requires approach or touch or appear with later pull/break
        if approach is None and touch is None and lc.pull_ts is None and lc.break_ts is None:
            continue

        wall_price = float(lc.wall_price)
        side = lc.side
        # search window for first contact
        win0 = approach or lc.appear_ts
        win1 = (touch or lc.break_ts or lc.pull_ts or lc.end_ts) + 5_000
        trade_contact_ms, _ = _first_trade_contact(
            trades,
            wall_price=wall_price,
            side=side,
            symbol=lc.symbol,
            start_ms=win0,
            end_ms=win1,
        )

        # first contact = earliest of touch / trade-in-band
        candidates = [t for t in (touch, trade_contact_ms) if t is not None]
        if not candidates and lc.pull_ts is not None and approach is not None:
            # pulled before contact — still an episode for Pull study
            first_contact = None
            resolution_hint = "PULLED_BEFORE_CONTACT"
        elif not candidates:
            continue
        else:
            first_contact = min(candidates)
            resolution_hint = None

        seq += 1
        attack_id = f"atk_{seed}_{seq}"
        is_primary = True
        # secondary retests: additional touches after first + cooldown
        n_retest = max(0, int(lc.n_touch_events) - 1) if lc.n_touch_events else 0

        s_contact = _sample_at(samples, first_contact) if first_contact else _sample_at(samples, approach or lc.appear_ts)
        qty_contact = _wall_qty_at(s_contact, side)
        px_contact = _wall_price_at(s_contact, side) or wall_price
        mid = safe_float(s_contact.mid) if s_contact else None
        notional = None
        if qty_contact is not None and px_contact is not None:
            notional = qty_contact * px_contact

        active_end = lc.reclaim_ts or lc.break_ts or lc.pull_ts or lc.end_ts
        if first_contact is not None:
            active_end = max(active_end, first_contact)
        duration = None
        if first_contact is not None:
            duration = max(0, active_end - first_contact)

        ep = {
            "attack_id": attack_id,
            "wall_id": lc.lifecycle_id,
            "lifecycle_id": lc.lifecycle_id,
            "symbol": lc.symbol,
            "side": side,
            "direction": lc.direction,
            "is_primary": is_primary,
            "is_retest": False,
            "n_retests_in_lifecycle": n_retest,
            "approach_at": approach,
            "first_contact_at": first_contact,
            "active_attack_start_at": first_contact,
            "active_attack_end_at": active_end,
            "resolution_at": lc.reclaim_ts or lc.break_ts or lc.pull_ts or lc.end_ts,
            "wall_price_at_contact": px_contact,
            "wall_size_at_contact": qty_contact,
            "wall_notional_at_contact": notional,
            "wall_dist_bps_at_contact": bps_between(px_contact, mid, mid) if mid and px_contact else None,
            "wall_dist_ticks_at_contact": ticks_between(px_contact, mid, lc.symbol) if mid and px_contact else None,
            "attack_duration_ms": duration,
            "lifecycle_completion_v2": lc.completion_class,
            "resolution_hint_pre": resolution_hint,
            "source_quality": "OK" if first_contact or resolution_hint else "PARTIAL",
            "cooldown_ms": cooldown_ms,
            "semantic_role": "metadata",
        }
        episodes.append(ep)
        events.append(
            {
                "event_id": f"{attack_id}_contact",
                "attack_id": attack_id,
                "lifecycle_id": lc.lifecycle_id,
                "symbol": lc.symbol,
                "side": side,
                "event_type": "FIRST_CONTACT" if first_contact else "NO_CONTACT_PULL",
                "ts_ms": first_contact or lc.pull_ts or approach,
                "wall_price": px_contact,
                "wall_qty": qty_contact,
            }
        )

        # secondary retest markers (no duplicate primary)
        if n_retest > 0 and touch is not None:
            for r in range(n_retest):
                seq += 1
                rid = f"atk_{seed}_{seq}"
                episodes.append(
                    {
                        **ep,
                        "attack_id": rid,
                        "is_primary": False,
                        "is_retest": True,
                        "parent_attack_id": attack_id,
                        "first_contact_at": touch,  # approximate; V2 stores first touch only
                        "source_quality": "RETEST_APPROX",
                    }
                )

    return episodes, events
