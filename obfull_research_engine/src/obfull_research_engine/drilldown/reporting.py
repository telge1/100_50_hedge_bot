"""Write drilldown result artifacts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


def _dump(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def write_outputs(
    *,
    out_dir: Path,
    result: dict[str, Any],
    cfg: dict[str, Any],
    config_path: Path,
    config_hash: str,
    coverage_report: dict[str, Any],
    resources: dict[str, Any],
    content_hash: str,
    verdict: str,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    sel = result["selected"]
    not_sel = result["not_selected"]
    if not sel.empty:
        sel.to_csv(out_dir / "selected_candidates.csv", index=False)
    else:
        pd.DataFrame().to_csv(out_dir / "selected_candidates.csv", index=False)
    not_sel_path = out_dir / "not_selected_candidates.csv"
    if not not_sel.empty:
        not_sel.to_csv(not_sel_path, index=False)

    groups = result["groups"]
    if isinstance(groups, pd.DataFrame) and not groups.empty:
        g = groups.copy()
        for c in ("candidate_ids", "candidate_types"):
            if c in g.columns:
                g[c] = g[c].map(lambda x: json.dumps(x, default=str))
        g.to_csv(out_dir / "candidate_groups.csv", index=False)
    else:
        pd.DataFrame().to_csv(out_dir / "candidate_groups.csv", index=False)

    _dump(
        out_dir / "selection_manifest.json",
        {
            **(result.get("selection_manifest") or {}),
            "config_sha256": config_hash,
            "content_hash": content_hash,
        },
    )

    for name, key in [
        ("event_timeline.parquet", "timeline"),
        ("states_100ms.parquet", "states_100ms"),
        ("removal_attribution.parquet", "attribution"),
        ("refill_events.parquet", "refills"),
        ("wall_events.parquet", "walls"),
    ]:
        df = result.get(key)
        if df is None or not isinstance(df, pd.DataFrame):
            pd.DataFrame().to_parquet(out_dir / name, index=False)
        else:
            df.to_parquet(out_dir / name, index=False)

    support = result.get("support")
    if support is None or not isinstance(support, pd.DataFrame) or support.empty:
        pd.DataFrame().to_csv(out_dir / "candidate_support.csv", index=False)
    else:
        s = support.copy()
        for c in ("support_reasons", "contradiction_reasons", "exact_evidence", "proxy_evidence", "ordering_limitations"):
            if c in s.columns:
                s[c] = s[c].map(lambda x: json.dumps(x, default=str))
        s.to_csv(out_dir / "candidate_support.csv", index=False)

    _dump(out_dir / "aggregation_parity.json", result.get("parity") or {})
    _dump(
        out_dir / "coverage_summary.json",
        {
            "coverage_verdict": coverage_report.get("verdict"),
            "start": coverage_report.get("start"),
            "end": coverage_report.get("end"),
            "blocked": result.get("blocked") or [],
        },
    )
    _dump(out_dir / "causality_proof.json", result.get("causality_proof") or {})
    _dump(out_dir / "quality_summary.json", result.get("quality") or {})
    _dump(out_dir / "resource_measurements.json", resources)
    _dump(out_dir / "config_snapshot.json", {"path": str(config_path), "sha256": config_hash, "config": cfg})
    _dump(
        out_dir / "output_manifest.json",
        {
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "verdict": verdict,
            "config_sha256": config_hash,
            "content_hash": content_hash,
            "n_selected": int(len(sel)) if sel is not None else 0,
            "attribution_summary": result.get("attribution_summary"),
        },
    )
