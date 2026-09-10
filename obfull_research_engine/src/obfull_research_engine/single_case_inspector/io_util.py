"""Atomic IO, run-key, content hash for single-case inspector."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from ..analyze.run_key import atomic_write_json, atomic_write_text
from ..paths import ENGINE_ROOT
from ..timeparse import format_utc_z
from . import CONTRACT_VERSION, INSPECTOR_VERSION, SCHEMA_VERSION


def case_dir(symbol: str, focus_ts: datetime) -> Path:
    # Compact UTC from aware UTC input (CLI guarantees Z / UTC).
    compact = focus_ts.strftime("%Y-%m-%dT%H%M%SZ")
    return ENGINE_ROOT / "results" / "single_case_inspector_v1" / symbol.upper() / compact


def compute_run_key(
    *,
    symbol: str,
    focus_z: str,
    pre_seconds: int,
    post_seconds: int,
    flags: dict[str, bool],
    market_profile_identity: dict[str, Any] | None = None,
) -> str:
    payload = {
        "inspector": INSPECTOR_VERSION,
        "contract": CONTRACT_VERSION,
        "symbol": symbol.upper(),
        "focus_ts": focus_z,
        "pre_seconds": int(pre_seconds),
        "post_seconds": int(post_seconds),
        "with_footprint": bool(flags.get("with_footprint")),
        "with_avr": bool(flags.get("with_avr")),
        "with_oi": bool(flags.get("with_oi")),
        "with_liquidations": bool(flags.get("with_liquidations")),
    }
    # Legacy inspector keys stay identical unless Market Profile is explicitly on.
    if flags.get("with_market_profile"):
        payload["with_market_profile"] = True
        if market_profile_identity:
            payload.update(market_profile_identity)
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "sci_" + hashlib.sha256(blob).hexdigest()[:16]


def atomic_df_csv(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_csv(tmp, index=False)
    os.replace(tmp, path)


def atomic_df_parquet(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def content_hash_payload(obj: Any) -> str:
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(blob).hexdigest()


def write_case_artifacts(
    out_dir: Path,
    *,
    manifest: dict[str, Any],
    coverage: dict[str, Any],
    full_ob: dict[str, Any],
    pre_focus_df: pd.DataFrame,
    focus_snapshot: dict[str, Any],
    fp_timeline: pd.DataFrame,
    avr_timeline: pd.DataFrame,
    oi_df: pd.DataFrame,
    liq_df: pd.DataFrame,
    evidence_df: pd.DataFrame,
    outcomes_df: pd.DataFrame,
    causality: dict[str, Any],
    quality: dict[str, Any],
    technical: dict[str, Any],
    case_report_md: str,
    test_report: str,
    run_key: str,
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    # Idempotency: if manifest exists with same run_key + content hash skip overwrite of identity
    prior = out_dir / "case_manifest.json"
    idem: dict[str, Any] = {"run_key": run_key}

    atomic_write_json(out_dir / "case_manifest.json", manifest)
    atomic_write_json(out_dir / "coverage_report.json", coverage)
    atomic_write_json(out_dir / "full_ob_availability.json", full_ob)
    atomic_df_parquet(out_dir / "pre_focus_multiscale_context.parquet", pre_focus_df)
    atomic_write_json(out_dir / "focus_snapshot.json", focus_snapshot)
    atomic_df_csv(out_dir / "footprint_5s_timeline.csv", fp_timeline)
    atomic_df_csv(out_dir / "avr_timeline.csv", avr_timeline)
    atomic_df_csv(out_dir / "oi_context.csv", oi_df)
    atomic_df_csv(out_dir / "liquidation_context.csv", liq_df)
    atomic_df_csv(out_dir / "early_evidence_timeline.csv", evidence_df)
    atomic_df_csv(out_dir / "post_focus_outcomes.csv", outcomes_df)
    atomic_write_json(out_dir / "causality_proof.json", causality)
    atomic_write_json(out_dir / "quality_report.json", quality)
    atomic_write_json(out_dir / "technical_report.json", technical)
    atomic_write_text(out_dir / "CASE_REPORT.md", case_report_md)
    atomic_write_text(out_dir / "test_report.txt", test_report)

    # Content hash over stable core
    core = {
        "coverage": coverage,
        "full_ob_status": full_ob.get("status"),
        "focus_snapshot_keys": sorted(focus_snapshot.keys()),
        "n_pre_focus": len(pre_focus_df),
        "n_evidence": len(evidence_df),
        "n_outcomes": len(outcomes_df),
        "outcomes": outcomes_df.to_dict(orient="records") if not outcomes_df.empty else [],
        "evidence": evidence_df.to_dict(orient="records") if not evidence_df.empty else [],
        "oi": oi_df.to_dict(orient="records") if not oi_df.empty else [],
        "liq": liq_df.to_dict(orient="records") if not liq_df.empty else [],
    }
    ch = content_hash_payload(core)
    idem["content_hash"] = ch
    if prior.exists():
        try:
            old = json.loads(prior.read_text(encoding="utf-8"))
            idem["prior_run_key"] = old.get("run_key")
            idem["same_run_key"] = old.get("run_key") == run_key
        except Exception:  # noqa: BLE001
            pass
    atomic_write_json(out_dir / "idempotency_report.json", idem)
    return idem
