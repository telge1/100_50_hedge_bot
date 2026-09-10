"""Atomic writers for episode_outcome_public_trade_v1."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .overlap import overlap_summary


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _atomic_write_json(path: Path, obj: Any) -> None:
    _atomic_write_text(path, json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n")


def _atomic_df_parquet(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def _atomic_df_csv(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_csv(tmp, index=False)
    os.replace(tmp, path)


def write_pt_outputs(
    *,
    out_dir: Path,
    outcomes: pd.DataFrame,
    cfg: dict[str, Any],
    config_hash: str,
    source_episode_config_hash: str,
    coverage_report: dict[str, Any],
    pt_coverage: dict[str, Any],
    causality_proof: dict[str, Any],
    idempotency: dict[str, Any],
    row_validation: dict[str, Any],
    summary: dict[str, Any],
    resources: dict[str, Any],
    prefix_parity: dict[str, Any] | None,
    trade_ordering: dict[str, Any],
    dedup_report: dict[str, Any],
    book_mid_comparison: pd.DataFrame | None,
    price_semantics: dict[str, Any],
    verdict: str,
    parquet_name: str = "episode_outcomes_public_trade_v1.parquet",
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    _atomic_df_parquet(out_dir / parquet_name, outcomes)

    sample = outcomes.head(50).copy()
    for c in sample.columns:
        if sample[c].dtype == object:
            sample[c] = sample[c].map(lambda x: json.dumps(x, default=str) if isinstance(x, (list, dict)) else x)
    _atomic_df_csv(out_dir / "episode_outcomes_public_trade_sample.csv", sample)

    _atomic_write_json(
        out_dir / "outcome_config.json",
        {
            "sha256": config_hash,
            "source_episode_config_hash": source_episode_config_hash,
            "config": cfg,
        },
    )
    _atomic_write_json(
        out_dir / "public_trade_price_contract.json",
        {
            "schema_version": cfg.get("schema_version"),
            "price_source": cfg.get("price_source"),
            "price_contract_version": cfg.get("price_contract_version"),
            "sha256": config_hash,
            "anchor_event_cut": cfg.get("anchor_event_cut"),
            "endpoint_event_cut": cfg.get("endpoint_event_cut"),
            "max_anchor_trade_age_ms": cfg.get("max_anchor_trade_age_ms"),
            "max_endpoint_trade_age_ms": cfg.get("max_endpoint_trade_age_ms"),
            "ingest_timestamp_semantics": cfg.get("ingest_timestamp_semantics"),
            "anchor_ingest_policy": cfg.get("anchor_ingest_policy"),
            "forbid_book_mid_fallback": cfg.get("forbid_book_mid_fallback"),
        },
    )
    _atomic_write_json(out_dir / "public_trade_coverage.json", pt_coverage)
    _atomic_write_json(out_dir / "coverage_report.json", coverage_report)

    cov_rows = []
    for h, g in outcomes.groupby("horizon_seconds"):
        reasons = g.loc[g["outcome_status"] == "CENSORED", "censor_reason"].value_counts().to_dict()
        cov_rows.append(
            {
                "horizon_seconds": int(h),
                "n": int(len(g)),
                "COMPLETE": int((g["outcome_status"] == "COMPLETE").sum()),
                "CENSORED": int((g["outcome_status"] == "CENSORED").sum()),
                "NO_VALID_ANCHOR": int((g["outcome_status"] == "NO_VALID_ANCHOR").sum()),
                "censor_reasons": json.dumps(reasons, sort_keys=True),
            }
        )
    _atomic_df_csv(out_dir / "outcome_coverage_by_horizon.csv", pd.DataFrame(cov_rows).sort_values("horizon_seconds"))

    cens = outcomes[outcomes["outcome_status"] != "COMPLETE"].copy()
    for c in ("quality_flags", "proxy_flags"):
        if c in cens.columns:
            cens[c] = cens[c].map(lambda x: json.dumps(x, default=str) if isinstance(x, (list, dict)) else x)
    _atomic_df_csv(out_dir / "censored_outcomes.csv", cens)

    if book_mid_comparison is not None:
        _atomic_df_csv(out_dir / "book_mid_vs_public_trade_comparison.csv", book_mid_comparison)
    _atomic_write_json(out_dir / "price_semantics_comparison.json", price_semantics)
    _atomic_write_json(out_dir / "trade_ordering_report.json", trade_ordering)
    _atomic_write_json(out_dir / "deduplication_report.json", dedup_report)
    _atomic_write_json(out_dir / "causality_proof.json", causality_proof)
    _atomic_write_json(out_dir / "idempotency_report.json", idempotency)
    if prefix_parity is not None:
        _atomic_write_json(out_dir / "prefix_safety.json", prefix_parity)
    _atomic_df_csv(out_dir / "resource_measurements.csv", pd.DataFrame([resources]))
    _atomic_write_json(out_dir / "outcome_summary.json", {**summary, "verdict": verdict, "row_validation": row_validation})
    _atomic_write_json(
        out_dir / "output_manifest.json",
        {
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "verdict": verdict,
            "outcome_config_hash": config_hash,
            "content_hash": idempotency.get("content_hash"),
            "n_rows": int(len(outcomes)),
        },
    )
    _ = overlap_summary  # overlap already on rows; keep import used if needed later
    _ = np
