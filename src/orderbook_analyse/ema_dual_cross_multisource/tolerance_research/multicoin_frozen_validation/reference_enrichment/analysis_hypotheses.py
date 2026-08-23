"""Preregistered H1–H4 analysis (read enriched files only; no market DB).

Do not run automatically from --enrich. Invoked only via --analyze / helpers.
Quartile edges are computed once globally from the research sample (features only).
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from ..diagnostics_analysis import cliffs_delta


CONFIRMING_LIKE = frozenset({"CONFIRMING", "SUPPORTING", "STRONGLY_CONFIRMING"})


def global_quartile_edges(series: pd.Series) -> dict[str, float]:
    """Quartile edges from feature values only (no outcome optimization)."""
    s = pd.to_numeric(series, errors="coerce").dropna()
    if s.empty:
        return {}
    q = s.quantile([0.25, 0.5, 0.75]).to_dict()
    return {"q25": float(q[0.25]), "q50": float(q[0.5]), "q75": float(q[0.75])}


def assign_quartile(value: float | None, edges: dict[str, float]) -> str | None:
    if value is None or not edges or any(k not in edges for k in ("q25", "q50", "q75")):
        return None
    if math.isnan(float(value)):
        return None
    v = float(value)
    if v <= edges["q25"]:
        return "Q1"
    if v <= edges["q50"]:
        return "Q2"
    if v <= edges["q75"]:
        return "Q3"
    return "Q4"


def _metrics(df: pd.DataFrame) -> dict[str, Any]:
    if df is None or df.empty:
        return {
            "n_trades": 0,
            "n_coins": 0,
            "net_pnl_usdt": None,
            "expectancy_usdt": None,
            "net_winrate": None,
            "profit_factor_net": None,
            "tp_count": 0,
            "sl_count": 0,
            "time_count": 0,
        }
    pnl = pd.to_numeric(df["label__net_pnl_usdt"], errors="coerce")
    wins = pnl > 0
    losses = pnl < 0
    gp = float(pnl[wins].sum()) if wins.any() else 0.0
    gl = float((-pnl[losses]).sum()) if losses.any() else 0.0
    pf = (gp / gl) if gl > 0 else (None if gp == 0 else float("inf"))
    return {
        "n_trades": int(len(df)),
        "n_coins": int(df["symbol"].nunique()) if "symbol" in df.columns else None,
        "net_pnl_usdt": float(pnl.sum()),
        "expectancy_usdt": float(pnl.mean()),
        "net_winrate": float(wins.mean()),
        "profit_factor_net": pf,
        "tp_count": int(pd.to_numeric(df.get("label__tp_exit"), errors="coerce").fillna(0).sum()),
        "sl_count": int(pd.to_numeric(df.get("label__sl_exit"), errors="coerce").fillna(0).sum()),
        "time_count": int(pd.to_numeric(df.get("label__time_exit"), errors="coerce").fillna(0).sum()),
    }


def bootstrap_ci(values: list[float], *, n_boot: int = 1000, seed: int = 42) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    rng = np.random.default_rng(seed)
    arr = np.asarray(values, dtype=float)
    means = [float(rng.choice(arr, size=len(arr), replace=True).mean()) for _ in range(n_boot)]
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def _stability_slices(df: pd.DataFrame) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if df.empty:
        return out
    out["long"] = _metrics(
        df[df["feature__direction"].astype(str).str.upper().isin({"LONG", "BULLISH", "BUY"})]
    )
    out["short"] = _metrics(
        df[df["feature__direction"].astype(str).str.upper().isin({"SHORT", "BEARISH", "SELL"})]
    )
    # half by decision_at
    ts = pd.to_datetime(df["feature__decision_at"], utc=True, errors="coerce")
    mid = ts.quantile(0.5)
    out["first_half"] = _metrics(df[ts <= mid])
    out["second_half"] = _metrics(df[ts > mid])
    # drop best trade per coin
    pnl = pd.to_numeric(df["label__net_pnl_usdt"], errors="coerce")
    idx_best = pnl.groupby(df["symbol"]).idxmax()
    out["without_best_per_coin"] = _metrics(df.drop(index=idx_best, errors="ignore"))
    # equal-weight coin
    coin_exp = pnl.groupby(df["symbol"]).mean()
    out["equal_weight_coin_expectancy"] = float(coin_exp.mean()) if len(coin_exp) else None
    out["per_coin"] = {str(k): float(v) for k, v in coin_exp.items()}
    return out


def analyze_h1_orderbook(df: pd.DataFrame) -> list[dict[str, Any]]:
    """H1: non-neutral trade-confirming OB vs neutral OB within SUPPORTIVE sample."""
    rows = []
    verd = df.get("feature__existing_orderbook_verdict")
    if verd is None:
        return rows
    confirming = df[verd.astype(str).str.upper().isin(CONFIRMING_LIKE)]
    # directional imbalance non-null and |x|>0 as non-neutral numeric confirmation proxy
    imb = pd.to_numeric(df.get("feature__ob_imbalance_directional"), errors="coerce")
    non_neutral = df[(imb.notna()) & (imb != 0) & (imb > 0)]
    neutral = df[verd.astype(str).str.upper().isin({"NEUTRAL", "INCONCLUSIVE_DATA"}) | imb.isna()]
    for name, sub in (("confirming_like_verdict", confirming), ("directional_ob_positive", non_neutral), ("neutral_or_missing", neutral)):
        m = _metrics(sub)
        feat_vals = pd.to_numeric(sub.get("feature__ob_imbalance_directional"), errors="coerce").dropna().tolist()
        other = pd.to_numeric(neutral.get("feature__ob_imbalance_directional"), errors="coerce").dropna().tolist() if name != "neutral_or_missing" else []
        rows.append(
            {
                "hypothesis": "H1",
                "slice": name,
                **m,
                "mean_feature": float(np.mean(feat_vals)) if feat_vals else None,
                "median_feature": float(np.median(feat_vals)) if feat_vals else None,
                "cliffs_delta_vs_neutral": cliffs_delta(feat_vals, other) if other and feat_vals else None,
                "bootstrap_ci_low": bootstrap_ci(pd.to_numeric(sub["label__net_pnl_usdt"], errors="coerce").dropna().tolist())[0],
                "bootstrap_ci_high": bootstrap_ci(pd.to_numeric(sub["label__net_pnl_usdt"], errors="coerce").dropna().tolist())[1],
                "missing_share": float(imb.isna().mean()) if len(df) else None,
                **{f"stability__{k}": v for k, v in _stability_slices(sub).items() if not isinstance(v, dict)},
            }
        )
    return rows


def analyze_h2_atr_quartiles(df: pd.DataFrame) -> list[dict[str, Any]]:
    rows = []
    for col in ("feature__atr14_pct", "feature__tp_atr_ratio", "feature__sl_atr_ratio"):
        edges = global_quartile_edges(df[col]) if col in df.columns else {}
        qcol = f"{col}__quartile"
        if col not in df.columns or not edges:
            rows.append({"hypothesis": "H2", "feature": col, "quartile": None, "edges": edges, **_metrics(pd.DataFrame())})
            continue
        assigned = df[col].map(lambda v, e=edges: assign_quartile(None if pd.isna(v) else float(v), e))
        tmp = df.copy()
        tmp[qcol] = assigned
        for q in ("Q1", "Q2", "Q3", "Q4"):
            sub = tmp[tmp[qcol] == q]
            feat_vals = pd.to_numeric(sub[col], errors="coerce").dropna().tolist()
            rows.append(
                {
                    "hypothesis": "H2",
                    "feature": col,
                    "quartile": q,
                    "edges": edges,
                    **_metrics(sub),
                    "mean_feature": float(np.mean(feat_vals)) if feat_vals else None,
                    "median_feature": float(np.median(feat_vals)) if feat_vals else None,
                    "missing_share": float(pd.to_numeric(df[col], errors="coerce").isna().mean()),
                }
            )
    return rows


def analyze_h3_ema_structure(df: pd.DataFrame) -> list[dict[str, Any]]:
    rows = []
    profitable = df[pd.to_numeric(df["label__net_pnl_usdt"], errors="coerce") > 0]
    unprofitable = df[pd.to_numeric(df["label__net_pnl_usdt"], errors="coerce") <= 0]
    for col in ("feature__ema59_slope_atr", "feature__ema9_20_distance_atr"):
        x = pd.to_numeric(profitable.get(col), errors="coerce").dropna().tolist()
        y = pd.to_numeric(unprofitable.get(col), errors="coerce").dropna().tolist()
        rows.append(
            {
                "hypothesis": "H3",
                "feature": col,
                "profitable": _metrics(profitable),
                "unprofitable": _metrics(unprofitable),
                "mean_feature_profitable": float(np.mean(x)) if x else None,
                "mean_feature_unprofitable": float(np.mean(y)) if y else None,
                "median_feature_profitable": float(np.median(x)) if x else None,
                "median_feature_unprofitable": float(np.median(y)) if y else None,
                "cliffs_delta": cliffs_delta(x, y),
                "missing_share": float(pd.to_numeric(df.get(col), errors="coerce").isna().mean()) if col in df.columns else 1.0,
            }
        )
    return rows


def analyze_h4_trade_flow(df: pd.DataFrame) -> list[dict[str, Any]]:
    verd = df.get("feature__existing_trade_flow_verdict")
    if verd is None:
        return []
    conf = df[verd.astype(str).str.upper() == "CONFIRMING"]
    other = df[verd.astype(str).str.upper() != "CONFIRMING"]
    rows = []
    for name, sub in (("CONFIRMING", conf), ("NOT_CONFIRMING", other)):
        pnl = pd.to_numeric(sub["label__net_pnl_usdt"], errors="coerce").dropna().tolist()
        rows.append(
            {
                "hypothesis": "H4",
                "slice": name,
                **_metrics(sub),
                "bootstrap_ci_low": bootstrap_ci(pnl)[0],
                "bootstrap_ci_high": bootstrap_ci(pnl)[1],
                **{f"stability__{k}": v for k, v in _stability_slices(sub).items() if not isinstance(v, dict)},
            }
        )
    return rows


def leave_one_coin_out_logistic(df: pd.DataFrame, feature_cols: list[str]) -> list[dict[str, Any]]:
    """Diagnostic L2 logistic; imputation+scaling inside each train fold only."""
    try:
        from sklearn.impute import SimpleImputer
        from sklearn.linear_model import LogisticRegression
        from sklearn.metrics import roc_auc_score
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler
    except ImportError:
        return [{"error": "sklearn_not_available"}]

    rows = []
    y = (pd.to_numeric(df["label__net_pnl_usdt"], errors="coerce") > 0).astype(int)
    X = df[feature_cols].apply(pd.to_numeric, errors="coerce")
    coins = df["symbol"].astype(str).unique()
    for hold in coins:
        tr = df["symbol"].astype(str) != hold
        te = ~tr
        if te.sum() < 1 or tr.sum() < 5 or y[tr].nunique() < 2:
            rows.append({"holdout_coin": hold, "auc": None, "reason": "insufficient_fold"})
            continue
        pipe = Pipeline(
            [
                ("imputer", SimpleImputer(strategy="median")),
                ("scaler", StandardScaler()),
                ("clf", LogisticRegression(penalty="l2", C=1.0, max_iter=500, solver="lbfgs")),
            ]
        )
        pipe.fit(X.loc[tr], y.loc[tr])
        proba = pipe.predict_proba(X.loc[te])[:, 1]
        try:
            auc = float(roc_auc_score(y.loc[te], proba)) if y.loc[te].nunique() > 1 else None
        except ValueError:
            auc = None
        rows.append({"holdout_coin": hold, "auc": auc, "n_train": int(tr.sum()), "n_test": int(te.sum())})
    return rows


def run_all_hypotheses(df: pd.DataFrame) -> dict[str, Any]:
    return {
        "h1": analyze_h1_orderbook(df),
        "h2": analyze_h2_atr_quartiles(df),
        "h3": analyze_h3_ema_structure(df),
        "h4": analyze_h4_trade_flow(df),
        "loco": leave_one_coin_out_logistic(
            df,
            [
                "feature__atr14_pct",
                "feature__tp_atr_ratio",
                "feature__ema59_slope_atr",
                "feature__ema9_20_distance_atr",
                "feature__ob_imbalance_directional",
                "feature__directional_flow_5m",
            ],
        ),
    }
