"""Outcome-blind early evidence timeline (pre-focus only)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd

from ..avr_multiscale.config import map_avr_state
from ..timeparse import format_utc_z


CLEAR_BUY = {"BUY_CONTROL", "BUY_ABSORPTION", "VACUUM_UP"}
CLEAR_SELL = {"SELL_CONTROL", "SELL_ABSORPTION", "VACUUM_DOWN"}


def build_early_evidence(
    *,
    avr_df: pd.DataFrame | None,
    focus_unix: int,
    footprint_pre: list[dict[str, Any]],
    oi_rows: list[dict[str, Any]],
    liq_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Scan only available_at <= focus_unix. Never uses post-focus outcomes."""
    events: list[dict[str, Any]] = []
    if avr_df is not None and not avr_df.empty:
        pre = avr_df[avr_df["available_at_unix"] <= focus_unix].sort_values("available_at_unix")
        firsts: dict[str, pd.Series] = {}
        for _, r in pre.iterrows():
            mapped = map_avr_state(str(r["avr_state"]))
            if mapped in CLEAR_BUY | CLEAR_SELL:
                if mapped not in firsts:
                    firsts[mapped] = r
        for st, r in firsts.items():
            direction = "BULLISH" if st in CLEAR_BUY else "BEARISH"
            events.append(
                _ev(
                    earliest=int(r["available_at_unix"]),
                    available=int(r["available_at_unix"]),
                    evidence_type=st,
                    direction=direction,
                    support=["AVR"],
                    contradict=[],
                    quality="CONTRACT_AVR_STATE",
                )
            )

    # Aggression: use 60s window max 1s delta if extreme vs 300s median magnitude
    fp60 = next((x for x in footprint_pre if x.get("fp_window_s") == 60), None)
    fp300 = next((x for x in footprint_pre if x.get("fp_window_s") == 300), None)
    if fp60 and fp60.get("fp_max_1s_delta_notional") is not None:
        d = float(fp60["fp_max_1s_delta_notional"])
        base = abs(float(fp300.get("fp_delta_notional") or 0.0)) / 300.0 if fp300 else 0.0
        # outcome-blind heuristic relative to same-case past average per-second delta magnitude
        if base > 0 and abs(d) > 5.0 * base:
            direction = "BULLISH" if d > 0 else "BEARISH"
            ts = int(fp60.get("fp_max_1s_delta_state_ts_unix") or (focus_unix - 1))
            events.append(
                _ev(
                    earliest=ts + 1,  # available after second completes
                    available=ts + 1,
                    evidence_type="EXCEPTIONAL_AGGRESSION_1S",
                    direction=direction,
                    support=["FOOTPRINT"],
                    contradict=[],
                    quality="DESCRIPTIVE_RELATIVE_TO_PRE_FOCUS_BASE",
                )
            )

    if fp60 and fp60.get("fp_delta_acceleration") is not None:
        acc = float(fp60["fp_delta_acceleration"])
        if abs(acc) > 0:
            events.append(
                _ev(
                    earliest=focus_unix,  # computed at focus from pre-focus halves
                    available=focus_unix,
                    evidence_type="DELTA_ACCELERATION",
                    direction="BULLISH" if acc > 0 else "BEARISH",
                    support=["FOOTPRINT"],
                    contradict=[],
                    quality="DESCRIPTIVE_PRE_FOCUS_HALF_WINDOWS",
                )
            )

    # OI co-move in 300s window
    oi300 = next((x for x in oi_rows if x.get("window_s") == 300), None)
    if oi300 and oi300.get("coverage") == "OK":
        events.append(
            _ev(
                earliest=focus_unix,
                available=focus_unix,
                evidence_type=f"OI_{oi300.get('oi_direction')}",
                direction="CONTEXT_ONLY",
                support=["OI"],
                contradict=[],
                quality="DESCRIPTIVE_QUADRANT_" + str(oi300.get("quadrant")),
            )
        )

    # Liquidation impulse in 300s
    liq300 = next((x for x in liq_rows if x.get("window_s") == 300), None)
    if liq300 and (liq300.get("long_count", 0) + liq300.get("short_count", 0)) > 0:
        net = float(liq300.get("net_notional") or 0)
        direction = "CONTEXT_ONLY"
        if liq300.get("max_event_time"):
            mt = datetime.fromisoformat(str(liq300["max_event_time"]).replace("Z", "+00:00"))
            earliest = int(mt.timestamp())
        else:
            earliest = focus_unix - 1
        events.append(
            _ev(
                earliest=earliest,
                available=earliest,  # event time as availability for liq
                evidence_type="LIQUIDATION_IMPULSE",
                direction=direction,
                support=["LIQUIDATIONS"],
                contradict=[],
                quality="EVENT_LEVEL_PRE_FOCUS",
            )
        )

    # Filter: nothing with available_at > focus
    events = [e for e in events if int(e["evidence_available_at_unix"]) <= focus_unix]
    events.sort(key=lambda e: (e["earliest_evidence_at_unix"], e["evidence_type"]))

    # Contradictions among clear directional AVR firsts
    dirs = {e["direction"] for e in events if e["direction"] in {"BULLISH", "BEARISH"}}
    if len(dirs) > 1:
        for e in events:
            if e["direction"] in {"BULLISH", "BEARISH"}:
                other = "BEARISH" if e["direction"] == "BULLISH" else "BULLISH"
                if other in dirs and "AVR_OPPOSITE_CLEAR_STATE" not in e["contradicting_modalities"]:
                    e["contradicting_modalities"].append("AVR_OPPOSITE_CLEAR_STATE")

    summary: dict[str, Any]
    if not events:
        summary = {
            "status": "NO_EARLY_EVIDENCE_DETECTED",
            "earliest_evidence_at": None,
            "note": "Focus is a chart selection, not an auto-detected signal.",
        }
    else:
        first = events[0]
        summary = {
            "status": "EARLY_EVIDENCE_PRESENT",
            "earliest_evidence_at": first["earliest_evidence_at"],
            "earliest_type": first["evidence_type"],
            "earliest_direction": first["direction"],
            "n_events": len(events),
            "note": "Outcome-blind; thresholds not fit on post-focus path.",
        }
    return events, summary


def _ev(
    *,
    earliest: int,
    available: int,
    evidence_type: str,
    direction: str,
    support: list[str],
    contradict: list[str],
    quality: str,
) -> dict[str, Any]:
    return {
        "earliest_evidence_at": format_utc_z(datetime.fromtimestamp(earliest, tz=timezone.utc)),
        "earliest_evidence_at_unix": earliest,
        "evidence_available_at": format_utc_z(datetime.fromtimestamp(available, tz=timezone.utc)),
        "evidence_available_at_unix": available,
        "evidence_type": evidence_type,
        "direction": direction,
        "supporting_modalities": list(support),
        "contradicting_modalities": list(contradict),
        "quality": quality,
    }
