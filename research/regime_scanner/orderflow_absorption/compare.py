"""Summaries, control comparisons, and decision for absorption audit."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.orderflow_absorption.config import AbsorptionConfig, thr_label


def _merge(assigns: list[dict[str, Any]], outcomes: list[dict[str, Any]]) -> pd.DataFrame:
    a = pd.DataFrame(assigns)
    o = pd.DataFrame(outcomes)
    if a.empty or o.empty:
        return pd.DataFrame()
    return a.merge(o, on=["symbol", "timestamp", "lookback"], how="inner", suffixes=("", "_oc"))


def _side_cols(side: str, h: int, thr: float) -> dict[str, str]:
    tag = thr_label(thr)
    pref = f"h{h}_{tag}"
    if side == "bearish":
        return {
            "fav_reached": f"{pref}_down_reached",
            "adv_reached": f"{pref}_up_reached",
            "fav_first": f"{pref}_bear_fav_first",
            "adv_first": f"{pref}_bear_adv_first",
            "mfe": f"h{h}_bear_mfe",
            "mae": f"h{h}_bear_mae",
            "edge": f"h{h}_bear_edge",
            "close": f"h{h}_close_ret_pct",
            "neither": f"{pref}_neither",
            "both": f"{pref}_both",
        }
    if side == "bullish":
        return {
            "fav_reached": f"{pref}_up_reached",
            "adv_reached": f"{pref}_down_reached",
            "fav_first": f"{pref}_bull_fav_first",
            "adv_first": f"{pref}_bull_adv_first",
            "mfe": f"h{h}_bull_mfe",
            "mae": f"h{h}_bull_mae",
            "edge": f"h{h}_bull_edge",
            "close": f"h{h}_close_ret_pct",
            "neither": f"{pref}_neither",
            "both": f"{pref}_both",
        }
    # neutral / C5: long perspective
    return {
        "fav_reached": f"{pref}_up_reached",
        "adv_reached": f"{pref}_down_reached",
        "fav_first": f"{pref}_up_first",
        "adv_first": f"{pref}_down_first",
        "mfe": f"h{h}_mfe_pct",
        "mae": f"h{h}_mae_pct",
        "edge": f"h{h}_edge",
        "close": f"h{h}_close_ret_pct",
        "neither": f"{pref}_neither",
        "both": f"{pref}_both",
    }


def _metric_row(g: pd.DataFrame, side: str, h: int, thr: float, cfg: AbsorptionConfig) -> dict[str, Any] | None:
    cols = _side_cols(side, h, thr)
    if g.empty or cols["fav_reached"] not in g.columns:
        return None
    coins = g["symbol"].value_counts(normalize=True)
    n = int(len(g))
    close = g[cols["close"]]
    # for bearish, signed close return toward favorable is -close
    if side == "bearish":
        signed_close = -close
    else:
        signed_close = close
    return {
        "n": n,
        "insufficient_sample": n < cfg.min_sample,
        "n_coins": int(g["symbol"].nunique()),
        "btc_n": int((g["symbol"] == "BTCUSDT").sum()),
        "eth_n": int((g["symbol"] == "ETHUSDT").sum()),
        "apt_n": int((g["symbol"] == "APTUSDT").sum()),
        "max_coin_share": float(coins.max()) if len(coins) else np.nan,
        "fav_reached_pct": float(g[cols["fav_reached"]].mean() * 100),
        "adv_reached_pct": float(g[cols["adv_reached"]].mean() * 100),
        "fav_first_pct": float(g[cols["fav_first"]].mean() * 100),
        "adv_first_pct": float(g[cols["adv_first"]].mean() * 100),
        "neither_pct": float(g[cols["neither"]].mean() * 100),
        "both_pct": float(g[cols["both"]].mean() * 100),
        "mean_mfe": float(g[cols["mfe"]].mean()),
        "median_mfe": float(g[cols["mfe"]].median()),
        "mean_mae": float(g[cols["mae"]].mean()),
        "median_mae": float(g[cols["mae"]].median()),
        "mean_edge": float(g[cols["edge"]].mean()),
        "median_edge": float(g[cols["edge"]].median()),
        "mean_close_ret": float(signed_close.mean()),
        "median_close_ret": float(signed_close.median()),
    }


def summarize(assigns: list[dict[str, Any]], outcomes: list[dict[str, Any]], cfg: AbsorptionConfig) -> pd.DataFrame:
    m = _merge(assigns, outcomes)
    if m.empty:
        return pd.DataFrame()
    rows = []
    for keys, g0 in m.groupby(["pattern", "flow_rule", "lookback", "expected_side"], dropna=False):
        pattern, flow_rule, lookback, side = keys
        for h in cfg.horizons:
            g = g0[g0[f"h{h}_valid"] == True] if f"h{h}_valid" in g0.columns else g0  # noqa: E712
            if g.empty:
                continue
            for thr in cfg.move_thresholds:
                met = _metric_row(g, str(side), h, thr, cfg)
                if not met:
                    continue
                rows.append(
                    {
                        "pattern": pattern,
                        "flow_rule": flow_rule,
                        "lookback": lookback,
                        "horizon": h,
                        "threshold": thr,
                        "threshold_label": thr_label(thr),
                        "expected_side": side,
                        "group": "overall",
                        "group_value": "all",
                        **met,
                    }
                )
    return pd.DataFrame(rows)


def coin_summary(assigns: list[dict[str, Any]], outcomes: list[dict[str, Any]], cfg: AbsorptionConfig) -> pd.DataFrame:
    m = _merge(assigns, outcomes)
    if m.empty:
        return pd.DataFrame()
    rows = []
    h, thr = 6, 0.005
    for (sym, pattern, flow_rule, lookback, side), g0 in m.groupby(
        ["symbol", "pattern", "flow_rule", "lookback", "expected_side"]
    ):
        g = g0[g0[f"h{h}_valid"] == True] if f"h{h}_valid" in g0.columns else g0  # noqa: E712
        met = _metric_row(g, str(side), h, thr, cfg)
        if not met:
            continue
        rows.append(
            {
                "symbol": sym,
                "pattern": pattern,
                "flow_rule": flow_rule,
                "lookback": lookback,
                "horizon": h,
                "threshold": thr,
                "expected_side": side,
                **met,
            }
        )
    return pd.DataFrame(rows)


def lookback_summary(summary: pd.DataFrame) -> pd.DataFrame:
    if summary.empty:
        return summary
    return summary[summary["group"] == "overall"].copy()


def oi_diagnostic(assigns: list[dict[str, Any]], outcomes: list[dict[str, Any]], cfg: AbsorptionConfig) -> pd.DataFrame:
    m = _merge(assigns, outcomes)
    if m.empty:
        return pd.DataFrame()
    m = m[m["pattern"].isin(["A1", "A2", "A3", "A4"])]
    rows = []
    h, thr = 6, 0.005
    for (pattern, flow_rule, lookback, oi, side), g0 in m.groupby(
        ["pattern", "flow_rule", "lookback", "oi_state", "expected_side"]
    ):
        g = g0[g0[f"h{h}_valid"] == True] if f"h{h}_valid" in g0.columns else g0  # noqa: E712
        met = _metric_row(g, str(side), h, thr, cfg)
        if not met:
            continue
        rows.append(
            {
                "pattern": pattern,
                "flow_rule": flow_rule,
                "lookback": lookback,
                "oi_state": oi,
                "horizon": h,
                "threshold": thr,
                "expected_side": side,
                **met,
            }
        )
    return pd.DataFrame(rows)


def _block(g: pd.DataFrame, side: str, h: int, thr: float, cfg: AbsorptionConfig) -> dict[str, float]:
    met = _metric_row(g, side, h, thr, cfg)
    if not met:
        return {}
    return {
        "n": float(met["n"]),
        "fav_first_pct": met["fav_first_pct"],
        "adv_first_pct": met["adv_first_pct"],
        "mean_mfe": met["mean_mfe"],
        "mean_mae": met["mean_mae"],
        "mean_edge": met["mean_edge"],
        "mean_close_ret": met["mean_close_ret"],
    }


def control_comparisons(
    assigns: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    cfg: AbsorptionConfig,
) -> pd.DataFrame:
    m = _merge(assigns, outcomes)
    if m.empty:
        return pd.DataFrame()
    pairs = [
        ("A1", "C1", "bearish"),
        ("A1", "C3", "bearish"),
        ("A2", "C1", "bearish"),
        ("A2", "C3", "bearish"),
        ("A3", "C2", "bullish"),
        ("A3", "C4", "bullish"),
        ("A4", "C2", "bullish"),
        ("A4", "C4", "bullish"),
    ]
    rows = []
    for flow_rule in ("F1", "F2", "F3"):
        for lookback in cfg.lookbacks:
            for h in cfg.horizons:
                for thr in cfg.move_thresholds:
                    ml = m[(m["flow_rule"] == flow_rule) & (m["lookback"] == lookback)]
                    if f"h{h}_valid" in ml.columns:
                        ml = ml[ml[f"h{h}_valid"] == True]  # noqa: E712
                    for treat, ctrl, side in pairs:
                        a = ml[ml["pattern"] == treat]
                        b = ml[ml["pattern"] == ctrl]
                        ta, tb = _block(a, side, h, thr, cfg), _block(b, side, h, thr, cfg)
                        if not ta or not tb or tb["n"] <= 0:
                            continue
                        rows.append(
                            {
                                "comparison": f"{treat}_vs_{ctrl}",
                                "flow_rule": flow_rule,
                                "lookback": lookback,
                                "horizon": h,
                                "threshold": thr,
                                "expected_side": side,
                                "treatment_n": ta["n"],
                                "control_n": tb["n"],
                                "sample_ratio": ta["n"] / tb["n"],
                                **{f"d_{k}": ta[k] - tb[k] for k in ta if k != "n"},
                                "treatment_fav_first_pct": ta["fav_first_pct"],
                                "control_fav_first_pct": tb["fav_first_pct"],
                                "treatment_mean_edge": ta["mean_edge"],
                                "control_mean_edge": tb["mean_edge"],
                            }
                        )
    return pd.DataFrame(rows)


def decide(summary: pd.DataFrame, comparisons: pd.DataFrame, cfg: AbsorptionConfig) -> tuple[str, str]:
    if summary.empty:
        return "NO_USEFUL_ABSORPTION_PATTERN", "no summary"

    focus = summary[
        (summary["pattern"].isin(["A1", "A2", "A3", "A4"]))
        & (summary["flow_rule"] == "F1")
        & (summary["n"] >= cfg.min_sample_strong)
        & (summary["n_coins"] >= 2)
        & (summary["max_coin_share"] <= cfg.max_coin_share_strong)
        & (summary["apt_n"] / summary["n"] <= 0.70)
    ]
    if focus.empty:
        weak = summary[
            (summary["pattern"].isin(["A1", "A2", "A3", "A4"]))
            & (summary["n"] >= cfg.min_sample)
        ]
        if not weak.empty:
            return "WEAK_ABSORPTION_PATTERN_MORE_DATA_NEEDED", "sample exists but strong gates not met"
        return "NO_USEFUL_ABSORPTION_PATTERN", "no absorption patterns with min sample"

    hits: list[str] = []
    for _, r in focus.iterrows():
        pat = r["pattern"]
        # direction fit
        if r["fav_first_pct"] <= r["adv_first_pct"]:
            continue
        # lookback consistency on median_edge sign for same horizon/threshold/flow
        sub = focus[
            (focus["pattern"] == pat)
            & (focus["horizon"] == r["horizon"])
            & (focus["threshold"] == r["threshold"])
            & (focus["flow_rule"] == r["flow_rule"])
        ]
        if sub["lookback"].nunique() >= 2:
            edges = sub.sort_values("lookback")["median_edge"].to_numpy()
            if len(edges) >= 2 and np.sign(edges[0]) != np.sign(edges[-1]) and abs(edges[0]) > 0.02 and abs(edges[-1]) > 0.02:
                continue
        # vs primary flow controls
        ctrl_names = ("C1", "C3") if pat in ("A1", "A2") else ("C2", "C4")
        better_count = 0
        for cn in ctrl_names:
            c = comparisons[
                (comparisons["comparison"] == f"{pat}_vs_{cn}")
                & (comparisons["flow_rule"] == r["flow_rule"])
                & (comparisons["lookback"] == r["lookback"])
                & (comparisons["horizon"] == r["horizon"])
                & (comparisons["threshold"] == r["threshold"])
            ]
            if c.empty:
                continue
            c0 = c.iloc[0]
            if float(c0.get("d_fav_first_pct", 0) or 0) > 2 and float(c0.get("d_mean_edge", 0) or 0) > 0:
                better_count += 1
        if better_count >= 1:
            hits.append(f"{pat}/{r['flow_rule']}/lb{int(r['lookback'])}/h{int(r['horizon'])}")

    if hits:
        return "ABSORPTION_PATTERN_FOUND", f"qualifying: {sorted(set(hits))[:12]}"
    if not focus.empty:
        return "WEAK_ABSORPTION_PATTERN_MORE_DATA_NEEDED", "passes sample gates but control uplift unclear"
    return "NO_USEFUL_ABSORPTION_PATTERN", "no clear uplift vs flow controls"
