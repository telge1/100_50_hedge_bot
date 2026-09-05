"""F3 wall-absorption discovery orchestrator."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from orderbook_analyse.oi_liq_impact_l2.wall_absorption.audit import (
    AuditResult,
    WallAbsorptionError,
    audit_to_json,
    run_data_availability_audit,
)
from orderbook_analyse.oi_liq_impact_l2.wall_absorption.clusters import (
    build_flush_clusters,
    cluster_sensitivity_counts,
)
from orderbook_analyse.oi_liq_impact_l2.wall_absorption.constants import (
    FORMAT_VERSION,
    WINDOW_END,
    WINDOW_START,
)

DEFAULT_F1_DIR = Path("results/oi_liq_impact_l2/discovery_smoke_btc_60m_v2")
DEFAULT_F2_DIR = Path("results/oi_liq_impact_l2/event_chain_btc_60m_f2")
DEFAULT_OUTPUT_DIR = Path("results/oi_liq_impact_l2/wall_absorption_btc_f3")


@dataclass(frozen=True)
class WallAbsorptionRunResult:
    passed_audit: bool
    output_dir: Path
    verdict: str


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _write_json(path: Path, value: object) -> None:
    _atomic_write(
        path,
        json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True) + "\n",
    )


def _write_csv(path: Path, rows: list[Mapping[str, object]], fieldnames: tuple[str, ...]) -> None:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(fieldnames), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({name: row.get(name, "") for name in fieldnames})
    _atomic_write(path, buffer.getvalue())


def _load_f1_paths(f1_dir: Path) -> dict[str, Path]:
    required = {
        "minute_features": f1_dir / "minute_features.csv",
        "flush_candidates": f1_dir / "flush_candidates.csv",
        "discovery_manifest": f1_dir / "discovery_manifest.json",
    }
    for name, path in required.items():
        if not path.is_file():
            raise WallAbsorptionError(f"missing F1 artifact: {path}")
    return required


def _blocked_manifest(
    *,
    audit: AuditResult,
    f1_dir: Path,
    f2_dir: Path | None,
    input_hashes: dict[str, str],
) -> dict[str, Any]:
    return {
        "format_version": FORMAT_VERSION,
        "verdict": "BTC_F3_WALL_ABSORPTION_DISCOVERY_BLOCKED",
        "window": {"start": WINDOW_START, "end": WINDOW_END, "semantics": "[start,end)"},
        "f1_input_dir": str(f1_dir.resolve()),
        "f2_input_dir": str(f2_dir.resolve()) if f2_dir else None,
        "input_hashes": input_hashes,
        "audit_verdict": audit.verdict,
        "block_reason": audit.block_reason,
        "threshold_search": False,
        "profitability_claim": False,
        "labels_policy": "future labels never influence wall selection or transitions",
        "aggregate_depth_substitution_forbidden": True,
    }


def run_wall_absorption_discovery(
    *,
    f1_dir: Path = DEFAULT_F1_DIR,
    f2_dir: Path | None = DEFAULT_F2_DIR,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    files_root: Path | str | None = None,
    query_clickhouse: bool = True,
    force: bool = False,
) -> WallAbsorptionRunResult:
    f1_dir = f1_dir.resolve()
    output_dir = output_dir.resolve()
    f2_dir = f2_dir.resolve() if f2_dir else None

    paths = _load_f1_paths(f1_dir)
    input_hashes = {name: _sha256(path) for name, path in paths.items()}
    if f2_dir and (f2_dir / "event_chain_manifest.json").is_file():
        input_hashes["f2_event_chain_manifest"] = _sha256(
            f2_dir / "event_chain_manifest.json"
        )

    audit_kwargs: dict[str, Any] = {"query_clickhouse": query_clickhouse}
    if files_root is not None:
        audit_kwargs["files_root"] = files_root
    audit = run_data_availability_audit(**audit_kwargs)

    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "data_availability_audit.json", audit_to_json(audit))
    _write_json(
        output_dir / "wall_discovery_manifest.json",
        _blocked_manifest(
            audit=audit,
            f1_dir=f1_dir,
            f2_dir=f2_dir,
            input_hashes=input_hashes,
        )
        if not audit.passed
        else {
            "format_version": FORMAT_VERSION,
            "verdict": "BTC_F3_WALL_ABSORPTION_DISCOVERY_READY",
            "window": {"start": WINDOW_START, "end": WINDOW_END},
            "f1_input_dir": str(f1_dir),
            "input_hashes": input_hashes,
        },
    )

    if not audit.passed and not force:
        # Still export offline-safe cluster metadata from F1 for auditability.
        candidates = pd.read_csv(paths["flush_candidates"]).to_dict(orient="records")
        clusters = build_flush_clusters(candidates, gap_minutes=1)
        cluster_rows = [
            {
                "cluster_id": c.cluster_id,
                "symbol": c.symbol,
                "direction": c.direction,
                "cluster_start": c.cluster_start,
                "cluster_end": c.cluster_end,
                "primary_candidate_id": c.primary_candidate_id,
                "candidate_ids": "|".join(c.candidate_ids),
                "flush_minutes": c.flush_minutes,
                "gap_minutes": c.gap_minutes,
            }
            for c in clusters
        ]
        _write_csv(
            output_dir / "flush_clusters.csv",
            cluster_rows,
            fieldnames=(
                "cluster_id",
                "symbol",
                "direction",
                "cluster_start",
                "cluster_end",
                "primary_candidate_id",
                "candidate_ids",
                "flush_minutes",
                "gap_minutes",
            ),
        )
        _write_json(
            output_dir / "wall_funnel_summary.json",
            {
                "verdict": "BTC_F3_WALL_ABSORPTION_DISCOVERY_BLOCKED",
                "block_reason": audit.block_reason,
                "original_flush_candidates": len(candidates),
                "primary_gap_clusters": len(clusters),
                "cluster_sensitivity": cluster_sensitivity_counts(candidates),
                "profitability_claim": False,
            },
        )
        return WallAbsorptionRunResult(
            passed_audit=False,
            output_dir=output_dir,
            verdict="BTC_F3_WALL_ABSORPTION_DISCOVERY_BLOCKED",
        )

    raise WallAbsorptionError(
        "audit passed unexpectedly; full per-level F3 pipeline not yet wired to production source"
    )
