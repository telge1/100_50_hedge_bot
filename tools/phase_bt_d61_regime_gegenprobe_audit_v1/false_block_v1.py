"""Reference-case false-block / pass audit (causal, before entry intent)."""

from __future__ import annotations

from typing import Any

REFERENCE_ANCHORS = [
    {
        "case_id": "sep9_desired_1630",
        "decision_close_utc": "2026-09-09T16:30:00Z",
        "intent_role": "desired_short_anchor",
        "intent_bar_utc": "2026-09-09T16:30:00Z",
    },
    {
        "case_id": "sep9_desired_2055",
        "decision_close_utc": "2026-09-09T20:55:00Z",
        "intent_role": "desired_short_anchor",
        "intent_bar_utc": "2026-09-09T20:55:00Z",
    },
    {
        "case_id": "sep11_problem_1225_fill",
        "decision_close_utc": "2026-09-11T12:25:00Z",
        "intent_role": "d46_simulated_fill",
        "intent_bar_utc": "2026-09-11T12:05:00Z",
        "note": "First sustained PENDING_CREATE before fill at 12:25",
    },
    {
        "case_id": "sep11_problem_1235_fill",
        "decision_close_utc": "2026-09-11T12:35:00Z",
        "intent_role": "d46_simulated_fill",
        "intent_bar_utc": "2026-09-11T12:30:00Z",
        "note": "PENDING_CREATE before fill at 12:35",
    },
]


def build_false_block_audit(
    rows_by_bar: dict[str, dict[str, Any]],
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for spec in REFERENCE_ANCHORS:
        anchor = spec["decision_close_utc"]
        intent = spec["intent_bar_utc"]
        row_anchor = rows_by_bar.get(anchor)
        row_intent = rows_by_bar.get(intent)
        entry: dict[str, Any] = {**spec, "anchor_in_dataset": row_anchor is not None, "intent_in_dataset": row_intent is not None}
        if not row_anchor:
            entry["verdict"] = "COVERAGE_GAP"
            out.append(entry)
            continue
        flags = row_anchor.get("ablation") or {}
        entry.update(
            {
                "research_regime": row_anchor.get("research_regime"),
                "first_variant_drop_at_anchor": row_anchor.get("first_variant_drop"),
                "variant_E_at_anchor": flags.get("variant_E_plus_no_stack_growth"),
                "variant_D_at_anchor": flags.get("variant_D_plus_upper_isolation"),
                "block_at_anchor": not flags.get("variant_E_plus_no_stack_growth", False),
            }
        )
        if row_intent and intent != anchor:
            flags_i = row_intent.get("ablation") or {}
            entry["first_variant_drop_at_intent"] = row_intent.get("first_variant_drop")
            entry["variant_E_at_intent"] = flags_i.get("variant_E_plus_no_stack_growth")
            entry["block_before_intent"] = not flags_i.get("variant_E_plus_no_stack_growth", False)
            wid = row_intent.get("window_id")
            entry["first_warning_bar"] = _first_warning_before(intent, rows, window_id=wid)
            entry["warning_delay_bars"] = _bar_delta(entry.get("first_warning_bar"), intent)
        else:
            entry["block_before_intent"] = entry["block_at_anchor"]
        out.append(entry)
    return out


def _parse_utc(s: str):
    from datetime import datetime, timezone

    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def _first_warning_before(intent_bar: str, rows: list[dict[str, Any]], *, window_id: str | None) -> str | None:
    """Earliest bar at or before intent within the same window (past-only)."""
    intent_dt = _parse_utc(intent_bar)
    scoped = [r for r in rows if r.get("window_id") == window_id and _parse_utc(r["decision_close_utc"]) <= intent_dt]
    scoped.sort(key=lambda r: r["decision_close_utc"])
    for r in scoped:
        if r.get("research_regime") == "SIDEWAYS_CANDIDATE":
            return r["decision_close_utc"]
        if r.get("ablation", {}).get("research_block_sideways_stack"):
            return r["decision_close_utc"]
    return None


def _bar_delta(warning: str | None, intent: str) -> int | None:
    if not warning:
        return None
    from datetime import datetime, timezone

    return int((_parse_utc(intent) - _parse_utc(warning)).total_seconds() // 300)
