"""Orchestrate orderflow absorption audit (read-only)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import pandas as pd

from research.regime_scanner.liquidation_exhaustion.loader import load_joined_5m
from research.regime_scanner.orderflow_absorption.compare import (
    coin_summary,
    control_comparisons,
    decide,
    lookback_summary,
    oi_diagnostic,
    summarize,
)
from research.regime_scanner.orderflow_absorption.config import AbsorptionConfig, default_config
from research.regime_scanner.orderflow_absorption.features import compute_feature_rows
from research.regime_scanner.orderflow_absorption.outcomes import compute_outcomes
from research.regime_scanner.orderflow_absorption.patterns import assignment_rows

logger = logging.getLogger(__name__)


def run_symbol(df: pd.DataFrame, cfg: AbsorptionConfig) -> dict[str, Any]:
    feats = compute_feature_rows(df, cfg)
    stated, assigns = assignment_rows(feats, cfg)
    outcomes = compute_outcomes(df, feats, cfg)
    return {
        "features": stated,
        "assignments": assigns,
        "outcomes": outcomes,
        "n_anchors": len(feats),
    }


def run_audit(
    *,
    symbols: list[str],
    start: datetime,
    end: datetime,
    cfg: AbsorptionConfig | None = None,
) -> dict[str, Any]:
    cfg = cfg or default_config()
    joined = load_joined_5m(
        symbols=symbols, start=start, end=end, import_version=cfg.import_version
    )
    by_symbol_cov = []
    if not joined.empty:
        for sym, g in joined.groupby("symbol", sort=True):
            by_symbol_cov.append(
                {
                    "symbol": str(sym),
                    "joined_rows": int(len(g)),
                    "min_bucket": str(g["bucket_start"].min()),
                    "max_bucket": str(g["bucket_start"].max()),
                }
            )
    cov = {"joined_rows": int(len(joined)), "by_symbol": by_symbol_cov}

    all_feat: list[dict[str, Any]] = []
    all_asg: list[dict[str, Any]] = []
    all_oc: list[dict[str, Any]] = []
    by_symbol: dict[str, Any] = {}

    for sym in symbols:
        g = (
            joined[joined["symbol"] == sym].sort_values("bucket_start").reset_index(drop=True)
            if len(joined)
            else pd.DataFrame()
        )
        if g.empty:
            by_symbol[sym] = {"joined_rows": 0, "anchors": 0}
            continue
        logger.info("symbol=%s rows=%s", sym, len(g))
        res = run_symbol(g, cfg)
        all_feat.extend(res["features"])
        all_asg.extend(res["assignments"])
        all_oc.extend(res["outcomes"])
        by_symbol[sym] = {"joined_rows": int(len(g)), "anchors": int(res["n_anchors"])}

    summary = summarize(all_asg, all_oc, cfg)
    comparisons = control_comparisons(all_asg, all_oc, cfg)
    coins = coin_summary(all_asg, all_oc, cfg)
    lbs = lookback_summary(summary)
    oi_diag = oi_diagnostic(all_asg, all_oc, cfg)
    decision, rationale = decide(summary, comparisons, cfg)

    asg_df = pd.DataFrame(all_asg)
    pattern_counts: dict[str, int] = {}
    pattern_counts_f1: dict[str, int] = {}
    if not asg_df.empty:
        pattern_counts = asg_df.groupby("pattern").size().astype(int).to_dict()
        f1 = asg_df[asg_df["flow_rule"].isin(["F1", "ALL"])]
        pattern_counts_f1 = f1.groupby("pattern").size().astype(int).to_dict()

    return {
        "coverage": cov,
        "joined_rows": int(len(joined)),
        "features": all_feat,
        "assignments": all_asg,
        "outcomes": all_oc,
        "summary": summary.to_dict(orient="records") if not summary.empty else [],
        "comparisons": comparisons.to_dict(orient="records") if not comparisons.empty else [],
        "coin_summary": coins.to_dict(orient="records") if not coins.empty else [],
        "lookback_summary": lbs.to_dict(orient="records") if not lbs.empty else [],
        "oi_diagnostic": oi_diag.to_dict(orient="records") if not oi_diag.empty else [],
        "pattern_counts": pattern_counts,
        "pattern_counts_f1": pattern_counts_f1,
        "n_feature_rows": len(all_feat),
        "by_symbol": by_symbol,
        "decision": decision,
        "decision_rationale": rationale,
        "config_hash": cfg.config_hash(),
        "cfg": cfg.to_dict(),
        "db_writes": False,
    }
