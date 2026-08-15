"""Treatment labels and K1–K4 control assignments."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.orderflow_absorption_level.config import LevelAbsorptionConfig


def treatment_for_event(event: dict[str, Any]) -> list[str]:
    """Return treatment group labels for one absorption event."""
    pattern = str(event["pattern"])
    no_level = bool(event.get("no_level"))
    far = bool(event.get("far_from_level"))
    lt = event.get("level_type")
    side = event.get("level_side")
    confluent = bool(event.get("confluent"))
    labels: list[str] = []

    if pattern == "A4":
        if no_level:
            labels.append("A4_NO_SUPPORT")
        elif far:
            labels.append("A4_FAR_FROM_SUPPORT")
        else:
            labels.append("A4_AT_ANY_SUPPORT")
            if lt == "protected" and side == "support":
                labels.append("A4_AT_PROTECTED_LOW")
            if lt == "external_swing" and side == "support":
                labels.append("A4_AT_EXTERNAL_SWING_LOW")
            if confluent:
                labels.append("A4_AT_CONFLUENT_SUPPORT")
    elif pattern == "A2":
        if no_level:
            labels.append("A2_NO_RESISTANCE")
        elif far:
            labels.append("A2_FAR_FROM_RESISTANCE")
        else:
            labels.append("A2_AT_ANY_RESISTANCE")
            if lt == "protected" and side == "resistance":
                labels.append("A2_AT_PROTECTED_HIGH")
            if lt == "external_swing" and side == "resistance":
                labels.append("A2_AT_EXTERNAL_SWING_HIGH")
    elif pattern == "A1":
        labels.append("A1_DIAGNOSTIC")
        if no_level:
            labels.append("A1_NO_RESISTANCE")
        elif far:
            labels.append("A1_FAR_FROM_RESISTANCE")
        else:
            labels.append("A1_AT_ANY_RESISTANCE")
    return labels


def build_treatment_assignments(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ev in events:
        for label in treatment_for_event(ev):
            rows.append(
                {
                    "event_id": ev["event_id"],
                    "symbol": ev["symbol"],
                    "pattern": ev["pattern"],
                    "treatment": label,
                    "level_id": ev.get("level_id"),
                    "level_type": ev.get("level_type"),
                    "distance_bucket_at_entry": ev.get("distance_bucket_at_entry"),
                    "confluent": ev.get("confluent"),
                    "event_start_index": ev.get("event_start_index"),
                    "event_start_timestamp": ev.get("event_start_timestamp"),
                }
            )
    return rows


def atr_bucket(atr_pct: float, edges: tuple[float, float] | None) -> str:
    if edges is None or not np.isfinite(atr_pct):
        return "atr_unknown"
    lo, hi = edges
    if atr_pct <= lo:
        return "atr_low"
    if atr_pct <= hi:
        return "atr_mid"
    return "atr_high"


def calendar_week(ts: Any) -> str:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    iso = t.isocalendar()
    return f"{iso.year}-W{int(iso.week):02d}"


def compute_atr_tercile_edges(df: pd.DataFrame) -> tuple[float, float] | None:
    if "atr_14" not in df.columns or "close" not in df.columns:
        return None
    atr = df["atr_14"].astype(float)
    close = df["close"].astype(float)
    pct = (atr / close.replace(0, np.nan)).dropna()
    pct = pct[np.isfinite(pct)]
    if len(pct) < 30:
        return None
    return (float(pct.quantile(1 / 3)), float(pct.quantile(2 / 3)))


def build_control_assignments(
    events: list[dict[str, Any]],
    *,
    c2_support_events: list[dict[str, Any]] | None = None,
    c1_resistance_events: list[dict[str, Any]] | None = None,
    k2_support_events: list[dict[str, Any]] | None = None,
    k2_resistance_events: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Tag events with control roles K1–K4 (plus treatment mirrors)."""
    rows: list[dict[str, Any]] = []

    def _add(ev: dict[str, Any], control: str, role: str) -> None:
        rows.append(
            {
                "event_id": ev["event_id"],
                "symbol": ev["symbol"],
                "pattern": ev.get("pattern"),
                "control": control,
                "role": role,
                "level_type": ev.get("level_type"),
                "distance_bucket_at_entry": ev.get("distance_bucket_at_entry"),
                "event_start_timestamp": ev.get("event_start_timestamp"),
                "event_start_index": ev.get("event_start_index"),
            }
        )

    for ev in events:
        pattern = str(ev["pattern"])
        no_level = bool(ev.get("no_level"))
        far = bool(ev.get("far_from_level"))
        in_zone = not no_level and not far
        if pattern == "A4":
            if no_level or far:
                _add(ev, "K1", "A4_NO_SUPPORT" if no_level else "A4_FAR_FROM_SUPPORT")
                _add(ev, "K4", "A4_FAR_OR_NO_LEVEL")
            if in_zone:
                _add(ev, "TREATMENT", "A4_AT_SUPPORT")
        elif pattern == "A2":
            if no_level or far:
                _add(ev, "K1", "A2_NO_RESISTANCE" if no_level else "A2_FAR_FROM_RESISTANCE")
                _add(ev, "K4", "A2_FAR_OR_NO_LEVEL")
            if in_zone:
                _add(ev, "TREATMENT", "A2_AT_RESISTANCE")

    for ev in c2_support_events or []:
        _add(ev, "K3", "C2_AT_SUPPORT")
    for ev in c1_resistance_events or []:
        _add(ev, "K3", "C1_AT_RESISTANCE")
    for ev in k2_support_events or []:
        _add(ev, "K2", "SUPPORT_NO_A4")
    for ev in k2_resistance_events or []:
        _add(ev, "K2", "RESISTANCE_NO_A2")
    return rows


def match_control_pairs(
    treatment_events: list[dict[str, Any]],
    control_events: list[dict[str, Any]],
    *,
    df_by_symbol: dict[str, pd.DataFrame],
    atr_edges_by_symbol: dict[str, tuple[float, float] | None],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Deterministic 1:1 strata match on (symbol, atr_bucket, calendar_week). No outcomes."""
    def stratum(ev: dict[str, Any]) -> tuple[str, str, str]:
        sym = str(ev["symbol"])
        df = df_by_symbol.get(sym)
        edges = atr_edges_by_symbol.get(sym)
        atr_b = "atr_unknown"
        if df is not None:
            i = int(ev["event_start_index"])
            if 0 <= i < len(df):
                atr = float(df["atr_14"].iloc[i - 1]) if i >= 1 else float("nan")
                close = float(df["close"].iloc[i])
                atr_pct = atr / close if np.isfinite(atr) and close > 0 else float("nan")
                atr_b = atr_bucket(atr_pct, edges)
        week = calendar_week(ev["event_start_timestamp"])
        return (sym, atr_b, week)

    ctrl_pool: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for c in sorted(control_events, key=lambda e: (e["symbol"], e["event_start_index"], e["event_id"])):
        ctrl_pool.setdefault(stratum(c), []).append(c)

    used: set[str] = set()
    pairs: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    for t in sorted(treatment_events, key=lambda e: (e["symbol"], e["event_start_index"], e["event_id"])):
        key = stratum(t)
        candidates = [c for c in ctrl_pool.get(key, []) if c["event_id"] not in used]
        if not candidates:
            unmatched.append(
                {
                    "treatment_event_id": t["event_id"],
                    "symbol": t["symbol"],
                    "stratum_symbol": key[0],
                    "stratum_atr_bucket": key[1],
                    "stratum_week": key[2],
                    "match_status": "unmatched",
                }
            )
            continue
        c = candidates[0]
        used.add(c["event_id"])
        pairs.append(
            {
                "treatment_event_id": t["event_id"],
                "control_event_id": c["event_id"],
                "symbol": t["symbol"],
                "stratum_symbol": key[0],
                "stratum_atr_bucket": key[1],
                "stratum_week": key[2],
                "match_status": "matched",
            }
        )
    return pairs, unmatched


def build_k2_touch_events(
    df: pd.DataFrame,
    inventory: list[dict[str, Any]],
    absorption_anchor_indices: set[int],
    *,
    side: str,
    pattern_label: str,
    cfg: LevelAbsorptionConfig,
    symbol: str,
) -> list[dict[str, Any]]:
    """K2: level touch without absorption pattern on that bar (event-deduped lightly)."""
    from research.regime_scanner.orderflow_absorption_level.level_assign import (
        atr_reference_at,
        distance_atr,
        pick_level_for_anchor,
    )
    from research.regime_scanner.orderflow_absorption_level.levels_build import active_levels_at
    from research.regime_scanner.orderflow_absorption_level.events import make_event_id

    seq = df["sequence_id"].to_numpy() if "sequence_id" in df.columns else np.zeros(len(df))
    events: list[dict[str, Any]] = []
    active: dict[str, Any] | None = None
    for i in range(len(df)):
        if i in absorption_anchor_indices:
            if active is not None:
                events.append(active)
                active = None
            continue
        close_t = float(df["close"].iloc[i])
        atr_ref = atr_reference_at(df, i)
        visible = active_levels_at(inventory, i, sequence_id=seq[i])
        picked = pick_level_for_anchor(
            visible,
            close_t=close_t,
            atr_ref=atr_ref,
            wanted_side=side,
            max_distance_atr=cfg.max_distance_atr,
            confluence_atr=cfg.confluence_atr,
        )
        in_zone = not picked["no_level"] and not picked["far_from_level"]
        if not in_zone:
            if active is not None:
                events.append(active)
                active = None
            continue
        if active is not None and active.get("level_id") == picked.get("level_id"):
            active["event_end_index"] = i
            active["event_end_timestamp"] = str(df["bucket_start"].iloc[i])
            continue
        if active is not None:
            events.append(active)
        start_ts = str(df["bucket_start"].iloc[i])
        eid = make_event_id(
            symbol=symbol,
            pattern=pattern_label,
            flow_rule="F1",
            lookback=24,
            level_id=str(picked.get("level_id")),
            event_start_timestamp=start_ts,
        )
        active = {
            "event_id": eid,
            "symbol": symbol,
            "sequence_id": seq[i],
            "pattern": pattern_label,
            "direction": "bullish" if side == "support" else "bearish",
            "flow_rule": "F1",
            "lookback": 24,
            "level_id": picked.get("level_id"),
            "level_type": picked.get("level_type"),
            "level_side": side,
            "level_price": picked.get("level_price"),
            "event_start_index": i,
            "event_end_index": i,
            "event_start_timestamp": start_ts,
            "event_end_timestamp": start_ts,
            "anchor_count": 1,
            "first_anchor_index": i,
            "last_anchor_index": i,
            "min_distance_atr": picked.get("distance_atr"),
            "median_distance_atr": picked.get("distance_atr"),
            "distance_bucket_at_entry": picked.get("distance_bucket"),
            "confluent": picked.get("confluent"),
            "entry_eligible_index": i,
            "entry_eligible_timestamp": start_ts,
            "confirmation_type": "R0",
            "event_end_reason": "k2_touch",
            "no_level": False,
            "far_from_level": False,
            "atr_reference": atr_ref if np.isfinite(atr_ref) else None,
            "anchor_price": close_t,
        }
    if active is not None:
        events.append(active)
    return events


def build_flow_control_events_from_assignments(
    level_assignments: list[dict[str, Any]],
    df: pd.DataFrame,
    *,
    pattern: str,
    cfg: LevelAbsorptionConfig,
) -> list[dict[str, Any]]:
    """Build events for C1/C2 at level (K3) via same event builder."""
    from research.regime_scanner.orderflow_absorption_level.events import build_absorption_level_events

    filtered = [
        a
        for a in level_assignments
        if str(a["pattern"]) == pattern
        and str(a.get("flow_rule")) in cfg.flow_rules
        and int(a.get("lookback") or 0) in cfg.lookbacks
        and not a.get("no_level")
        and not a.get("far_from_level")
    ]
    return build_absorption_level_events(df, filtered, patterns=(pattern,), cfg=cfg)
