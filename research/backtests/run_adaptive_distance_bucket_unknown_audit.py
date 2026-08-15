#!/usr/bin/env python3
"""Classify Stage-C empty/unknown distance buckets without mutating Stage-C outputs.

Reads an existing Stage-C result directory and writes a NEW audit folder.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.backtests.adaptive_distance_staging import (
    REAL_DISTANCE_BUCKETS,
    classify_distance_status,
    compute_original_distance_pct,
    is_adaptive_profile,
    select_distance_bucket,
    theoretical_bucket_label,
)
from research.backtests.adaptive_distance_staging_metrics import (
    summarize_by_distance_status,
    summarize_by_profile_distance_status,
)
from research.backtests.inventory_mtm_freeze import safe_float
from research.backtests.multicoin_price_staging_grid import (
    assert_output_dir_safe,
    atomic_write_json,
    load_csv_rows,
    write_csv,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = (
    ROOT / "research/backtests/results/adaptive_distance_staging_stage_c_20260722"
)
DEFAULT_OUT = (
    ROOT / "research/backtests/results/adaptive_distance_bucket_unknown_audit_20260722"
)


def _classify_legacy_stage_c_row(row: dict[str, Any]) -> dict[str, Any]:
    """Classify one Stage-C raw row using exported fields only (no overwrite of input)."""
    prof = str(row.get("profile") or "")
    d = safe_float(row.get("original_distance_pct"))
    if d == 0.0 and not row.get("original_distance_pct"):
        d = None
    # safe_float may return 0.0 for missing — treat blank as None
    raw_d = row.get("original_distance_pct")
    if raw_d in (None, ""):
        d = None
    else:
        try:
            d = float(raw_d)
        except (TypeError, ValueError):
            d = None

    bucket = row.get("distance_bucket") or None
    if bucket == "":
        bucket = None
    max_cycle = int(safe_float(row.get("max_cycle")))
    theo = None
    if d is not None:
        theo = theoretical_bucket_label(select_distance_bucket(d))

    # Stage-C never exported plans list; infer follow-up from distance/bucket presence.
    has_followup = bool(bucket) or (d is not None and d > 0)
    status = classify_distance_status(
        profile=prof,
        max_cycle=max_cycle,
        distance_pct=d,
        bucket=bucket or theo,
        has_c4_followup_plan=has_followup,
        plan_accepted=None,
        adaptive=is_adaptive_profile(prof),
    )
    # Preserve real buckets when present.
    if bucket in REAL_DISTANCE_BUCKETS:
        status = str(bucket)
    elif theo in REAL_DISTANCE_BUCKETS and is_adaptive_profile(prof):
        status = str(theo)
    elif prof == "two_early_medium" and not has_followup:
        status = "fixed_profile_no_adaptive_bucket"

    return {
        **{k: row.get(k) for k in (
            "pair_key",
            "run_key",
            "profile",
            "coin",
            "window_id",
            "start_index",
            "max_cycle",
            "staging_activated",
            "fallback_used",
            "effective_stage_count_after_rounding",
            "planned_stages",
            "original_distance_pct",
            "distance_bucket",
        )},
        "distance_status": status,
        "theoretical_distance_bucket": theo or bucket,
        "has_exported_distance": int(d is not None),
        "has_exported_bucket": int(bool(bucket)),
        "classification_source": "stage_c_csv_audit",
    }


def _reconstruct_distance_row(row: dict[str, Any]) -> dict[str, Any]:
    """Recompute distance from exported first/full if present; else from original_distance_pct."""
    first = row.get("first_leg_fill_price")
    full = row.get("full_trigger_price")
    exported = row.get("original_distance_pct")
    recon = None
    try:
        if first not in (None, "") and full not in (None, ""):
            recon = compute_original_distance_pct(float(first), float(full))
    except (TypeError, ValueError):
        recon = None
    exported_f = None
    try:
        if exported not in (None, ""):
            exported_f = float(exported)
    except (TypeError, ValueError):
        exported_f = None
    match = None
    if recon is not None and exported_f is not None:
        match = abs(recon - exported_f) < 1e-9
    bucket_export = row.get("distance_bucket") or None
    bucket_recon = (
        theoretical_bucket_label(select_distance_bucket(recon)) if recon is not None else None
    )
    return {
        "pair_key": row.get("pair_key"),
        "run_key": row.get("run_key"),
        "profile": row.get("profile"),
        "exported_original_distance_pct": exported_f,
        "reconstructed_distance_pct": recon,
        "distance_match": match,
        "exported_bucket": bucket_export,
        "reconstructed_bucket": bucket_recon,
        "bucket_match": (
            (str(bucket_export) == str(bucket_recon))
            if bucket_export and bucket_recon
            else None
        ),
    }


def run_audit(*, input_dir: Path, output_dir: Path) -> dict[str, Any]:
    assert_output_dir_safe(output_dir, resume=False)
    if output_dir.resolve() == input_dir.resolve():
        raise RuntimeError("refusing to write audit into the Stage-C input directory")
    output_dir.mkdir(parents=True, exist_ok=True)

    raw = load_csv_rows(input_dir / "raw_profile_runs.csv")
    if not raw:
        raise RuntimeError(f"no raw_profile_runs.csv in {input_dir}")

    classified = [_classify_legacy_stage_c_row(r) for r in raw]
    reconstructions = [_reconstruct_distance_row(r) for r in raw if r.get("original_distance_pct")]

    write_csv(output_dir / "unknown_classification.csv", classified)
    write_csv(output_dir / "distance_reconstruction.csv", reconstructions)
    write_csv(output_dir / "summary_by_distance_status.csv", summarize_by_distance_status(classified))
    write_csv(
        output_dir / "summary_by_profile_distance_status.csv",
        summarize_by_profile_distance_status(classified),
    )

    # Reproduce Stage-C adaptive_equal empty breakdown
    ae = [r for r in classified if r.get("profile") == "adaptive_equal"]
    ae_empty = [r for r in ae if not r.get("distance_bucket") and not r.get("has_exported_distance")]
    n_before = sum(1 for r in ae_empty if r["distance_status"] == "not_applicable_before_cycle4")
    n_pending = sum(1 for r in ae_empty if r["distance_status"] == "cycle4_pending_no_followup")
    n_real = Counter(
        r["distance_status"] for r in ae if r["distance_status"] in REAL_DISTANCE_BUCKETS
    )

    tem = [r for r in classified if r.get("profile") == "two_early_medium"]
    tem_fixed = sum(
        1 for r in tem if r["distance_status"] == "fixed_profile_no_adaptive_bucket"
    )

    integrity = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "input_dir": str(input_dir),
        "n_raw_rows": len(raw),
        "adaptive_equal": {
            "n": len(ae),
            "empty_export": len(ae_empty),
            "not_applicable_before_cycle4": n_before,
            "cycle4_pending_no_followup": n_pending,
            "bucket_counts": dict(n_real),
            "reproduces_188_4": bool(n_before == 188 and n_pending == 4),
        },
        "two_early_medium": {
            "n": len(tem),
            "fixed_profile_no_adaptive_bucket": tem_fixed,
        },
        "reconstruction_n": len(reconstructions),
        "reconstruction_mismatches": sum(
            1 for r in reconstructions if r.get("distance_match") is False
        ),
        "pass": bool(n_before == 188 and n_pending == 4 and tem_fixed == len(tem)),
    }
    atomic_write_json(output_dir / "integrity.json", integrity)

    report = [
        "# Adaptive Distance Bucket Unknown Audit",
        "",
        f"Input: `{input_dir}`",
        f"Generated: `{integrity['generated_at']}`",
        "",
        "## adaptive_equal empty export",
        "",
        f"- not_applicable_before_cycle4: **{n_before}** (expect 188)",
        f"- cycle4_pending_no_followup: **{n_pending}** (expect 4)",
        f"- real buckets: `{dict(n_real)}`",
        f"- reproduces 188/4: **{integrity['adaptive_equal']['reproduces_188_4']}**",
        "",
        "## two_early_medium",
        "",
        f"- fixed_profile_no_adaptive_bucket: **{tem_fixed}/{len(tem)}**",
        "",
        "## Reconstruction",
        "",
        f"- rows with exported distance: {len(reconstructions)}",
        f"- mismatches: {integrity['reconstruction_mismatches']}",
        "",
        f"## Integrity pass: **{integrity['pass']}**",
        "",
    ]
    (output_dir / "REPORT.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    return integrity


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = p.parse_args(argv)
    integrity = run_audit(input_dir=args.input, output_dir=args.out)
    print(json.dumps(integrity, indent=2))
    return 0 if integrity.get("pass") else 1


if __name__ == "__main__":
    raise SystemExit(main())
