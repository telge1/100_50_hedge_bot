"""Terminal report formatting for OBFULL coverage checks."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .timeparse import format_duration, format_utc_z, parse_utc_z


def render_coverage_report(report: dict[str, Any]) -> str:
    start = parse_utc_z(report["start"], field="start")
    end = parse_utc_z(report["end"], field="end")
    st = report["source_status"]
    lines = [
        "OBFULL RESEARCH DATA CHECK",
        "",
        f"Symbol:        {report['symbol']}",
        f"Start UTC:     {report['start']}",
        f"End UTC:       {report['end']}",
        f"Duration:      {format_duration(start, end)}",
        "",
        f"FULL OB:       {st.get('FULL_OB')}",
        f"CHECKPOINT:    {st.get('CHECKPOINT')}",
        f"REPLAY CHAIN:  {st.get('REPLAY_CHAIN')}",
        f"PUBLIC TRADES: {st.get('PUBLIC_TRADES')}",
        f"PRICE:         {st.get('PRICE')}",
        f"OPEN INTEREST: {st.get('OPEN_INTEREST')}",
        f"LIQUIDATIONS:  {st.get('LIQUIDATIONS')}",
        "",
    ]
    missing = report.get("missing_intervals") or []
    if missing:
        lines.append("Missing intervals:")
        for m in missing:
            lines.append(f"- {m['source']}: {m['start']}–{m['end']}")
        lines.append("")
    lines.append(f"VERDICT: {report['verdict']}")
    if report["verdict"] == "DATA_COMPLETE":
        if report.get("analysis_started"):
            lines.append("Starting analysis...")
        else:
            lines.append("Coverage complete (analysis not requested).")
    else:
        lines.append("Analysis was not started.")
    return "\n".join(lines) + "\n"
