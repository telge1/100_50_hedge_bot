"""Atomic writers for episode_outcomes_v1."""

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


def _median(s: pd.Series) -> float | None:
    s = pd.to_numeric(s, errors="coerce").dropna()
    if s.empty:
        return None
    return float(s.median())


def _q(s: pd.Series, q: float) -> float | None:
    s = pd.to_numeric(s, errors="coerce").dropna()
    if s.empty:
        return None
    return float(s.quantile(q))


def descriptive_by_horizon(df: pd.DataFrame) -> list[dict[str, Any]]:
    rows = []
    for h, g in df.groupby("horizon_seconds"):
        complete = g[g["outcome_status"] == "COMPLETE"]
        dir_counts = g["direction_hint"].value_counts().to_dict()
        rows.append(
            {
                "horizon_seconds": int(h),
                "n_episodes": int(len(g)),
                "n_complete": int((g["outcome_status"] == "COMPLETE").sum()),
                "n_censored": int((g["outcome_status"] == "CENSORED").sum()),
                "n_no_valid_anchor": int((g["outcome_status"] == "NO_VALID_ANCHOR").sum()),
                "median_return_bps": _median(complete["return_bps"]),
                "return_q25_bps": _q(complete["return_bps"], 0.25),
                "return_q75_bps": _q(complete["return_bps"], 0.75),
                "median_max_up_bps": _median(complete["max_up_bps"]),
                "median_max_down_bps": _median(complete["max_down_bps"]),
                "median_mfe_bps_directional": _median(complete["mfe_bps"]),
                "median_mae_bps_directional": _median(complete["mae_bps"]),
                "median_path_coverage_pct": _median(g["path_coverage_pct"]),
                "n_overlap_clusters": int(g["overlap_cluster_id"].nunique()),
                "direction_counts": dir_counts,
            }
        )
    return rows


def _strata_table(df: pd.DataFrame, key: str) -> pd.DataFrame:
    rows = []
    for (h, k), g in df.groupby(["horizon_seconds", key], dropna=False):
        complete = g[g["outcome_status"] == "COMPLETE"]
        rows.append(
            {
                "horizon_seconds": int(h),
                key: k,
                "n": int(len(g)),
                "n_complete": int(len(complete)),
                "n_censored": int((g["outcome_status"] == "CENSORED").sum()),
                "median_return_bps": _median(complete["return_bps"]),
                "median_mfe_bps": _median(complete["mfe_bps"]),
                "median_mae_bps": _median(complete["mae_bps"]),
            }
        )
    return pd.DataFrame(rows).sort_values(["horizon_seconds", key]).reset_index(drop=True)


def write_outputs(
    *,
    out_dir: Path,
    outcomes: pd.DataFrame,
    cfg: dict[str, Any],
    config_hash: str,
    source_episode_config_hash: str,
    coverage_report: dict[str, Any],
    state_meta: dict[str, Any],
    causality_proof: dict[str, Any],
    idempotency: dict[str, Any],
    row_validation: dict[str, Any],
    summary: dict[str, Any],
    resources: dict[str, Any],
    prefix_parity: dict[str, Any] | None,
    verdict: str,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    _atomic_df_parquet(out_dir / "episode_outcomes_v1.parquet", outcomes)

    sample = outcomes.head(50).copy()
    for c in sample.columns:
        if sample[c].dtype == object:
            sample[c] = sample[c].map(lambda x: json.dumps(x, default=str) if isinstance(x, (list, dict)) else x)
    _atomic_df_csv(out_dir / "episode_outcomes_sample.csv", sample)

    _atomic_write_json(
        out_dir / "outcome_config.json",
        {
            "sha256": config_hash,
            "source_episode_config_hash": source_episode_config_hash,
            "config": cfg,
            "price_source": state_meta.get("price_source"),
            "fallback_price_source": state_meta.get("fallback_price_source"),
        },
    )

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
    cov_df = pd.DataFrame(cov_rows).sort_values("horizon_seconds")
    _atomic_df_csv(out_dir / "outcome_coverage_by_horizon.csv", cov_df)

    _atomic_df_csv(out_dir / "outcomes_by_direction.csv", _strata_table(outcomes, "direction_hint"))
    _atomic_df_csv(out_dir / "outcomes_by_behavior_type.csv", _strata_table(outcomes, "primary_behavior_type"))
    _atomic_df_csv(out_dir / "outcomes_by_primary_trigger.csv", _strata_table(outcomes, "primary_trigger_type"))
    _atomic_df_csv(out_dir / "outcomes_by_support_status.csv", _strata_table(outcomes, "support_status"))

    oc = outcomes.copy()
    oc["single_vs_multi"] = np.where(oc["candidate_count"].astype(int) <= 1, "SINGLE", "MULTI")
    _atomic_df_csv(out_dir / "outcomes_by_single_multi.csv", _strata_table(oc, "single_vs_multi"))

    _atomic_df_csv(out_dir / "overlap_clusters.csv", overlap_summary(outcomes))

    cens = outcomes[outcomes["outcome_status"] != "COMPLETE"].copy()
    for c in ("quality_flags", "proxy_flags"):
        if c in cens.columns:
            cens[c] = cens[c].map(lambda x: json.dumps(x, default=str) if isinstance(x, (list, dict)) else x)
    _atomic_df_csv(out_dir / "censored_outcomes.csv", cens)

    quality = {
        "n_rows": int(len(outcomes)),
        "status_counts": outcomes["outcome_status"].value_counts().to_dict(),
        "censor_reason_counts": {
            ("null" if k is None or (isinstance(k, float) and pd.isna(k)) else str(k)): int(v)
            for k, v in outcomes["censor_reason"].value_counts(dropna=False).items()
        },
        "n_with_proxy_flags": int(outcomes["proxy_flags"].map(lambda x: isinstance(x, list) and len(x) > 0).sum()),
        "n_mfe_mae_undefined": int(
            outcomes["quality_flags"].map(lambda x: isinstance(x, list) and "MFE_MAE_UNDEFINED_DIRECTION" in x).sum()
        ),
        "state_meta": state_meta,
        "row_validation": row_validation,
    }
    _atomic_write_json(out_dir / "quality_summary.json", quality)

    summary_out = {
        **summary,
        "verdict": verdict,
        "by_horizon": descriptive_by_horizon(outcomes),
        "descriptive_only": True,
        "no_profitability_claim": True,
        "no_prediction_claim": True,
    }
    _atomic_write_json(out_dir / "outcome_summary.json", summary_out)

    _atomic_write_json(out_dir / "causality_proof.json", causality_proof)
    _atomic_write_json(out_dir / "idempotency_report.json", idempotency)
    _atomic_write_json(out_dir / "coverage_report.json", coverage_report)
    if prefix_parity is not None:
        _atomic_write_json(out_dir / "prefix_parity.json", prefix_parity)

    res_df = pd.DataFrame([resources])
    _atomic_df_csv(out_dir / "resource_measurements.csv", res_df)

    _atomic_write_json(
        out_dir / "output_manifest.json",
        {
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "verdict": verdict,
            "outcome_config_hash": config_hash,
            "content_hash": idempotency.get("content_hash"),
            "n_rows": int(len(outcomes)),
            "resources": resources,
        },
    )

    # Abschlussbericht skeleton filled by CLI with final numbers
