"""CLI rendering for localized coverage."""

from __future__ import annotations

from typing import Any


def render_localized_coverage(report: dict[str, Any]) -> str:
    lines: list[str] = []
    fo = report.get("full_ob_summary") or {}
    lines.append("OBFULL RESEARCH DATA CHECK (LOCALIZED_EXCLUSION_V1)")
    lines.append("")
    lines.append(f"Symbol:        {report.get('symbol')}")
    lines.append(f"Start UTC:     {report.get('start')}")
    lines.append(f"End UTC:       {report.get('end')}")
    lines.append("")
    lines.append(f"FULL OB: {fo.get('status')}")
    lines.append("")
    lines.append(f"Sequence gaps:       {fo.get('n_sequence_gaps')}")
    lines.append(f"Tainted intervals:   {fo.get('n_tainted_intervals')}")
    lines.append(f"Excluded duration:   {fo.get('excluded_ob_duration_seconds')}s")
    lines.append(f"Usable spans:        {fo.get('n_usable_spans')}")
    lines.append(f"Usable duration:     {fo.get('usable_duration_seconds')}s")
    lines.append("")
    lines.append("EXCLUDED:")
    excl = report.get("excluded_spans") or []
    if not excl:
        lines.append("- (none)")
    for e in excl:
        lines.append(
            f"- {e.get('start')}–{e.get('end')} reason={e.get('reason')} "
            f"dur={e.get('duration_seconds')}s"
        )
    lines.append("")
    lines.append("USABLE:")
    usable = report.get("usable_spans") or []
    if not usable:
        lines.append("- (none)")
    for u in usable:
        lines.append(
            f"- {u.get('start')}–{u.get('end')} id={u.get('span_id')} "
            f"dur={u.get('duration_seconds')}s eligible_from={u.get('analysis_eligible_start')}"
        )
    focus = report.get("focus")
    if focus:
        lines.append("")
        lines.append("FOCUS:")
        lines.append(f"- timestamp: {focus.get('focus_ts')}")
        lines.append(f"- status: {focus.get('status')}")
        lb = focus.get("required_lookbacks") or {}
        lines.append(f"- required lookback: avr={lb.get('avr_baseline_s')}s multiscale={lb.get('avr_multiscale_s')}s")
        if focus.get("blocking_interval"):
            b = focus["blocking_interval"]
            lines.append(
                f"- blocking interval: {b.get('start')}–{b.get('end')} reason={b.get('reason')}"
            )
        if focus.get("reasons"):
            lines.append(f"- reasons: {', '.join(focus['reasons'])}")
    lines.append("")
    strict = report.get("strict_shadow") or {}
    lines.append(
        f"STRICT_WHOLE_WINDOW shadow: {strict.get('verdict')} "
        f"(gap_count={strict.get('gap_count')})"
    )
    lines.append("")
    lines.append(f"VERDICT: {report.get('verdict')}")
    return "\n".join(lines) + "\n"
