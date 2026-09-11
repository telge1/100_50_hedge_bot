"""Unified technical report writers for analyze orchestrator."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from .run_key import atomic_write_json, atomic_write_text


def write_unified_report(
    *,
    reports_dir: Path,
    manifest: dict[str, Any],
    precheck: dict[str, Any],
    state_summary: dict[str, Any],
    candidate_summary: dict[str, Any],
    episode_summary: dict[str, Any],
    outcome_summary: dict[str, Any],
    causality: dict[str, Any],
    idempotency: dict[str, Any],
    resources: dict[str, Any],
    parity: dict[str, Any] | None,
    verdict: str,
    avr_summary: dict[str, Any] | None = None,
) -> dict[str, str]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}

    unified = {
        "verdict": verdict,
        "orchestrator_version": manifest.get("orchestrator_version"),
        "run_key": manifest.get("run_key"),
        "symbol": manifest.get("symbol"),
        "start": manifest.get("start"),
        "end": manifest.get("end"),
        "config_hashes": manifest.get("config_hashes"),
        "stages": {
            k: {
                "status": v.get("status"),
                "reused": v.get("reused"),
                "row_count": v.get("row_count"),
                "duration_seconds": v.get("duration_seconds"),
                "config_hash": v.get("config_hash"),
            }
            for k, v in (manifest.get("stages") or {}).items()
        },
        "coverage": {
            "verdict": (precheck.get("coverage") or {}).get("verdict"),
            "source_status": (precheck.get("coverage") or {}).get("source_status"),
            "future_public_trades": precheck.get("future_public_trades"),
            "avr_warmup": precheck.get("avr_warmup"),
            "localized_avr_warmup": precheck.get("localized_avr_warmup"),
            "warmup_anchor_policy": precheck.get("warmup_anchor_policy"),
            "analyzable_spans": [
                {
                    "span_id": s.get("span_id"),
                    "start": s.get("start"),
                    "end": s.get("end"),
                    "analysis_eligible_start": s.get("final_analysis_eligible_start")
                    or s.get("analysis_eligible_start"),
                }
                for s in (precheck.get("localized_avr_warmup") or {}).get("analyzable_spans")
                or (precheck.get("coverage") or {}).get("usable_spans")
                or []
            ],
            "excluded_spans": (precheck.get("coverage") or {}).get("excluded_spans")
            or (precheck.get("localized") or {}).get("excluded_spans")
            or [],
        },
        "modules": [
            "interval_coverage",
            "hour_builder / mb_state_1s_v1",
            "episode_candidate_v1",
            "behavior_episode_group_v1",
            "avr_episode_context_v1 (optional)",
            "episode_outcome_public_trade_v1_1 LAST_TRADE_CARRY",
        ],
        "state_summary": state_summary,
        "candidate_summary": candidate_summary,
        "episode_summary": episode_summary,
        "avr_summary": avr_summary,
        "outcome_summary": outcome_summary,
        "pilot_parity": parity,
        "note": "TECHNICAL RESEARCH ONLY — no predictive, profitable, or ranking claims.",
    }
    p_json = reports_dir / "unified_technical_report.json"
    atomic_write_json(p_json, unified)
    paths["unified_technical_report.json"] = str(p_json)

    md = _render_md(unified, causality, idempotency, resources)
    p_md = reports_dir / "UNIFIED_TECHNICAL_REPORT.md"
    atomic_write_text(p_md, md)
    paths["UNIFIED_TECHNICAL_REPORT.md"] = str(p_md)

    p_c = reports_dir / "causality_proof.json"
    atomic_write_json(p_c, causality)
    paths["causality_proof.json"] = str(p_c)

    p_i = reports_dir / "idempotency_report.json"
    atomic_write_json(p_i, idempotency)
    paths["idempotency_report.json"] = str(p_i)

    p_r = reports_dir / "resource_measurements.csv"
    pd.DataFrame([resources]).to_csv(p_r, index=False)
    paths["resource_measurements.csv"] = str(p_r)

    return paths


def _render_md(
    unified: dict[str, Any],
    causality: dict[str, Any],
    idempotency: dict[str, Any],
    resources: dict[str, Any],
) -> str:
    cs = unified.get("candidate_summary") or {}
    es = unified.get("episode_summary") or {}
    os_ = unified.get("outcome_summary") or {}
    lines = [
        "# UNIFIED TECHNICAL REPORT — OBFULL RESEARCH ANALYZE V1",
        "",
        f"**Verdict:** `{unified.get('verdict')}`",
        f"**Run key:** `{unified.get('run_key')}`",
        f"**Window:** `{unified.get('symbol')}` `{unified.get('start')}–{unified.get('end')}`",
        "",
        "## Coverage",
        f"- Feature coverage: `{(unified.get('coverage') or {}).get('verdict')}`",
        f"- Future PT: `{(unified.get('coverage') or {}).get('future_public_trades')}`",
        f"- Analyzable spans: `{((cs.get('span_aware') or {}).get('analyzable_span_count'))}`",
        f"- Candidates per span: `{((cs.get('span_aware') or {}).get('spans'))}`",
        f"- Episodes per span: `{es.get('episodes_per_span')}`",
        f"- Cross-span candidates: `{((cs.get('span_aware') or {}).get('n_cross_span_candidates'))}`",
        f"- Cross-span episodes: `{es.get('cross_span_episodes')}`",
        "",
        "## Counts",
        f"- State rows: `{(unified.get('state_summary') or {}).get('n_state_rows')}`",
        f"- Candidates: `{cs.get('n_candidates')}`",
        f"- Episodes: `{es.get('n_episodes')}` (reduction `{es.get('reduction_rate')}`)",
        f"- Outcomes: `{os_.get('n_outcome_rows')}` COMPLETE `{os_.get('n_complete')}`",
        "",
        "## Direction / Support",
        f"- Direction: `{es.get('direction_counts')}`",
        f"- Support: `{es.get('support_counts')}`",
        "",
        "## Outcome by horizon",
        f"`{os_.get('status_by_horizon')}`",
        "",
        "## Quality / Age",
        f"- Quality flags: `{os_.get('quality_flag_counts')}`",
        f"- Age: `{os_.get('price_age')}`",
        f"- Censor reasons: `{os_.get('censor_reason_counts')}`",
        "",
        "## Causality (summary)",
        f"`{causality}`",
        "",
        "## Idempotency",
        f"`{idempotency}`",
        "",
        "## Resources",
        f"`{resources}`",
        "",
        "No pattern ranking, prediction, win/loss, or trading claims.",
        "",
    ]
    return "\n".join(lines)
