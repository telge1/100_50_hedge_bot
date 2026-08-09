"""Orchestrate BE50 drawdown distribution audit (existing equity only)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_wave_fade_be50_drawdown_audit import (
    AUDIT_VERSION,
    COMPARE_THRESHOLDS,
    EQUITY_CSV,
    EXPECTED_BE50_MAX_DD,
    MAX_DD_TOL,
    OUT_DIR_DEFAULT,
    REF_DIR,
    START_EQUITY,
    THRESHOLDS,
    TRADES_CSV,
)
from orderbook_analyse.fractal_wave_fade_be50_drawdown_audit.episodes import extract_episodes


def _load_frames() -> tuple[pd.DataFrame, pd.DataFrame]:
    eq = pd.read_csv(EQUITY_CSV)
    tr = pd.read_csv(TRADES_CSV)
    for c in ("entry_time", "exit_time_baseline", "be50_exit_time"):
        if c in tr.columns:
            tr[c] = pd.to_datetime(tr[c], utc=True)
    eq = eq.sort_values("seq").reset_index(drop=True)
    tr = tr.sort_values("seq").reset_index(drop=True)
    if len(eq) != len(tr) or not (eq["trade_id"].to_numpy() == tr["trade_id"].to_numpy()).all():
        raise RuntimeError("equity_comparison and full_trade_comparison trade_id/seq mismatch")
    return eq, tr


def _verify_max_dd(equity: np.ndarray, label: str, expected: float | None = None) -> dict[str, Any]:
    path = np.concatenate([[START_EQUITY], equity.astype(float)])
    peak = np.maximum.accumulate(path)
    dd = np.where(peak > 0, (path / peak - 1.0) * 100.0, 0.0)
    mx = float(dd[1:].min()) if len(equity) else 0.0
    ok = True
    reason = None
    if expected is not None and abs(mx - expected) > MAX_DD_TOL:
        ok = False
        reason = f"{label} max_dd={mx} expected≈{expected}"
    return {"ok": ok, "max_dd_pct": mx, "reason": reason, "label": label}


def _threshold_counts(eps: pd.DataFrame, thresholds: list[float]) -> pd.DataFrame:
    n = max(len(eps), 1)
    rows = []
    for thr in thresholds:
        # depths are negative; >= thr% means abs depth >= thr i.e. max_drawdown_pct <= -thr
        cnt = int((eps["max_drawdown_pct"] <= -thr).sum()) if len(eps) else 0
        rows.append(
            {
                "dd_threshold_pct": thr,
                "n_episodes": cnt,
                "share_of_all_episodes": cnt / n if len(eps) else 0.0,
            }
        )
    return pd.DataFrame(rows)


def _spacing(eps: pd.DataFrame, thr: float) -> pd.DataFrame:
    sub = eps[eps["max_drawdown_pct"] <= -thr].sort_values("trough_time").reset_index(drop=True)
    if sub.empty:
        return pd.DataFrame()
    rows = []
    for i, r in sub.iterrows():
        prev_gap = next_gap = None
        if i > 0:
            prev_gap = (
                pd.Timestamp(r["trough_time"]) - pd.Timestamp(sub.loc[i - 1, "trough_time"])
            ).total_seconds() / 86400.0
        if i < len(sub) - 1:
            next_gap = (
                pd.Timestamp(sub.loc[i + 1, "trough_time"]) - pd.Timestamp(r["trough_time"])
            ).total_seconds() / 86400.0
        rows.append(
            {
                "threshold_pct": thr,
                "episode_id": int(r["episode_id"]),
                "peak_time": r["peak_time"],
                "trough_time": r["trough_time"],
                "recovery_time": r["recovery_time"],
                "max_drawdown_pct": float(r["max_drawdown_pct"]),
                "days_since_prev_ge_thr": prev_gap,
                "days_until_next_ge_thr": next_gap,
            }
        )
    return pd.DataFrame(rows)


def _spacing_stats(spacing: pd.DataFrame) -> dict[str, Any]:
    gaps = spacing["days_since_prev_ge_thr"].dropna().astype(float)
    if gaps.empty:
        return {
            "n": int(len(spacing)),
            "mean_gap_days": None,
            "median_gap_days": None,
            "min_gap_days": None,
            "max_gap_days": None,
        }
    return {
        "n": int(len(spacing)),
        "mean_gap_days": float(gaps.mean()),
        "median_gap_days": float(gaps.median()),
        "min_gap_days": float(gaps.min()),
        "max_gap_days": float(gaps.max()),
    }


def _calendar_tables(eps: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    if eps.empty:
        return pd.DataFrame(), pd.DataFrame()
    d = eps.copy()
    d["trough_month"] = pd.to_datetime(d["trough_time"], utc=True).dt.strftime("%Y-%m")
    d["trough_year"] = pd.to_datetime(d["trough_time"], utc=True).dt.strftime("%Y")

    def agg(g: pd.DataFrame) -> dict[str, Any]:
        return {
            "worst_dd": float(g["max_drawdown_pct"].min()),
            "n_ge_5": int((g["max_drawdown_pct"] <= -5).sum()),
            "n_ge_10": int((g["max_drawdown_pct"] <= -10).sum()),
            "n_ge_12": int((g["max_drawdown_pct"] <= -12).sum()),
            "n_ge_14": int((g["max_drawdown_pct"] <= -14).sum()),
            "n_episodes": int(len(g)),
        }

    monthly = (
        d.groupby("trough_month", sort=True)
        .apply(lambda g: pd.Series(agg(g)), include_groups=False)
        .reset_index()
    )
    yearly = (
        d.groupby("trough_year", sort=True)
        .apply(lambda g: pd.Series(agg(g)), include_groups=False)
        .reset_index()
    )
    return monthly, yearly


def _duration_block(eps: pd.DataFrame) -> dict[str, Any]:
    def med(series):
        s = series.dropna().astype(float)
        return None if s.empty else float(s.median())

    def block(sub: pd.DataFrame) -> dict[str, Any]:
        return {
            "n": int(len(sub)),
            "median_peak_to_trough_hours": med(sub["duration_to_trough_hours"]),
            "median_trough_to_recovery_hours": med(sub["duration_trough_to_recovery_hours"]),
            "median_full_duration_hours": med(sub["duration_to_recovery_hours"]),
            "max_recovery_hours": None
            if sub["duration_trough_to_recovery_hours"].dropna().empty
            else float(sub["duration_trough_to_recovery_hours"].dropna().max()),
            "median_trades_to_trough": med(sub["trades_to_trough"]),
            "median_trades_to_recovery": med(sub["trades_to_recovery"]),
            "max_trades_to_recovery": None
            if sub["trades_to_recovery"].dropna().empty
            else float(sub["trades_to_recovery"].dropna().max()),
        }

    out = {"all": block(eps)}
    for thr in (5.0, 10.0, 12.0):
        out[f"ge_{thr:g}"] = block(eps[eps["max_drawdown_pct"] <= -thr])
    return out


def _classify(sorted_depths: np.ndarray, thr_counts: dict[float, int]) -> str:
    """sorted_depths ascending (most negative first)."""
    if len(sorted_depths) == 0:
        return "DRAWDOWN_BASELINE_MISMATCH"
    mx = float(sorted_depths[0])
    second = float(sorted_depths[1]) if len(sorted_depths) > 1 else 0.0
    n10 = thr_counts.get(10.0, 0)
    n12 = thr_counts.get(12.0, 0)
    n14 = thr_counts.get(14.0, 0)
    n15 = thr_counts.get(15.0, 0)

    # Recurring large DDs in the 10–15% band (research question focus)
    if n10 >= 5 or n12 >= 3 or n14 >= 3:
        return "LARGE_DRAWDOWNS_ARE_RECURRING"
    # Max itself rare, but another deep DD exists nearby
    if n15 <= 1 and abs(second) >= 10.0 and (n10 >= 2 or n12 >= 2):
        return "MAX_DD_IS_RARE_BUT_NOT_UNIQUE"
    if n10 >= 2 and abs(mx) - abs(second) < 3.0:
        return "MAX_DD_IS_RARE_BUT_NOT_UNIQUE"
    # Isolated extreme
    if n15 <= 1 and n14 <= 1 and n12 <= 1 and abs(mx) >= abs(second) + 3.0:
        return "MAX_DD_IS_CLEAR_OUTLIER"
    if n10 <= 1:
        return "MAX_DD_IS_CLEAR_OUTLIER"
    return "MAX_DD_IS_RARE_BUT_NOT_UNIQUE"


def run_analysis(*, out_dir: Path = OUT_DIR_DEFAULT) -> dict[str, Any]:
    eq, tr = _load_frames()
    be_eq = eq["be50_total"].astype(float).to_numpy()
    base_eq = eq["baseline_total"].astype(float).to_numpy()

    be_check = _verify_max_dd(be_eq, "BE50", EXPECTED_BE50_MAX_DD)
    base_check = _verify_max_dd(base_eq, "Baseline", None)
    if not be_check["ok"]:
        return {
            "decision": "DRAWDOWN_BASELINE_MISMATCH",
            "mismatch": be_check,
            "out_dir": out_dir,
            "audit_version": AUDIT_VERSION,
        }

    be_times = list(pd.to_datetime(tr["be50_exit_time"], utc=True))
    base_times = list(pd.to_datetime(tr["exit_time_baseline"], utc=True))
    be_reasons = list(eq["be50_reason"].astype(str))
    base_reasons = list(eq["baseline_reason"].astype(str))
    trade_ids = list(eq["trade_id"].astype(int))

    be_eps = extract_episodes(
        be_eq,
        times=be_times,
        reasons=be_reasons,
        trade_ids=trade_ids,
        start_equity=START_EQUITY,
    )
    base_eps = extract_episodes(
        base_eq,
        times=base_times,
        reasons=base_reasons,
        trade_ids=trade_ids,
        start_equity=START_EQUITY,
    )

    # sanity: episode max should match series max
    if len(be_eps) and abs(float(be_eps["max_drawdown_pct"].min()) - be_check["max_dd_pct"]) > 1e-6:
        return {
            "decision": "DRAWDOWN_BASELINE_MISMATCH",
            "mismatch": {
                "ok": False,
                "reason": (
                    f"episode min DD {be_eps['max_drawdown_pct'].min()} "
                    f"!= series max DD {be_check['max_dd_pct']}"
                ),
            },
            "out_dir": out_dir,
            "audit_version": AUDIT_VERSION,
        }

    be_thr = _threshold_counts(be_eps, THRESHOLDS)
    base_thr = _threshold_counts(base_eps, COMPARE_THRESHOLDS)
    be_thr_map = {float(r.dd_threshold_pct): int(r.n_episodes) for r in be_thr.itertuples()}
    base_thr_map = {float(r.dd_threshold_pct): int(r.n_episodes) for r in base_thr.itertuples()}

    # also count compare thresholds for BE50
    be_cmp = _threshold_counts(be_eps, COMPARE_THRESHOLDS)
    be_cmp_map = {float(r.dd_threshold_pct): int(r.n_episodes) for r in be_cmp.itertuples()}

    depths = np.sort(be_eps["max_drawdown_pct"].to_numpy()) if len(be_eps) else np.array([])
    top10 = be_eps.sort_values("max_drawdown_pct").head(10).copy()
    top10.insert(0, "rank", np.arange(1, len(top10) + 1))

    p90 = float(np.percentile(depths, 10)) if len(depths) else None  # 10th pct = deep tail (negative)
    # For negative DD, lower percentile = more severe. User asked p90/p95/p99 of drawdown episodes
    # typically meaning severity percentiles: p90 = 90th percentile of |DD| as negative value
    # Interpret as percentiles of the (negative) max_drawdown_pct distribution:
    # p90 = np.percentile(depths, 10) because depths sorted ascending... 
    # Standard: percentile of the signed DD values: p10 is near worst, p90 is near shallow.
    # User wants: p90_dd, p95_dd, p99_dd as severity → use abs then negate:
    abs_d = np.abs(depths) if len(depths) else np.array([0.0])
    p90_dd = -float(np.percentile(abs_d, 90)) if len(depths) else None
    p95_dd = -float(np.percentile(abs_d, 95)) if len(depths) else None
    p99_dd = -float(np.percentile(abs_d, 99)) if len(depths) else None

    second = float(depths[1]) if len(depths) > 1 else None
    third = float(depths[2]) if len(depths) > 2 else None
    top10_median = float(np.median(top10["max_drawdown_pct"])) if len(top10) else None

    decision = _classify(depths, be_thr_map)

    spacing_frames = []
    spacing_stats = {}
    for thr in (10.0, 12.0, 14.0):
        sp = _spacing(be_eps, thr)
        if len(sp):
            sp = sp.copy()
            sp["threshold_label"] = f">={thr:g}%"
            spacing_frames.append(sp)
        spacing_stats[f"ge_{thr:g}"] = _spacing_stats(sp)
    if spacing_frames:
        large_spacing = pd.concat(spacing_frames, ignore_index=True, sort=False)
    else:
        large_spacing = pd.DataFrame()

    monthly, yearly = _calendar_tables(be_eps)
    dur = _duration_block(be_eps)

    # baseline vs be50 comparison table
    cmp_rows = []
    for thr in COMPARE_THRESHOLDS:
        cmp_rows.append(
            {
                "dd_threshold_pct": thr,
                "baseline_n": base_thr_map.get(thr, 0),
                "be50_n": be_cmp_map.get(thr, 0),
                "delta_n": be_cmp_map.get(thr, 0) - base_thr_map.get(thr, 0),
            }
        )
    base_depths = np.sort(base_eps["max_drawdown_pct"].to_numpy()) if len(base_eps) else np.array([])
    base_abs = np.abs(base_depths) if len(base_depths) else np.array([0.0])
    cmp_summary = {
        "baseline_max_dd": float(base_depths[0]) if len(base_depths) else None,
        "baseline_2nd": float(base_depths[1]) if len(base_depths) > 1 else None,
        "baseline_3rd": float(base_depths[2]) if len(base_depths) > 2 else None,
        "baseline_p95_dd": -float(np.percentile(base_abs, 95)) if len(base_depths) else None,
        "baseline_median_duration_hours": float(base_eps["duration_to_recovery_hours"].median())
        if len(base_eps) and base_eps["duration_to_recovery_hours"].notna().any()
        else None,
        "baseline_n_ge_10": base_thr_map.get(10.0, 0),
        "be50_max_dd": float(depths[0]) if len(depths) else None,
        "be50_2nd": second,
        "be50_3rd": third,
        "be50_p95_dd": p95_dd,
        "be50_median_duration_hours": float(be_eps["duration_to_recovery_hours"].median())
        if len(be_eps) and be_eps["duration_to_recovery_hours"].notna().any()
        else None,
        "be50_n_ge_10": be_thr_map.get(10.0, 0),
    }
    baseline_vs = pd.DataFrame(cmp_rows)

    # linear leverage sensitivity
    refs = {
        "worst_dd": float(depths[0]) if len(depths) else None,
        "second_largest_dd": second,
        "p95_dd": p95_dd,
        "typical_ge10_dd": float(
            be_eps.loc[be_eps["max_drawdown_pct"] <= -10, "max_drawdown_pct"].median()
        )
        if (be_eps["max_drawdown_pct"] <= -10).any()
        else None,
    }
    lev_rows = []
    for name, val in refs.items():
        row = {"metric": name, "historical_dd_pct": val, "note": "LINEAR_RISK_APPROXIMATION_ONLY"}
        for lev in (2, 3, 4, 5):
            row[f"x{lev}"] = None if val is None else float(val) * lev
        lev_rows.append(row)
    leverage = pd.DataFrame(lev_rows)

    # SL-series link for >=10%
    ge10 = be_eps[be_eps["max_drawdown_pct"] <= -10].copy()
    sl_link = {
        "n_ge10": int(len(ge10)),
        "median_longest_true_sl": float(ge10["longest_true_sl_streak"].median()) if len(ge10) else None,
        "median_longest_non_winner": float(ge10["longest_non_winner_streak"].median())
        if len(ge10)
        else None,
        "mean_n_sl": float(ge10["n_sl"].mean()) if len(ge10) else None,
        "mean_n_be": float(ge10["n_be"].mean()) if len(ge10) else None,
        "mean_n_tp": float(ge10["n_tp"].mean()) if len(ge10) else None,
        "share_non_winner_gt_true_sl": float(
            (ge10["longest_non_winner_streak"] > ge10["longest_true_sl_streak"]).mean()
        )
        if len(ge10)
        else None,
    }

    months_ge10 = int((monthly["n_ge_10"] > 0).sum()) if len(monthly) else 0

    payload = {
        "audit_version": AUDIT_VERSION,
        "ref_dir": str(REF_DIR),
        "out_dir": out_dir,
        "decision": decision,
        "be50_max_check": be_check,
        "baseline_max_check": base_check,
        "n_episodes_be50": int(len(be_eps)),
        "n_episodes_baseline": int(len(base_eps)),
        "be50_episodes": be_eps.sort_values("max_drawdown_pct"),
        "baseline_episodes": base_eps.sort_values("max_drawdown_pct"),
        "threshold_counts": be_thr,
        "top10": top10,
        "monthly": monthly,
        "yearly": yearly,
        "large_spacing": large_spacing,
        "spacing_stats": spacing_stats,
        "duration": dur,
        "baseline_vs_be50": baseline_vs,
        "cmp_summary": cmp_summary,
        "leverage": leverage,
        "sl_link": sl_link,
        "months_with_ge10": months_ge10,
        "stats": {
            "max_dd": float(depths[0]) if len(depths) else None,
            "second_largest_dd": second,
            "third_largest_dd": third,
            "median_top10_dd": top10_median,
            "p90_dd": p90_dd,
            "p95_dd": p95_dd,
            "p99_dd": p99_dd,
            "n_ge_10": be_thr_map.get(10.0, 0),
            "n_ge_12": be_thr_map.get(12.0, 0),
            "n_ge_13": be_thr_map.get(13.0, 0),
            "n_ge_14": be_thr_map.get(14.0, 0),
            "n_ge_15": be_thr_map.get(15.0, 0),
            "median_ge10_recovery_hours": dur.get("ge_10", {}).get("median_trough_to_recovery_hours"),
            "median_ge10_full_duration_hours": dur.get("ge_10", {}).get("median_full_duration_hours"),
            "median_ge10_trades_to_recovery": dur.get("ge_10", {}).get("median_trades_to_recovery"),
        },
    }
    return payload
