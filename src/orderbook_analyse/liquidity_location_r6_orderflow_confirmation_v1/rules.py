"""Transparent R6 confirmation rule candidates + OOS evaluation."""

from __future__ import annotations

from typing import Any, Callable

import numpy as np
import pandas as pd

from orderbook_analyse.liquidity_location_pool_edge_validation_v2.stats import (
    block_bootstrap_rate,
    wilson_interval,
)

RuleFn = Callable[[pd.DataFrame], pd.Series]


def rule_defs() -> list[dict[str, Any]]:
    return [
        {
            "rule_id": "R6_impact_compression",
            "definition": "R6 & impact_compression_flag at T3",
            "mask": lambda d: d["impact_compression_flag"].fillna(False),
            "predicts": "DEFENDED",
        },
        {
            "rule_id": "R6_depth_replenishment",
            "definition": "R6 & depth_replenishment_flag at T3",
            "mask": lambda d: d["depth_replenishment_flag"].fillna(False),
            "predicts": "DEFENDED",
        },
        {
            "rule_id": "R6_flow_flip",
            "definition": "R6 & flow_flip_flag at T3",
            "mask": lambda d: d["flow_flip_flag"].fillna(False),
            "predicts": "DEFENDED",
        },
        {
            "rule_id": "R6_near_edge_reclaim",
            "definition": "R6 & near_edge_reclaim at T3",
            "mask": lambda d: d["near_edge_reclaim"].fillna(False),
            "predicts": "DEFENDED",
        },
        {
            "rule_id": "R6_liq_flush",
            "definition": "R6 & liq_flush_toward_pool (only when liq VALID)",
            "mask": lambda d: (d["liq_status"] == "VALID") & d["liq_flush_toward_pool"].fillna(False),
            "predicts": "SWEPT_RECLAIMED",
        },
        {
            "rule_id": "R6_oi_drop",
            "definition": "R6 & oi_drop_on_sweep (only when oi VALID)",
            "mask": lambda d: (d["oi_status"] == "VALID") & d["oi_drop_on_sweep"].fillna(False),
            "predicts": "CONSUMED_ACCEPTED",
        },
        {
            "rule_id": "R6_replenish_flow_flip",
            "definition": "R6 & depth_replenishment & flow_flip",
            "mask": lambda d: d["depth_replenishment_flag"].fillna(False) & d["flow_flip_flag"].fillna(False),
            "predicts": "DEFENDED",
        },
        {
            "rule_id": "R6_impact_compression_reclaim",
            "definition": "R6 & impact_compression & near_edge_reclaim",
            "mask": lambda d: d["impact_compression_flag"].fillna(False) & d["near_edge_reclaim"].fillna(False),
            "predicts": "DEFENDED",
        },
        {
            "rule_id": "R6_wall_persistence",
            "definition": "R6 & wall_persistence_proxy",
            "mask": lambda d: d["wall_persistence_proxy"].fillna(False),
            "predicts": "DEFENDED",
        },
        {
            "rule_id": "R6_strong_consumption_no_replenish",
            "definition": "R6 & depth_depletion & ~replenishment → CONSUMED_ACCEPTED",
            "mask": lambda d: d["depth_depletion_flag"].fillna(False)
            & ~d["depth_replenishment_flag"].fillna(False),
            "predicts": "CONSUMED_ACCEPTED",
        },
        {
            "rule_id": "R6_absorption",
            "definition": "R6 & absorption_flag at touch",
            "mask": lambda d: d["absorption_flag"].fillna(False),
            "predicts": "DEFENDED",
        },
        {
            "rule_id": "R6_book_flip",
            "definition": "R6 & book_flip_toward_defense",
            "mask": lambda d: d["book_flip_toward_defense"].fillna(False),
            "predicts": "DEFENDED",
        },
    ]


def _precision_recall(sub: pd.DataFrame, predicts: str) -> dict[str, Any]:
    n = len(sub)
    if n == 0:
        return {
            "n": 0,
            "precision": None,
            "recall": None,
            "defense_precision": None,
            "sweep_reclaim_precision": None,
            "consume_precision": None,
        }
    y = sub["label_primary"].astype(str)
    prec = float((y == predicts).mean())
    # class-wise precision among selected
    return {
        "n": n,
        "precision": prec,
        "defense_precision": float((y == "DEFENDED").mean()),
        "sweep_reclaim_precision": float((y == "SWEPT_RECLAIMED").mean()),
        "consume_precision": float((y == "CONSUMED_ACCEPTED").mean()),
        "count_match": int((y == predicts).sum()),
    }


def evaluate_rules(
    feat: pd.DataFrame,
    *,
    r6_defense_baseline: float,
    single_defense_baseline: float = 0.059,
    min_n_discovery: int = 20,
    min_n_oos: int = 12,
    n_boot: int = 200,
) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    """Select on discovery only; confirm on OOS once."""
    rules = rule_defs()
    cand_rows = []
    oos_rows = []
    selected = []

    disc = feat[feat["temporal_split"] == "discovery"]
    val = feat[feat["temporal_split"] == "validation"]
    oos = feat[feat["temporal_split"] == "oos"]

    for rule in rules:
        rid = rule["rule_id"]
        predicts = rule["predicts"]

        def _eval(split_df: pd.DataFrame) -> dict[str, Any]:
            m = rule["mask"](split_df)
            sub = split_df.loc[m.fillna(False)]
            pr = _precision_recall(sub, predicts)
            # recall among all positives in split
            pos = split_df[split_df["label_primary"] == predicts]
            recall = None
            if len(pos):
                recall = float(rule["mask"](pos).fillna(False).mean())
            pr["recall"] = recall
            return pr

        d = _eval(disc)
        v = _eval(val)
        o = _eval(oos)
        if d["n"] >= min_n_discovery:
            selected.append(rid)

        # lift vs R6 baseline defense rate (for DEFENDED rules)
        baseline = r6_defense_baseline if predicts == "DEFENDED" else None
        oos_lift = None
        oos_lift_single = None
        if o["precision"] is not None and predicts == "DEFENDED":
            oos_lift = o["precision"] - r6_defense_baseline
            oos_lift_single = o["precision"] - single_defense_baseline

        boot = block_bootstrap_rate(
            feat.loc[rule["mask"](feat).fillna(False)].assign(
                _hit=lambda x: x["label_primary"] == predicts
            ),
            success_col="_hit",
            block_cols=["utc_day"],
            n_boot=n_boot,
            seed=hash(rid) % 10000,
        ) if len(feat.loc[rule["mask"](feat).fillna(False)]) else {
            "rate": None,
            "boot_lo": None,
            "boot_hi": None,
            "n": 0,
        }

        # symbol / side / tf stability on discovery
        stab = []
        sub_d = disc.loc[rule["mask"](disc).fillna(False)]
        for col in ("symbol", "side", "timeframe"):
            if col in sub_d.columns and len(sub_d):
                for k, g in sub_d.groupby(col):
                    if len(g) >= 5:
                        stab.append(f"{col}={k}:{float((g['label_primary']==predicts).mean()):.2f}(n={len(g)})")

        cand_rows.append(
            {
                "rule_id": rid,
                "definition": rule["definition"],
                "predicts": predicts,
                "decision_at": "T3_primary_window",
                "selected_on_discovery": rid in selected,
                "discovery_n": d["n"],
                "discovery_precision": d["precision"],
                "discovery_defense_precision": d["defense_precision"],
                "discovery_sweep_reclaim_precision": d["sweep_reclaim_precision"],
                "discovery_consume_precision": d["consume_precision"],
                "discovery_recall": d["recall"],
                "validation_n": v["n"],
                "validation_precision": v["precision"],
                "oos_n": o["n"],
                "oos_precision": o["precision"],
                "oos_defense_precision": o["defense_precision"],
                "oos_sweep_reclaim_precision": o["sweep_reclaim_precision"],
                "oos_consume_precision": o["consume_precision"],
                "oos_recall": o["recall"],
                "oos_lift_vs_r6_defense_baseline": oos_lift,
                "oos_lift_vs_single_baseline": oos_lift_single,
                "r6_defense_baseline": r6_defense_baseline,
                "single_defense_baseline": single_defense_baseline,
                "boot_day_lo": boot.get("boot_lo"),
                "boot_day_hi": boot.get("boot_hi"),
                "stability_discovery": "; ".join(stab[:12]),
            }
        )

        confirmed = False
        reason = "not_selected"
        if rid not in selected:
            reason = "not_selected_discovery"
        elif o["n"] < min_n_oos:
            reason = "insufficient_oos_n"
        elif d["precision"] is None or o["precision"] is None:
            reason = "null_precision"
        else:
            # same direction vs R6 baseline for DEFENDED; for other labels require precision>baseline class rate
            class_base = float((disc["label_primary"] == predicts).mean()) if len(disc) else 0.0
            same_dir = (o["precision"] - class_base) * (d["precision"] - class_base) > 0
            effect = abs(o["precision"] - class_base) >= 0.05
            sub_o = oos.loc[rule["mask"](oos).fillna(False)]
            sym_ok = int((sub_o.groupby("symbol").size() >= 3).sum() >= 2) if len(sub_o) else 0
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
                "discovery_precision": d["precision"],
                "validation_precision": v["precision"],
                "oos_precision": o["precision"],
                "oos_n": o["n"],
                "predicts": predicts,
            }
        )

    return pd.DataFrame(cand_rows), pd.DataFrame(oos_rows), selected
