"""Report writers for liquidation exhaustion audit (no secrets)."""

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
    # union keys for sparse outcome dicts
    keys: list[str] = []
    seen = set()
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

    cov = result.get("coverage", {}).get("by_symbol") or []
    if not cov and result.get("coverage"):
        cov = [result["coverage"]]
    _write_csv(out_dir / "joined_5m_coverage.csv", cov if isinstance(cov, list) else [cov])
    files.append("joined_5m_coverage.csv")

    _write_csv(out_dir / "raw_burst_buckets.csv", result.get("raw_bursts") or [])
    files.append("raw_burst_buckets.csv")
    _write_csv(out_dir / "event_clusters.csv", result.get("clusters") or [])
    files.append("event_clusters.csv")
    _write_csv(out_dir / "deduplicated_events.csv", result.get("events") or [])
    files.append("deduplicated_events.csv")
    _write_csv(out_dir / "reclaim_events.csv", result.get("reclaims") or [])
    files.append("reclaim_events.csv")
    _write_csv(out_dir / "forward_outcomes.csv", result.get("outcomes") or [])
    files.append("forward_outcomes.csv")
    _write_csv(out_dir / "controls.csv", result.get("controls") or [])
    files.append("controls.csv")

    summary = {
        "joined_rows": result.get("coverage", {}).get("joined_rows"),
        "n_raw_bursts": len(result.get("raw_bursts") or []),
        "n_clusters": len(result.get("clusters") or []),
        "n_dedup_events": len(result.get("events") or []),
        "n_reclaims": len(result.get("reclaims") or []),
        "n_outcomes": len(result.get("outcomes") or []),
        "by_symbol": result.get("by_symbol"),
        "config_hash": result.get("config_hash"),
        "db_writes": False,
        "run_kind": "smoke",
    }
    (out_dir / "smoke_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    files.append("smoke_summary.json")

    integrity = {
        **summary,
        "status": "smoke_ok",
        "no_secrets": True,
        "no_db_writes": True,
        "deterministic": True,
    }
    (out_dir / "integrity.json").write_text(
        json.dumps(integrity, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    files.append("integrity.json")

    report = f"""# Liquidation Exhaustion Reversal — Smoke Report

## Status
Smoke pipeline completed.

## Counts
- joined_rows: {summary['joined_rows']}
- raw_burst_buckets: {summary['n_raw_bursts']}
- event_clusters: {summary['n_clusters']}
- deduplicated_events: {summary['n_dedup_events']}
- reclaim_events: {summary['n_reclaims']}
- forward_outcomes: {summary['n_outcomes']}

## Notes
- Read-only; no DB writes
- Small sample may yield few/zero events — not a failure if pipeline completes
- Full 12-coin audit evaluation is a separate offline step:
  `python -m research.regime_scanner.run_liquidation_exhaustion_evaluation`
"""
    (out_dir / "REPORT.md").write_text(report, encoding="utf-8")
    files.append("REPORT.md")
    return files


def write_full_run_artifacts(out_dir: Path, result: dict[str, Any]) -> list[str]:
    """Write raw full-run CSVs + stub pointing to offline evaluation (not a smoke report)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    files: list[str] = []

    cov = result.get("coverage", {}).get("by_symbol") or []
    if not cov and result.get("coverage"):
        cov = [result["coverage"]]
    _write_csv(out_dir / "joined_5m_coverage.csv", cov if isinstance(cov, list) else [cov])
    files.append("joined_5m_coverage.csv")

    _write_csv(out_dir / "raw_burst_buckets.csv", result.get("raw_bursts") or [])
    files.append("raw_burst_buckets.csv")
    _write_csv(out_dir / "event_clusters.csv", result.get("clusters") or [])
    files.append("event_clusters.csv")
    _write_csv(out_dir / "deduplicated_events.csv", result.get("events") or [])
    files.append("deduplicated_events.csv")
    _write_csv(out_dir / "reclaim_events.csv", result.get("reclaims") or [])
    files.append("reclaim_events.csv")
    _write_csv(out_dir / "forward_outcomes.csv", result.get("outcomes") or [])
    files.append("forward_outcomes.csv")
    _write_csv(out_dir / "controls.csv", result.get("controls") or [])
    files.append("controls.csv")

    summary = {
        "joined_rows": result.get("coverage", {}).get("joined_rows"),
        "n_raw_bursts": len(result.get("raw_bursts") or []),
        "n_clusters": len(result.get("clusters") or []),
        "n_dedup_events": len(result.get("events") or []),
        "n_reclaims": len(result.get("reclaims") or []),
        "n_outcomes": len(result.get("outcomes") or []),
        "by_symbol": result.get("by_symbol"),
        "config_hash": result.get("config_hash"),
        "db_writes": False,
        "run_kind": "full_raw",
        "evaluation_required": True,
    }
    (out_dir / "full_run_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    files.append("full_run_summary.json")

    integrity = {
        **summary,
        "status": "full_raw_ok",
        "no_secrets": True,
        "no_db_writes": True,
        "deterministic": True,
        "evaluation_command": (
            "PYTHONPATH=. python -m research.regime_scanner.run_liquidation_exhaustion_evaluation "
            f"--input-dir {out_dir} --output-dir {out_dir} --mode full"
        ),
    }
    (out_dir / "integrity.json").write_text(
        json.dumps(integrity, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    files.append("integrity.json")

    (out_dir / "REPORT.md").write_text(
        f"""# Liquidation Exhaustion Reversal — Full Raw Run

## Status
Raw full-run artifacts written. **Offline evaluation not yet applied in this step.**

## Counts
- joined_rows: {summary.get('joined_rows')}
- raw_burst_buckets: {summary.get('n_raw_bursts')}
- event_clusters: {summary.get('n_clusters')}
- deduplicated_events: {summary.get('n_dedup_events')}
- reclaim_events: {summary.get('n_reclaims')}
- forward_outcomes: {summary.get('n_outcomes')}

## Next step (required)

```bash
PYTHONPATH=. python -m research.regime_scanner.run_liquidation_exhaustion_evaluation \\
  --input-dir {out_dir} \\
  --output-dir {out_dir} \\
  --mode full
```

This produces MFE/MAE, first-touch, exits, gates, and the final Full Audit REPORT.md.
""",
        encoding="utf-8",
    )
    files.append("REPORT.md")
    return files
