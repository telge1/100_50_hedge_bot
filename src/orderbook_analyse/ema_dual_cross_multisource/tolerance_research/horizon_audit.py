"""Horizon audit utilities and MFE/MAE export helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from .mfe_mae import FIRST_HIT_PAIRS, HORIZONS_MIN, compute_all_horizons, compute_mfe_mae_horizon


def horizons_for_signal_tf(signal_tf: str) -> tuple[int, ...]:
    """Outcome horizons to compute per signal timeframe."""
    if signal_tf == "30m":
        return (15, 30, 60, 120, 240)
    return HORIZONS_MIN


def flatten_horizons_row(
    *,
    candidate_id: str,
    mode_id: str,
    timeframe: str,
    direction: str,
    entry_at: str,
    entry_price: float,
    horizons: dict[str, dict[str, Any]],
    extra: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for h, oc in horizons.items():
        row = {
            "candidate_id": candidate_id,
            "mode_id": mode_id,
            "timeframe": timeframe,
            "direction": direction,
            "entry_at": entry_at,
            "entry_price": entry_price,
            "horizon_min": int(h),
            "mfe_pct": oc.get("mfe_pct"),
            "mae_pct": oc.get("mae_pct"),
            "mfe_at": oc.get("mfe_at"),
            "mae_at": oc.get("mae_at"),
            "close_return_pct": oc.get("close_return_pct"),
            "mfe_minus_mae": oc.get("mfe_minus_mae"),
            "mfe_mae_ratio": oc.get("mfe_mae_ratio"),
            "first_extreme": oc.get("first_extreme"),
            "minutes_to_mfe": oc.get("minutes_to_mfe"),
            "minutes_to_mae": oc.get("minutes_to_mae"),
            "coverage": oc.get("coverage"),
        }
        for tp, sl in FIRST_HIT_PAIRS:
            row[f"pair_t{tp:.2f}_a{sl:.2f}"] = (oc.get("first_hit_pairs") or {}).get(f"t{tp:.2f}_a{sl:.2f}")
        if extra:
            row.update(extra)
        rows.append(row)
    return rows


def check_horizon_monotonicity(horizons: dict[str, dict[str, Any]], ordered: tuple[int, ...]) -> dict[str, Any]:
    """MFE and MAE running extrema must not decrease across longer horizons."""
    issues: list[str] = []
    prev_mfe = prev_mae = None
    vals: dict[str, Any] = {}
    for h in ordered:
        hs = str(h)
        oc = horizons.get(hs) or {}
        mfe, mae = oc.get("mfe_pct"), oc.get("mae_pct")
        vals[f"mfe_{h}"] = mfe
        vals[f"mae_{h}"] = mae
        if mfe is None or mae is None:
            issues.append(f"missing_h{h}")
            continue
        if prev_mfe is not None and float(mfe) + 1e-9 < float(prev_mfe):
            issues.append(f"mfe_decrease_{ordered[ordered.index(h)-1]}_to_{h}")
        if prev_mae is not None and float(mae) + 1e-9 < float(prev_mae):
            issues.append(f"mae_decrease_{ordered[ordered.index(h)-1]}_to_{h}")
        prev_mfe, prev_mae = float(mfe), float(mae)
    return {"monotonic_ok": len(issues) == 0, "issues": issues, **vals}


def horizon_progression_row(horizons: dict[str, dict[str, Any]], ordered: tuple[int, ...]) -> dict[str, Any]:
    """Per-candidate horizon evolution metrics."""
    mono = check_horizon_monotonicity(horizons, ordered)
    best_h, best_diff = None, None
    for h in ordered:
        oc = horizons.get(str(h)) or {}
        d = oc.get("mfe_minus_mae")
        if d is None:
            continue
        if best_diff is None or float(d) > float(best_diff):
            best_diff, best_h = float(d), h

    def _delta(a: int, b: int, key: str) -> float | None:
        va = (horizons.get(str(a)) or {}).get(key)
        vb = (horizons.get(str(b)) or {}).get(key)
        if va is None or vb is None:
            return None
        return round(float(vb) - float(va), 6)

    h30 = horizons.get("30") or horizons.get(str(ordered[0]))
    h240 = horizons.get("240") or horizons.get(str(ordered[-1]))
    early_mae = (h30 or {}).get("mae_pct")
    late_mfe = (h240 or {}).get("mfe_pct")
    overtaken = None
    if early_mae is not None and late_mfe is not None:
        overtaken = float(late_mfe) > float(early_mae)

    out: dict[str, Any] = {
        "best_horizon_mfe_minus_mae": best_h,
        "best_mfe_minus_mae": best_diff,
        "monotonic_ok": mono["monotonic_ok"],
        "monotonic_issues": mono.get("issues"),
        "early_mae_overtaken_by_late_mfe": overtaken,
    }
    if 30 in ordered and 60 in ordered:
        out["mfe_delta_30_to_60"] = _delta(30, 60, "mfe_pct")
        out["mae_delta_30_to_60"] = _delta(30, 60, "mae_pct")
    if 60 in ordered and 120 in ordered:
        out["mfe_delta_60_to_120"] = _delta(60, 120, "mfe_pct")
        out["mae_delta_60_to_120"] = _delta(60, 120, "mae_pct")
    if 120 in ordered and 240 in ordered:
        out["mfe_delta_120_to_240"] = _delta(120, 240, "mfe_pct")
        out["mae_delta_120_to_240"] = _delta(120, 240, "mae_pct")
    return out


def manual_recompute_check(
    candles_1m: pd.DataFrame,
    *,
    direction: str,
    entry_at: str,
    entry_price: float,
    horizon_min: int,
    stored: dict[str, Any],
) -> dict[str, Any]:
    """Recompute one horizon and compare to stored values."""
    oc = compute_mfe_mae_horizon(
        candles_1m,
        direction=direction,
        entry_at=entry_at,
        entry_price=float(entry_price),
        horizon_min=horizon_min,
    )
    entry_ts = datetime.fromisoformat(str(entry_at).replace("Z", "+00:00"))
    if entry_ts.tzinfo is None:
        entry_ts = entry_ts.replace(tzinfo=timezone.utc)
    end = entry_ts + timedelta(minutes=horizon_min)
    tcol = pd.to_datetime(candles_1m["open_time"])
    if getattr(tcol.dt, "tz", None) is not None:
        mask = (tcol >= pd.Timestamp(entry_ts)) & (tcol < pd.Timestamp(end))
    else:
        mask = (tcol >= pd.Timestamp(entry_ts.replace(tzinfo=None))) & (
            tcol < pd.Timestamp(end.replace(tzinfo=None))
        )
    path = candles_1m.loc[mask]
    match_mfe = stored.get("mfe_pct") is not None and oc.get("mfe_pct") is not None and abs(
        float(stored["mfe_pct"]) - float(oc["mfe_pct"])
    ) < 1e-4
    match_mae = stored.get("mae_pct") is not None and oc.get("mae_pct") is not None and abs(
        float(stored["mae_pct"]) - float(oc["mae_pct"])
    ) < 1e-4
    return {
        "horizon_min": horizon_min,
        "recomputed_mfe": oc.get("mfe_pct"),
        "stored_mfe": stored.get("mfe_pct"),
        "recomputed_mae": oc.get("mae_pct"),
        "stored_mae": oc.get("mae_pct"),
        "match_mfe": match_mfe,
        "match_mae": match_mae,
        "path_bars": len(path),
        "path_first": str(path.iloc[0]["open_time"]) if len(path) else None,
        "path_last": str(path.iloc[-1]["open_time"]) if len(path) else None,
        "entry_at": entry_at,
        "horizon_end": end.isoformat(),
    }


def audit_prior_export(
    prior_dir: str,
    candles_1m: pd.DataFrame,
) -> dict[str, Any]:
    """Audit xrp_shortlist_with_sources: horizons computed but not per-candidate CSV."""
    p = pd.read_csv(f"{prior_dir}/candidates_with_sources.csv")
    ml = pd.read_csv(f"{prior_dir}/mode_level_comparison.csv")
    horizons_present = sorted(ml["horizon_min"].unique().tolist())
    per_candidate_export = any(c.startswith("h") and "mfe_pct" in c for c in p.columns)

    mono_results = []
    manual_checks = []
    recomputed_rows = []

    # sample picks
    samples = []
    for tf, d in [("15m", "BULLISH"), ("15m", "BEARISH"), ("5m", "BULLISH"), ("5m", "BEARISH")]:
        sub = p[(p.timeframe == tf) & (p.direction == d)]
        if len(sub):
            samples.append(sub.iloc[0])

    for cand in p.itertuples():
        horizons = compute_all_horizons(
            candles_1m,
            direction=cand.direction,
            entry_at=cand.entry_at,
            entry_price=float(cand.entry_price),
        )
        ordered = tuple(int(x) for x in horizons.keys())
        mono_results.append(
            {
                "candidate_id": cand.candidate_id,
                "timeframe": cand.timeframe,
                **check_horizon_monotonicity(horizons, tuple(sorted(ordered))),
            }
        )
        recomputed_rows.extend(
            flatten_horizons_row(
                candidate_id=cand.candidate_id,
                mode_id=cand.mode_id,
                timeframe=cand.timeframe,
                direction=cand.direction,
                entry_at=cand.entry_at,
                entry_price=float(cand.entry_price),
                horizons=horizons,
            )
        )

    for s in samples:
        horizons = compute_all_horizons(
            candles_1m,
            direction=s.direction,
            entry_at=s.entry_at,
            entry_price=float(s.entry_price),
        )
        for h in (60, 120, 240):
            manual_checks.append(
                {
                    "candidate_id": s.candidate_id,
                    "timeframe": s.timeframe,
                    "direction": s.direction,
                    **manual_recompute_check(
                        candles_1m,
                        direction=s.direction,
                        entry_at=s.entry_at,
                        entry_price=float(s.entry_price),
                        horizon_min=h,
                        stored=horizons.get(str(h)) or {},
                    ),
                }
            )

    mono_df = pd.DataFrame(mono_results)
    return {
        "per_candidate_horizon_columns_in_prior_csv": per_candidate_export,
        "aggregated_horizons_in_mode_level_comparison": horizons_present,
        "all_horizons_computed_internally": horizons_present == [15, 30, 60, 120, 240],
        "finding": (
            "MFE/MAE for 15/30/60/120/240 min were computed in-runner but only exported as "
            "aggregates in mode_level_comparison.csv and source_filtered_mfe_mae.csv; "
            "candidates_with_sources.csv lacked per-candidate horizon columns."
        ),
        "n_candidates": len(p),
        "monotonicity_failures": int((~mono_df["monotonic_ok"]).sum()) if len(mono_df) else 0,
        "manual_checks": manual_checks,
        "manual_all_match": all(c.get("match_mfe") and c.get("match_mae") for c in manual_checks),
        "recomputed_sample_stats": {
            "n_rows": len(recomputed_rows),
            "horizons": horizons_present,
        },
    }
