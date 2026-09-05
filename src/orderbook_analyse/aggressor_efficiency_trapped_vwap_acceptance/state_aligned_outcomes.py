"""State-aligned forward outcomes — descriptive only; never feeds matching."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from orderbook_analyse.aggressor_efficiency_flip.models import SecondBucket
from orderbook_analyse.aggressor_efficiency_flip.timeutil import ensure_utc, floor_second, iso_z, parse_utc
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.freeze_v1 import (
    OUTCOME_HORIZONS_EVAL_S,
)

# Price-up favorable when align_sign == +1; price-down favorable when -1.
DIRECTIONAL_STATES = {
    "ATTACKER_WINNING",
    "ATTACKER_TRAPPED_REJECTION",
    "ACCEPTED_ABOVE",
    "ACCEPTED_BELOW",
    "FAILED_BREAK",
    "BREAK_RECLAIMED",
    "NO_BREAK",
}
NON_DIRECTIONAL_STATES = {
    "ABSORPTION_NO_RESOLUTION",
    "MIXED_OR_UNKNOWN",
    "BREAK_WITHOUT_HEALTHY_FLOW",
    "UNKNOWN_EDGE",
    "UNKNOWN_DATA",
}


def attack_align_sign(direction: str) -> int:
    """+1 if up is attack-favorable (LONG), -1 if down is (SHORT)."""
    return 1 if direction == "LONG" else -1


def break_align_sign(*, wall_side: Optional[str], direction: str) -> int:
    """Break direction: ASK break = up; BID break = down. Alignment is AGAINST break."""
    w = (wall_side or "").upper()
    if w == "ASK":
        break_up = 1
    elif w == "BID":
        break_up = -1
    else:
        # fall back: SHORT attacks ASK
        break_up = 1 if direction == "SHORT" else -1
    return -break_up  # against break


def alignment_for_state(
    state: str,
    *,
    direction: str,
    wall_side: Optional[str],
    acceptance_state: Optional[str] = None,
    allow_acceptance_fallback: bool = False,
) -> tuple[Optional[int], str]:
    """Return (align_sign or None if non-directional, reason).

    When allow_acceptance_fallback is False (combined-state primary path),
    ABSORPTION / MIXED never inherit ACCEPTED_* direction.
    """
    s = state or ""
    acc = acceptance_state or ""

    if s in NON_DIRECTIONAL_STATES and not allow_acceptance_fallback:
        return None, "non_directional"

    if s == "ATTACKER_WINNING":
        return attack_align_sign(direction), "attack_direction"
    if s == "ATTACKER_TRAPPED_REJECTION":
        return -attack_align_sign(direction), "against_attack_direction"

    # Acceptance / break labels — only as explicit state or permitted fallback
    label = s
    if allow_acceptance_fallback and s in NON_DIRECTIONAL_STATES:
        label = acc or s

    if label == "ACCEPTED_ABOVE" or (allow_acceptance_fallback and acc == "ACCEPTED_ABOVE"):
        return 1, "bullish"
    if label == "ACCEPTED_BELOW" or (allow_acceptance_fallback and acc == "ACCEPTED_BELOW"):
        return -1, "bearish"
    if label in {"FAILED_BREAK", "BREAK_RECLAIMED"} or (
        allow_acceptance_fallback and acc in {"FAILED_BREAK", "BREAK_RECLAIMED"}
    ):
        return break_align_sign(wall_side=wall_side, direction=direction), "against_break_direction"
    if label == "NO_BREAK" or (allow_acceptance_fallback and acc == "NO_BREAK"):
        return None, "no_break_non_directional"
    if s in NON_DIRECTIONAL_STATES or acc in {"UNKNOWN_EDGE", "UNKNOWN_DATA"}:
        return None, "non_directional"
    return None, "unknown_state"


def _bucket_price(b: Optional[SecondBucket]) -> Optional[float]:
    if b is None:
        return None
    if b.last_price is not None and b.last_price > 0:
        return float(b.last_price)
    if b.first_price is not None and b.first_price > 0:
        return float(b.first_price)
    return None


def price_at(buckets: dict[datetime, SecondBucket], ts: datetime) -> Optional[float]:
    sec = floor_second(ensure_utc(ts))
    # prefer closed bucket ending at sec (bucket key = start of second)
    b = buckets.get(sec)
    px = _bucket_price(b)
    if px is not None:
        return px
    # search back up to 3s
    for i in range(1, 4):
        px = _bucket_price(buckets.get(sec - timedelta(seconds=i)))
        if px is not None:
            return px
    return None


def compute_path_metrics(
    *,
    entry_ts: datetime,
    entry_price: float,
    horizon_s: int,
    buckets: dict[datetime, SecondBucket],
    data_end: datetime,
    align_sign: Optional[int],
) -> dict[str, Any]:
    entry_ts = ensure_utc(entry_ts)
    data_end = floor_second(ensure_utc(data_end))
    end_h = entry_ts + timedelta(seconds=horizon_s)
    complete = end_h <= data_end
    raw = {
        "horizon_s": horizon_s,
        "outcome_coverage_complete": complete,
        "outcome_data_quality": "OK" if complete else "INCOMPLETE_HORIZON",
        "raw_return_bps": None,
        "state_aligned_return_bps": None,
        "raw_up_return_bps": None,
        "raw_down_return_bps": None,
        "MFE_bps": None,
        "MAE_bps": None,
        "MFE_before_MAE": None,
        "time_to_MFE_seconds": None,
        "time_to_MAE_seconds": None,
        "maximum_retrace_bps": None,
        "align_sign": align_sign,
        "directional": align_sign is not None,
    }
    if entry_price is None or entry_price <= 0:
        raw["outcome_data_quality"] = "NO_ENTRY_PRICE"
        return raw

    scan_end = min(end_h, data_end)
    mfe = mae = 0.0
    t_mfe = t_mae = None
    last = float(entry_price)
    peak_fav = 0.0
    max_retrace = 0.0
    cur = floor_second(entry_ts)
    while cur < scan_end:
        b = buckets.get(cur)
        if b and b.high_price is not None and b.low_price is not None:
            up = (b.high_price - entry_price) / entry_price * 1e4
            dn = (entry_price - b.low_price) / entry_price * 1e4
            if align_sign is None or align_sign > 0:
                fav, adv = up, dn
            else:
                fav, adv = dn, up
            if fav > mfe:
                mfe = fav
                t_mfe = cur + timedelta(seconds=1)
            if adv > mae:
                mae = adv
                t_mae = cur + timedelta(seconds=1)
            peak_fav = max(peak_fav, fav)
            # retrace from peak favorable
            if peak_fav > 0:
                max_retrace = max(max_retrace, peak_fav - fav)
            last = b.last_price or last
        cur += timedelta(seconds=1)

    raw_ret = (last - entry_price) / entry_price * 1e4
    raw["raw_return_bps"] = raw_ret
    raw["raw_up_return_bps"] = raw_ret
    raw["raw_down_return_bps"] = -raw_ret
    if align_sign is not None:
        raw["state_aligned_return_bps"] = raw_ret * align_sign
    raw["MFE_bps"] = mfe
    raw["MAE_bps"] = mae
    if t_mfe and t_mae:
        raw["MFE_before_MAE"] = t_mfe <= t_mae
    elif t_mfe and not t_mae:
        raw["MFE_before_MAE"] = True
    elif t_mae and not t_mfe:
        raw["MFE_before_MAE"] = False
    raw["time_to_MFE_seconds"] = (t_mfe - entry_ts).total_seconds() if t_mfe else None
    raw["time_to_MAE_seconds"] = (t_mae - entry_ts).total_seconds() if t_mae else None
    raw["maximum_retrace_bps"] = max_retrace
    if not complete:
        raw["outcome_data_quality"] = "INCOMPLETE_HORIZON"
    return raw


def first_acceptance_available_ts(
    feat: dict[str, Any],
    decision_ts: datetime,
) -> Optional[datetime]:
    """Earliest checkpoint where acceptance leaves UNKNOWN_EDGE."""
    cps = feat.get("acceptance_checkpoints") or {}
    if isinstance(cps, str):
        import json

        try:
            cps = json.loads(cps)
        except Exception:
            cps = {}
    best: Optional[datetime] = None
    for key, row in (cps or {}).items():
        if not isinstance(row, dict):
            continue
        st = row.get("state")
        if st in {None, "UNKNOWN_EDGE", "UNKNOWN_DATA"}:
            continue
        ts_s = row.get("checkpoint_ts")
        if ts_s:
            ts = parse_utc(ts_s) if isinstance(ts_s, str) else decision_ts
        else:
            # cp_5s → +5
            try:
                sec = int(str(key).replace("cp_", "").replace("s", ""))
            except ValueError:
                continue
            ts = decision_ts + timedelta(seconds=sec)
        if best is None or ts < best:
            best = ts
    return best


def first_trap_available_ts(feat: dict[str, Any], decision_ts: datetime) -> Optional[datetime]:
    cps = feat.get("trap_checkpoints") or {}
    if isinstance(cps, str):
        import json

        try:
            cps = json.loads(cps)
        except Exception:
            cps = {}
    # trap label is available at each closed checkpoint; first non-UNKNOWN
    best: Optional[datetime] = None
    for key, row in (cps or {}).items():
        if not isinstance(row, dict):
            continue
        lab = row.get("trap_label") or row.get("status")
        if lab in {None, "UNKNOWN_DATA"}:
            continue
        ts_s = row.get("checkpoint_ts")
        if ts_s:
            ts = parse_utc(ts_s) if isinstance(ts_s, str) else decision_ts
        else:
            try:
                sec = int(str(key).replace("cp_", "").replace("s", ""))
            except ValueError:
                continue
            ts = decision_ts + timedelta(seconds=sec)
        if best is None or ts < best:
            best = ts
    return best or (decision_ts + timedelta(seconds=5))


def build_decision_timestamps(feat: dict[str, Any], event_flow_start: datetime, event_flow_end: datetime, decision_ts: datetime) -> dict[str, Any]:
    edge_match_ts = feat.get("matched_edge_available_ts")
    # Edge match is known at flow_start for causal join (as-of flow_start)
    edge_match_available = event_flow_start
    trap_ts = first_trap_available_ts(feat, decision_ts)
    acc_ts = first_acceptance_available_ts(feat, decision_ts)
    row = {
        "event_id": feat.get("event_id"),
        "symbol": feat.get("symbol"),
        "direction": feat.get("direction"),
        "flow_start_ts": iso_z(event_flow_start),
        "flow_end_ts": iso_z(event_flow_end),
        "decision_ts": iso_z(decision_ts),
        "edge_match_available_ts": iso_z(edge_match_available),
        "matched_edge_catalog_available_ts": edge_match_ts,
        "trap_first_available_ts": iso_z(trap_ts) if trap_ts else None,
        "acceptance_first_available_ts": iso_z(acc_ts) if acc_ts else None,
        "state_5s_available_ts": iso_z(decision_ts + timedelta(seconds=5)),
        "state_10s_available_ts": iso_z(decision_ts + timedelta(seconds=10)),
        "state_30s_available_ts": iso_z(decision_ts + timedelta(seconds=30)),
        "state_60s_available_ts": iso_z(decision_ts + timedelta(seconds=60)),
        "final_research_state_available_ts": iso_z(decision_ts + timedelta(seconds=60)),
        "final_research_state": feat.get("final_research_state"),
        "final_acceptance_state": feat.get("final_acceptance_state"),
        "final_trap_label": feat.get("final_trap_label"),
        "edge_match_confidence_class": feat.get("edge_match_confidence_class"),
        "edge_join_status": feat.get("edge_join_status"),
    }
    return row


def primary_outcome_anchor(feat: dict[str, Any], decision_ts: datetime) -> tuple[datetime, str]:
    """Primary evaluation starts when the final research/acceptance state is available."""
    acc = feat.get("final_acceptance_state")
    if acc and acc not in {"UNKNOWN_EDGE", "UNKNOWN_DATA", None}:
        ts = first_acceptance_available_ts(feat, decision_ts)
        if ts is not None:
            return ts, "acceptance_first_available"
    # else use 60s combined state availability
    return decision_ts + timedelta(seconds=60), "final_research_state_60s"


def attach_forward_outcomes_for_event(
    *,
    feat: dict[str, Any],
    buckets: dict[datetime, SecondBucket],
    data_end: datetime,
    flow_start: datetime,
    flow_end: datetime,
    decision_ts: datetime,
    horizons: tuple[int, ...] = OUTCOME_HORIZONS_EVAL_S,
) -> list[dict[str, Any]]:
    """One row per (anchor, horizon). Does not mutate feat."""
    rows: list[dict[str, Any]] = []
    direction = feat.get("direction") or "LONG"
    wall_side = feat.get("wall_side")
    combined = feat.get("final_research_state") or "MIXED_OR_UNKNOWN"
    acceptance = feat.get("final_acceptance_state")
    primary_ts, primary_reason = primary_outcome_anchor(feat, decision_ts)

    anchors = [
        ("state_available", primary_ts, primary_reason),
        ("flow_start", flow_start, "diagnostic_flow_start"),
        ("flow_end", flow_end, "diagnostic_flow_end"),
    ]

    # Combined primary: never invent direction from acceptance under ABSORPTION/MIXED
    align_sign, align_reason = alignment_for_state(
        combined,
        direction=direction,
        wall_side=wall_side,
        acceptance_state=acceptance,
        allow_acceptance_fallback=False,
    )
    # Acceptance cohort companion
    acc_align, acc_align_reason = alignment_for_state(
        acceptance or "UNKNOWN_EDGE",
        direction=direction,
        wall_side=wall_side,
        acceptance_state=acceptance,
        allow_acceptance_fallback=True,
    )

    for anchor_name, anchor_ts, anchor_reason in anchors:
        entry_px = price_at(buckets, anchor_ts)
        for h in horizons:
            # Combined / primary alignment
            m = compute_path_metrics(
                entry_ts=anchor_ts,
                entry_price=entry_px or 0.0,
                horizon_s=h,
                buckets=buckets,
                data_end=data_end,
                align_sign=align_sign if anchor_name == "state_available" else attack_align_sign(direction),
            )
            if anchor_name != "state_available":
                # diagnostic: attack-direction signed + raw only
                m["align_sign"] = attack_align_sign(direction)
                m["directional"] = True
                m["state_aligned_return_bps"] = (
                    None
                    if m["raw_return_bps"] is None
                    else m["raw_return_bps"] * attack_align_sign(direction)
                )
                m["diagnostic_anchor"] = True
            else:
                m["diagnostic_anchor"] = False

            # acceptance-aligned companion fields
            if acc_align is not None and m["raw_return_bps"] is not None:
                m["acceptance_aligned_return_bps"] = m["raw_return_bps"] * acc_align
            else:
                m["acceptance_aligned_return_bps"] = None

            rows.append(
                {
                    "event_id": feat.get("event_id"),
                    "symbol": feat.get("symbol"),
                    "direction": direction,
                    "wall_side": wall_side,
                    "edge_match_confidence_class": feat.get("edge_match_confidence_class"),
                    "edge_join_status": feat.get("edge_join_status"),
                    "final_acceptance_state": acceptance,
                    "final_trap_label": feat.get("final_trap_label"),
                    "final_research_state": combined,
                    "anchor": anchor_name,
                    "anchor_reason": anchor_reason,
                    "outcome_start_ts": iso_z(anchor_ts),
                    "outcome_entry_price": entry_px,
                    "align_reason": align_reason if anchor_name == "state_available" else "attack_direction_diagnostic",
                    "acceptance_align_reason": acc_align_reason,
                    "include_in_directional_hit_rate": bool(
                        anchor_name == "state_available" and align_sign is not None
                    ),
                    **m,
                }
            )
    return rows
