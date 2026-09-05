"""Pre-specified candidate rules; selection only on Discovery split."""

from __future__ import annotations

from typing import Any, Callable

import pandas as pd

from .stats import block_bootstrap_rate, wilson_interval

RuleFn = Callable[[pd.DataFrame], pd.Series]


def _rules() -> list[dict[str, Any]]:
    return [
        {
            "rule_id": "R1_6plus_ema200",
            "definition": "n_components>=6 AND overlaps_ema200; outcome=defended",
            "mask": lambda d: d["multi_6plus"] & d["overlaps_ema200"].fillna(False),
            "outcome": "defended",
            "baseline_mask": lambda d: d["multi_6plus"],
        },
        {
            "rule_id": "R2_4plus_bull_bid",
            "definition": "n_components>=4 AND side=BID AND bullish_stack; outcome=defended",
            "mask": lambda d: d["multi_4plus"] & (d["side"] == "BID") & d["bullish_stack"].fillna(False),
            "outcome": "defended",
            "baseline_mask": lambda d: d["multi_4plus"] & (d["side"] == "BID"),
        },
        {
            "rule_id": "R3_single_impulsive",
            "definition": "n_components==1 AND approach=impulsive_toward; outcome=consumed_accepted",
            "mask": lambda d: (d["n_components"] == 1) & (d["approach_regime"] == "impulsive_toward"),
            "outcome": "consumed_accepted",
            "baseline_mask": lambda d: d["n_components"] == 1,
        },
        {
            "rule_id": "R4_sweep_reclaim_ema20_with",
            "definition": "swept_reclaimed AND ema20_slope_with_reaction; outcome=swept_reclaimed (incidence among touched)",
            "mask": lambda d: d["touched"].astype(bool)
            & d["ema20_slope_with_reaction"].fillna(False)
            & d["swept"].astype(bool),
            "outcome": "swept_reclaimed",
            "baseline_mask": lambda d: d["touched"].astype(bool) & d["swept"].astype(bool),
        },
        {
            "rule_id": "R5_old_multi_slow",
            "definition": "multi_pool AND age_at_touch in {7-12,13-24,>24} AND slow_toward; outcome=defended",
            "mask": lambda d: d["multi_pool"]
            & d["age_at_touch_bucket"].isin(["7-12", "13-24", ">24"])
            & (d["approach_regime"] == "slow_toward"),
            "outcome": "defended",
            "baseline_mask": lambda d: d["multi_pool"],
        },
        {
            "rule_id": "R6_6plus_vs_single_defense",
            "definition": "multi_6plus AND delayed_touch AND distance_atr in {0.5-1,1-2,2-3,>3}; outcome=defended",
            "mask": lambda d: d["multi_6plus"]
            & (d["touch_timing"] == "delayed_touch")
            & d["distance_atr_bucket"].isin(["0.5-1", "1-2", "2-3", ">3"]),
            "outcome": "defended",
            "baseline_mask": lambda d: (d["n_components"] == 1)
            & (d["touch_timing"] == "delayed_touch")
            & d["distance_atr_bucket"].isin(["0.5-1", "1-2", "2-3", ">3"]),
            "baseline_is_other_population": True,
        },
        {
            "rule_id": "R7_multi_ema_multi_band",
            "definition": "multi_pool AND n_ema_overlaps>=2; outcome=defended",
            "mask": lambda d: d["multi_multi_ema"].fillna(False),
            "outcome": "defended",
            "baseline_mask": lambda d: d["multi_no_ema"].fillna(False),
            "baseline_is_other_population": True,
        },
        {
            "rule_id": "R8_ask_bear_4plus",
            "definition": "n_components>=4 AND side=ASK AND bearish_stack; outcome=defended",
            "mask": lambda d: d["multi_4plus"] & (d["side"] == "ASK") & d["bearish_stack"].fillna(False),
            "outcome": "defended",
            "baseline_mask": lambda d: d["multi_4plus"] & (d["side"] == "ASK"),
        },
    ]


def _split_rate(df: pd.DataFrame, mask: pd.Series, outcome: str) -> dict[str, Any]:
    sub = df.loc[mask.fillna(False)]
    n = len(sub)
    if n == 0 or outcome not in sub.columns:
        return {"n": 0, "rate": None, "wilson_lo": None, "wilson_hi": None}
    s = int(sub[outcome].fillna(False).astype(bool).sum())
    lo, hi = wilson_interval(s, n)
    return {"n": n, "rate": s / n, "wilson_lo": lo, "wilson_hi": hi, "count": s}


def evaluate_candidates(
    df: pd.DataFrame,
    *,
    min_n_discovery: int = 30,
    min_n_oos: int = 20,
    n_boot: int = 300,
) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    """Select on Discovery only; report Validation + OOS. Returns candidates, oos_results, stable_ids."""
    rules = _rules()
    cand_rows: list[dict[str, Any]] = []
    oos_rows: list[dict[str, Any]] = []
    selected: list[str] = []

    disc = df[df["temporal_split"] == "discovery"]
    val = df[df["temporal_split"] == "validation"]
    oos = df[df["temporal_split"] == "oos"]

    for rule in rules:
        rid = rule["rule_id"]
        m_all = rule["mask"](df)
        # selection uses discovery only
        d_rate = _split_rate(disc, rule["mask"](disc), rule["outcome"])
        if d_rate["n"] >= min_n_discovery:
            selected.append(rid)

        base_other = bool(rule.get("baseline_is_other_population"))
        b_disc = _split_rate(disc, rule["baseline_mask"](disc), rule["outcome"])
        # If baseline is other population, compare rates; else baseline among same mask parents
        if not base_other:
            # baseline = overall rate of outcome in discovery for comparable universe in baseline_mask
            pass

        v_rate = _split_rate(val, rule["mask"](val), rule["outcome"])
        o_rate = _split_rate(oos, rule["mask"](oos), rule["outcome"])
        b_oos = _split_rate(oos, rule["baseline_mask"](oos), rule["outcome"])

        boot_day = block_bootstrap_rate(
            df.loc[m_all.fillna(False)],
            success_col=rule["outcome"],
            block_cols=["utc_day"],
            n_boot=n_boot,
            seed=hash(rid) % 10_000,
        )
        boot_sym_day = block_bootstrap_rate(
            df.loc[m_all.fillna(False)],
            success_col=rule["outcome"],
            block_cols=["symbol", "utc_day"],
            n_boot=n_boot,
            seed=hash(rid) % 10_000 + 1,
        )

        abs_diff = None
        rel = None
        if o_rate["rate"] is not None and b_oos["rate"] is not None and b_oos["rate"] > 0:
            abs_diff = o_rate["rate"] - b_oos["rate"]
            rel = o_rate["rate"] / b_oos["rate"]

        # symbol/tf stability on discovery
        stab = []
        sub_d = disc.loc[rule["mask"](disc).fillna(False)]
        if len(sub_d):
            for sym, sg in sub_d.groupby("symbol"):
                if len(sg) >= 10:
                    stab.append(f"{sym}:{float(sg[rule['outcome']].mean()):.3f}(n={len(sg)})")
            for tf, tg in sub_d.groupby("timeframe"):
                if len(tg) >= 10:
                    stab.append(f"{tf}:{float(tg[rule['outcome']].mean()):.3f}(n={len(tg)})")

        row = {
            "rule_id": rid,
            "definition": rule["definition"],
            "outcome": rule["outcome"],
            "selected_on_discovery": rid in selected,
            "discovery_n": d_rate["n"],
            "discovery_rate": d_rate["rate"],
            "discovery_wilson_lo": d_rate["wilson_lo"],
            "discovery_wilson_hi": d_rate["wilson_hi"],
            "discovery_baseline_n": b_disc["n"],
            "discovery_baseline_rate": b_disc["rate"],
            "validation_n": v_rate["n"],
            "validation_rate": v_rate["rate"],
            "oos_n": o_rate["n"],
            "oos_rate": o_rate["rate"],
            "oos_wilson_lo": o_rate["wilson_lo"],
            "oos_wilson_hi": o_rate["wilson_hi"],
            "oos_baseline_n": b_oos["n"],
            "oos_baseline_rate": b_oos["rate"],
            "oos_abs_diff_vs_baseline": abs_diff,
            "oos_rel_vs_baseline": rel,
            "boot_day_lo": boot_day["boot_lo"],
            "boot_day_hi": boot_day["boot_hi"],
            "boot_sym_day_lo": boot_sym_day["boot_lo"],
            "boot_sym_day_hi": boot_sym_day["boot_hi"],
            "stability_discovery": "; ".join(stab),
        }
        cand_rows.append(row)

        # OOS confirmation criteria
        confirmed = False
        reason = "insufficient"
        if rid not in selected:
            reason = "not_selected_discovery"
        elif o_rate["n"] < min_n_oos or d_rate["n"] < min_n_discovery:
            reason = "insufficient_n"
        elif d_rate["rate"] is None or o_rate["rate"] is None or b_oos["rate"] is None:
            reason = "null_rate"
        else:
            same_dir = (o_rate["rate"] - b_oos["rate"]) * (d_rate["rate"] - (b_disc["rate"] or 0)) > 0
            effect = abs(o_rate["rate"] - b_oos["rate"]) >= 0.03
            # not single-symbol driven: require >=2 symbols with n>=5 in OOS mask
            sub_o = oos.loc[rule["mask"](oos).fillna(False)]
            sym_ok = (sub_o.groupby("symbol").size() >= 5).sum() >= 2 if len(sub_o) else 0
            if same_dir and effect and sym_ok:
                confirmed = True
                reason = "oos_confirmed"
            elif not same_dir:
                reason = "direction_flip"
            elif not effect:
                reason = "small_effect"
            else:
                reason = "single_symbol_dominated"

        oos_rows.append(
            {
                "rule_id": rid,
                "confirmed_oos": confirmed,
                "reason": reason,
                "discovery_rate": d_rate["rate"],
                "validation_rate": v_rate["rate"],
                "oos_rate": o_rate["rate"],
                "oos_baseline_rate": b_oos["rate"],
                "oos_n": o_rate["n"],
            }
        )

    return pd.DataFrame(cand_rows), pd.DataFrame(oos_rows), selected
