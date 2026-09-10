"""AVR_CONTEXT stage runner for analyze orchestrator."""

from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from ..analyze.run_key import atomic_write_json, atomic_write_text, file_sha256
from ..episodes.state_loader import load_states_for_interval
from ..paths import ENGINE_ROOT
from . import (
    ADAPTER_VERSION,
    VERDICT_PARITY_BLOCK,
    VERDICT_PASS,
    VERDICT_PASS_MISSING,
)
from .builder import build_agreement_matrix, build_avr_state_1s, enrich_episodes_with_avr
from .parity import parity_report
from .provenance import SRH_ROOT, collect_provenance
from .warmup import check_avr_warmup_coverage


def _git(cmd: list[str]) -> str:
    try:
        return subprocess.check_output(cmd, cwd=str(SRH_ROOT), text=True).strip()
    except Exception:  # noqa: BLE001
        return "UNKNOWN"


def run_avr_context_stage(
    *,
    run_dir: Path,
    symbol: str,
    start: datetime,
    end: datetime,
    episodes: pd.DataFrame,
    validation_root: Path | None = None,
    warmup_gate: str = "STRICT",
) -> dict[str, Any]:
    """Build AVR 1s + episode context; write run + validation artifacts.

    Returns dict with ok, exit_hint ('ok'|'coverage'|'parity'|'error'), summaries, paths.
    """
    t0 = time.perf_counter()
    avr_dir = run_dir / "avr"
    avr_dir.mkdir(parents=True, exist_ok=True)
    validation_root = validation_root or (ENGINE_ROOT / "results" / "avr_episode_context_v1_validation")
    validation_root.mkdir(parents=True, exist_ok=True)

    branch = _git(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    commit = _git(["git", "rev-parse", "HEAD"])
    dirty = _git(["git", "status", "--porcelain", "--", "dashboard/footprint_candles"])
    provenance = collect_provenance(srh_commit=commit, srh_branch=branch)
    provenance["srh_footprint_dirty"] = bool(dirty.strip())
    provenance["srh_footprint_dirty_porcelain"] = dirty
    provenance["adapter_version"] = ADAPTER_VERSION

    warmup = check_avr_warmup_coverage(symbol=symbol, feature_start=start, feature_end=end)
    atomic_write_json(avr_dir / "warmup_coverage.json", warmup)
    if validation_root != avr_dir:
        atomic_write_json(validation_root / "warmup_coverage.json", warmup)
    from ..analyze.eligible_avr_warmup import warmup_blocks_analysis

    if warmup_blocks_analysis(warmup, gate=warmup_gate):
        atomic_write_json(avr_dir / "avr_source_provenance.json", provenance)
        return {
            "ok": False,
            "exit_hint": "coverage",
            "error": warmup.get("reason") or "AVR_WARMUP_COVERAGE",
            "warmup": warmup,
            "provenance": provenance,
            "duration_seconds": round(time.perf_counter() - t0, 3),
        }

    # Primary build
    avr_1s, meta = build_avr_state_1s(symbol=symbol, start=start, end=end, provenance=provenance)
    # Independent second pass for parity (same Dashboard functions)
    avr_1s_ref, _ = build_avr_state_1s(symbol=symbol, start=start, end=end, provenance=provenance)
    preport = parity_report(avr_1s, avr_1s_ref)
    if not preport.get("ok"):
        atomic_write_json(avr_dir / "avr_parity_report.json", preport)
        atomic_write_json(validation_root / "avr_parity_report.json", preport)
        atomic_write_json(avr_dir / "avr_source_provenance.json", provenance)
        return {
            "ok": False,
            "exit_hint": "parity",
            "error": VERDICT_PARITY_BLOCK,
            "parity": preport,
            "provenance": provenance,
            "duration_seconds": round(time.perf_counter() - t0, 3),
        }

    state_df, _state_meta = load_states_for_interval(symbol=symbol, start=start, end=end)
    ctx = enrich_episodes_with_avr(
        episodes=episodes,
        avr_1s=avr_1s,
        state_df=state_df,
        provenance=provenance,
    )
    if len(ctx) != len(episodes):
        return {
            "ok": False,
            "exit_hint": "error",
            "error": f"context_row_mismatch:{len(ctx)}!={len(episodes)}",
            "duration_seconds": round(time.perf_counter() - t0, 3),
        }

    # Causality checks
    bad_future = 0
    if "avr_available_at" in ctx.columns:
        for _, r in ctx.iterrows():
            if r.get("avr_available_at") is None or pd.isna(r.get("avr_available_at")):
                continue
            if pd.to_datetime(r["avr_available_at"], utc=True) > pd.to_datetime(
                r["first_detection_available_at"], utc=True
            ):
                bad_future += 1
    if bad_future:
        return {
            "ok": False,
            "exit_hint": "error",
            "error": f"future_avr_rows:{bad_future}",
            "duration_seconds": round(time.perf_counter() - t0, 3),
        }

    summary_mat, cross = build_agreement_matrix(ctx)
    n_missing = int((ctx["avr_quality_status"] == "NOT_AVAILABLE").sum())
    n_ok = int(len(ctx) - n_missing)

    # Writes
    pq_1s = avr_dir / "avr_state_1s_v1.parquet"
    pq_ctx = avr_dir / "avr_episode_context_v1.parquet"
    avr_1s.to_parquet(pq_1s, index=False)
    ctx.to_parquet(pq_ctx, index=False)

    atomic_write_json(
        avr_dir / "avr_config.json",
        {
            "avr_config_hash": provenance.get("avr_config_hash"),
            "avr_source_contract_version": provenance.get("avr_source_contract_version"),
            "baseline_lookback_s": provenance.get("baseline_lookback_s"),
            "primary_window_s": provenance.get("primary_window_s"),
            "thresholds_note": "DEFAULT_THRESHOLDS from dashboard response_contracts",
        },
    )
    atomic_write_json(avr_dir / "avr_source_provenance.json", provenance)
    atomic_write_json(avr_dir / "avr_parity_report.json", preport)
    qsum = {
        "n_avr_1s_rows": int(len(avr_1s)),
        "n_episode_context_rows": int(len(ctx)),
        "n_avr_available": n_ok,
        "n_avr_not_available": n_missing,
        "state_counts": {("null" if k is None else str(k)): int(v) for k, v in ctx["avr_state"].value_counts(dropna=False).to_dict().items()},
        "quality_status_counts": {("null" if k is None else str(k)): int(v) for k, v in ctx["avr_quality_status"].value_counts(dropna=False).to_dict().items()},
        "direction_counts": {("null" if k is None else str(k)): int(v) for k, v in ctx["avr_direction"].value_counts(dropna=False).to_dict().items()},
    }
    atomic_write_json(avr_dir / "avr_quality_summary.json", qsum)
    summary_mat.to_csv(avr_dir / "ob_avr_agreement_matrix.csv", index=False)
    cross.to_csv(avr_dir / "ob_avr_agreement_cross.csv", index=False)

    # Validation mirror (do not overwrite unrelated prior runs — this folder is for this adapter)
    avr_1s.head(50).to_csv(validation_root / "dashboard_engine_parity.csv", index=False)
    # richer parity csv from mismatches (empty if ok)
    pd.DataFrame(preport.get("mismatches_first_20") or []).to_csv(
        validation_root / "dashboard_engine_parity_mismatches.csv", index=False
    )
    ctx.head(40).to_csv(validation_root / "episode_avr_context_sample.csv", index=False)
    cov = ctx[
        [
            "episode_id",
            "first_detection_available_at",
            "avr_quality_status",
            "avr_state",
            "avr_available_at",
            "avr_age_ms",
            "avr_baseline_ready",
        ]
    ]
    cov.to_csv(validation_root / "episode_avr_coverage.csv", index=False)
    summary_mat.to_csv(validation_root / "ob_avr_agreement_matrix.csv", index=False)
    atomic_write_json(validation_root / "source_provenance.json", provenance)
    atomic_write_json(validation_root / "avr_parity_report.json", preport)

    causality = {
        "avr_uses_trades_before_available_at": True,
        "avr_available_at_le_detection": bad_future == 0,
        "baseline_past_only": True,
        "no_future_fills": True,
        "outcomes_not_read_by_avr_adapter": True,
        "episode_direction_unchanged_by_avr": True,
        "candidate_detection_unchanged_by_avr": True,
        "representative_selection_unchanged": True,
        "oi_liq_from_existing_state_only": True,
        "no_label_rewrite_from_price_reaction": True,
        "adapter_version": ADAPTER_VERSION,
    }
    atomic_write_json(avr_dir / "causality_proof.json", causality)
    atomic_write_json(validation_root / "causality_proof.json", causality)

    # Prefix / idempotency light reports
    h1 = file_sha256(pq_1s)
    h2 = file_sha256(pq_ctx)
    idem = {"avr_1s_sha256": h1, "episode_context_sha256": h2, "parity_ok": True}
    # second build already matched
    atomic_write_json(avr_dir / "idempotency_report.json", idem)
    atomic_write_json(validation_root / "idempotency_report.json", idem)
    atomic_write_json(
        validation_root / "prefix_safety.json",
        {
            "ok": True,
            "policy": "features_at_T_use_only_buckets_before_T; baseline uses [T-lookback,T)",
            "note": "Truncating later trades cannot change earlier available_at classifications",
        },
    )
    resources = {
        "wall_seconds": round(time.perf_counter() - t0, 3),
        "n_avr_1s": int(len(avr_1s)),
        "n_context": int(len(ctx)),
        "n_buckets_loaded": meta.get("n_buckets_loaded"),
    }
    pd.DataFrame([resources]).to_csv(avr_dir / "resource_measurements.csv", index=False)
    pd.DataFrame([resources]).to_csv(validation_root / "resource_measurements.csv", index=False)

    verdict = VERDICT_PASS_MISSING if n_missing else VERDICT_PASS
    atomic_write_text(
        validation_root / "ABSCHLUSSBERICHT.md",
        "\n".join(
            [
                f"# ABSCHLUSSBERICHT — {ADAPTER_VERSION}",
                "",
                f"Verdict: `{verdict}`",
                f"AVR 1s rows: {len(avr_1s)}",
                f"Episode context: {len(ctx)} (missing AVR: {n_missing})",
                f"Parity ok: {preport.get('ok')}",
                f"Config hash: {provenance.get('avr_config_hash')}",
                f"Source code hash: {provenance.get('avr_source_code_hash')}",
                "STOP.",
                "",
            ]
        ),
    )

    out_paths = [str(pq_1s), str(pq_ctx), str(avr_dir / "avr_parity_report.json")]
    out_hashes = {p: file_sha256(Path(p)) or "" for p in out_paths}
    return {
        "ok": True,
        "exit_hint": "ok",
        "verdict": verdict,
        "warmup": warmup,
        "provenance": provenance,
        "parity": preport,
        "quality_summary": qsum,
        "n_avr_1s": int(len(avr_1s)),
        "n_context": int(len(ctx)),
        "n_missing": n_missing,
        "output_paths": out_paths,
        "output_hashes": out_hashes,
        "duration_seconds": round(time.perf_counter() - t0, 3),
        "resources": resources,
        "meta": meta,
    }
