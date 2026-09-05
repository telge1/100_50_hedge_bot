"""Stage A — EMA setup only (never emits LONG/SHORT).

Production semantics (Continuous Discovery V2):

- from_below → resistance (EMA above price)
- from_above → support (EMA below price)
- inside → ambiguous

``zone_role_at_watch`` is frozen for the episode; later mid-zone / breakout
does not rewrite the original approach role. Stage B (microstructure) may
add ``post_break_role`` after a confirmed breakout.

Stage A contract (all emit paths):

- candidate_direction = NONE
- emit_directional_marker = false
"""

from __future__ import annotations

from typing import Any

DIRECTION_NONE = "NONE"

# States that are Stage-A / pre-confirmation — never directional markers.
STAGE_A_NON_EMIT_STATES = frozenset(
    {
        "watch_zone",
        "wait_microstructure_confirmation",
        "wait_next_zone_confirmation",
        "block_flat_compression",
        "no_trade",
        "data_incomplete",
    }
)

# Only after Stage-B confirmation may a marker become directional.
CONFIRMED_DIRECTED_STATES = frozenset(
    {
        "defense_rejection_confirmed",
        "breakout_confirmed",
        "false_breakout_confirmed",
        "possible_regime_flip",
        "full_regime_flip_confirmed",
    }
)


def zone_role_from_approach(approach: str) -> str:
    """Map approach_direction → original zone role at watch."""
    a = str(approach or "").strip().lower()
    if a == "from_below":
        return "resistance"
    if a == "from_above":
        return "support"
    return "ambiguous"


def is_stacked_zone(zone_name: str) -> bool:
    return str(zone_name or "").upper().startswith("STACKED")


def stage_a_allows_microstructure(
    *,
    block_flat_compression: bool,
    near_zone: bool,
    watch_armed: bool,
    exact_touch: bool = False,
) -> bool:
    """EMA stage gate: micro only after exact touch, not flat, and armed.

    Proximity watch alone (near_zone without exact_touch) never frees Stage B.
    """
    if block_flat_compression:
        return False
    if not exact_touch:
        return False
    if not near_zone and not exact_touch:
        return False
    return bool(watch_armed)


def _needs_clearance_gate(primary_class: str, mechanism: str) -> bool:
    """Clearance applies to directed defense / breakout / false-breakout paths."""
    mech_u = str(mechanism or "").upper()
    cls = str(primary_class or "").upper()
    if mech_u in {"BREAKOUT", "FALSE_BREAKOUT", "ASK_DEFENSE", "BID_DEFENSE"}:
        return True
    if cls in {
        "ABSORPTION_THEN_BREAKOUT",
        "BREAKOUT_WITHOUT_CONFIRMED_ABSORPTION",
        "LIQUIDITY_PULL_BREAKOUT",
        "FALSE_BREAKOUT_RECLAIM",
        "DEFENSE_REJECTION",
    }:
        return True
    if "DEFENSE" in cls or "BREAKOUT" in cls or "RECLAIM" in cls:
        return True
    return False


def decide_wait_next_zone(
    *,
    zone_name: str,
    mechanism: str,
    primary_class: str,
    clearance_wait: bool,
) -> tuple[bool, str]:
    """Clearance / stacked gate for directed Stage-B outcomes.

    Stacked EMA contact always requires wait_next (no directed candidate).
    Clearance wait applies to defense and breakout/false-breakout alike.
    """
    if is_stacked_zone(zone_name):
        return True, "STACKED_ZONE_NO_DIRECTED"
    if clearance_wait and _needs_clearance_gate(primary_class, mechanism):
        return True, "WAIT_NEXT_ZONE_CONFIRMATION"
    return False, ""


def freeze_role_fields(
    *,
    approach_at_watch: str,
    post_break_role: str | None = None,
) -> dict[str, str]:
    """Emit role timeline fields; watch role is authoritative for direction mapping."""
    role_watch = zone_role_from_approach(approach_at_watch)
    out = {
        "approach_direction": str(approach_at_watch or ""),
        "zone_role_at_watch": role_watch,
        "zone_role_at_touch": role_watch,
        "zone_role_at_decision": role_watch,
    }
    if post_break_role:
        out["post_break_role"] = str(post_break_role)
    return out


def post_break_role_after_confirmed_breakout(zone_role_at_watch: str) -> str | None:
    """Former resistance becomes support after upside break (and mirror)."""
    r = str(zone_role_at_watch or "").lower()
    if r == "resistance":
        return "support"
    if r == "support":
        return "resistance"
    return None


def involves_ema200(zone_name: str) -> bool:
    u = str(zone_name or "").upper()
    return u == "EMA200" or "EMA200" in u


def normalize_candidate_direction(raw: Any) -> str:
    """Map any raw direction to LONG | SHORT | NONE."""
    d = str(raw or "").strip().upper()
    if d in ("LONG", "SHORT"):
        return d
    return DIRECTION_NONE


def stage_a_direction_payload(*, reason: str = "stage_a_no_direction") -> dict[str, Any]:
    """Mandatory Stage-A direction fields — never LONG/SHORT, never chart markers."""
    return {
        "candidate_direction": DIRECTION_NONE,
        "direction_reason": reason,
        "emit_directional_marker": False,
    }


def emit_directional_marker_for(
    *,
    candidate_state: str,
    candidate_direction: Any,
) -> bool:
    """True only when Stage-B confirmed state × LONG/SHORT."""
    state = str(candidate_state or "")
    if state in STAGE_A_NON_EMIT_STATES:
        return False
    if state not in CONFIRMED_DIRECTED_STATES:
        return False
    return normalize_candidate_direction(candidate_direction) in ("LONG", "SHORT")


def attach_direction_fields(
    *,
    candidate_state: str,
    zone_role: str,
    raw_direction: Any,
    direction_reason: str,
    block_directed_marker: bool = False,
    allow_directed: bool = True,
) -> dict[str, Any]:
    """Hard-gate direction + marker flag for every candidate emit path.

    Stage A / non-confirmed states always yield NONE + emit=false, regardless
    of any upstream raw_direction leak.

    Paket 2E: confirmed ``reaction_state`` keeps LONG/SHORT even when clearance
    or regime blocks the chart marker (``emit_directional_marker=false``).
    """
    _ = zone_role  # role is informational; direction already resolved upstream
    state = str(candidate_state or "")
    if state in STAGE_A_NON_EMIT_STATES:
        return stage_a_direction_payload(
            reason=direction_reason or "stage_a_no_direction"
        )
    d = normalize_candidate_direction(raw_direction)
    if state in CONFIRMED_DIRECTED_STATES and d in ("LONG", "SHORT"):
        emit = (
            emit_directional_marker_for(candidate_state=state, candidate_direction=d)
            and not block_directed_marker
            and allow_directed
        )
        return {
            "candidate_direction": d,
            "direction_reason": direction_reason,
            "emit_directional_marker": emit,
        }
    if not emit_directional_marker_for(candidate_state=state, candidate_direction=d):
        return {
            "candidate_direction": DIRECTION_NONE,
            "direction_reason": direction_reason or "no_direction",
            "emit_directional_marker": False,
        }
    return {
        "candidate_direction": d,
        "direction_reason": direction_reason,
        "emit_directional_marker": True,
    }
