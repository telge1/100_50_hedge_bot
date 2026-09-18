"""PILOT_REPORT_V2.md writer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from obfull_research_engine.timeparse import format_utc_z

from . import V1_OUTCOME_INVALIDATION
from .params import PilotParamsV2
from .schema import EventV2


def choose_verdict_v2(
    *, n_events: int, blocked: str | None = None, resource_abort: bool = False
) -> str:
    if resource_abort:
        return "MP_PILOT_V2_RESOURCE_ABORT"
    if blocked:
        up = blocked.upper()
        if "MEMORY" in up or "RESOURCE" in up:
            return "MP_PILOT_V2_RESOURCE_ABORT"
        if any(x in blocked for x in ("DATA", "COVERAGE", "EPOCH", "SILVER")):
            return "MP_PILOT_V2_BLOCKED_DATA"
        return "MP_PILOT_V2_BLOCKED_IMPLEMENTATION"
    if n_events < 30:
        return "MP_PILOT_V2_SUCCESS_LOW_SAMPLE"
    return "MP_PILOT_V2_SUCCESS"


def write_pilot_report_v2(
    path: Path,
    *,
    params: PilotParamsV2,
    result: dict[str, Any],
    verdict: str,
    v1_summary: dict[str, Any] | None = None,
) -> None:
    summary = result.get("summary") or {}
    runtime = result.get("runtime") or {}
    stats = summary.get("rejection_stats") or {}
    v1_audit = result.get("v1_audit") or {}
    lines = [
        "# MP Edge Event Pilot Report V2",
        "",
        f"## 1. Verdict\n\n`{verdict}`\n",
        "## 2. V1 Semantic Audit (critical)",
        "",
        V1_OUTCOME_INVALIDATION,
        "",
        "- V1 TRUE_BREAK outcome direction correct? **NO**",
        f"- Audit sample size: {v1_audit.get('n_audited')}",
        f"- Wrong approach in sample: {v1_audit.get('n_wrong_approach_in_sample')}",
        "",
        "## 3. Pilotfenster / Epoch",
        f"- {format_utc_z(params.start)} → {format_utc_z(params.end)}",
        f"- epoch: `{result.get('epoch_id')}`",
        "",
        "## 4. Parameters",
        "```json",
        json.dumps(params.to_manifest_dict(), indent=2, sort_keys=True),
        "```",
        "",
        "## 5. Rejection / Approach stats",
        "```json",
        json.dumps(stats, indent=2, sort_keys=True),
        "```",
        "",
        "## 6. Events",
        f"- total: {summary.get('n_events')}",
        f"- by label: {summary.get('events_by_label')}",
        f"- by trade_side: {summary.get('events_by_trade_side')}",
        f"- by confluence: {summary.get('events_by_confluence_class')}",
        f"- censored: {summary.get('n_censored')}",
        f"- transition patterns: {summary.get('transition_patterns')}",
        "",
        "## 7. V1 vs V2 comparison",
    ]
    if v1_summary:
        lines += [
            "```json",
            json.dumps(
                {
                    "v1": {
                        "n_events": v1_summary.get("n_events"),
                        "labels": v1_summary.get("events_by_label"),
                    },
                    "v2": {
                        "n_events": summary.get("n_events"),
                        "labels": summary.get("events_by_label"),
                        "rejection_stats": stats,
                    },
                },
                indent=2,
                sort_keys=True,
            ),
            "```",
        ]
    lines += [
        "",
        "## 8. TP/SL (trade_side, gross) @300s and @1800s",
        "```json",
        json.dumps(
            {
                "300": (summary.get("tpsl_by_horizon") or {}).get("300"),
                "1800": (summary.get("tpsl_by_horizon") or {}).get("1800"),
            },
            indent=2,
            sort_keys=True,
        ),
        "```",
        "",
        "## 9. Runtime",
        f"- elapsed_s: {runtime.get('elapsed_s')}",
        f"- peak_rss_mib: {runtime.get('peak_rss_mib')}",
        f"- mids_read: {result.get('mids_read')}",
        "",
        "## 10. Notes",
        "- No profit-based parameter selection.",
        "- V1 run artifacts left untouched.",
        "",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_v1_summary(v1_dir: Path) -> dict[str, Any] | None:
    p = v1_dir / "event_summary.json"
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))
