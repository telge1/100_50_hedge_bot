"""Markdown report writer (percent units only)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def write_report(
    out_dir: Path,
    *,
    verdict: str,
    summary: dict[str, Any],
    runtime: dict[str, Any] | None = None,
) -> None:
    out_dir = Path(out_dir)
    lines = [
        "# MP Price Path 4h Report v1",
        "",
        "## Verdict",
        "",
        f"`{verdict}`",
        "",
        "## Units",
        "",
        "All price excursions are in **percent** (not basis points). 100 bps = 1.00%.",
        "",
        "## Summary",
        "",
        "```json",
        json.dumps(summary, indent=2, sort_keys=True, default=str),
        "```",
        "",
    ]
    if runtime:
        lines.extend(
            [
                "## Runtime",
                "",
                "```json",
                json.dumps(runtime, indent=2, sort_keys=True, default=str),
                "```",
                "",
            ]
        )
    lines.extend(
        [
            "## Method notes",
            "",
            "- Entry: existing V2 `trigger_ts` / `trigger_price` / `trade_side` (unchanged).",
            "- Path source: `signal_generator.candles_1m` (UTC), preferred full 1m history.",
            "- Intrabar High/Low order unknown → exclusive vs inclusive MAE-before-MFE bounds; same-candle TP/SL = AMBIGUOUS.",
            "- WALL_PERSISTENCE filter frozen from enrichment Discovery thresholds (not re-tuned).",
            "- NON_OVERLAPPING_4H is causal chronological selection only.",
            "- Costs applied only to close returns, never subtracted from MFE/MAE.",
            "",
            "## Constraints",
            "",
            "- No MP recompute / no OB enrichment redo / no live integration.",
            "",
        ]
    )
    text = "\n".join(lines)
    if "bps" in text.lower() and "100 bps = 1.00%" not in text:
        # allow the conversion note only
        pass
    (out_dir / "PRICE_PATH_4H_REPORT.md").write_text(text, encoding="utf-8")
