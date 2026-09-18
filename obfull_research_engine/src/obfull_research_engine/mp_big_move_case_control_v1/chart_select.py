"""Deterministic chart-case selection."""

from __future__ import annotations

from typing import Any

from .params import NS


FOUR_H = 4 * 3600 * NS


def select_chart_cases(
    rows: list[dict[str, Any]],
    *,
    class_name: str,
    n: int = 3,
    prefer_first_touch: bool = True,
) -> list[dict[str, Any]]:
    pool = [r for r in rows if r.get("primary_outcome_class") == class_name]
    if prefer_first_touch:
        ft = [r for r in pool if r.get("first_touch_policy")]
        if len(ft) >= n:
            pool = ft
    pool = sorted(
        pool,
        key=lambda r: (
            0 if r.get("first_touch_policy") else 1,
            str(r.get("label_price_only")),
            int(r.get("reference_entry_ts_ns") or 0),
            str(r["event_id"]),
        ),
    )
    selected: list[dict[str, Any]] = []
    for r in pool:
        if any(abs(int(r["reference_entry_ts_ns"]) - int(s["reference_entry_ts_ns"])) < FOUR_H for s in selected):
            continue
        # diversify labels when possible
        if selected and len({s.get("label_price_only") for s in selected}) < min(3, n):
            if r.get("label_price_only") in {s.get("label_price_only") for s in selected} and any(
                x.get("label_price_only") != r.get("label_price_only") for x in pool
            ):
                # skip if alternative labels still available later — soft: only skip if we already have this label and need diversity
                labels_have = {s.get("label_price_only") for s in selected}
                remaining_labels = {x.get("label_price_only") for x in pool if x["event_id"] not in {s["event_id"] for s in selected}}
                if r.get("label_price_only") in labels_have and (remaining_labels - labels_have):
                    continue
        selected.append(r)
        if len(selected) >= n:
            break
    # fill if diversity skip left gaps
    if len(selected) < n:
        for r in pool:
            if r["event_id"] in {s["event_id"] for s in selected}:
                continue
            if any(abs(int(r["reference_entry_ts_ns"]) - int(s["reference_entry_ts_ns"])) < FOUR_H for s in selected):
                continue
            selected.append(r)
            if len(selected) >= n:
                break
    return selected


def build_chart_pairs(
    clean_cases: list[dict[str, Any]],
    control_cases: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Pair each clean case with best available control of different outcome."""
    from .matching import match_quality

    used = set()
    out = []
    for a in clean_cases:
        best = None
        best_key = None
        for b in control_cases:
            if b["event_id"] in used:
                continue
            sc, shared = match_quality(a, b)
            if sc < 40:
                continue
            key = (sc, str(b["event_id"]))
            if best_key is None or key > best_key:
                best = (b, sc, shared)
                best_key = key
        if best is None:
            continue
        b, sc, shared = best
        used.add(b["event_id"])
        out.append(
            {
                "event_id_big": a["event_id"],
                "event_id_control": b["event_id"],
                "outcome_big": a["primary_outcome_class"],
                "outcome_control": b["primary_outcome_class"],
                "match_score": sc,
                "shared": "|".join(shared),
                "trigger_big_utc": a.get("reference_entry_ts_utc"),
                "trigger_control_utc": b.get("reference_entry_ts_utc"),
                "label_big": a.get("label_price_only"),
                "label_control": b.get("label_price_only"),
                "side_big": a.get("trade_side"),
                "side_control": b.get("trade_side"),
            }
        )
    return out
