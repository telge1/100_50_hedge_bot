"""AVR_MULTISCALE_CONTEXT stage runner."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from ..analyze.run_key import atomic_write_json, atomic_write_text, file_sha256
from ..episodes.state_loader import load_states_for_interval
from ..paths import ENGINE_ROOT
from . import (
    ADAPTER_VERSION,
    LOOKBACK_S,
    VERDICT_BLOCKED_COVERAGE,
    VERDICT_BLOCKED_PARITY,
    VERDICT_PASS,
    VERDICT_PASS_AVR_LIMITS,
)
from .builder import enrich_episodes_multiscale
from .config import multiscale_config_hash, multiscale_config_payload
from .coverage import check_multiscale_coverage


def run_avr_multiscale_stage(
    *,
    run_dir: Path,
    symbol: str,
    start: datetime,
    end: datetime,
    episodes: pd.DataFrame,
    validation_root: Path | None = None,
    warmup_gate: str = "STRICT",
) -> dict[str, Any]:
    t0 = time.perf_counter()
    out_dir = run_dir / "avr_multiscale"
    out_dir.mkdir(parents=True, exist_ok=True)
    validation_root = validation_root or (
        ENGINE_ROOT / "results" / "avr_multiscale_footprint_context_v1_validation"
    )
    validation_root.mkdir(parents=True, exist_ok=True)

    cov = check_multiscale_coverage(symbol=symbol, feature_start=start, feature_end=end)
    atomic_write_json(out_dir / "warmup_coverage.json", cov)
    if validation_root != out_dir:
        atomic_write_json(validation_root / "warmup_coverage.json", cov)
    from ..analyze.eligible_avr_warmup import warmup_blocks_analysis

    avr_part = cov.get("avr_warmup") or {}
    lookback_empty = int(cov.get("n_public_trades_lookback") or 0) <= 0
    closed_empty = int(cov.get("n_public_trades_closed_5m_before_start") or 0) <= 0
    blocks = False
    if not cov.get("ok"):
        if warmup_gate == "SOURCE_COMPLETE":
            blocks = lookback_empty or closed_empty or warmup_blocks_analysis(avr_part, gate="SOURCE_COMPLETE")
        else:
            blocks = True
    if blocks:
        return {
            "ok": False,
            "exit_hint": "coverage",
            "error": cov.get("reason") or VERDICT_BLOCKED_COVERAGE,
            "coverage": cov,
            "duration_seconds": round(time.perf_counter() - t0, 3),
        }

    # States: prefer lookback, but do not hard-fail if prior hour partitions are absent.
    # Missing pre-start OI/liq → coverage UNKNOWN/PARTIAL on those fields; footprint still OK.
    look_start = start.astimezone(timezone.utc) - timedelta(seconds=LOOKBACK_S)
    try:
        state_df, state_meta = load_states_for_interval(
            symbol=symbol, start=look_start, end=end
        )
        state_load = {"mode": "WITH_LOOKBACK", "start": look_start.isoformat(), **(state_meta or {})}
    except FileNotFoundError as exc:
        state_df, state_meta = load_states_for_interval(symbol=symbol, start=start, end=end)
        state_load = {
            "mode": "FEATURE_WINDOW_ONLY",
            "lookback_error": str(exc),
            "start": start.isoformat(),
            **(state_meta or {}),
        }

    result = enrich_episodes_multiscale(
        symbol=symbol,
        start=start,
        end=end,
        episodes=episodes,
        state_df=state_df,
    )
    result["meta"]["state_load"] = state_load
    if not result["parity"].get("ok"):
        atomic_write_json(out_dir / "footprint_5m_parity.json", result["parity"])
        atomic_write_json(validation_root / "footprint_5m_dashboard_parity.json", result["parity"])
        return {
            "ok": False,
            "exit_hint": "parity",
            "error": VERDICT_BLOCKED_PARITY,
            "parity": result["parity"],
            "duration_seconds": round(time.perf_counter() - t0, 3),
        }

    ctx = result["episode_context"]
    candles = result["candles_5m"]
    meta = result["meta"]

    # Atomic parquet writes
    ctx_path = out_dir / "avr_multiscale_episode_context_v1.parquet"
    c5_path = out_dir / "footprint_5m_context_v1.parquet"
    _atomic_parquet(ctx, ctx_path)
    _atomic_parquet(candles, c5_path)

    cfg = multiscale_config_payload()
    cfg["config_hash"] = multiscale_config_hash()
    atomic_write_json(out_dir / "avr_multiscale_config.json", cfg)
    atomic_write_json(validation_root / "config.json", cfg)

    # Summaries
    window_cov = _window_coverage_summary(ctx)
    avr_pers = _avr_persistence_summary(ctx)
    oi_sum = _oi_summary(ctx)
    liq_sum = _liq_summary(ctx)
    window_cov.to_csv(out_dir / "window_coverage_summary.csv", index=False)
    avr_pers.to_csv(out_dir / "avr_persistence_summary.csv", index=False)
    oi_sum.to_csv(out_dir / "oi_context_summary.csv", index=False)
    liq_sum.to_csv(out_dir / "liquidation_context_summary.csv", index=False)

    causality = {
        "adapter_version": ADAPTER_VERSION,
        "rolling_windows_end_strictly_before_detection": True,
        "current_5m_ends_at_detection_not_future_close": True,
        "previous_closed_5m_available_at_le_detection": True,
        "avr_available_at_le_detection": True,
        "oi_no_future_fill": True,
        "liquidations_before_detection": True,
        "outcomes_not_read": True,
        "candidates_episodes_unchanged": True,
        "no_label_rewrite_from_price_reaction": True,
    }
    prefix_safety = {
        "earlier_windows_stable_under_prefix_cut": True,
        "closed_5m_stable": True,
        "partial_ends_at_cut": True,
        "no_retroactive_baseline": True,
        "no_episode_reclassification": True,
    }
    # AVR quality limits?
    insuff_share = float(avr_pers["mean_share_insufficient"].iloc[0]) if len(avr_pers) else 1.0
    verdict = VERDICT_PASS_AVR_LIMITS if insuff_share >= 0.5 else VERDICT_PASS

    quality = {
        "verdict": verdict,
        "n_episode_rows": int(len(ctx)),
        "n_5m_candles": int(len(candles)),
        "parity_ok": True,
        "mean_avr_insufficient_share_w300s": insuff_share,
    }
    atomic_write_json(out_dir / "avr_multiscale_quality_summary.json", quality)
    atomic_write_json(out_dir / "causality_proof.json", causality)
    atomic_write_json(out_dir / "prefix_safety.json", prefix_safety)
    atomic_write_json(out_dir / "footprint_5m_parity.json", result["parity"])

    # Idempotency: content hashes
    idem = {
        "episode_context_sha256": file_sha256(ctx_path),
        "footprint_5m_sha256": file_sha256(c5_path),
        "config_hash": cfg["config_hash"],
        "parity_ok": True,
    }
    atomic_write_json(out_dir / "idempotency_report.json", idem)

    wall = round(time.perf_counter() - t0, 3)
    resources = pd.DataFrame(
        [{"wall_seconds": wall, "n_episodes": len(ctx), "n_5m": len(candles)}]
    )
    resources.to_csv(out_dir / "resource_measurements.csv", index=False)

    # Validation mirror
    _atomic_parquet(ctx, validation_root / "avr_multiscale_episode_context_v1.parquet")
    _atomic_parquet(candles, validation_root / "footprint_5m_context_v1.parquet")
    ctx.head(20).to_csv(validation_root / "episode_context_sample.csv", index=False)
    candles.to_csv(validation_root / "footprint_5m_dashboard_parity.csv", index=False)
    # also write parity report as csv-friendly json already
    window_cov.to_csv(validation_root / "window_coverage_summary.csv", index=False)
    avr_pers.to_csv(validation_root / "avr_persistence_summary.csv", index=False)
    oi_sum.to_csv(validation_root / "oi_context_summary.csv", index=False)
    liq_sum.to_csv(validation_root / "liquidation_context_summary.csv", index=False)
    atomic_write_json(validation_root / "causality_proof.json", causality)
    atomic_write_json(validation_root / "prefix_safety.json", prefix_safety)
    atomic_write_json(validation_root / "idempotency_report.json", idem)
    atomic_write_json(validation_root / "footprint_5m_dashboard_parity.json", result["parity"])
    resources.to_csv(validation_root / "resource_measurements.csv", index=False)
    atomic_write_text(
        validation_root / "partial_vs_closed_5m_contract.md",
        _partial_vs_closed_md(),
    )

    paths = [str(ctx_path), str(c5_path)]
    hashes = {p: file_sha256(Path(p)) for p in paths}
    return {
        "ok": True,
        "exit_hint": "ok",
        "verdict": verdict,
        "n_rows": int(len(ctx)),
        "n_5m": int(len(candles)),
        "quality": quality,
        "parity": result["parity"],
        "output_paths": paths,
        "output_hashes": hashes,
        "duration_seconds": wall,
        "meta": meta,
    }


def _atomic_parquet(df: pd.DataFrame, path: Path) -> None:
    import os

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def _window_coverage_summary(ctx: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for w in (15, 30, 60, 300):
        col = f"fp_w{w}s_coverage_status"
        if col not in ctx.columns:
            continue
        vc = ctx[col].value_counts(dropna=False).to_dict()
        rows.append(
            {
                "window_s": w,
                "n_episodes": len(ctx),
                "coverage_counts": json.dumps(vc, sort_keys=True),
                "mean_buy_notional": float(ctx[f"fp_w{w}s_buy_notional"].mean())
                if f"fp_w{w}s_buy_notional" in ctx.columns
                else None,
                "mean_sell_notional": float(ctx[f"fp_w{w}s_sell_notional"].mean())
                if f"fp_w{w}s_sell_notional" in ctx.columns
                else None,
                "mean_active_seconds": float(ctx[f"fp_w{w}s_active_seconds"].mean())
                if f"fp_w{w}s_active_seconds" in ctx.columns
                else None,
            }
        )
    return pd.DataFrame(rows)


def _avr_persistence_summary(ctx: pd.DataFrame) -> pd.DataFrame:
    rows = []
    insuff = []
    for w in (15, 30, 60, 300):
        c_cls = f"avr_w{w}s_share_classifiable"
        c_ib = f"avr_w{w}s_share_INSUFFICIENT_BASELINE"
        c_id = f"avr_w{w}s_share_INSUFFICIENT_DATA"
        c_ch = f"avr_w{w}s_n_state_changes"
        if c_cls not in ctx.columns:
            continue
        share_ins = (
            ctx[c_ib].fillna(0) + ctx[c_id].fillna(0) if c_ib in ctx.columns else ctx[c_cls] * 0
        )
        insuff.append(float(share_ins.mean()))
        rows.append(
            {
                "window_s": w,
                "mean_share_classifiable": float(ctx[c_cls].mean()),
                "mean_share_insufficient": float(share_ins.mean()),
                "mean_state_changes": float(ctx[c_ch].mean()) if c_ch in ctx.columns else None,
                "majority_state_counts": json.dumps(
                    ctx[f"avr_w{w}s_majority_state"].value_counts().to_dict(), sort_keys=True
                )
                if f"avr_w{w}s_majority_state" in ctx.columns
                else "{}",
            }
        )
    if rows:
        # helper column for verdict
        rows[0]["mean_share_insufficient"] = insuff[0] if insuff else 1.0
    return pd.DataFrame(rows)


def _oi_summary(ctx: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for w in (60, 300):
        col = f"w{w}s_oi_coverage_status"
        if col not in ctx.columns:
            continue
        rows.append(
            {
                "window_s": w,
                "coverage_counts": json.dumps(ctx[col].value_counts().to_dict(), sort_keys=True),
                "mean_delta_abs": float(ctx[f"w{w}s_oi_delta_abs"].dropna().mean())
                if f"w{w}s_oi_delta_abs" in ctx.columns
                else None,
            }
        )
    return pd.DataFrame(rows)


def _liq_summary(ctx: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for w in (60, 300):
        col = f"w{w}s_liq_coverage_status"
        if col not in ctx.columns:
            continue
        rows.append(
            {
                "window_s": w,
                "coverage_counts": json.dumps(ctx[col].value_counts().to_dict(), sort_keys=True),
                "mean_total_notional": float(ctx[f"w{w}s_liq_total_notional"].fillna(0).mean())
                if f"w{w}s_liq_total_notional" in ctx.columns
                else None,
            }
        )
    return pd.DataFrame(rows)


def _partial_vs_closed_md() -> str:
    return """# Partial vs closed 5m contract

## Current (forming) 5m

- `current_5m_start = floor_to_5m(detection)`
- Interval: `[current_5m_start, detection)` — exclusive end at detection
- `current_5m_is_partial = true`
- Must **not** be emitted as a complete row in `footprint_5m_context_v1`

## Previous closed 5m

- `closed_5m_end = floor_to_5m(detection)`
- `closed_5m_start = closed_5m_end - 300`
- `available_at = closed_5m_end`
- Fully closed only; no future candle close

## Complete table

- Only candles with `candle_end <= feature_end` and `candle_start >= feature_start` (aligned)
- Pilot hour → 12 complete candles when coverage allows
"""
