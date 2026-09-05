"""Causal reclaim and breakout confirmation state machines."""

from __future__ import annotations

from typing import Any

from orderbook_analyse.l2_wall_to_wall_discovery import BREAK_DISTANCE_BPS
from orderbook_analyse.l2_wall_to_wall_discovery.models import (
    bps_between,
    sample_at,
    samples_between,
    trade_side_for_module,
    wall_qty,
)
from orderbook_analyse.ob200_v3_raw_discovery.reconstruct import SampleRow


def _beyond_break(side: str, mid: float, wall: float) -> bool:
    """True if mid is on the break/attack side of the wall."""
    if side == "BID":
        return mid < wall
    return mid > wall


def _on_reclaim_side(side: str, mid: float, wall: float) -> bool:
    """True if mid is back on the defending side of the wall."""
    if side == "BID":
        return mid >= wall
    return mid <= wall


def _hold_duration_ms(
    path: list[SampleRow],
    *,
    start_i: int,
    predicate,
) -> int:
    if start_i >= len(path):
        return 0
    t0 = path[start_i].ts_ms
    t1 = t0
    for s in path[start_i:]:
        if not predicate(s):
            break
        t1 = s.ts_ms
    return max(0, t1 - t0)


def detect_reclaim_signals(
    episode: dict[str, Any],
    samples: list[SampleRow],
    ts_index: list[int],
    proxy_5s: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Causal reclaim confirmations after first_contact; one row per variant that fires."""
    fc = episode.get("first_contact_at")
    wall = episode.get("wall_price_at_contact")
    if fc is None or fc == "" or wall is None or wall == "":
        return []
    fc = int(float(fc))
    wall = float(wall)
    side = episode["side"]
    # inclusive of first_contact sample
    path = samples_between(samples, ts_index, fc - 1, fc + 300_000)
    if not path:
        return []

    # find first reclaim cross after a genuine beyond/pierce (not the contact sample itself)
    crossed_i = None
    saw_beyond = False
    saw_touch = False
    for i, s in enumerate(path):
        if _beyond_break(side, s.mid, wall):
            saw_beyond = True
            saw_touch = True
        elif abs(s.mid - wall) / wall * 10000 <= 1.0:
            saw_touch = True
        if saw_beyond and _on_reclaim_side(side, s.mid, wall):
            crossed_i = i
            break
        # bounce without pierce: after touch, mid moves away on reclaim side
        if (
            (not saw_beyond)
            and saw_touch
            and _on_reclaim_side(side, s.mid, wall)
            and abs(s.mid - wall) / wall * 10000 > 1.5
            and i > 0
        ):
            crossed_i = i
            break
    if crossed_i is None:
        return []

    cross_ts = path[crossed_i].ts_ms
    hold_ms = _hold_duration_ms(
        path, start_i=crossed_i, predicate=lambda s: _on_reclaim_side(side, s.mid, wall)
    )

    # retest: after cross, mid approaches wall again then holds reclaim side
    retest_ok = False
    retest_ts = None
    after = path[crossed_i:]
    approached = False
    for s in after[1:]:
        dist = abs(s.mid - wall) / wall * 10000 if wall else 999
        if dist <= 1.5:
            approached = True
        elif approached and _on_reclaim_side(side, s.mid, wall) and dist > 1.5:
            retest_ok = True
            retest_ts = s.ts_ms
            break

    refill_ok = False
    if proxy_5s:
        resili = proxy_5s.get("resilience_ratio")
        refill = proxy_5s.get("refill_ratio")
        absorb = str(proxy_5s.get("absorption_proxy")).lower() in ("true", "1")
        try:
            refill_ok = absorb or (resili is not None and float(resili) >= 0.6) or (
                refill is not None and float(refill) >= 0.5
            )
        except (TypeError, ValueError):
            refill_ok = absorb
    # proxy window is 5s after contact — confirm only after that causal horizon
    refill_conf_ts = max(cross_ts, fc + 5_000)

    variants: list[tuple[str, int, bool]] = []
    # confirmed_at for each variant
    variants.append(("R1_CROSS", cross_ts, True))
    if hold_ms >= 1000:
        # confirmation when 1s hold completes
        conf = cross_ts + 1000
        variants.append(("R2_HOLD_1S", conf, True))
    if hold_ms >= 3000:
        variants.append(("R3_HOLD_3S", cross_ts + 3000, True))
    if retest_ok and retest_ts is not None:
        variants.append(("R4_RETEST_HOLD", retest_ts, True))
    if refill_ok:
        variants.append(("R5_REFILL_RECLAIM", refill_conf_ts, True))

    out = []
    pos = trade_side_for_module("WALL_HOLD_RECLAIM", side)
    for var, conf_ts, ok in variants:
        if not ok:
            continue
        entry_s = sample_at(samples, ts_index, conf_ts)
        # first sample strictly after confirmed_at
        entry_path = samples_between(samples, ts_index, conf_ts, conf_ts + 60_000)
        if not entry_path:
            continue
        entry = entry_path[0]
        out.append(
            {
                "signal_id": f"{episode['attack_id']}_{var}",
                "attack_id": episode["attack_id"],
                "lifecycle_id": episode.get("lifecycle_id") or episode.get("wall_id"),
                "module": "WALL_HOLD_RECLAIM",
                "variant": var,
                "symbol": episode["symbol"],
                "wall_side": side,
                "position_side": pos,
                "wall_price": wall,
                "first_contact_at": fc,
                "confirmed_at": conf_ts,
                "entry_at": entry.ts_ms,
                "entry_mid": entry.mid,
                "entry_latency_ms": entry.ts_ms - conf_ts,
                "hold_ms_at_confirm": hold_ms,
                "semantic_role": "causal_feature",
            }
        )
    return out


def detect_breakout_signals(
    episode: dict[str, Any],
    samples: list[SampleRow],
    ts_index: list[int],
    proxy_5s: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    fc = episode.get("first_contact_at")
    wall = episode.get("wall_price_at_contact")
    if fc is None or fc == "" or wall is None or wall == "":
        return []
    fc = int(float(fc))
    wall = float(wall)
    side = episode["side"]
    path = samples_between(samples, ts_index, fc - 1, fc + 300_000)
    if not path:
        return []

    # first time beyond wall
    break_i = None
    for i, s in enumerate(path):
        if _beyond_break(side, s.mid, wall):
            break_i = i
            break
    if break_i is None:
        return []

    break_ts = path[break_i].ts_ms
    hold_ms = _hold_duration_ms(
        path, start_i=break_i, predicate=lambda s: _beyond_break(side, s.mid, wall)
    )
    # distance confirm
    dist_ok = False
    dist_ts = None
    for s in path[break_i:]:
        d = bps_between(s.mid, wall, wall)
        if d is not None and d >= BREAK_DISTANCE_BPS and _beyond_break(side, s.mid, wall):
            dist_ok = True
            dist_ts = s.ts_ms
            break

    # retest fail: returns near wall from break side then fails to reclaim
    retest_fail = False
    retest_ts = None
    near = False
    for s in path[break_i + 1 :]:
        d = abs(s.mid - wall) / wall * 10000
        if d <= 1.5:
            near = True
        elif near and _beyond_break(side, s.mid, wall) and d > 1.5:
            retest_fail = True
            retest_ts = s.ts_ms
            break
        elif near and _on_reclaim_side(side, s.mid, wall):
            break  # reclaimed — not fail

    removed_ok = False
    if proxy_5s:
        pull = str(proxy_5s.get("pull_proxy")).lower() in ("true", "1")
        deplete = proxy_5s.get("depletion_ratio")
        resili = proxy_5s.get("resilience_ratio")
        try:
            removed_ok = pull or (deplete is not None and float(deplete) >= 0.5) or (
                resili is not None and float(resili) <= 0.4
            )
        except (TypeError, ValueError):
            removed_ok = pull

    candidates: list[tuple[str, int]] = []
    if hold_ms >= 1000:
        candidates.append(("B1_HOLD_1S", break_ts + 1000))
    if hold_ms >= 3000:
        candidates.append(("B2_HOLD_3S", break_ts + 3000))
    if dist_ok and dist_ts is not None:
        candidates.append(("B3_DISTANCE_CONFIRM", dist_ts))
    if retest_fail and retest_ts is not None:
        candidates.append(("B4_RETEST_FAIL", retest_ts))
    if removed_ok and hold_ms >= 1000:
        # pull/depletion proxy is causal only after the 5s attribution window
        candidates.append(("B5_WALL_REMOVED_CONFIRM", max(break_ts + 1000, fc + 5_000)))

    out = []
    pos = trade_side_for_module("WALL_REMOVED_BREAK", side)
    for var, conf_ts in candidates:
        entry_path = samples_between(samples, ts_index, conf_ts, conf_ts + 60_000)
        if not entry_path:
            continue
        entry = entry_path[0]
        out.append(
            {
                "signal_id": f"{episode['attack_id']}_{var}",
                "attack_id": episode["attack_id"],
                "lifecycle_id": episode.get("lifecycle_id") or episode.get("wall_id"),
                "module": "WALL_REMOVED_BREAK",
                "variant": var,
                "symbol": episode["symbol"],
                "wall_side": side,
                "position_side": pos,
                "wall_price": wall,
                "first_contact_at": fc,
                "confirmed_at": conf_ts,
                "entry_at": entry.ts_ms,
                "entry_mid": entry.mid,
                "entry_latency_ms": entry.ts_ms - conf_ts,
                "hold_ms_at_confirm": hold_ms,
                "semantic_role": "causal_feature",
            }
        )
    return out
