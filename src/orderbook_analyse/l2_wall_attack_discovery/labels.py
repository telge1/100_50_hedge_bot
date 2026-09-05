"""Ex-post resolution labels (future allowed; never as causal features)."""

from __future__ import annotations

import bisect
from typing import Any

from orderbook_analyse.l2_wall_attack_discovery import RESOLUTION_HORIZONS_S
from orderbook_analyse.l2_wall_attack_discovery.models import safe_float
from orderbook_analyse.ob200_v3_raw_discovery.reconstruct import SampleRow


def _sample_at(samples: list[SampleRow], ts_index: list[int], ts_ms: int) -> SampleRow | None:
    if not samples:
        return None
    i = bisect.bisect_right(ts_index, ts_ms) - 1
    if i < 0:
        return None
    return samples[i]


def _qty(s: SampleRow | None, side: str) -> float | None:
    if s is None:
        return None
    return safe_float(s.bid_wall_qty if side == "BID" else s.ask_wall_qty)


def _mid(s: SampleRow | None) -> float | None:
    return None if s is None else safe_float(s.mid)


def _broke(side: str, mid: float, wall: float, hold_bps: float = 1.0) -> bool:
    if side == "BID":
        return mid < wall * (1 - hold_bps / 10000)
    return mid > wall * (1 + hold_bps / 10000)


def _away(side: str, mid: float, wall: float, away_bps: float = 2.0) -> bool:
    if side == "BID":
        return mid > wall * (1 + away_bps / 10000)
    return mid < wall * (1 - away_bps / 10000)


def classify_resolution(
    episode: dict[str, Any],
    samples: list[SampleRow],
    proxy_by_h: dict[int, dict[str, Any]],
    *,
    horizon_s: int,
    ts_index: list[int] | None = None,
) -> dict[str, Any]:
    """Ex-post label at a single horizon. Uses path after contact."""
    out = {
        "attack_id": episode["attack_id"],
        "horizon_s": horizon_s,
        "resolution_class": "DATA_UNAVAILABLE",
        "semantic_role": "ex_post_label",
        "break_observed": False,
        "reclaim_observed": False,
        "failure_reason": "",
    }
    if episode.get("resolution_hint_pre") == "PULLED_BEFORE_CONTACT":
        out["resolution_class"] = "PULLED_BEFORE_CONTACT"
        return out
    fc = episode.get("first_contact_at")
    wall = safe_float(episode.get("wall_price_at_contact"))
    side = episode["side"]
    if fc is None or wall is None:
        out["failure_reason"] = "no_contact"
        return out

    tss = ts_index if ts_index is not None else [s.ts_ms for s in samples]
    t1 = int(fc) + horizon_s * 1000
    s0 = _sample_at(samples, tss, int(fc))
    s1 = _sample_at(samples, tss, t1)
    m0, m1 = _mid(s0), _mid(s1)
    q0, q1 = _qty(s0, side), _qty(s1, side)
    if m0 is None or m1 is None:
        out["failure_reason"] = "missing_mid"
        return out

    proxy = proxy_by_h.get(min(horizon_s, 60), {})
    i0 = bisect.bisect_right(tss, int(fc))
    i1 = bisect.bisect_right(tss, t1)
    broke = False
    reclaim = False
    for s in samples[i0:i1]:
        mid = _mid(s)
        if mid is None:
            continue
        if _broke(side, mid, wall):
            broke = True
        elif broke:
            if side == "BID" and mid >= wall:
                reclaim = True
            if side == "ASK" and mid <= wall:
                reclaim = True

    out["break_observed"] = broke
    out["reclaim_observed"] = reclaim

    attack_n = proxy.get("attack_side_notional") or 0
    deplete = proxy.get("depletion_ratio")
    refill = proxy.get("refill_ratio")
    resili = proxy.get("resilience_ratio")
    pull = bool(proxy.get("pull_proxy"))
    absorb = bool(proxy.get("absorption_proxy"))
    t2d = proxy.get("trade_to_display_ratio")
    flow_died = attack_n <= 0 or (proxy.get("trades_present") is False)

    if flow_died and not broke and (resili is None or resili < 0.5) and not absorb:
        out["resolution_class"] = "FLOW_DIED_NO_DEFENSE"
        out["failure_reason"] = "attack_flow_absent"
        return out
    if broke and reclaim:
        out["resolution_class"] = "BREAK_RECLAIM"
        return out
    if broke and not reclaim:
        if _broke(side, m1, wall, hold_bps=1.5):
            out["resolution_class"] = "CLEAN_BREAK_CONTINUATION"
        else:
            out["resolution_class"] = "AMBIGUOUS"
            out["failure_reason"] = "break_without_clear_continuation_or_reclaim"
        return out
    if pull and (t2d is None or t2d < 0.35):
        out["resolution_class"] = "PULLED_ON_CONTACT"
        return out
    if absorb or (refill is not None and refill >= 0.5 and attack_n > 0 and not broke):
        out["resolution_class"] = "ABSORBED_REFILLED"
        return out
    if not broke and q1 is not None and q0 is not None and q1 >= 0.5 * q0 and _away(side, m1, wall):
        out["resolution_class"] = "DEFENDED"
        return out
    if not broke and attack_n > 0 and (deplete is None or deplete < 0.3):
        out["resolution_class"] = "DEFENDED"
        return out
    if flow_died:
        out["resolution_class"] = "FLOW_DIED_NO_DEFENSE"
        return out
    out["resolution_class"] = "AMBIGUOUS"
    out["failure_reason"] = "mixed_signals"
    return out


def label_all_horizons(
    episode: dict[str, Any],
    samples: list[SampleRow],
    proxies: list[dict[str, Any]],
    *,
    ts_index: list[int] | None = None,
) -> list[dict[str, Any]]:
    by_h = {int(p["horizon_s"]): p for p in proxies}
    tss = ts_index if ts_index is not None else [s.ts_ms for s in samples]
    return [
        classify_resolution(episode, samples, by_h, horizon_s=h, ts_index=tss)
        for h in RESOLUTION_HORIZONS_S
    ]
