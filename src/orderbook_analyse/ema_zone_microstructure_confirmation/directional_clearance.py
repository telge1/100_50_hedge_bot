"""Paket 2D/2E — directional next-zone clearance and stacked-band checks.

After confirmed Stage-B microstructure, verify whether another EMA zone
blocks the expected move (defense, breakout, false-breakout/reclaim).

Edge-to-edge band distances only — never EMA-center shortcuts.

Paket 2E: clearance never replaces ``reaction_state`` — it only sets
``clearance_status``, ``block_directed_marker``, and ``wait_next_zone``.
"""

from __future__ import annotations

from typing import Any

from orderbook_analyse.ema_zone_microstructure_confirmation.defaults import (
    NEXT_ZONE_CLEARANCE_ATR_MULT,
    NEXT_ZONE_CLEARANCE_PCT_HI,
    NEXT_ZONE_CLEARANCE_PCT_LO,
)
from orderbook_analyse.ema_zone_microstructure_confirmation.stage_a import (
    CONFIRMED_DIRECTED_STATES,
    is_stacked_zone,
    normalize_candidate_direction,
)
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows import MISSING
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.zones import (
    EmaZone,
    zones_overlap,
)

EMA_STRENGTH = {"EMA20": 1, "EMA59": 2, "EMA200": 3, "STACKED": 2}

CLEARANCE_STATUS_CLEAR = "clear"
CLEARANCE_STATUS_NEXT_ZONE_NEAR = "next_zone_near"
CLEARANCE_STATUS_STACKED_ZONE = "stacked_zone"
CLEARANCE_STATUS_BLOCKED = "blocked"


def expected_move_direction(
    *,
    candidate_state: str,
    zone_role: str,
    candidate_direction: str = "",
) -> str:
    """UP | DOWN | NONE — expected price path after confirmed micro reaction."""
    d = normalize_candidate_direction(candidate_direction)
    if d == "LONG":
        return "UP"
    if d == "SHORT":
        return "DOWN"

    state = str(candidate_state or "")
    role = str(zone_role or "").lower()
    if state == "defense_rejection_confirmed":
        return "DOWN" if role == "resistance" else "UP"
    if state == "breakout_confirmed":
        return "UP" if role == "resistance" else "DOWN"
    if state == "false_breakout_confirmed":
        return "DOWN" if role == "resistance" else "UP"
    if state in ("possible_regime_flip", "full_regime_flip_confirmed"):
        return "UP" if role == "resistance" else "DOWN"
    return "NONE"


def _constituent_zones(zones: dict[str, EmaZone | None], zone_key: str) -> list[EmaZone]:
    if is_stacked_zone(zone_key):
        suffix = zone_key.split(":", 1)[-1]
        names = [n.strip() for n in suffix.replace("STACKED_EMA_ZONE", "").split("+") if n.strip()]
        if not names:
            names = [n for n, z in zones.items() if z is not None]
        return [zones[n] for n in names if zones.get(n) is not None]  # type: ignore[misc]
    z = zones.get(zone_key)
    return [z] if z is not None else []


def bands_overlap_for_zone(
    zones: dict[str, EmaZone | None],
    zone_key: str,
) -> bool:
    """True when current zone overlaps another EMA band (incl. stacked contact)."""
    if is_stacked_zone(zone_key):
        return True
    primary = zones.get(zone_key)
    if primary is None:
        return False
    for name, other in zones.items():
        if name == zone_key or other is None:
            continue
        if zones_overlap(primary, other):
            return True
    return False


def _edge_gap_up(*, current: EmaZone, nxt: EmaZone) -> float:
    """Free space above current band until next band lower edge."""
    return max(0.0, float(nxt.low) - float(current.high))


def _edge_gap_down(*, current: EmaZone, nxt: EmaZone) -> float:
    """Free space below current band until next band upper edge."""
    return max(0.0, float(current.low) - float(nxt.high))


def _next_zone_in_direction(
    *,
    current: EmaZone,
    zones: dict[str, EmaZone | None],
    zone_key: str,
    move: str,
) -> EmaZone | None:
    """Nearest EMA band in expected move direction (edge-based)."""
    move_u = str(move or "").upper()
    pool: list[EmaZone] = []
    for name, z in zones.items():
        if z is None:
            continue
        if not is_stacked_zone(zone_key) and name == zone_key:
            continue
        if move_u == "UP" and z.low >= current.high:
            pool.append(z)
        elif move_u == "DOWN" and z.high <= current.low:
            pool.append(z)
    if not pool:
        return None
    if move_u == "UP":
        return min(pool, key=lambda z: _edge_gap_up(current=current, nxt=z))
    if move_u == "DOWN":
        return min(pool, key=lambda z: _edge_gap_down(current=current, nxt=z))
    return None


def _clearance_wait_from_gap(*, gap: float, mid: float, atr: float) -> tuple[bool, str]:
    if gap <= 0.0:
        return True, "BANDS_OVERLAP_NEXT_ZONE"
    pct = (gap / mid) * 100.0 if mid else 0.0
    atr_mult = gap / atr if atr else 0.0
    too_close = (
        NEXT_ZONE_CLEARANCE_PCT_LO <= pct <= NEXT_ZONE_CLEARANCE_PCT_HI
        or (0 < atr_mult <= NEXT_ZONE_CLEARANCE_ATR_MULT and pct <= NEXT_ZONE_CLEARANCE_PCT_HI)
    )
    if too_close:
        return True, "NEXT_ZONE_TOO_CLOSE"
    return False, "CLEARANCE_OK"


def stacked_zone_breakout_complete(
    *,
    samples: list[Any],
    decision_ms: int,
    zone_low: float,
    zone_high: float,
    expected_move: str,
) -> bool:
    """Causal: entire stacked band cleared through by decision time."""
    move = str(expected_move or "").upper()
    if move not in {"UP", "DOWN"}:
        return False
    window = [s for s in samples if getattr(s, "ts_ms", 0) <= decision_ms]
    if not window:
        return False
    if move == "UP":
        return any(float(s.mid) > float(zone_high) for s in window)
    return any(float(s.mid) < float(zone_low) for s in window)


def analyze_directional_clearance(
    *,
    current_zone: EmaZone,
    current_zone_key: str,
    zones: dict[str, EmaZone | None],
    expected_move: str,
    mid: float,
    samples: list[Any] | None = None,
    decision_ms: int | None = None,
    candidate_state: str = "",
) -> dict[str, Any]:
    """Compute edge clearance + stacked breakout completeness."""
    move = str(expected_move or "NONE").upper()
    overlap = bands_overlap_for_zone(zones, current_zone_key)
    stacked = is_stacked_zone(current_zone_key)

    out: dict[str, Any] = {
        "current_zone": current_zone_key,
        "current_zone_band_low": current_zone.low,
        "current_zone_band_high": current_zone.high,
        "expected_move_direction": move if move in {"UP", "DOWN"} else "NONE",
        "next_zone": MISSING,
        "next_zone_band_low": MISSING,
        "next_zone_band_high": MISSING,
        "next_zone_distance_pct": MISSING,
        "next_zone_distance_atr": MISSING,
        "bands_overlap": overlap,
        "stacked_zone": stacked,
        "stacked_breakout_complete": MISSING,
        "wait_next_zone": False,
        "block_directed_marker": False,
        "clearance_reason": "",
    }

    if move not in {"UP", "DOWN"}:
        out["clearance_reason"] = "NO_EXPECTED_MOVE"
        out["clearance_status"] = CLEARANCE_STATUS_CLEAR
        return out

    nxt = _next_zone_in_direction(
        current=current_zone,
        zones=zones,
        zone_key=current_zone_key,
        move=move,
    )
    gap = 0.0
    if nxt is not None:
        gap = _edge_gap_up(current=current_zone, nxt=nxt) if move == "UP" else _edge_gap_down(
            current=current_zone, nxt=nxt
        )
        out["next_zone"] = nxt.name
        out["next_zone_band_low"] = nxt.low
        out["next_zone_band_high"] = nxt.high
        pct = (gap / mid) * 100.0 if mid else 0.0
        atr_mult = gap / current_zone.atr if current_zone.atr else 0.0
        out["next_zone_distance_pct"] = pct
        out["next_zone_distance_atr"] = atr_mult
        wait, reason = _clearance_wait_from_gap(gap=gap, mid=mid, atr=current_zone.atr)
        out["wait_next_zone"] = wait
        out["clearance_reason"] = reason
    else:
        out["clearance_reason"] = "NO_NEXT_ZONE_IN_PATH"

    if stacked and samples is not None and decision_ms is not None:
        complete = stacked_zone_breakout_complete(
            samples=samples,
            decision_ms=decision_ms,
            zone_low=current_zone.low,
            zone_high=current_zone.high,
            expected_move=move,
        )
        out["stacked_breakout_complete"] = complete
    elif stacked:
        out["stacked_breakout_complete"] = False
    else:
        out["stacked_breakout_complete"] = MISSING

    # Gate directed markers
    state = str(candidate_state or "")
    block = False
    reason = str(out.get("clearance_reason") or "")

    if stacked:
        if state == "breakout_confirmed":
            if not out.get("stacked_breakout_complete"):
                block = True
                reason = "STACKED_BREAKOUT_INCOMPLETE"
            elif out["wait_next_zone"]:
                block = True
                reason = str(out["clearance_reason"] or "NEXT_ZONE_TOO_CLOSE")
        else:
            # Defense / false-breakout / other in stacked contact — no directed arrow.
            block = True
            reason = "STACKED_ZONE_NO_DIRECTED"
    elif out["wait_next_zone"]:
        block = True

    out["block_directed_marker"] = block
    if block:
        out["wait_next_zone"] = True
        out["clearance_reason"] = reason
    out["clearance_status"] = clearance_status_from_analysis(out)
    return out


def clearance_status_from_analysis(clearance: dict[str, Any]) -> str:
    """Map clearance analysis → persisted clearance_status (Paket 2E)."""
    if not clearance.get("block_directed_marker"):
        return CLEARANCE_STATUS_CLEAR

    reason = str(clearance.get("clearance_reason") or "")
    if clearance.get("stacked_zone") or clearance.get("bands_overlap"):
        return CLEARANCE_STATUS_STACKED_ZONE
    if reason in {
        "STACKED_ZONE_NO_DIRECTED",
        "STACKED_BREAKOUT_INCOMPLETE",
        "BANDS_OVERLAP_NEXT_ZONE",
    }:
        return CLEARANCE_STATUS_STACKED_ZONE
    if reason == "NEXT_ZONE_TOO_CLOSE":
        return CLEARANCE_STATUS_NEXT_ZONE_NEAR
    return CLEARANCE_STATUS_BLOCKED


def enrich_clearance_for_emit(
    *,
    reaction_state: str,
    reasons: list[str],
    clearance: dict[str, Any],
) -> tuple[str, list[str], dict[str, Any]]:
    """Attach clearance metadata without replacing Stage-B reaction_state."""
    state = str(reaction_state or "")
    rc = list(reasons)
    out = dict(clearance)
    if "clearance_status" not in out or not out.get("clearance_status"):
        out["clearance_status"] = clearance_status_from_analysis(out)

    if state in CONFIRMED_DIRECTED_STATES and out.get("block_directed_marker"):
        reason = str(out.get("clearance_reason") or "")
        for code in (
            reason,
            "CLEARANCE_BLOCKS_DIRECTED_MARKER",
        ):
            if code and code not in rc:
                rc.append(code)
        if out.get("wait_next_zone") and "WAIT_NEXT_ZONE" not in rc:
            rc.append("WAIT_NEXT_ZONE")

    return state, rc, out


def clearance_fields_for_emit(clearance: dict[str, Any]) -> dict[str, Any]:
    """Subset persisted on candidate / contact rows."""
    keys = (
        "current_zone",
        "current_zone_band_low",
        "current_zone_band_high",
        "expected_move_direction",
        "next_zone",
        "next_zone_band_low",
        "next_zone_band_high",
        "next_zone_distance_pct",
        "next_zone_distance_atr",
        "bands_overlap",
        "stacked_zone",
        "stacked_breakout_complete",
        "clearance_status",
        "clearance_reason",
        "block_directed_marker",
        "wait_next_zone",
    )
    return {k: clearance.get(k, MISSING) for k in keys}
