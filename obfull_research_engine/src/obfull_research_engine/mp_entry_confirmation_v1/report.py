"""Report writers for entry confirmation run."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_pilot_report(path: Path, *, summary: dict[str, Any], cases: list[dict[str, Any]]) -> None:
    lines = [
        "# Entry Confirmation Pilot Report",
        "",
        f"Verdict (pilot gate): `{summary.get('pilot_ok')}`",
        "",
        "## Checks",
        "",
        "```json",
        json.dumps(summary.get("checks", {}), indent=2, sort_keys=True, default=str),
        "```",
        "",
        "## Manual cases (plausibility only — rules not fitted)",
        "",
    ]
    for c in cases:
        lines.append(f"### {c.get('case_id')}")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(c, indent=2, sort_keys=True, default=str))
        lines.append("```")
        lines.append("")
    write_text(path, "\n".join(lines))


def write_three_cases_report(path: Path, cases: list[dict[str, Any]]) -> None:
    lines = [
        "# Three Manual Cases — Fixed-Rule Validation",
        "",
        "Rules were not adjusted to fit these cases. Outcomes are documented as-is.",
        "",
    ]
    for c in cases:
        lines += [
            f"## {c.get('case_id')} — {c.get('label')} {c.get('trade_side')}",
            "",
            f"- alert/trigger_ts: {c.get('alert_ts_utc')}",
            f"- confirmed_entry: {c.get('confirmed')}",
            f"- no_entry_reason: {c.get('no_entry_reason')}",
            f"- confirmation_ts: {c.get('confirmation_ts_utc')}",
            f"- entry_ts / entry_price: {c.get('entry_ts_utc')} / {c.get('entry_price')}",
            f"- alert_to_entry_minutes: {c.get('alert_to_entry_minutes')}",
            f"- entry_price_difference_pct: {c.get('entry_price_difference_pct')}",
            f"- entry_improved_vs_original: {c.get('entry_improved_vs_original')}",
            f"- primary_result 0.41/0.15: {c.get('primary_result')}",
            f"- mae_before_0.41_pct (confirmed): {c.get('mae_before_0_41_pct')}",
            f"- notes: {c.get('notes')}",
            "",
        ]
    write_text(path, "\n".join(lines))


def write_main_report(path: Path, *, verdict: str, summary: dict[str, Any]) -> None:
    lines = [
        "# Entry Confirmation Report v1",
        "",
        f"## Verdict",
        "",
        f"`{verdict}`",
        "",
        "SUCCESS does not imply live readiness.",
        "",
        "## Summary",
        "",
        "```json",
        json.dumps(summary, indent=2, sort_keys=True, default=str),
        "```",
        "",
        "## Units",
        "",
        "All excursions and costs are in **percent**.",
        "",
        "## Notes",
        "",
        "- Original MP triggers are alerts only; trades require causal 5m confirmation.",
        "- Parameters frozen in `frozen_entry_confirmation_contract_v1.json` before outcomes.",
        "- WALL_PERSISTENCE thresholds unchanged from price-path/enrichment freeze.",
        "",
    ]
    write_text(path, "\n".join(lines))
