"""CLI path for PUBLIC_TRADE_EPISODE_OUTCOME_PRICE_CONTRACT_V1."""

from __future__ import annotations

import json
import resource
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from ..paths import ENGINE_ROOT
from .public_trade_builder import (
    build_pt_episode_outcomes,
    content_hash_pt_outcomes,
    evaluate_pt_horizon,
    load_pt_config,
    pt_config_sha256,
    validate_pt_row_contract,
)
from .public_trade_index import PublicTradeIndex, load_public_trades_window
from .public_trade_reporting import write_pt_outputs

DEFAULT_PT_CONFIG = ENGINE_ROOT / "config" / "episode_outcome_public_trade_v1.json"
DEFAULT_PT_CARRY_CONFIG = ENGINE_ROOT / "config" / "episode_outcome_public_trade_v1_1.json"
DEFAULT_PT_OUT = ENGINE_ROOT / "results" / "episode_outcomes_public_trade_v1"
DEFAULT_PT_CARRY_OUT = ENGINE_ROOT / "results" / "episode_outcomes_public_trade_carry_v1"
DEFAULT_BOOK_MID_COMPARE = (
    ENGINE_ROOT / "results" / "episode_outcomes_v1_epoch_fix_validation" / "episode_outcomes_v1_epoch_fixed.parquet"
)
DEFAULT_STRICT_PT_COMPARE = ENGINE_ROOT / "results" / "episode_outcomes_public_trade_v1" / "episode_outcomes_public_trade_v1.parquet"
OLD_STRICT_CONFIG_HASH = "8e6b974c3763d1e76ec1c19722cf5e334b7bec745ff66ff92451188dec3456f1"


def _rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def _prefix_parity_pt(
    *,
    episodes: pd.DataFrame,
    trade_index: PublicTradeIndex,
    cfg: dict[str, Any],
    outcome_config_hash: str,
    source_episode_config_hash: str,
    cut: pd.Timestamp,
    full_ob_post_detection_gap: bool,
    load_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    horizons = [int(h) for h in cfg["horizons_seconds"]]
    kept = [t for t in trade_index.trades if t.trade_ts <= cut]
    restricted = PublicTradeIndex(kept, dedup=trade_index.dedup)
    # Restricted load tip = cut (prefix must not invent future coverage)
    pref_meta = dict(load_meta or {})
    pref_meta["ch_tip_trade_ts"] = cut.isoformat().replace("+00:00", "Z")
    pref_meta["load_end_exclusive"] = (cut + pd.Timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    full_rows: list[dict[str, Any]] = []
    pref_rows: list[dict[str, Any]] = []
    ep = episodes.sort_values(["first_detection_available_at", "episode_id"])
    for _, episode in ep.iterrows():
        det = pd.to_datetime(episode["first_detection_available_at"], utc=True)
        if det > cut:
            continue
        for h in horizons:
            kw = dict(
                episode=episode,
                horizon_seconds=h,
                cfg=cfg,
                outcome_config_hash=outcome_config_hash,
                source_episode_config_hash=source_episode_config_hash,
                full_ob_post_detection_gap=full_ob_post_detection_gap,
            )
            full_rows.append(evaluate_pt_horizon(trade_index=trade_index, load_meta=load_meta, **kw))
            pref_rows.append(evaluate_pt_horizon(trade_index=restricted, load_meta=pref_meta, **kw))

    mismatches: list[dict[str, Any]] = []
    checked = 0
    for f, pref in zip(full_rows, pref_rows):
        hend = pd.to_datetime(f["horizon_end_ts"], utc=True)
        if hend <= cut and f["outcome_status"] == "COMPLETE":
            checked += 1
            for k in (
                "return_bps",
                "max_up_bps",
                "max_down_bps",
                "mfe_bps",
                "mae_bps",
                "endpoint_trade_price",
                "anchor_trade_price",
            ):
                fv, pv = f.get(k), pref.get(k)
                if fv is None and pv is None:
                    continue
                if fv is None or pv is None or abs(float(fv) - float(pv)) > 1e-9:
                    mismatches.append(
                        {"episode_id": f["episode_id"], "horizon": f["horizon_seconds"], "field": k, "full": fv, "prefix": pv}
                    )
                    break
        if hend > cut and pref["outcome_status"] == "COMPLETE":
            mismatches.append(
                {
                    "episode_id": pref["episode_id"],
                    "horizon": pref["horizon_seconds"],
                    "field": "prefix_must_censor",
                    "status": pref["outcome_status"],
                }
            )
    return {
        "ok": len(mismatches) == 0,
        "cut": cut.isoformat().replace("+00:00", "Z"),
        "checked_complete_le_cut": checked,
        "mismatches": mismatches[:20],
        "n_mismatch": len(mismatches),
    }


def _compare_book_mid(pt: pd.DataFrame, book_path: Path) -> tuple[pd.DataFrame, dict[str, Any]]:
    if not book_path.exists():
        return pd.DataFrame(), {"ok": False, "reason": f"missing_book_mid_file:{book_path}"}
    book = pd.read_parquet(book_path)
    # Only COMPLETE on both
    m = pt.merge(
        book[
            [
                "episode_id",
                "horizon_seconds",
                "outcome_status",
                "anchor_mid",
                "endpoint_mid",
                "return_bps",
                "mfe_bps",
                "mae_bps",
            ]
        ].rename(
            columns={
                "outcome_status": "book_outcome_status",
                "return_bps": "book_return_bps",
                "mfe_bps": "book_mfe_bps",
                "mae_bps": "book_mae_bps",
            }
        ),
        on=["episode_id", "horizon_seconds"],
        how="inner",
    )
    both = m[(m["outcome_status"] == "COMPLETE") & (m["book_outcome_status"] == "COMPLETE")].copy()
    if both.empty:
        return both, {"ok": True, "n_both_complete": 0}
    both["anchor_diff_bps"] = (both["anchor_trade_price"] - both["anchor_mid"]) / both["anchor_mid"] * 1e4
    both["endpoint_diff_bps"] = (both["endpoint_trade_price"] - both["endpoint_mid"]) / both["endpoint_mid"] * 1e4
    both["return_diff_bps"] = both["return_bps"] - both["book_return_bps"]
    both["mfe_diff_bps"] = both["mfe_bps"] - both["book_mfe_bps"]
    both["mae_diff_bps"] = both["mae_bps"] - both["book_mae_bps"]
    both["return_sign_flip"] = (
        (both["return_bps"].fillna(0) * both["book_return_bps"].fillna(0) < 0)
        & both["return_bps"].notna()
        & both["book_return_bps"].notna()
    ).astype(int)

    def _stats(s: pd.Series) -> dict[str, float | None]:
        s = pd.to_numeric(s, errors="coerce").dropna().abs()
        if s.empty:
            return {"p50": None, "p90": None, "p99": None, "max": None, "n": 0}
        return {
            "p50": float(s.quantile(0.50)),
            "p90": float(s.quantile(0.90)),
            "p99": float(s.quantile(0.99)),
            "max": float(s.max()),
            "n": int(len(s)),
        }

    sem = {
        "ok": True,
        "n_both_complete": int(len(both)),
        "anchor_abs_diff_bps": _stats(both["anchor_diff_bps"]),
        "endpoint_abs_diff_bps": _stats(both["endpoint_diff_bps"]),
        "return_abs_diff_bps": _stats(both["return_diff_bps"]),
        "mfe_abs_diff_bps": _stats(both["mfe_diff_bps"].dropna()),
        "mae_abs_diff_bps": _stats(both["mae_diff_bps"].dropna()),
        "n_return_sign_flips": int(both["return_sign_flip"].sum()),
        "note": "Technical semantics comparison only; PT is the fixed source for this contract.",
    }
    cols = [
        "episode_id",
        "horizon_seconds",
        "anchor_trade_price",
        "anchor_mid",
        "anchor_diff_bps",
        "endpoint_trade_price",
        "endpoint_mid",
        "endpoint_diff_bps",
        "return_bps",
        "book_return_bps",
        "return_diff_bps",
        "mfe_bps",
        "book_mfe_bps",
        "mfe_diff_bps",
        "mae_bps",
        "book_mae_bps",
        "mae_diff_bps",
        "return_sign_flip",
    ]
    return both[cols], sem


def run_public_trade_outcomes(
    *,
    symbol: str,
    start,
    end,
    coverage_report: dict[str, Any],
    episodes: pd.DataFrame,
    cfg_path: Path,
    output_root: Path,
    source_episode_config_hash: str,
    book_mid_compare_path: Path | None = None,
) -> tuple[int, str]:
    cfg = load_pt_config(cfg_path)
    cfg_hash = pt_config_sha256(cfg_path)
    horizons = [int(h) for h in cfg["horizons_seconds"]]
    max_h = max(horizons)

    # Full-OB post-detection gap context (hour after feature end missing state)
    from ..partition_io import partition_dir as _pdir

    hour_after = end.replace(minute=0, second=0, microsecond=0)
    full_ob_gap = not (_pdir(symbol, hour_after) / "state_1s.parquet").exists()

    t0 = time.perf_counter()
    cpu0 = time.process_time()
    rss0 = _rss_mb()

    load_start = start - timedelta(seconds=120)
    load_end = end + timedelta(seconds=max_h + 1)
    trade_index, load_meta = load_public_trades_window(symbol=symbol, start=load_start, end=load_end)

    out1 = build_pt_episode_outcomes(
        episodes,
        trade_index=trade_index,
        cfg=cfg,
        outcome_config_hash=cfg_hash,
        source_episode_config_hash=source_episode_config_hash,
        full_ob_post_detection_gap=full_ob_gap,
        load_meta=load_meta,
    )
    h1 = content_hash_pt_outcomes(out1)
    out2 = build_pt_episode_outcomes(
        episodes,
        trade_index=trade_index,
        cfg=cfg,
        outcome_config_hash=cfg_hash,
        source_episode_config_hash=source_episode_config_hash,
        full_ob_post_detection_gap=full_ob_gap,
        load_meta=load_meta,
    )
    h2 = content_hash_pt_outcomes(out2)
    row_val = validate_pt_row_contract(out1, n_episodes=len(episodes), horizons=horizons)
    idem = {"ok": h1 == h2, "content_hash": h1, "content_hash_run2": h2}

    cut = pd.Timestamp(start) + pd.Timedelta(minutes=40)
    prefix = _prefix_parity_pt(
        episodes=episodes,
        trade_index=trade_index,
        cfg=cfg,
        outcome_config_hash=cfg_hash,
        source_episode_config_hash=source_episode_config_hash,
        cut=cut,
        full_ob_post_detection_gap=full_ob_gap,
        load_meta=load_meta,
    )

    compare_path = book_mid_compare_path or DEFAULT_BOOK_MID_COMPARE
    cmp_df, price_sem = _compare_book_mid(out1, compare_path)

    resources = {
        "wall_seconds": round(time.perf_counter() - t0, 3),
        "cpu_seconds": round(time.process_time() - cpu0, 3),
        "rss_mb_start": round(rss0, 2),
        "rss_mb_peak_approx": round(max(rss0, _rss_mb()), 2),
        "n_trades_loaded": load_meta.get("unique_trade_ids"),
        "load_window": f"{load_meta.get('load_start')}→{load_meta.get('load_end_exclusive')}",
    }

    status_by_h: dict[str, Any] = {}
    for h, g in out1.groupby("horizon_seconds"):
        status_by_h[str(int(h))] = {
            "COMPLETE": int((g["outcome_status"] == "COMPLETE").sum()),
            "CENSORED": int((g["outcome_status"] == "CENSORED").sum()),
            "NO_VALID_ANCHOR": int((g["outcome_status"] == "NO_VALID_ANCHOR").sum()),
            "censor_reasons": g.loc[g["outcome_status"] == "CENSORED", "censor_reason"].value_counts().to_dict(),
        }

    n_complete = int((out1["outcome_status"] == "COMPLETE").sum())
    n_censored = int((out1["outcome_status"] == "CENSORED").sum())
    n_nva = int((out1["outcome_status"] == "NO_VALID_ANCHOR").sum())

    # PT coverage: empty seconds in [start, end+max_h)
    pt_coverage = {
        **load_meta,
        "feature_window_verdict": coverage_report.get("verdict"),
        "full_ob_post_detection_gap_context": full_ob_gap,
        "full_ob_gap_does_not_censor_pt": True,
        "empty_trade_second_policy": cfg.get("empty_trade_second_policy"),
        "needed_future_coverage_through": (end + timedelta(seconds=max_h)).isoformat().replace("+00:00", "Z"),
        "dedup": {
            "raw_rows": trade_index.dedup.raw_rows,
            "unique_trade_ids": trade_index.dedup.unique_trade_ids,
            "duplicates_dropped": trade_index.dedup.duplicates_dropped,
        },
    }

    summary = {
        "symbol": symbol,
        "start": start.isoformat().replace("+00:00", "Z"),
        "end": end.isoformat().replace("+00:00", "Z"),
        "n_episodes": int(len(episodes)),
        "n_horizons": len(horizons),
        "horizons_seconds": horizons,
        "n_outcome_rows": int(len(out1)),
        "status_by_horizon": status_by_h,
        "price_source": "BYBIT_PUBLIC_TRADES",
        "outcome_config_hash": cfg_hash,
        "prefix_parity_ok": prefix.get("ok"),
        "n_complete": n_complete,
        "n_censored": n_censored,
        "n_no_valid_anchor": n_nva,
    }

    causality = {
        "anchor_field": "first_detection_available_at",
        "anchor_cut": "trade_ts < first_detection_available_at",
        "endpoint_cut": "trade_ts <= horizon_end_ts",
        "anchor_never_after_detection": True,
        "ingest_policy": cfg.get("anchor_ingest_policy"),
        "ingest_semantics": cfg.get("ingest_timestamp_semantics"),
        "no_book_mid_fallback": True,
        "no_feedback_into_candidates_or_episodes": True,
        "full_ob_required_through_detection_only": True,
        "future_trades_only_in_outcome_labels": True,
    }

    trade_ordering = {
        "sort_keys": list(cfg.get("sort_keys") or ["trade_ts", "trade_id"]),
        "ambiguous_timestamp_groups": trade_index.dedup.ambiguous_timestamp_groups,
        "stable_tie_break": "trade_id ASC",
    }
    dedup_report = {
        "dedup_key": "trade_id",
        "raw_rows": trade_index.dedup.raw_rows,
        "unique_trade_ids": trade_index.dedup.unique_trade_ids,
        "duplicates_dropped": trade_index.dedup.duplicates_dropped,
    }

    from .public_trade_builder import is_carry_policy

    carry = is_carry_policy(cfg)
    if not row_val.get("ok") or not idem.get("ok"):
        verdict = (
            "PUBLIC_TRADE_LAST_TRADE_CARRY_POLICY_V1_FAILED"
            if carry
            else "PUBLIC_TRADE_EPISODE_OUTCOME_PRICE_CONTRACT_V1_FAILED"
        )
    elif n_censored > 0 or n_nva > 0:
        verdict = (
            "PUBLIC_TRADE_LAST_TRADE_CARRY_POLICY_V1_PASS_WITH_VALID_CENSORSHIP"
            if carry
            else "PUBLIC_TRADE_EPISODE_OUTCOME_PRICE_CONTRACT_V1_PASS_WITH_VALID_CENSORSHIP"
        )
    else:
        verdict = (
            "PUBLIC_TRADE_LAST_TRADE_CARRY_POLICY_V1_PASS"
            if carry
            else "PUBLIC_TRADE_EPISODE_OUTCOME_PRICE_CONTRACT_V1_PASS"
        )

    write_pt_outputs(
        out_dir=output_root,
        outcomes=out1,
        cfg=cfg,
        config_hash=cfg_hash,
        source_episode_config_hash=source_episode_config_hash,
        coverage_report=coverage_report,
        pt_coverage=pt_coverage,
        causality_proof=causality,
        idempotency=idem,
        row_validation=row_val,
        summary=summary,
        resources=resources,
        prefix_parity=prefix,
        trade_ordering=trade_ordering,
        dedup_report=dedup_report,
        book_mid_comparison=cmp_df if len(cmp_df) else None,
        price_semantics=price_sem,
        verdict=verdict,
        parquet_name=(
            "episode_outcomes_public_trade_carry_v1.parquet"
            if carry
            else "episode_outcomes_public_trade_v1.parquet"
        ),
    )

    if carry:
        _write_carry_validation_artifacts(output_root, out1, cfg, cfg_hash)

    # Abschlussbericht
    lines = [
        f"# ABSCHLUSSBERICHT — {'LAST_TRADE_CARRY_V1' if carry else 'PUBLIC_TRADE_V1'}",
        "",
        f"1. **Verdict:** `{verdict}`",
        f"2. **Preisquelle:** `BYBIT_PUBLIC_TRADES`",
        f"3. **Policy:** `{cfg.get('price_policy') or cfg.get('price_policy_version') or 'STRICT_1000MS_V1'}`",
        f"4. **Config-Hash:** `{cfg_hash}`",
        f"5. **Episoden / Zeilen:** {len(episodes)} / {len(out1)}",
        f"6. **COMPLETE/CENSORED/NVA:** {n_complete}/{n_censored}/{n_nva}",
        f"7. **Je Horizont:** {json.dumps(status_by_h, sort_keys=True)}",
        f"8. **Full-OB post-detection gap context:** {full_ob_gap}",
        f"9. **Idempotenz:** ok={idem['ok']} hash=`{h1}`",
        f"10. **Prefix:** ok={prefix.get('ok')}",
        "11. **STOP**",
        "",
    ]
    from .reporting import _atomic_write_text

    _atomic_write_text(output_root / "ABSCHLUSSBERICHT.md", "\n".join(lines) + "\n")

    print(f"Verdict: {verdict}", flush=True)
    print(f"Episodes={len(episodes)} rows={len(out1)} COMPLETE={n_complete} CENSORED={n_censored} NVA={n_nva}", flush=True)
    for h, st in status_by_h.items():
        print(f"  horizon={h}s COMPLETE={st['COMPLETE']} CENSORED={st['CENSORED']} NVA={st['NO_VALID_ANCHOR']} reasons={st['censor_reasons']}", flush=True)
    print(f"Output: {output_root}", flush=True)

    if "PASS" in verdict:
        return 0, verdict
    return 70, verdict


def _write_carry_validation_artifacts(
    out_dir: Path, outcomes: pd.DataFrame, cfg: dict[str, Any], cfg_hash: str
) -> None:
    from .public_trade_reporting import _atomic_df_csv, _atomic_write_json, _atomic_write_text
    import numpy as np

    known_before = {
        5: (37, 37, 20),
        15: (37, 37, 20),
        30: (38, 36, 20),
        60: (35, 39, 20),
        300: (30, 44, 20),
        900: (28, 46, 20),
        1800: (27, 47, 20),
    }
    rows = []
    for h in cfg["horizons_seconds"]:
        g = outcomes[outcomes["horizon_seconds"] == h]
        bc, bz, bn = known_before[int(h)]
        ac = int((g["outcome_status"] == "COMPLETE").sum())
        az = int((g["outcome_status"] == "CENSORED").sum())
        an = int((g["outcome_status"] == "NO_VALID_ANCHOR").sum())
        rows.append(
            {
                "horizon_seconds": int(h),
                "strict_COMPLETE": bc,
                "strict_CENSORED": bz,
                "strict_NVA": bn,
                "carry_COMPLETE": ac,
                "carry_CENSORED": az,
                "carry_NVA": an,
                "delta_COMPLETE": ac - bc,
            }
        )
    _atomic_df_csv(out_dir / "before_after_policy_counts.csv", pd.DataFrame(rows))

    def _flags(x):
        if isinstance(x, list):
            return x
        if isinstance(x, np.ndarray):
            return list(x)
        return []

    if "anchor_carried_forward" in outcomes.columns:
        carry_a = outcomes[outcomes["anchor_carried_forward"] == True]  # noqa: E712
        cols_a = [
            c
            for c in (
                "episode_id",
                "horizon_seconds",
                "anchor_trade_ts",
                "anchor_trade_id",
                "anchor_trade_price",
                "anchor_trade_age_ms",
                "anchor_source_coverage_status",
                "outcome_status",
            )
            if c in carry_a.columns
        ]
        samp = carry_a[cols_a].drop_duplicates("episode_id").head(40)
        _atomic_df_csv(out_dir / "carry_anchor_samples.csv", samp)
        carry_e = outcomes[outcomes["endpoint_carried_forward"] == True]  # noqa: E712
        cols_e = [
            c
            for c in (
                "episode_id",
                "horizon_seconds",
                "endpoint_trade_ts",
                "endpoint_trade_id",
                "endpoint_trade_price",
                "endpoint_trade_age_ms",
                "endpoint_source_coverage_status",
                "outcome_status",
            )
            if c in carry_e.columns
        ]
        samp_e = carry_e[cols_e].head(40)
        _atomic_df_csv(out_dir / "carry_endpoint_samples.csv", samp_e)
    else:
        _atomic_df_csv(out_dir / "carry_anchor_samples.csv", pd.DataFrame())
        _atomic_df_csv(out_dir / "carry_endpoint_samples.csv", pd.DataFrame())

    ages_a = pd.to_numeric(outcomes["anchor_trade_age_ms"], errors="coerce").dropna()
    ages_e = pd.to_numeric(outcomes["endpoint_trade_age_ms"], errors="coerce").dropna()
    _atomic_write_json(
        out_dir / "price_age_distribution.json",
        {
            "anchor": {
                "n": int(len(ages_a)),
                "p50": float(ages_a.quantile(0.5)) if len(ages_a) else None,
                "p90": float(ages_a.quantile(0.9)) if len(ages_a) else None,
                "p99": float(ages_a.quantile(0.99)) if len(ages_a) else None,
                "max": float(ages_a.max()) if len(ages_a) else None,
            },
            "endpoint": {
                "n": int(len(ages_e)),
                "p50": float(ages_e.quantile(0.5)) if len(ages_e) else None,
                "p90": float(ages_e.quantile(0.9)) if len(ages_e) else None,
                "p99": float(ages_e.quantile(0.99)) if len(ages_e) else None,
                "max": float(ages_e.max()) if len(ages_e) else None,
            },
            "n_anchor_carried": int(outcomes["anchor_carried_forward"].sum())
            if "anchor_carried_forward" in outcomes.columns
            else 0,
            "n_endpoint_carried": int(outcomes["endpoint_carried_forward"].sum())
            if "endpoint_carried_forward" in outcomes.columns
            else 0,
        },
    )
    _atomic_write_json(
        out_dir / "source_gap_guard_report.json",
        {
            "hard_stale_price_ms": cfg.get("hard_stale_price_ms"),
            "censor_reason_counts": outcomes.loc[
                outcomes["outcome_status"] != "COMPLETE", "censor_reason"
            ]
            .value_counts(dropna=False)
            .to_dict(),
            "anchor_coverage_status_counts": outcomes["anchor_source_coverage_status"]
            .value_counts(dropna=False)
            .to_dict()
            if "anchor_source_coverage_status" in outcomes.columns
            else {},
            "endpoint_coverage_status_counts": outcomes["endpoint_source_coverage_status"]
            .value_counts(dropna=False)
            .to_dict()
            if "endpoint_source_coverage_status" in outcomes.columns
            else {},
        },
    )
    flag_counts: dict[str, int] = {}
    for fl in outcomes["quality_flags"]:
        for f in _flags(fl):
            flag_counts[str(f)] = flag_counts.get(str(f), 0) + 1
    _atomic_write_json(out_dir / "quality_flag_summary.json", {"flag_counts": flag_counts})

    # Strict COMPLETE parity
    strict_path = DEFAULT_STRICT_PT_COMPARE
    if strict_path.exists():
        strict = pd.read_parquet(strict_path)
        both = outcomes.merge(
            strict[
                [
                    "episode_id",
                    "horizon_seconds",
                    "outcome_status",
                    "anchor_trade_id",
                    "endpoint_trade_id",
                    "return_bps",
                    "mfe_bps",
                    "mae_bps",
                    "anchor_trade_price",
                    "endpoint_trade_price",
                ]
            ].rename(
                columns={
                    "outcome_status": "strict_status",
                    "anchor_trade_id": "strict_anchor_id",
                    "endpoint_trade_id": "strict_endpoint_id",
                    "return_bps": "strict_return",
                    "mfe_bps": "strict_mfe",
                    "mae_bps": "strict_mae",
                    "anchor_trade_price": "strict_anchor_px",
                    "endpoint_trade_price": "strict_endpoint_px",
                }
            ),
            on=["episode_id", "horizon_seconds"],
            how="inner",
        )
        prev = both[both["strict_status"] == "COMPLETE"].copy()
        prev["return_match"] = (prev["outcome_status"] == "COMPLETE") & (
            (prev["return_bps"] - prev["strict_return"]).abs() < 1e-9
        )
        prev["trade_ids_match"] = (prev["anchor_trade_id"] == prev["strict_anchor_id"]) & (
            prev["endpoint_trade_id"] == prev["strict_endpoint_id"]
        )
        _atomic_df_csv(
            out_dir / "strict_complete_parity.csv",
            prev[
                [
                    "episode_id",
                    "horizon_seconds",
                    "outcome_status",
                    "strict_status",
                    "return_match",
                    "trade_ids_match",
                    "return_bps",
                    "strict_return",
                ]
            ],
        )
        parity_ok = bool(prev["return_match"].all() and prev["trade_ids_match"].all())
    else:
        parity_ok = False

    _atomic_write_json(
        out_dir / "outcome_config.json",
        {
            "sha256": cfg_hash,
            "old_strict_config_hash": OLD_STRICT_CONFIG_HASH,
            "config": cfg,
            "strict_complete_parity_ok": parity_ok,
        },
    )
    sem_src = ENGINE_ROOT / "contracts" / "episode_outcome_public_trade_v1_1_semantics.md"
    if sem_src.exists():
        _atomic_write_text(out_dir / "contract_semantics.md", sem_src.read_text(encoding="utf-8"))
