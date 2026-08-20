"""CSV / report export for EXECUTION_WALL detector."""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from orderbook_analyse.execution_wall_detector.types import (
    DETECTOR_VERSION,
    ExecutionWallParams,
    ExecutionWallSequence,
)


def _write_csv(path: Path, rows: Sequence[dict[str, Any]], fieldnames: Sequence[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        headers = list(fieldnames or [])
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=headers)
            w.writeheader()
        return
    headers = list(fieldnames) if fieldnames else list(rows[0].keys())
    for r in rows:
        for k in r:
            if k not in headers:
                headers.append(k)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=headers, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: _cell(r.get(k)) for k in headers})


def _cell(v: Any) -> Any:
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, datetime):
        return v.isoformat()
    return v


def sequence_to_row(seq: ExecutionWallSequence) -> dict[str, Any]:
    return {
        "wall_sequence_id": seq.wall_sequence_id,
        "symbol": seq.symbol,
        "side": seq.side,
        "wall_type": seq.wall_type,
        "wall_scope": seq.wall_scope,
        "representative_price": seq.representative_price,
        "price_min": seq.price_min,
        "price_max": seq.price_max,
        "first_seen": seq.first_seen.isoformat() if seq.first_seen else None,
        "last_active": seq.last_active.isoformat() if seq.last_active else None,
        "disappeared_at": seq.disappeared_at.isoformat() if seq.disappeared_at else None,
        "lifetime_ms": seq.lifetime_ms,
        "initial_qty": seq.initial_qty,
        "peak_qty": seq.peak_qty,
        "last_qty": seq.last_qty,
        "min_distance_bps": seq.min_distance_bps,
        "max_distance_bps": seq.max_distance_bps,
        "time_near_market_ms": seq.time_near_market_ms,
        "touch_time": seq.touch_time.isoformat() if seq.touch_time else None,
        "break_time": seq.break_time.isoformat() if seq.break_time else None,
        "touch_status": seq.touch_status,
        "terminal_state": seq.terminal_state,
        "sample_count": seq.sample_count,
        "local_multiple_peak": seq.local_multiple_peak,
        "local_percentile_peak": seq.local_percentile_peak,
        "executed_qty_estimate": seq.executed_qty_estimate,
        "cancelled_or_pulled_qty_estimate": seq.cancelled_or_pulled_qty_estimate,
        "unexplained_removed_qty": seq.unexplained_removed_qty,
        "refilled_qty": seq.refilled_qty,
        "refill_count": seq.refill_count,
        "pulled_before_touch": seq.pulled_before_touch,
        "absorption_candidate": seq.absorption_candidate,
        "breakout_attempted": seq.breakout_attempted,
        "breakout_accepted": seq.breakout_accepted,
        "breakout_failed": seq.breakout_failed,
        "execution_alignment_status": seq.execution_alignment_status,
        "notes": seq.notes,
    }


def build_markdown_report(report: dict[str, Any]) -> str:
    s = report.get("summary", {})
    lines = [
        f"# Execution Wall Detector Report ({DETECTOR_VERSION})",
        "",
        f"- Symbol: `{report.get('symbol')}`",
        f"- Window: `{report.get('start')}` → `{report.get('end')}`",
        f"- Params: see `execution_wall_report.json`",
        "",
        "## Answers",
        "",
        f"1. Near wall candidates (sample observations): **{s.get('candidate_observations')}**",
        f"2. Sequences: **{s.get('sequences')}**",
        f"3. Touch rate: **{s.get('touch_rate')}** ({s.get('touches')} / {s.get('sequences')})",
        f"4. Distance bands with most interactions: **{s.get('top_interaction_bands')}**",
        f"5. Execution (trade-aligned) sequences: **{s.get('executed_sequences')}** (rate {s.get('execution_rate')})",
        f"6. Pulling without trades: **{s.get('pulled_before_touch')}** (rate {s.get('pulling_rate')})",
        f"7. Absorption candidates: **{s.get('absorption_candidates')}**",
        f"8. Consumed + accepted breaks: **{s.get('accepted_breaks')}**",
        f"9. Failed breakouts: **{s.get('failed_breaks')}**",
        f"10. Structure vs Execution: see `structure_vs_execution_wall_comparison.csv`",
        f"11. Data quality OK enough?: **{s.get('data_quality_ok')}** — {s.get('data_quality_notes')}",
        f"12. Thresholds to investigate later: **{s.get('thresholds_to_tune')}**",
        "",
        "## Runtime",
        "",
        f"- Runtime sec: {report.get('runtime_sec')}",
        f"- Max RSS MiB: {report.get('max_rss_mib')}",
        "",
    ]
    return "\n".join(lines)


def write_outputs(
    output_dir: Path,
    *,
    params: ExecutionWallParams,
    symbol: str,
    start: datetime,
    end: datetime,
    candidates: Sequence[dict[str, Any]],
    sequences: Sequence[ExecutionWallSequence],
    transitions: Sequence[dict[str, Any]],
    trade_interactions: Sequence[dict[str, Any]],
    absorption_events: Sequence[dict[str, Any]],
    break_events: Sequence[dict[str, Any]],
    toxicity: Sequence[dict[str, Any]],
    forward_outcomes: Sequence[dict[str, Any]],
    distance_dist: Sequence[dict[str, Any]],
    candidate_dist: Sequence[dict[str, Any]],
    comparison: Sequence[dict[str, Any]],
    data_quality: Sequence[dict[str, Any]],
    errors: Sequence[dict[str, Any]],
    report: dict[str, Any],
    case_dumps: dict[str, Any] | None = None,
    skip_streamed: Sequence[str] = (),
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    skip = set(skip_streamed)

    mapping = {
        "execution_wall_candidates.csv": candidates,
        "execution_wall_sequences.csv": [sequence_to_row(s) for s in sequences],
        "execution_wall_transitions.csv": transitions,
        "execution_wall_trade_interactions.csv": trade_interactions,
        "execution_wall_absorption_events.csv": absorption_events,
        "execution_wall_break_events.csv": break_events,
        "execution_wall_toxicity.csv": toxicity,
        "execution_wall_forward_outcomes.csv": forward_outcomes,
        "execution_wall_distance_distribution.csv": distance_dist,
        "execution_wall_candidate_distance_distribution.csv": candidate_dist,
        "structure_vs_execution_wall_comparison.csv": comparison,
        "execution_wall_data_quality.csv": data_quality,
        "execution_wall_errors.csv": errors,
    }
    for name, rows in mapping.items():
        if name in skip:
            continue
        p = output_dir / name
        _write_csv(p, rows)
        paths[name] = p

    report_path = output_dir / "execution_wall_report.json"
    report_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    paths["execution_wall_report.json"] = report_path

    md_path = output_dir / "execution_wall_report.md"
    md_path.write_text(build_markdown_report(report), encoding="utf-8")
    paths["execution_wall_report.md"] = md_path

    if case_dumps is not None:
        dump_dir = output_dir / "case_dumps"
        dump_dir.mkdir(parents=True, exist_ok=True)
        for case_type, payload in case_dumps.items():
            p = dump_dir / f"{case_type}.json"
            p.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
            paths[f"case_dumps/{case_type}.json"] = p

    meta = {
        "detector_version": DETECTOR_VERSION,
        "symbol": symbol,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "params": params.to_dict(),
    }
    (output_dir / "execution_wall_run_meta.json").write_text(
        json.dumps(meta, indent=2, default=str), encoding="utf-8"
    )
    return paths
