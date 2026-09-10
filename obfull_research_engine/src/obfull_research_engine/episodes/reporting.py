"""Reporting / artifact writers for episode_candidate_v1."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from .detector import candidates_content_sha256, config_sha256


def _json_dump(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def write_episode_outputs(
    *,
    out_dir: Path,
    candidates: pd.DataFrame,
    diagnostics: dict[str, Any],
    cfg: dict[str, Any],
    config_path: Path,
    schema_path: Path,
    coverage_report: dict[str, Any],
    resources: dict[str, Any],
    causality_proof: dict[str, Any],
    idempotency: dict[str, Any],
    quality_summary: dict[str, Any],
    prefix_parity: dict[str, Any] | None = None,
    verdict: str,
) -> dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}

    pq = out_dir / "episode_candidates_v1.parquet"
    csv = out_dir / "episode_candidates_v1.csv"
    if candidates is None or candidates.empty:
        empty = pd.DataFrame(
            columns=[
                "schema_version",
                "candidate_id",
                "symbol",
                "candidate_type",
                "candidate_status",
                "context_start_ts",
                "trigger_ts",
                "detection_available_at",
                "baseline_start_ts",
                "baseline_end_ts",
                "baseline_valid",
                "coverage_status",
                "replay_epoch",
                "source_state_schema",
                "source_state_hash",
                "trigger_names",
                "trigger_count",
                "primary_trigger",
                "direction_hint",
                "confidence_kind",
                "threshold_version",
                "threshold_source",
                "feature_snapshot_json",
                "proxy_fields",
                "exact_fields",
                "quality_valid",
                "invalid_reason",
            ]
        )
        empty.to_parquet(pq, index=False)
        empty.to_csv(csv, index=False)
        cand_df = empty
    else:
        # stringify list cols for csv
        cand_df = candidates.copy()
        cand_df.to_parquet(pq, index=False)
        csv_df = cand_df.copy()
        for c in ("trigger_names", "proxy_fields", "exact_fields"):
            if c in csv_df.columns:
                csv_df[c] = csv_df[c].map(lambda x: json.dumps(x, default=str))
        csv_df.to_csv(csv, index=False)

    paths["parquet"] = str(pq)
    paths["csv"] = str(csv)

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    _json_dump(out_dir / "candidate_schema.json", schema)
    paths["candidate_schema"] = str(out_dir / "candidate_schema.json")

    cfg_hash = config_sha256(config_path)
    _json_dump(
        out_dir / "config_snapshot.json",
        {"path": str(config_path), "sha256": cfg_hash, "config": cfg},
    )
    paths["config_snapshot"] = str(out_dir / "config_snapshot.json")

    counts = diagnostics.get("candidate_counts") or {}
    summary = {
        "verdict": verdict,
        "n_candidates": int(len(cand_df)),
        "candidate_counts": counts,
        "trigger_fire_counts": diagnostics.get("trigger_fire_counts"),
        "coverage_verdict": coverage_report.get("verdict"),
        "symbol": coverage_report.get("symbol"),
        "start": coverage_report.get("start"),
        "end": coverage_report.get("end"),
        "config_sha256": cfg_hash,
        "candidates_content_sha256": candidates_content_sha256(cand_df if not cand_df.empty else candidates),
        "note": "CANDIDATE_ONLY — not predictive, not profitable, not proven causes",
    }
    _json_dump(out_dir / "candidate_summary.json", summary)
    paths["candidate_summary"] = str(out_dir / "candidate_summary.json")

    trig = pd.DataFrame(
        [{"trigger_name": k, "fire_count": v} for k, v in sorted((diagnostics.get("trigger_fire_counts") or {}).items())]
    )
    trig.to_csv(out_dir / "trigger_counts.csv", index=False)
    paths["trigger_counts"] = str(out_dir / "trigger_counts.csv")

    cc = pd.DataFrame([{"candidate_type": k, "count": v} for k, v in sorted(counts.items())])
    cc.to_csv(out_dir / "candidate_counts.csv", index=False)
    paths["candidate_counts"] = str(out_dir / "candidate_counts.csv")

    base_df = pd.DataFrame(diagnostics.get("baseline_diagnostics") or [])
    base_df.to_csv(out_dir / "baseline_diagnostics.csv", index=False)
    paths["baseline_diagnostics"] = str(out_dir / "baseline_diagnostics.csv")

    _json_dump(out_dir / "quality_summary.json", quality_summary)
    _json_dump(out_dir / "causality_proof.json", causality_proof)
    _json_dump(out_dir / "idempotency.json", idempotency)
    if prefix_parity is not None:
        _json_dump(out_dir / "prefix_parity.json", prefix_parity)
        paths["prefix_parity"] = str(out_dir / "prefix_parity.json")

    paths["quality_summary"] = str(out_dir / "quality_summary.json")
    paths["causality_proof"] = str(out_dir / "causality_proof.json")
    paths["idempotency"] = str(out_dir / "idempotency.json")

    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "verdict": verdict,
        "config_sha256": cfg_hash,
        "candidates_content_sha256": summary["candidates_content_sha256"],
        "resources": resources,
        "files": paths,
        "coverage": {
            "verdict": coverage_report.get("verdict"),
            "start": coverage_report.get("start"),
            "end": coverage_report.get("end"),
        },
    }
    _json_dump(out_dir / "output_manifest.json", manifest)
    paths["output_manifest"] = str(out_dir / "output_manifest.json")
    return paths
