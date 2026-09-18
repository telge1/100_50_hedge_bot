"""Enrichment report writer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def write_reports(
    out_dir: Path,
    *,
    verdict: str,
    audit: dict[str, Any],
    summary: dict[str, Any],
    analysis: dict[str, Any] | None = None,
) -> None:
    out_dir = Path(out_dir)
    lines = [
        "# OB Feature Enrichment Report v1",
        "",
        f"## Verdict",
        "",
        f"`{verdict}`",
        "",
        "## Input audit",
        "",
        "```json",
        json.dumps(audit.get("checks", audit), indent=2, sort_keys=True, default=str),
        "```",
        "",
        "## Summary",
        "",
        "```json",
        json.dumps(summary, indent=2, sort_keys=True, default=str),
        "```",
        "",
    ]
    if analysis:
        lines.extend(
            [
                "## Analysis notes",
                "",
                "- Discovery/Validation split is by complete windows (fixed before outcomes).",
                "- Feature-group thresholds use Discovery medians only; applied unchanged to Validation.",
                "- Costs (8 bps) are subtracted once from gross; never double-counted.",
                "- TP5 results are not used as primary evidence.",
                "",
                "```json",
                json.dumps(analysis.get("summary", {}), indent=2, sort_keys=True, default=str),
                "```",
                "",
            ]
        )
    lines.extend(
        [
            "## Hypotheses (not labels)",
            "",
            "- High aggression against zone + low penetration → possible absorption.",
            "- Aggression + depth pull + continuation → possible true break.",
            "- Aggression + penetration + refill + reclaim → possible failed break.",
            "",
            "## Constraints",
            "",
            "- MP batch artifacts were read-only.",
            "- No MP events recomputed.",
            "- No ML / live integration / parameter profit tuning.",
            "",
        ]
    )
    (out_dir / "ENRICHMENT_REPORT.md").write_text("\n".join(lines), encoding="utf-8")
