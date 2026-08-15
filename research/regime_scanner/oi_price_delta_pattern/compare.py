"""Pattern summaries and control comparisons."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.oi_price_delta_pattern.config import PatternConfig, thr_label


def _merge_assign_outcomes(
    assigns: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
) -> pd.DataFrame:
    a = pd.DataFrame(assigns)
    o = pd.DataFrame(outcomes)
    if a.empty or o.empty:
        return pd.DataFrame()
    keys = ["symbol", "timestamp", "lookback"]
    return a.merge(o, on=keys, how="inner", suffixes=("", "_oc"))


def summarize_patterns(
    assigns: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    cfg: PatternConfig,
) -> pd.DataFrame:
    m = _merge_assign_outcomes(assigns, outcomes)
    if m.empty:
        return pd.DataFrame()

    rows = []
    # Primary patterns + COMBO::* (filter COMBO in report focus later)
    for (pattern, lookback), g0 in m.groupby(["pattern", "lookback"], dropna=False):
        for h in cfg.horizons:
            if f"h{h}_valid" in g0.columns:
                g = g0[g0[f"h{h}_valid"] == True]  # noqa: E712
            else:
                g = g0
            if g.empty:
                continue
            for thr in cfg.move_thresholds:
                tag = thr_label(thr)
                pref = f"h{h}_{tag}"
                up_r = g.get(f"{pref}_up_reached")
                dn_r = g.get(f"{pref}_down_reached")
                if up_r is None:
                    continue
                coins = g["symbol"].value_counts(normalize=True)
                n = int(len(g))
                rows.append(
                    {
                        "pattern": pattern,
                        "lookback": lookback,
                        "horizon": h,
                        "threshold": thr,
                        "threshold_label": tag,
                        "n": n,
                        "insufficient_sample": n < cfg.min_sample,
                        "n_coins": int(g["symbol"].nunique()),
                        "btc_n": int((g["symbol"] == "BTCUSDT").sum()),
                        "eth_n": int((g["symbol"] == "ETHUSDT").sum()),
                        "apt_n": int((g["symbol"] == "APTUSDT").sum()),
                        "max_coin_share": float(coins.max()) if len(coins) else np.nan,
                        "up_reached_pct": float(up_r.mean() * 100),
                        "down_reached_pct": float(dn_r.mean() * 100),
                        "up_first_pct": float(g[f"{pref}_up_first"].mean() * 100),
                        "down_first_pct": float(g[f"{pref}_down_first"].mean() * 100),
                        "neither_pct": float(g[f"{pref}_neither"].mean() * 100),
                        "both_pct": float(g[f"{pref}_both"].mean() * 100),
                        "mean_mfe": float(g[f"h{h}_mfe_pct"].mean()),
                        "median_mfe": float(g[f"h{h}_mfe_pct"].median()),
                        "mean_mae": float(g[f"h{h}_mae_pct"].mean()),
                        "median_mae": float(g[f"h{h}_mae_pct"].median()),
                        "mean_edge": float(g[f"h{h}_edge"].mean()),
                        "median_edge": float(g[f"h{h}_edge"].median()),
                        "mean_close_ret": float(g[f"h{h}_close_ret_pct"].mean()),
                        "median_close_ret": float(g[f"h{h}_close_ret_pct"].median()),
                    }
                )
    return pd.DataFrame(rows)


def coin_summary(assigns: list[dict[str, Any]], outcomes: list[dict[str, Any]], cfg: PatternConfig) -> pd.DataFrame:
    m = _merge_assign_outcomes(assigns, outcomes)
    if m.empty:
        return pd.DataFrame()
    rows = []
    h = 6
    thr = 0.005
    tag = thr_label(thr)
    pref = f"h{h}_{tag}"
    for (sym, pattern, lookback), g in m.groupby(["symbol", "pattern", "lookback"]):
        if pattern.startswith("COMBO::"):
            continue
        if f"h{h}_valid" in g.columns:
            g = g[g[f"h{h}_valid"] == True]  # noqa: E712
        if g.empty or pref + "_up_reached" not in g.columns:
            continue
        rows.append(
            {
                "symbol": sym,
                "pattern": pattern,
                "lookback": lookback,
                "horizon": h,
                "threshold": thr,
                "n": int(len(g)),
                "up_reached_pct": float(g[f"{pref}_up_reached"].mean() * 100),
                "down_reached_pct": float(g[f"{pref}_down_reached"].mean() * 100),
                "median_edge": float(g[f"h{h}_edge"].median()),
                "median_close_ret": float(g[f"h{h}_close_ret_pct"].median()),
            }
        )
    return pd.DataFrame(rows)


def direction_summary(summary: pd.DataFrame) -> pd.DataFrame:
    if summary.empty:
        return summary
    s = summary[~summary["pattern"].astype(str).str.startswith("COMBO::")].copy()
    s["bias"] = np.where(s["up_first_pct"] >= s["down_first_pct"], "up_bias", "down_bias")
    return s


def _metric_block(g: pd.DataFrame, h: int, thr: float) -> dict[str, float]:
    tag = thr_label(thr)
    pref = f"h{h}_{tag}"
    if g.empty or f"{pref}_up_reached" not in g.columns:
        return {}
    return {
        "n": float(len(g)),
        "up_reached_pct": float(g[f"{pref}_up_reached"].mean() * 100),
        "down_reached_pct": float(g[f"{pref}_down_reached"].mean() * 100),
        "up_first_pct": float(g[f"{pref}_up_first"].mean() * 100),
        "down_first_pct": float(g[f"{pref}_down_first"].mean() * 100),
        "mean_mfe": float(g[f"h{h}_mfe_pct"].mean()),
        "mean_mae": float(g[f"h{h}_mae_pct"].mean()),
        "mean_edge": float(g[f"h{h}_edge"].mean()),
        "mean_close_ret": float(g[f"h{h}_close_ret_pct"].mean()),
    }


def pattern_comparisons(
    assigns: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    stated: list[dict[str, Any]],
    cfg: PatternConfig,
) -> pd.DataFrame:
    m = _merge_assign_outcomes(assigns, outcomes)
    if m.empty:
        return pd.DataFrame()
    stated_df = pd.DataFrame(stated)
    rows = []
    # default comparison horizon/threshold
    for lookback in cfg.lookbacks:
        for h in cfg.horizons:
            for thr in cfg.move_thresholds:
                ml = m[(m["lookback"] == lookback)]
                if f"h{h}_valid" in ml.columns:
                    ml = ml[ml[f"h{h}_valid"] == True]  # noqa: E712

                def grab(pattern: str) -> pd.DataFrame:
                    return ml[ml["pattern"] == pattern]

                ctrl = grab("P6")
                for pat, expect in (("P1", "up"), ("P2", "down")):
                    g = grab(pat)
                    a, b = _metric_block(g, h, thr), _metric_block(ctrl, h, thr)
                    if not a or not b:
                        continue
                    rows.append(
                        {
                            "comparison": f"{pat}_vs_P6",
                            "lookback": lookback,
                            "horizon": h,
                            "threshold": thr,
                            "expected_direction": expect,
                            "treatment_n": a["n"],
                            "control_n": b["n"],
                            **{f"d_{k}": a[k] - b[k] for k in a if k != "n"},
                            "treatment_up_reached_pct": a["up_reached_pct"],
                            "control_up_reached_pct": b["up_reached_pct"],
                            "treatment_down_reached_pct": a["down_reached_pct"],
                            "control_down_reached_pct": b["down_reached_pct"],
                        }
                    )

                # P3 vs price_up alone
                st = stated_df[stated_df["lookback"] == lookback]
                price_up_keys = set(
                    zip(st.loc[st["price_state"] == "price_up", "symbol"], st.loc[st["price_state"] == "price_up", "timestamp"])
                )
                # merge outcomes on stated price_up
                oc = pd.DataFrame(outcomes)
                oc = oc[oc["lookback"] == lookback]
                if f"h{h}_valid" in oc.columns:
                    oc = oc[oc[f"h{h}_valid"] == True]  # noqa: E712
                pu = oc[oc.apply(lambda r: (r["symbol"], r["timestamp"]) in price_up_keys, axis=1)] if len(oc) else oc
                p3 = grab("P3")
                a, b = _metric_block(p3, h, thr), _metric_block(pu, h, thr)
                if a and b:
                    rows.append(
                        {
                            "comparison": "P3_vs_price_up",
                            "lookback": lookback,
                            "horizon": h,
                            "threshold": thr,
                            "expected_direction": "up",
                            "treatment_n": a["n"],
                            "control_n": b["n"],
                            **{f"d_{k}": a[k] - b[k] for k in a if k != "n"},
                            "treatment_up_reached_pct": a["up_reached_pct"],
                            "control_up_reached_pct": b["up_reached_pct"],
                        }
                    )
                price_dn_keys = set(
                    zip(
                        st.loc[st["price_state"] == "price_down", "symbol"],
                        st.loc[st["price_state"] == "price_down", "timestamp"],
                    )
                )
                pdn = oc[oc.apply(lambda r: (r["symbol"], r["timestamp"]) in price_dn_keys, axis=1)] if len(oc) else oc
                p4 = grab("P4")
                a, b = _metric_block(p4, h, thr), _metric_block(pdn, h, thr)
                if a and b:
                    rows.append(
                        {
                            "comparison": "P4_vs_price_down",
                            "lookback": lookback,
                            "horizon": h,
                            "threshold": thr,
                            "expected_direction": "down",
                            "treatment_n": a["n"],
                            "control_n": b["n"],
                            **{f"d_{k}": a[k] - b[k] for k in a if k != "n"},
                            "treatment_down_reached_pct": a["down_reached_pct"],
                            "control_down_reached_pct": b["down_reached_pct"],
                        }
                    )

                # OI add-on: same price+delta with oi_up vs without
                st_l = stated_df[stated_df["lookback"] == lookback].copy()
                st_l["key"] = list(zip(st_l["symbol"], st_l["timestamp"]))
                oc2 = oc.copy()
                oc2["key"] = list(zip(oc2["symbol"], oc2["timestamp"]))
                merged = st_l.merge(oc2, on=["symbol", "timestamp", "lookback"], how="inner")
                if not merged.empty:
                    for ps in ("price_flat", "price_up", "price_down"):
                        for ds in ("delta_positive", "delta_negative", "delta_neutral"):
                            base = merged[(merged["price_state"] == ps) & (merged["delta_state"] == ds)]
                            with_oi = base[base["oi_state"] == "oi_up"]
                            without = base[base["oi_state"] != "oi_up"]
                            a, b = _metric_block(with_oi, h, thr), _metric_block(without, h, thr)
                            if a and b and a["n"] >= 10 and b["n"] >= 10:
                                rows.append(
                                    {
                                        "comparison": f"oi_up_vs_not|{ps}|{ds}",
                                        "lookback": lookback,
                                        "horizon": h,
                                        "threshold": thr,
                                        "treatment_n": a["n"],
                                        "control_n": b["n"],
                                        **{f"d_{k}": a[k] - b[k] for k in a if k != "n"},
                                    }
                                )
                    # Delta add-on given price_flat + oi_up
                    base = merged[(merged["price_state"] == "price_flat") & (merged["oi_state"] == "oi_up")]
                    conf = base[base["delta_state"] == "delta_positive"]
                    other = base[base["delta_state"] != "delta_positive"]
                    a, b = _metric_block(conf, h, thr), _metric_block(other, h, thr)
                    if a and b:
                        rows.append(
                            {
                                "comparison": "delta_pos_vs_not|price_flat|oi_up",
                                "lookback": lookback,
                                "horizon": h,
                                "threshold": thr,
                                "expected_direction": "up",
                                "treatment_n": a["n"],
                                "control_n": b["n"],
                                **{f"d_{k}": a[k] - b[k] for k in a if k != "n"},
                            }
                        )
                    conf = base[base["delta_state"] == "delta_negative"]
                    other = base[base["delta_state"] != "delta_negative"]
                    a, b = _metric_block(conf, h, thr), _metric_block(other, h, thr)
                    if a and b:
                        rows.append(
                            {
                                "comparison": "delta_neg_vs_not|price_flat|oi_up",
                                "lookback": lookback,
                                "horizon": h,
                                "threshold": thr,
                                "expected_direction": "down",
                                "treatment_n": a["n"],
                                "control_n": b["n"],
                                **{f"d_{k}": a[k] - b[k] for k in a if k != "n"},
                            }
                        )
    return pd.DataFrame(rows)


def decide(
    summary: pd.DataFrame,
    comparisons: pd.DataFrame,
    cfg: PatternConfig,
) -> tuple[str, str]:
    """Return (decision, rationale)."""
    if summary.empty:
        return "NO_USEFUL_OI_PRICE_DELTA_PATTERN", "no summary rows"

    focus = summary[
        (~summary["pattern"].astype(str).str.startswith("COMBO::"))
        & (summary["pattern"].isin(["P1", "P2", "P3", "P4"]))
        & (~summary["insufficient_sample"])
        & (summary["n_coins"] >= 2)
        & (summary["max_coin_share"] <= 0.8)
    ]
    if focus.empty:
        # weak if any pattern has n>=30 but fails coin diversity
        weak = summary[
            (~summary["pattern"].astype(str).str.startswith("COMBO::"))
            & (summary["pattern"].isin(["P1", "P2", "P3", "P4"]))
            & (summary["n"] >= cfg.min_sample)
        ]
        if not weak.empty:
            return "WEAK_PATTERN_MORE_DATA_NEEDED", "sample exists but coin diversity/stability insufficient"
        return "NO_USEFUL_OI_PRICE_DELTA_PATTERN", "no qualifying primary patterns"

    # check comparison edge vs control for default h=6 thr=0.005
    hits = []
    for _, r in focus.iterrows():
        pat = r["pattern"]
        if comparisons.empty:
            continue
        # direction fit
        if pat in ("P1", "P3") and r["up_first_pct"] <= r["down_first_pct"]:
            continue
        if pat in ("P2", "P4") and r["down_first_pct"] <= r["up_first_pct"]:
            continue
        # lookback consistency: need both 12 and 24 not opposite signs on edge
        sub = focus[(focus["pattern"] == pat) & (focus["horizon"] == r["horizon"]) & (focus["threshold"] == r["threshold"])]
        if sub["lookback"].nunique() >= 2:
            edges = sub.set_index("lookback")["median_edge"]
            if len(edges) >= 2 and np.sign(edges.iloc[0]) != np.sign(edges.iloc[1]) and abs(edges.iloc[0]) > 0.05 and abs(edges.iloc[1]) > 0.05:
                continue
        # vs P6
        cmp_name = f"{pat}_vs_P6" if pat in ("P1", "P2") else None
        if pat == "P3":
            cmp_name = "P3_vs_price_up"
        if pat == "P4":
            cmp_name = "P4_vs_price_down"
        if cmp_name is None:
            continue
        c = comparisons[
            (comparisons["comparison"] == cmp_name)
            & (comparisons["lookback"] == r["lookback"])
            & (comparisons["horizon"] == r["horizon"])
            & (comparisons["threshold"] == r["threshold"])
        ]
        if c.empty:
            continue
        c0 = c.iloc[0]
        better = False
        if pat in ("P1", "P3"):
            better = float(c0.get("d_up_first_pct", 0) or 0) > 2 and float(c0.get("d_mean_edge", 0) or 0) > 0
        if pat in ("P2", "P4"):
            # Short-side uplift: more down-first and weaker (more negative) long edge
            better = (
                float(c0.get("d_down_first_pct", 0) or 0) > 2
                and float(c0.get("d_mean_edge", 0) or 0) < 0
            )
        if better:
            hits.append(pat)

    if hits:
        return "PATTERN_FOUND_PROCEED_TO_FULL_AUDIT", f"qualifying patterns: {sorted(set(hits))}"
    if not focus.empty:
        return "WEAK_PATTERN_MORE_DATA_NEEDED", "patterns meet sample gates but control uplift unclear"
    return "NO_USEFUL_OI_PRICE_DELTA_PATTERN", "no clear uplift vs controls"
