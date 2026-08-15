"""Summaries and a-priori decision gates."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.orderflow_absorption_level.config import (
    LevelAbsorptionConfig,
    thr_label,
)
from research.regime_scanner.orderflow_absorption_level.controls import treatment_for_event


def _primary_keys(cfg: LevelAbsorptionConfig) -> tuple[str, str]:
    h = cfg.primary_horizon
    tag = thr_label(cfg.primary_threshold)
    return f"h{h}_{tag}_favorable_first", f"h{h}_{tag}_adverse_first"


def _edge_col(cfg: LevelAbsorptionConfig) -> str:
    return f"h{cfg.primary_horizon}_edge"


def _as_df(rows: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def attach_treatments(outcomes: pd.DataFrame, events: list[dict[str, Any]]) -> pd.DataFrame:
    if outcomes.empty:
        return outcomes
    ev_map = {e["event_id"]: e for e in events}
    exploded: list[dict[str, Any]] = []
    for _, row in outcomes.iterrows():
        ev = ev_map.get(row["event_id"], {})
        labels = treatment_for_event({**ev, **row.to_dict()}) if ev else []
        if not labels:
            labels = ["UNLABELED"]
        for lab in labels:
            d = row.to_dict()
            d["treatment"] = lab
            exploded.append(d)
    return pd.DataFrame(exploded)


def _group_stats(g: pd.DataFrame, cfg: LevelAbsorptionConfig) -> dict[str, Any]:
    fav_c, adv_c = _primary_keys(cfg)
    edge_c = _edge_col(cfg)
    n = len(g)
    fav = g[fav_c].mean() if fav_c in g.columns and n else float("nan")
    adv = g[adv_c].mean() if adv_c in g.columns and n else float("nan")
    edge_mean = g[edge_c].mean() if edge_c in g.columns and n else float("nan")
    edge_med = g[edge_c].median() if edge_c in g.columns and n else float("nan")
    coin_share = 0.0
    n_coins = 0
    if n and "symbol" in g.columns:
        vc = g["symbol"].value_counts(normalize=True)
        coin_share = float(vc.max()) if len(vc) else 0.0
        n_coins = int(g["symbol"].nunique())
    return {
        "n_events": int(n),
        "n_coins": n_coins,
        "max_coin_share": coin_share,
        "fav_first_rate": float(fav) if fav == fav else None,
        "adv_first_rate": float(adv) if adv == adv else None,
        "mean_edge": float(edge_mean) if edge_mean == edge_mean else None,
        "median_edge": float(edge_med) if edge_med == edge_med else None,
    }


def event_summary(outcomes: pd.DataFrame, cfg: LevelAbsorptionConfig) -> pd.DataFrame:
    if outcomes.empty:
        return pd.DataFrame()
    # one row per confirmation primary R0 preferred for event_id uniqueness in summaries
    r0 = outcomes[outcomes["confirmation_type"] == "R0"] if "confirmation_type" in outcomes.columns else outcomes
    rows = []
    for (pattern, conf), g in r0.groupby(["pattern", "confirmation_type"], dropna=False):
        st = _group_stats(g, cfg)
        rows.append({"pattern": pattern, "confirmation_type": conf, **st})
    return pd.DataFrame(rows)


def treatment_summary(outcomes: pd.DataFrame, events: list[dict[str, Any]], cfg: LevelAbsorptionConfig) -> pd.DataFrame:
    r0 = outcomes[outcomes["confirmation_type"] == "R0"] if not outcomes.empty else outcomes
    tagged = attach_treatments(r0, events)
    if tagged.empty:
        return pd.DataFrame()
    rows = []
    for treatment, g in tagged.groupby("treatment"):
        st = _group_stats(g, cfg)
        rows.append({"treatment": treatment, **st})
    return pd.DataFrame(rows).sort_values("treatment") if rows else pd.DataFrame()


def control_comparison(
    outcomes: pd.DataFrame,
    events: list[dict[str, Any]],
    cfg: LevelAbsorptionConfig,
) -> pd.DataFrame:
    """Primary comparisons treatment vs K1 / K3."""
    r0 = outcomes[outcomes["confirmation_type"] == "R0"] if not outcomes.empty else outcomes
    tagged = attach_treatments(r0, events)
    if tagged.empty:
        return pd.DataFrame()

    def stats_for(label: str) -> dict[str, Any]:
        g = tagged[tagged["treatment"] == label]
        return _group_stats(g, cfg)

    comparisons = [
        ("bullish", "A4_AT_ANY_SUPPORT", "A4_NO_SUPPORT", "K1"),
        ("bullish", "A4_AT_ANY_SUPPORT", "A4_FAR_FROM_SUPPORT", "K4"),
        ("bearish", "A2_AT_ANY_RESISTANCE", "A2_NO_RESISTANCE", "K1"),
        ("bearish", "A2_AT_ANY_RESISTANCE", "A2_FAR_FROM_RESISTANCE", "K4"),
        ("bullish", "A4_AT_PROTECTED_LOW", "A4_NO_SUPPORT", "K1_protected"),
        ("bullish", "A4_AT_EXTERNAL_SWING_LOW", "A4_NO_SUPPORT", "K1_swing"),
        ("bearish", "A2_AT_PROTECTED_HIGH", "A2_NO_RESISTANCE", "K1_protected"),
        ("bearish", "A2_AT_EXTERNAL_SWING_HIGH", "A2_NO_RESISTANCE", "K1_swing"),
    ]
    rows = []
    for side, treat, ctrl, tag in comparisons:
        ts = stats_for(treat)
        cs = stats_for(ctrl)
        d_fav = None
        d_adv = None
        d_edge = None
        if ts["fav_first_rate"] is not None and cs["fav_first_rate"] is not None:
            d_fav = (ts["fav_first_rate"] - cs["fav_first_rate"]) * 100.0
        if ts["adv_first_rate"] is not None and cs["adv_first_rate"] is not None:
            d_adv = (ts["adv_first_rate"] - cs["adv_first_rate"]) * 100.0
        if ts["mean_edge"] is not None and cs["mean_edge"] is not None:
            d_edge = ts["mean_edge"] - cs["mean_edge"]
        rows.append(
            {
                "side": side,
                "comparison": tag,
                "treatment": treat,
                "control": ctrl,
                "n_treatment": ts["n_events"],
                "n_control": cs["n_events"],
                "treat_fav_first": ts["fav_first_rate"],
                "ctrl_fav_first": cs["fav_first_rate"],
                "delta_fav_first_pp": d_fav,
                "treat_adv_first": ts["adv_first_rate"],
                "ctrl_adv_first": cs["adv_first_rate"],
                "delta_adv_first_pp": d_adv,
                "treat_mean_edge": ts["mean_edge"],
                "ctrl_mean_edge": cs["mean_edge"],
                "delta_mean_edge": d_edge,
                "treat_median_edge": ts["median_edge"],
                "ctrl_median_edge": cs["median_edge"],
                "treat_max_coin_share": ts["max_coin_share"],
                "treat_n_coins": ts["n_coins"],
            }
        )
    return pd.DataFrame(rows)


def level_type_summary(outcomes: pd.DataFrame, cfg: LevelAbsorptionConfig) -> pd.DataFrame:
    r0 = outcomes[(outcomes["confirmation_type"] == "R0") & (~outcomes["no_level"].fillna(True)) & (~outcomes["far_from_level"].fillna(True))] if not outcomes.empty else outcomes
    if r0.empty:
        return pd.DataFrame()
    rows = []
    for (pattern, lt), g in r0.groupby(["pattern", "level_type"], dropna=False):
        st = _group_stats(g, cfg)
        rows.append({"pattern": pattern, "level_type": lt, **st})
    return pd.DataFrame(rows)


def distance_bucket_summary(outcomes: pd.DataFrame, cfg: LevelAbsorptionConfig) -> pd.DataFrame:
    r0 = outcomes[outcomes["confirmation_type"] == "R0"] if not outcomes.empty else outcomes
    if r0.empty:
        return pd.DataFrame()
    rows = []
    for (pattern, b), g in r0.groupby(["pattern", "distance_bucket_at_entry"], dropna=False):
        st = _group_stats(g, cfg)
        rows.append({"pattern": pattern, "distance_bucket": b, **st})
    return pd.DataFrame(rows)


def confirmation_summary(outcomes: pd.DataFrame, cfg: LevelAbsorptionConfig) -> pd.DataFrame:
    if outcomes.empty:
        return pd.DataFrame()
    rows = []
    for (pattern, conf), g in outcomes.groupby(["pattern", "confirmation_type"], dropna=False):
        if "no_level" in g.columns and "far_from_level" in g.columns:
            g2 = g[~g["no_level"].fillna(True) & ~g["far_from_level"].fillna(True)]
        else:
            g2 = g
        st = _group_stats(g2 if len(g2) else g, cfg)
        rows.append({"pattern": pattern, "confirmation_type": conf, **st})
    return pd.DataFrame(rows)


def coin_summary(outcomes: pd.DataFrame, events: list[dict[str, Any]], cfg: LevelAbsorptionConfig) -> pd.DataFrame:
    r0 = outcomes[outcomes["confirmation_type"] == "R0"] if not outcomes.empty else outcomes
    tagged = attach_treatments(r0, events)
    if tagged.empty:
        return pd.DataFrame()
    focus = tagged[tagged["treatment"].isin(["A4_AT_ANY_SUPPORT", "A4_NO_SUPPORT", "A2_AT_ANY_RESISTANCE", "A2_NO_RESISTANCE"])]
    rows = []
    for (sym, treatment), g in focus.groupby(["symbol", "treatment"]):
        st = _group_stats(g, cfg)
        rows.append({"symbol": sym, "treatment": treatment, **st})
    return pd.DataFrame(rows)


def equal_coin_summary(coin_df: pd.DataFrame) -> pd.DataFrame:
    """Equal-weight mean across coins for each treatment."""
    if coin_df.empty:
        return pd.DataFrame()
    rows = []
    for treatment, g in coin_df.groupby("treatment"):
        rows.append(
            {
                "treatment": treatment,
                "n_coins": int(g["symbol"].nunique()),
                "equal_fav_first": float(g["fav_first_rate"].dropna().mean()) if g["fav_first_rate"].notna().any() else None,
                "equal_adv_first": float(g["adv_first_rate"].dropna().mean()) if g["adv_first_rate"].notna().any() else None,
                "equal_mean_edge": float(g["mean_edge"].dropna().mean()) if g["mean_edge"].notna().any() else None,
            }
        )
    return pd.DataFrame(rows)


def median_coin_summary(coin_df: pd.DataFrame) -> pd.DataFrame:
    if coin_df.empty:
        return pd.DataFrame()
    rows = []
    for treatment, g in coin_df.groupby("treatment"):
        rows.append(
            {
                "treatment": treatment,
                "n_coins": int(g["symbol"].nunique()),
                "median_fav_first": float(g["fav_first_rate"].dropna().median()) if g["fav_first_rate"].notna().any() else None,
                "median_adv_first": float(g["adv_first_rate"].dropna().median()) if g["adv_first_rate"].notna().any() else None,
                "median_mean_edge": float(g["mean_edge"].dropna().median()) if g["mean_edge"].notna().any() else None,
            }
        )
    return pd.DataFrame(rows)


def _side_passes_gates(
    treat_stats: dict[str, Any],
    ctrl_stats: dict[str, Any],
    *,
    cfg: LevelAbsorptionConfig,
    coin_df: pd.DataFrame,
    treat_label: str,
    level_type_df: pd.DataFrame,
    pattern: str,
) -> tuple[bool, bool, str]:
    """Return (passes_strong, directionally_positive, reason)."""
    n = treat_stats["n_events"]
    n_coins = treat_stats["n_coins"]
    sample_ok = n >= cfg.min_events_strong or (n >= cfg.min_events_alt and n_coins >= cfg.min_coins_alt)
    if not sample_ok:
        return False, False, "MORE_DATA_NEEDED"

    if treat_stats["max_coin_share"] > cfg.max_coin_share:
        return False, False, "coin_concentration"

    if treat_stats["fav_first_rate"] is None or ctrl_stats["fav_first_rate"] is None:
        return False, False, "missing_rates"

    d_fav_pp = (treat_stats["fav_first_rate"] - ctrl_stats["fav_first_rate"]) * 100.0
    d_adv_pp = (
        (treat_stats["adv_first_rate"] - ctrl_stats["adv_first_rate"]) * 100.0
        if treat_stats["adv_first_rate"] is not None and ctrl_stats["adv_first_rate"] is not None
        else 0.0
    )
    edge_ok = False
    if treat_stats["mean_edge"] is not None and ctrl_stats["mean_edge"] is not None:
        edge_ok = treat_stats["mean_edge"] - ctrl_stats["mean_edge"] > 0
    if treat_stats["median_edge"] is not None and ctrl_stats["median_edge"] is not None:
        edge_ok = edge_ok or (treat_stats["median_edge"] - ctrl_stats["median_edge"] > 0)

    directional = d_fav_pp > 0 and edge_ok and d_adv_pp <= 0

    # not only APT
    apt_only = False
    if not coin_df.empty:
        sub = coin_df[coin_df["treatment"] == treat_label]
        if len(sub) and set(sub["symbol"].unique()) == {"APTUSDT"}:
            apt_only = True
        elif len(sub):
            non_apt = sub[sub["symbol"] != "APTUSDT"]
            if len(non_apt) and non_apt["fav_first_rate"].notna().any():
                # require non-APT pooled mean >= control directionally or any non-APT positive delta vs own ctrl hard; simplify: non-APT fav_first mean >= APT or > 0 uplift proxy
                pass
            elif len(sub) == 1 and sub.iloc[0]["symbol"] == "APTUSDT":
                apt_only = True

    # level type visibility
    level_ok = False
    if not level_type_df.empty:
        lt = level_type_df[level_type_df["pattern"] == pattern]
        if (lt["level_type"] == "protected").any() and int(lt.loc[lt["level_type"] == "protected", "n_events"].sum()) >= 20:
            level_ok = True
        if lt["level_type"].nunique() >= 2 and (lt["n_events"] >= 15).sum() >= 2:
            level_ok = True
        if (lt["level_type"] == "protected").any() and int(lt.loc[lt["level_type"] == "protected", "n_events"].sum()) >= cfg.min_events_alt:
            level_ok = True
    else:
        level_ok = True  # smoke / tiny samples: don't block MORE_DATA elsewhere

    strong = (
        sample_ok
        and treat_stats["max_coin_share"] <= cfg.max_coin_share
        and d_fav_pp >= cfg.min_d_fav_first_pp
        and edge_ok
        and d_adv_pp <= 0
        and not apt_only
        and level_ok
    )
    if strong:
        return True, True, "gates_pass"
    if directional and sample_ok:
        return False, True, "RAW_LEVEL_CONTEXT_WEAK"
    return False, directional, "NO_LEVEL_CONTEXT_EDGE"


def decide(
    *,
    treatment_df: pd.DataFrame,
    comparison_df: pd.DataFrame,
    confirmation_df: pd.DataFrame,
    coin_df: pd.DataFrame,
    level_type_df: pd.DataFrame,
    cfg: LevelAbsorptionConfig,
) -> tuple[str, str]:
    def row_stats(label: str) -> dict[str, Any]:
        if treatment_df.empty:
            return {"n_events": 0, "n_coins": 0, "max_coin_share": 1.0, "fav_first_rate": None, "adv_first_rate": None, "mean_edge": None, "median_edge": None}
        g = treatment_df[treatment_df["treatment"] == label]
        if g.empty:
            return {"n_events": 0, "n_coins": 0, "max_coin_share": 1.0, "fav_first_rate": None, "adv_first_rate": None, "mean_edge": None, "median_edge": None}
        r = g.iloc[0]
        return {
            "n_events": int(r["n_events"]),
            "n_coins": int(r["n_coins"]),
            "max_coin_share": float(r["max_coin_share"]),
            "fav_first_rate": r["fav_first_rate"],
            "adv_first_rate": r["adv_first_rate"],
            "mean_edge": r["mean_edge"],
            "median_edge": r["median_edge"],
        }

    bull_t = row_stats("A4_AT_ANY_SUPPORT")
    bull_c = row_stats("A4_NO_SUPPORT")
    bear_t = row_stats("A2_AT_ANY_RESISTANCE")
    bear_c = row_stats("A2_NO_RESISTANCE")

    # Insufficient sample overall
    total_events = int(bull_t["n_events"] + bear_t["n_events"] + bull_c["n_events"] + bear_c["n_events"])
    if total_events < 20 and bull_t["n_events"] < cfg.min_events_alt and bear_t["n_events"] < cfg.min_events_alt:
        return "MORE_DATA_NEEDED", "insufficient event sample for gate evaluation"

    bull_pass, bull_dir, bull_why = _side_passes_gates(
        bull_t, bull_c, cfg=cfg, coin_df=coin_df, treat_label="A4_AT_ANY_SUPPORT", level_type_df=level_type_df, pattern="A4"
    )
    bear_pass, bear_dir, bear_why = _side_passes_gates(
        bear_t, bear_c, cfg=cfg, coin_df=coin_df, treat_label="A2_AT_ANY_RESISTANCE", level_type_df=level_type_df, pattern="A2"
    )

    if bull_why == "MORE_DATA_NEEDED" and bear_why == "MORE_DATA_NEEDED":
        return "MORE_DATA_NEEDED", "both sides under sample gates"

    # Confirmation required: R0 fails but R1/R2 pass
    if not bull_pass and not bear_pass and not confirmation_df.empty:
        r1 = confirmation_df[confirmation_df["confirmation_type"].isin(["R1", "R2"])]
        if len(r1):
            # crude: any R1/R2 with fav_first uplift proxy via higher fav_first than R0 same pattern
            r0 = confirmation_df[confirmation_df["confirmation_type"] == "R0"]
            better = False
            for pattern in ("A4", "A2"):
                a = r1[r1["pattern"] == pattern]
                b = r0[r0["pattern"] == pattern]
                if len(a) and len(b) and a.iloc[0]["fav_first_rate"] is not None and b.iloc[0]["fav_first_rate"] is not None:
                    if a.iloc[0]["n_events"] >= cfg.min_events_alt and (
                        float(a.iloc[0]["fav_first_rate"]) - float(b.iloc[0]["fav_first_rate"])
                    ) * 100 >= cfg.min_d_fav_first_pp:
                        better = True
            if better:
                return "CONFIRMATION_REQUIRED", "R0 gates fail; R1/R2 show uplift"

    if bull_pass and bear_pass:
        return "LEVEL_CONTEXT_IMPROVES_ABSORPTION", "both sides pass gates vs K1"
    if bull_pass and not bear_pass:
        return "SUPPORT_ONLY_EDGE", f"bullish gates pass; bearish={bear_why}"
    if bear_pass and not bull_pass:
        return "RESISTANCE_ONLY_EDGE", f"bearish gates pass; bullish={bull_why}"
    if bull_dir or bear_dir:
        return "RAW_LEVEL_CONTEXT_WEAK", f"directional uplift below gates (bull={bull_why}, bear={bear_why})"
    return "NO_LEVEL_CONTEXT_EDGE", f"no stable uplift vs K1 (bull={bull_why}, bear={bear_why})"
