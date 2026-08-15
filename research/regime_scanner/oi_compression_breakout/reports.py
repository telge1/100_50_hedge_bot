"""Report writers for OI compression breakout audit (no secrets / no DB writes)."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    seen: set[str] = set()
    for r in rows:
        for k in r.keys():
            if k not in seen:
                seen.add(k)
                keys.append(k)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in keys})


def write_smoke_reports(out_dir: Path, result: dict[str, Any]) -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    files: list[str] = []

    cov = result.get("coverage", {})
    cov_rows = cov.get("by_symbol") if isinstance(cov, dict) else None
    if not cov_rows and isinstance(cov, dict):
        cov_rows = [cov]
    _write_csv(out_dir / "joined_5m_coverage.csv", cov_rows or [])
    files.append("joined_5m_coverage.csv")

    mapping = [
        ("confirmed_boxes.csv", "boxes"),
        ("physical_compression_phases.csv", "physical_phases"),
        ("box_oi_features.csv", "oi_features"),
        ("breakout_events.csv", "breakouts"),
        ("forward_outcomes.csv", "outcomes"),
        ("candidate_breakout_outcomes.csv", "candidate_breakout_outcomes"),
        ("candidate_forward_outcomes.csv", "candidate_forward_outcomes"),
        ("box_filter_diagnostics.csv", "filter_diagnostics"),
        ("controls.csv", "controls"),
    ]
    for fname, key in mapping:
        _write_csv(out_dir / fname, result.get(key) or [])
        files.append(fname)

    summary = {
        "run_kind": "smoke",
        "joined_rows": result.get("joined_rows") or (cov.get("joined_rows") if isinstance(cov, dict) else None),
        "n_confirmed_boxes": len(result.get("boxes") or []),
        "n_physical_phases": len(result.get("physical_phases") or []),
        "n_oi_feature_rows": len(result.get("oi_features") or []),
        "n_breakouts": result.get("n_boxes_with_breakout")
        if result.get("n_boxes_with_breakout") is not None
        else sum(1 for b in (result.get("breakouts") or []) if not b.get("no_breakout")),
        "n_no_breakout": result.get("n_boxes_without_breakout")
        if result.get("n_boxes_without_breakout") is not None
        else sum(1 for b in (result.get("breakouts") or []) if b.get("no_breakout")),
        "n_breakout_rows": len(result.get("breakouts") or []),
        "n_outcomes": len(result.get("outcomes") or []),
        "n_candidate_breakouts": len(result.get("candidate_breakout_outcomes") or []),
        "n_candidate_forwards": len(result.get("candidate_forward_outcomes") or []),
        "oi_group_counts": result.get("oi_group_counts"),
        "box_length_counts": result.get("box_length_counts"),
        "bars_to_breakout_stats": result.get("bars_to_breakout_stats"),
        "population_counters": result.get("population_counters"),
        "max_wait_bars": result.get("max_wait_bars"),
        "by_symbol": result.get("by_symbol"),
        "config_hash": result.get("config_hash"),
        "db_writes": False,
        "no_db_writes": True,
    }
    (out_dir / "smoke_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    files.append("smoke_summary.json")

    integrity = {
        **summary,
        "status": "smoke_ok",
        "no_secrets": True,
        "deterministic": True,
        "audit": "oi_compression_breakout",
    }
    (out_dir / "integrity.json").write_text(
        json.dumps(integrity, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    files.append("integrity.json")

    report = f"""# OI Compression Breakout — Smoke Report

## Status
Smoke pipeline completed.

## Counts
- joined_rows: {summary['joined_rows']}
- confirmed_boxes: {summary['n_confirmed_boxes']}
- physical_phases: {summary['n_physical_phases']}
- oi_feature_rows (O0–O4 membership): {summary['n_oi_feature_rows']}
- breakout_event_rows: {summary['n_breakout_rows']}
- boxes_with_breakout: {summary['n_breakouts']}
- boxes_without_breakout: {summary['n_no_breakout']}
- box forward_outcomes: {summary['n_outcomes']}
- candidate_breakout_outcomes: {summary['n_candidate_breakouts']}
- candidate_forward_outcomes: {summary['n_candidate_forwards']}
- box_length_counts: {summary['box_length_counts']}
- oi_group_counts: {summary['oi_group_counts']}
- bars_to_breakout_stats: {summary['bars_to_breakout_stats']}
- population_counters: {summary.get('population_counters')}
- max_wait_bars: {summary.get('max_wait_bars')}

## Semantics
- Box bounds exclude confirm candle; freeze after confirm
- Breakout = close outside frozen box; fill = next 5m open
- Timeout → no_breakout=true (not invalidated); gap → invalidated
- O0 parent; O1–O4 subsets via candidate_* files
- Same-bar first-touch: adverse-first conservative
- Read-only; no DB writes

## Notes
Small/empty samples are OK in smoke if the pipeline completes.
Full 12-coin audit is a separate step (not run in smoke).
"""
    (out_dir / "REPORT.md").write_text(report, encoding="utf-8")
    files.append("REPORT.md")
    return files


def write_full_run_artifacts(out_dir: Path, result: dict[str, Any]) -> list[str]:
    """Write raw full-run CSVs + stub REPORT (evaluation is a later step)."""
    files = write_smoke_reports(out_dir, result)
    summary = json.loads((out_dir / "smoke_summary.json").read_text(encoding="utf-8"))
    summary["run_kind"] = "full_raw"
    (out_dir / "full_run_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    integrity = json.loads((out_dir / "integrity.json").read_text(encoding="utf-8"))
    integrity["status"] = "full_raw_ok"
    integrity["run_kind"] = "full_raw"
    (out_dir / "integrity.json").write_text(
        json.dumps(integrity, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    (out_dir / "REPORT.md").write_text(
        f"""# OI Compression Breakout — Full Raw Run

Raw artifacts written. Offline evaluation / full scientific report is a separate step.

## Counts
- joined_rows: {summary.get('joined_rows')}
- confirmed_boxes: {summary.get('n_confirmed_boxes')}
- breakouts: {summary.get('n_breakouts')}
- outcomes: {summary.get('n_outcomes')}
""",
        encoding="utf-8",
    )
    files.append("full_run_summary.json")
    return files
