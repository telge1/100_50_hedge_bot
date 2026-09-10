"""Markdown / JSON reports for generic defense episode builder runs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..bounded_level_first_analyzer_pilot_v1.persist import atomic_write_json, atomic_write_text
from . import AUDIT_ID, BOOK_SOURCE, CONTRACT_HASH, SCHEMA_VERSION


def write_reports(
    out_dir: Path,
    *,
    funnel: dict[str, Any],
    oracle: dict[str, Any],
    exclusions: list[dict[str, Any]],
    manifest: dict[str, Any],
    enriched_summary: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "audit_id": AUDIT_ID,
        "schema_version": SCHEMA_VERSION,
        "book_source": BOOK_SOURCE,
        "outcome_contract_hash": CONTRACT_HASH,
        "funnel": funnel,
        "oracle": oracle,
        "n_exclusions": len(exclusions),
        "exclusions_sample": exclusions[:100],
        "enriched_summary": enriched_summary or [],
        "manifest": manifest,
    }
    atomic_write_json(out_dir / "builder_report.json", report)
    md = _markdown(report)
    atomic_write_text(out_dir / "builder_report.md", md)
    return {
        "builder_report.json": str(out_dir / "builder_report.json"),
        "builder_report.md": str(out_dir / "builder_report.md"),
    }


def _markdown(report: dict[str, Any]) -> str:
    funnel = report.get("funnel") or {}
    oracle = report.get("oracle") or {}
    lines = [
        f"# {report.get('audit_id')}",
        "",
        f"- schema: `{report.get('schema_version')}`",
        f"- book_source: `{report.get('book_source')}`",
        f"- outcome_contract_hash: `{report.get('outcome_contract_hash')}`",
        "",
        "## Funnel",
        "",
    ]
    for k, v in funnel.items():
        if k == "exclusion_counts":
            continue
        lines.append(f"- **{k}**: {v}")
    excl = funnel.get("exclusion_counts") or {}
    if excl:
        lines.extend(["", "### Exclusion counts", ""])
        for reason, n in sorted(excl.items()):
            lines.append(f"- `{reason}`: {n}")
    lines.extend(
        [
            "",
            "## Oracle",
            "",
            f"- lookahead_ok: {((oracle.get('lookahead') or {}).get('ok'))}",
            f"- FP/FN/mismatch: {oracle.get('false_positives')}/"
            f"{oracle.get('false_negatives')}/{oracle.get('mismatches')}",
        ]
    )
    red = oracle.get("episode1_rediscovery")
    if red:
        lines.append(
            f"- episode1_rediscovery: ran={red.get('ran')} found={red.get('found')} "
            f"(assert-only, not a selector)"
        )
    lines.append("")
    return "\n".join(lines)
