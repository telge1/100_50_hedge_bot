"""CSV exports for BT-D73 signal-level replay artifacts."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    seen: set[str] = set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                keys.append(k)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def export_pending_chronology(replay: dict[str, Any], *, classifier: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ev in replay.get("pending_lifecycle") or []:
        rows.append({**ev, "classifier": classifier, "record_type": "lifecycle"})
    for ev in replay.get("pending_cancel_audit") or []:
        rows.append({**ev, "classifier": classifier, "record_type": "cancel_audit"})
    return rows


def export_pool_candidates(replay: dict[str, Any]) -> list[dict[str, Any]]:
    return list(replay.get("baseline_pool_candidates") or [])


def export_ema_gate_comparison(
    *,
    bars: list[str],
    replay_v1: dict[str, Any],
    replay_v3: dict[str, Any],
    ema_v1: dict[str, dict],
    ema_v3: dict[str, dict],
) -> list[dict[str, Any]]:
    from compare_chronology_v1 import build_close_comparison_rows

    return build_close_comparison_rows(
        pilot_id="selected_sep23_24",
        pause_mode="RESEARCH_READY",
        bars=bars,
        replay_v1=replay_v1,
        replay_v3=replay_v3,
        ema_rows_v1=ema_v1,
        ema_rows_v3=ema_v3,
    )


def export_signal_rows(
    *,
    replay_v1: dict[str, Any],
    replay_v3: dict[str, Any],
    ema_v1: dict[str, dict],
    ema_v3: dict[str, dict],
) -> list[dict[str, Any]]:
    """One row per baseline pool candidate with pending/position context."""
    pools = replay_v1.get("baseline_pool_candidates") or []
    g1 = {r["decision_close_utc"]: r for r in replay_v1.get("gate_decisions") or []}
    g3 = {r["decision_close_utc"]: r for r in replay_v3.get("gate_decisions") or []}
    life1 = _lifecycle_index(replay_v1)
    life3 = _lifecycle_index(replay_v3)
    shadow = {r["decision_close_utc"]: r for r in replay_v1.get("ema_shadow_at_candidates") or []}

    rows: list[dict[str, Any]] = []
    for p in pools:
        bar = p["decision_close_utc"]
        r1 = g1.get(bar, {})
        r3 = g3.get(bar, {})
        e1 = ema_v1.get(bar, {})
        e3 = ema_v3.get(bar, {})
        sh = shadow.get(bar, {})
        rows.append(
            {
                "decision_close_utc": bar,
                "pool_key": p.get("pool_key"),
                "pool_lower": p.get("pool_lower"),
                "pool_upper": p.get("pool_upper"),
                "pending_create_v1": life1.get(bar, {}).get("CREATE"),
                "pending_create_v3": life3.get(bar, {}).get("CREATE"),
                "planned_entry_v1": life1.get(bar, {}).get("planned_entry"),
                "planned_sl_v1": life1.get(bar, {}).get("planned_sl"),
                "planned_tp_v1": life1.get(bar, {}).get("planned_tp"),
                "ema_v1_decision": e1.get("ema_structure_decision") or r1.get("ema_decision"),
                "ema_v1_reason": e1.get("decision_reason_code") or r1.get("ema_reason_code"),
                "ema_v3_decision": e3.get("ema_structure_decision") or r3.get("ema_decision"),
                "ema_v3_reason": e3.get("decision_reason_code") or r3.get("ema_reason_code"),
                "ema_shadow_v1_decision": sh.get("ema_decision"),
                "ema_shadow_authorizes_v1": sh.get("ema_authorizes"),
                "v1_allowed": r1.get("allow_new_short_intent"),
                "v3_allowed": r3.get("allow_new_short_intent"),
                "v1_block_reasons": "|".join(r1.get("reasons") or []),
                "v3_block_reasons": "|".join(r3.get("reasons") or []),
                "pause_state_v1": r1.get("pause_state"),
                "pause_state_v3": r3.get("pause_state"),
                "simulated_execution_disclaimer": "LIMIT_TOUCH_NOT_EXCHANGE_FILL",
            }
        )
    return rows


def _lifecycle_index(replay: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for ev in replay.get("pending_lifecycle") or []:
        if ev.get("event") == "CREATE":
            out[str(ev.get("decision_close_utc"))] = ev
    return out


def export_chart_timestamps(
    *,
    gate_rows: list[dict[str, Any]],
    positions_v1: list[dict[str, Any]],
    positions_v3: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for r in gate_rows:
        if not r.get("valid_pool_candidate"):
            continue
        reason = []
        if r.get("ema_only_differs"):
            reason.append("EMA_V1_V3_DIFF")
        if r.get("pool_conditional_differs"):
            reason.append("POOL_CONDITIONAL_DIFF")
        if r.get("v1_pending_open_end") != r.get("v3_pending_open_end"):
            reason.append("PENDING_STATE_DIFF")
        if reason:
            rows.append(
                {
                    "decision_close_utc": r["decision_close_utc"],
                    "chart_review_reason": "|".join(reason),
                    "pool_key": r.get("pool_key"),
                    "ema_v1": r.get("ema_v1_decision"),
                    "ema_v3": r.get("ema_v3_decision"),
                }
            )
    for ev in positions_v1 + positions_v3:
        rows.append(
            {
                "decision_close_utc": ev.get("decision_close_utc"),
                "chart_review_reason": f"MODELED_{ev.get('event')}_{ev.get('classifier')}",
                "pool_key": ev.get("pool_key"),
                "ema_v1": "",
                "ema_v3": "",
            }
        )
    return rows


def write_all_exports(
    out_dir: Path,
    *,
    bars: list[str],
    replay_v1: dict[str, Any],
    replay_v3: dict[str, Any],
    replay_v1_paused: dict[str, Any],
    ema_v1: dict[str, dict],
    ema_v3: dict[str, dict],
) -> None:
    from compare_chronology_v1 import fill_exit_rows

    _write_csv(out_dir / "pool_candidates.csv", export_pool_candidates(replay_v1))
    gate_rows = export_ema_gate_comparison(
        bars=bars, replay_v1=replay_v1, replay_v3=replay_v3, ema_v1=ema_v1, ema_v3=ema_v3
    )
    _write_csv(out_dir / "ema_v1_v3_gate_comparison.csv", gate_rows)
    _write_csv(out_dir / "pending_chronology_v1.csv", export_pending_chronology(replay_v1, classifier="v1"))
    _write_csv(out_dir / "pending_chronology_v3.csv", export_pending_chronology(replay_v3, classifier="v3"))
    pos_v1 = fill_exit_rows(replay_v1, classifier="v1", pilot_id="selected_sep23_24")
    pos_v3 = fill_exit_rows(replay_v3, classifier="v3", pilot_id="selected_sep23_24")
    _write_csv(out_dir / "modeled_positions_v1.csv", pos_v1)
    _write_csv(out_dir / "modeled_positions_v3.csv", pos_v3)
    _write_csv(
        out_dir / "signal_chart_timestamps.csv",
        export_chart_timestamps(gate_rows=gate_rows, positions_v1=pos_v1, positions_v3=pos_v3),
    )
    # Detailed per-candidate signal sheet (pool_candidates.csv is baseline-only; this enriches gates)
    _write_csv(
        out_dir / "pool_candidate_signals.csv",
        export_signal_rows(replay_v1=replay_v1, replay_v3=replay_v3, ema_v1=ema_v1, ema_v3=ema_v3),
    )
    _ = replay_v1_paused  # pause track summarized in replay_summary.json
