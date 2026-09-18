"""BATCH_REPORT.md writer + verdict."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from .params import SEMANTICS_HASH
from .windows import BatchWindow


def choose_verdict(
    *,
    complete: int,
    failed: int,
    included: int,
    n_events: int,
    aborted: bool,
    blocked: str | None = None,
) -> str:
    if blocked:
        if "DATA" in blocked or "EPOCH" in blocked or "SILVER" in blocked:
            return "MP_BATCH_BLOCKED_DATA"
        if "MEMORY" in blocked.upper() or "RESOURCE" in blocked.upper():
            return "MP_BATCH_RESOURCE_ABORT"
        return "MP_BATCH_BLOCKED_IMPLEMENTATION"
    if aborted:
        return "MP_BATCH_RESOURCE_ABORT"
    if failed and complete:
        return "MP_BATCH_PARTIAL"
    if failed and not complete:
        return "MP_BATCH_BLOCKED_IMPLEMENTATION"
    if complete < included:
        return "MP_BATCH_PARTIAL"
    if n_events < 100:
        return "MP_BATCH_SUCCESS_LOW_SAMPLE"
    return "MP_BATCH_SUCCESS"


def write_batch_report(
    path: Path,
    *,
    result: dict[str, Any],
    verdict: str,
) -> None:
    summaries = result.get("summaries") or {}
    runtime = result.get("runtime") or {}
    events = result.get("events") or []
    labels = Counter(e.get("label_price_only") for e in events)
    classes = Counter(e.get("confluence_class") for e in events)
    roles = Counter(e.get("event_role") for e in events)
    trades = Counter(e.get("trade_side") for e in events if e.get("trade_side"))
    included: list[BatchWindow] = result.get("included_windows") or []
    hours = sum(w.duration_s for w in included if w.window_id in {
        # completed ones approximated by complete count — use all included if full success
    }) / 3600.0
    # safer: sum duration of completed from all_windows via status — use included if complete==included
    if result.get("windows_complete") == result.get("windows_included"):
        hours = sum(w.duration_s for w in included) / 3600.0
    else:
        hours = sum(w.duration_s for w in included) / 3600.0  # upper bound note

    def find_group(rows: list[dict], name: str) -> dict:
        for r in rows or []:
            if r.get("group") == name:
                return r
        return {}

    confl = summaries.get("summary_by_confluence") or []
    c1 = find_group(confl, "C1_30M")
    c2_1h = find_group(confl, "C2_30M_1H")
    c2_4h = find_group(confl, "C2_30M_4H")
    pol = summaries.get("summary_by_selection_policy") or []
    cost = summaries.get("cost_scenario_summary") or []

    lines = [
        "# MP Edge Event Batch Report v1",
        "",
        f"## 1. Verdict\n\n`{verdict}`\n",
        f"## 2. Semantics hash\n\n`{result.get('semantics_hash') or SEMANTICS_HASH}`\n",
        "## 3. Windows",
        f"- total discovered: {result.get('windows_total')}",
        f"- included (≥2h COMPLETE single-epoch): {result.get('windows_included')}",
        f"- complete: {result.get('windows_complete')}",
        f"- failed: {result.get('windows_failed')}",
        f"- skipped(resume): {result.get('windows_skipped')}",
        f"- safe market hours (included): {hours:.2f}",
        "",
        "## 4. Events",
        f"- total: {len(events)}",
        f"- labels: {dict(labels)}",
        f"- confluence: {dict(classes)}",
        f"- roles: {dict(roles)}",
        f"- trade_side: {dict(trades)}",
        "",
        "## 5. Selection policies @1800s (tp30_sl25)",
        "```json",
        json.dumps(pol, indent=2, sort_keys=True, default=str)[:8000],
        "```",
        "",
        "## 6. Cost scenarios @1800s",
        "```json",
        json.dumps(cost, indent=2, sort_keys=True, default=str),
        "```",
        "",
        "## 7. C1_30M vs C2_30M_1H / C2_30M_4H",
        "```json",
        json.dumps({"C1_30M": c1, "C2_30M_1H": c2_1h, "C2_30M_4H": c2_4h}, indent=2, default=str),
        "```",
        "",
        "## 8. Runtime",
        f"- elapsed_s: {runtime.get('elapsed_s')}",
        f"- peak_rss_mib: {runtime.get('peak_rss_mib')}",
        f"- mids_read: {runtime.get('mids_read')}",
        "",
        "## 9. Notes",
        "- Multi-epoch continuous merges were split into single-epoch windows.",
        "- V2 semantics frozen; no profit-based parameter changes.",
        "- Costs subtracted once from gross mark-to-market returns.",
        "- Bootstrap blocks = episode_id.",
        "",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
