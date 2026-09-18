"""Matched case-control pairing (no forced bad matches)."""

from __future__ import annotations

from typing import Any


CLEAN_LIKE = {"VERY_BIG_CLEAN_MOVE", "BIG_CLEAN_MOVE", "QUALIFIED_MOVE"}
CONTROL_LIKE = {"WRONG_WAY", "NO_EXPANSION", "BIG_DIRTY_MOVE", "STOP_THEN_LATE_TARGET", "LOW_FAVORABLE_EXPANSION"}


def _atr_bucket(v: Any) -> str:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "UNK"
    if x < 0.05:
        return "ATR_LO"
    if x < 0.12:
        return "ATR_MID"
    return "ATR_HI"


def match_quality(a: dict[str, Any], b: dict[str, Any]) -> tuple[int, list[str]]:
    score = 0
    shared = []
    if a.get("label_price_only") == b.get("label_price_only"):
        score += 40
        shared.append("label")
    else:
        return 0, []
    if a.get("trade_side") == b.get("trade_side"):
        score += 25
        shared.append("side")
    else:
        return 0, []
    if a.get("event_role") == b.get("event_role"):
        score += 15
        shared.append("role")
    if a.get("confluence_class") == b.get("confluence_class"):
        score += 10
        shared.append("confluence")
    elif str(a.get("confluence_class", ""))[:2] == str(b.get("confluence_class", ""))[:2]:
        score += 5
        shared.append("confluence_family")
    if a.get("session_utc") == b.get("session_utc"):
        score += 5
        shared.append("session")
    if _atr_bucket(a.get("atr14_5m_pct")) == _atr_bucket(b.get("atr14_5m_pct")):
        score += 5
        shared.append("atr_bucket")
    return score, shared


def build_matched_pairs(
    rows: list[dict[str, Any]],
    *,
    min_score: int = 65,
) -> list[dict[str, Any]]:
    """Greedy deterministic matching of clean-like vs control-like outcomes."""
    cases = sorted(
        [r for r in rows if r.get("primary_outcome_class") in CLEAN_LIKE],
        key=lambda r: (str(r.get("label_price_only")), int(r.get("reference_entry_ts_ns") or 0), str(r["event_id"])),
    )
    controls = sorted(
        [r for r in rows if r.get("primary_outcome_class") in CONTROL_LIKE],
        key=lambda r: (str(r.get("label_price_only")), int(r.get("reference_entry_ts_ns") or 0), str(r["event_id"])),
    )
    used_c: set[str] = set()
    used_k: set[str] = set()
    pairs: list[dict[str, Any]] = []
    for a in cases:
        if a["event_id"] in used_c:
            continue
        best = None
        best_key: tuple | None = None
        best_shared: list[str] = []
        for b in controls:
            if b["event_id"] in used_k:
                continue
            if a["event_id"] == b["event_id"]:
                continue
            sc, shared = match_quality(a, b)
            if sc < min_score:
                continue
            if a.get("primary_outcome_class") == b.get("primary_outcome_class"):
                continue
            try:
                atr_pen = abs(float(a.get("atr14_5m_pct") or 0) - float(b.get("atr14_5m_pct") or 0))
            except (TypeError, ValueError):
                atr_pen = 9.0
            key = (
                sc,
                -atr_pen,
                -abs(int(a.get("reference_entry_ts_ns") or 0) - int(b.get("reference_entry_ts_ns") or 0)),
                str(b["event_id"]),
            )
            if best_key is None or key > best_key:
                best = b
                best_key = key
                best_shared = shared
        if best is None or best_key is None:
            continue
        b = best
        used_c.add(a["event_id"])
        used_k.add(b["event_id"])
        diffs = {}
        for k in (
            "hit_qty",
            "refill_ratio",
            "wall_survival_after_hits",
            "aggression_without_progress",
            "imbalance_at_trigger",
            "dist_ema59_pct",
            "trade_with_medium_term_trend",
            "atr14_5m_pct",
            "return_previous_60m_pct",
        ):
            va, vb = a.get(k), b.get(k)
            try:
                diffs[k] = float(va) - float(vb)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                diffs[k] = None
        pairs.append(
            {
                "event_id_big": a["event_id"],
                "event_id_control": b["event_id"],
                "match_score": int(best_key[0]),
                "shared_criteria": "|".join(best_shared),
                "label": a.get("label_price_only"),
                "trade_side": a.get("trade_side"),
                "event_role": a.get("event_role"),
                "outcome_big": a.get("primary_outcome_class"),
                "outcome_control": b.get("primary_outcome_class"),
                "mfe_big": a.get("mfe_4h_pct"),
                "mfe_control": b.get("mfe_4h_pct"),
                "mae_before_big": a.get("mae_before_0_41_pct"),
                "mae_before_control": b.get("mae_before_0_41_pct"),
                "trigger_ts_big_utc": a.get("reference_entry_ts_utc"),
                "trigger_ts_control_utc": b.get("reference_entry_ts_utc"),
                **{f"diff_{k}": v for k, v in diffs.items()},
            }
        )
    return pairs
