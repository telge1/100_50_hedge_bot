"""Descriptive analysis for v2 enriched reference trades (research-only; no strategy change)."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from . import constants as C


def _ci_wilson(wins: int, n: int, z: float = 1.96) -> tuple[float | None, float | None]:
    if n <= 0:
        return None, None
    p = wins / n
    denom = 1 + z**2 / n
    centre = p + z**2 / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2))
    return (centre - margin) / denom, (centre + margin) / denom


def _group_metrics(g: pd.DataFrame) -> dict[str, Any]:
    def _num(col: str) -> pd.Series:
        if col not in g.columns:
            return pd.Series(dtype=float)
        s = pd.to_numeric(g[col], errors="coerce")
        if isinstance(s, pd.Series):
            return s
        return pd.Series([s], dtype=float)

    pnl = _num("label__net_pnl_usdt")
    gross = _num("label__gross_pnl_usdt")
    mfe = _num("label__mfe_pct")
    mae = _num("label__mae_pct")
    oc = g["label__outcome_class"] if "label__outcome_class" in g.columns else None
    n = int(len(g))
    n_win = int((oc == "WIN").sum()) if oc is not None else int((pnl > 0).sum())
    n_loss = int((oc == "LOSS").sum()) if oc is not None else int((pnl <= 0).sum())
    n_inc = int((oc == "INCOMPLETE").sum()) if oc is not None else 0
    wins_sum = float(pnl[pnl > 0].sum()) if n and pnl.notna().any() else 0.0
    loss_sum = float((-pnl[pnl < 0]).sum()) if n and pnl.notna().any() else 0.0
    pf = (wins_sum / loss_sum) if loss_sum > 0 else (None if wins_sum == 0 else float("inf"))
    lo, hi = _ci_wilson(n_win, n_win + n_loss) if (n_win + n_loss) > 0 else (None, None)
    feat_cov_cols = [c for c in g.columns if c.startswith(C.FEATURE_PREFIX) and c.endswith("__coverage_status")]
    if feat_cov_cols:
        ok_share = float(np.mean([(g[c] == "OK").mean() for c in feat_cov_cols]))
    else:
        ok_share = None
    return {
        "n_trades": n,
        "n_win": n_win,
        "n_loss": n_loss,
        "n_incomplete": n_inc,
        "winrate": (n_win / (n_win + n_loss)) if (n_win + n_loss) else None,
        "gross_pnl_usdt": float(gross.sum()) if gross.notna().any() else None,
        "net_pnl_usdt": float(pnl.sum()) if pnl.notna().any() else None,
        "mean_pnl_usdt": float(pnl.mean()) if pnl.notna().any() else None,
        "median_pnl_usdt": float(pnl.median()) if pnl.notna().any() else None,
        "mean_mfe_pct": float(mfe.mean()) if mfe.notna().any() else None,
        "mean_mae_pct": float(mae.mean()) if mae.notna().any() else None,
        "profit_factor": pf,
        "winrate_ci95_low": lo,
        "winrate_ci95_high": hi,
        "feature_ok_share": ok_share,
        "small_sample": n < 20,
    }


def _breakdown(df: pd.DataFrame, col: str) -> list[dict[str, Any]]:
    if col not in df.columns:
        return [{"group_col": col, "group": None, "error": "missing_column", **_group_metrics(df.iloc[0:0])}]
    rows = []
    for key, g in df.groupby(df[col].astype(str), dropna=False):
        rows.append({"group_col": col, "group": key, **_group_metrics(g)})
    return rows


def _feature_win_loss(df: pd.DataFrame, feature_cols: list[str]) -> list[dict[str, Any]]:
    out = []
    win = df[df["label__outcome_class"] == "WIN"]
    loss = df[df["label__outcome_class"] == "LOSS"]
    for col in feature_cols:
        if col not in df.columns:
            continue
        wv = pd.to_numeric(win[col], errors="coerce")
        lv = pd.to_numeric(loss[col], errors="coerce")
        if wv.notna().sum() < 5 or lv.notna().sum() < 5:
            continue
        # Cliffs delta approx via median standardized difference
        pooled = pd.concat([wv.dropna(), lv.dropna()])
        if pooled.std() == 0 or pooled.isna().all():
            continue
        # simple effect: mean difference / pooled std
        effect = (float(wv.mean()) - float(lv.mean())) / float(pooled.std())
        out.append(
            {
                "feature": col,
                "n_win": int(wv.notna().sum()),
                "n_loss": int(lv.notna().sum()),
                "mean_win": float(wv.mean()),
                "mean_loss": float(lv.mean()),
                "median_win": float(wv.median()),
                "median_loss": float(lv.median()),
                "effect_size_mean_diff_over_std": effect,
                "missing_share": float(df[col].isna().mean()),
                "note": "Descriptive only; not a strategy threshold. Multiple-testing risk applies.",
            }
        )
    out.sort(key=lambda r: abs(r["effect_size_mean_diff_over_std"]), reverse=True)
    return out


def run_v2_analysis(df: pd.DataFrame) -> dict[str, Any]:
    df = df.copy()
    if "label__outcome_class" not in df.columns:
        pnl = pd.to_numeric(df.get("label__net_pnl_usdt"), errors="coerce")
        df["label__outcome_class"] = np.where(pnl > 0, "WIN", "LOSS")
    if "symbol" not in df.columns:
        df["symbol"] = "UNKNOWN"

    df["is_xrp"] = df["symbol"].astype(str).str.upper().eq("XRPUSDT")
    if "feature__direction" in df.columns:
        df["direction_norm"] = df["feature__direction"].astype(str).str.upper()
    else:
        df["direction_norm"] = "UNKNOWN"
    if "feature__coverage_segment" in df.columns:
        df["full_multisource"] = df["feature__coverage_segment"].astype(str).eq("FULL_MULTISOURCE")
    else:
        df["full_multisource"] = False

    atr = pd.to_numeric(df.get("feature__atr14_pct"), errors="coerce") if "feature__atr14_pct" in df.columns else pd.Series(dtype=float)
    ret4 = pd.to_numeric(df.get("feature__return_4h_pct"), errors="coerce") if "feature__return_4h_pct" in df.columns else pd.Series(dtype=float)
    if atr.notna().sum() >= 3:
        df["vol_regime"] = pd.cut(
            atr, bins=[-np.inf, atr.quantile(0.33), atr.quantile(0.66), np.inf], labels=["LOW", "MID", "HIGH"]
        )
    else:
        df["vol_regime"] = "UNKNOWN"
    if ret4.notna().sum() >= 3:
        df["trend_regime"] = pd.cut(
            ret4.abs(),
            bins=[-np.inf, ret4.abs().quantile(0.33), ret4.abs().quantile(0.66), np.inf],
            labels=["RANGE", "MILD", "TREND"],
        )
    else:
        df["trend_regime"] = "UNKNOWN"

    coin = []
    for sym, g in df.groupby("symbol"):
        m = _group_metrics(g)
        coin.append({"symbol": sym, "min_n_for_robust": 20, **m})
    coin.sort(key=lambda r: (r["net_pnl_usdt"] is not None, r["net_pnl_usdt"] or -1e18), reverse=True)

    feature_cols = [
        c
        for c in df.columns
        if c.startswith(C.FEATURE_PREFIX)
        and not c.endswith(("__coverage_status", "__missing_reason", "__causal", "__feature_asof", "__source_table"))
        and pd.api.types.is_numeric_dtype(df[c])
    ][:80]

    full = df[df["full_multisource"]]
    limited = df[~df["full_multisource"]]
    summary = {
        "overall": _group_metrics(df),
        "win_vs_loss": {
            "WIN": _group_metrics(df[df["label__outcome_class"] == "WIN"]),
            "LOSS": _group_metrics(df[df["label__outcome_class"] == "LOSS"]),
        },
        "xrp_vs_rest": {
            "XRPUSDT": _group_metrics(df[df["is_xrp"]]),
            "NON_XRP": _group_metrics(df[~df["is_xrp"]]),
        },
        "full_multisource_effect": {
            "FULL_MULTISOURCE": _group_metrics(full),
            "LIMITED": _group_metrics(limited),
            "n_full_rows": int(len(full)),
            "note": (
                "v2 pad alignment produced more FULL_MULTISOURCE segments; "
                "compare descriptively only — not a causal claim."
            ),
        },
        "multiple_testing_warning": (
            "Many group/feature comparisons are exploratory; "
            "do not treat in-sample differences as validated filters."
        ),
        "robustness": {
            "n_coins_with_n_ge_20": sum(1 for r in coin if r["n_trades"] >= 20),
            "n_coins_total": len(coin),
            "do_not_claim_robust_if_small_sample": True,
        },
    }
    return {
        "summary": summary,
        "coin_breakdown": coin,
        "regime_breakdown": _breakdown(df, "vol_regime")
        + _breakdown(df, "trend_regime")
        + _breakdown(df, "feature__session_bucket")
        + _breakdown(df, "direction_norm")
        + _breakdown(df, "full_multisource"),
        "feature_comparison": _feature_win_loss(df, feature_cols),
        "group_tables": {
            "outcome": _breakdown(df, "label__outcome_class"),
            "mode": _breakdown(df, "label__mode_id"),
            "coverage_segment": _breakdown(df, "feature__coverage_segment"),
        },
    }


def analysis_report_md(analysis: dict[str, Any]) -> str:
    s = analysis["summary"]
    lines = [
        "# Multicoin reference enrichment v2 — analysis",
        "",
        "## Overall",
        f"- n={s['overall']['n_trades']} winrate={s['overall']['winrate']} "
        f"net_pnl={s['overall']['net_pnl_usdt']} PF={s['overall']['profit_factor']}",
        f"- small_sample_flag={s['overall']['small_sample']}",
        "",
        "## XRP vs rest",
        f"- XRP: {s['xrp_vs_rest']['XRPUSDT']}",
        f"- NON_XRP: {s['xrp_vs_rest']['NON_XRP']}",
        "",
        "## FULL_MULTISOURCE",
        f"- {s['full_multisource_effect']}",
        "",
        f"## Warnings",
        f"- {s['multiple_testing_warning']}",
        f"- Robust coins (n≥20): {s['robustness']['n_coins_with_n_ge_20']}/{s['robustness']['n_coins_total']}",
        "",
        "No in-sample filter is recommended as a finished strategy rule.",
        "",
    ]
    return "\n".join(lines)
