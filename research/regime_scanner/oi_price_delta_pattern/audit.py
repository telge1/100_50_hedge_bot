"""Orchestrate OI/price/delta pattern audit (read-only)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import pandas as pd

from research.regime_scanner.liquidation_exhaustion.loader import load_joined_5m
from research.regime_scanner.oi_price_delta_pattern.compare import (
    coin_summary,
    decide,
    direction_summary,
    pattern_comparisons,
    summarize_patterns,
)
from research.regime_scanner.oi_price_delta_pattern.config import PatternConfig, default_config
from research.regime_scanner.oi_price_delta_pattern.features import compute_feature_rows
from research.regime_scanner.oi_price_delta_pattern.outcomes import compute_outcomes
from research.regime_scanner.oi_price_delta_pattern.states import assignment_rows

logger = logging.getLogger(__name__)


def run_symbol(df: pd.DataFrame, cfg: PatternConfig) -> dict[str, Any]:
    feats = compute_feature_rows(df, cfg.lookbacks)
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
    cfg: PatternConfig | None = None,
) -> dict[str, Any]:
    cfg = cfg or default_config()
    joined = load_joined_5m(
        symbols=symbols, start=start, end=end, import_version=cfg.import_version
    )
    # build coverage like OICB
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
    cov = {
        "joined_rows": int(len(joined)),
        "symbols": sorted(joined["symbol"].unique().tolist()) if len(joined) else [],
        "by_symbol": by_symbol_cov,
    }

    all_feat: list[dict[str, Any]] = []
    all_asg: list[dict[str, Any]] = []
    all_oc: list[dict[str, Any]] = []
    by_symbol: dict[str, Any] = {}

    for sym in symbols:
        g = joined[joined["symbol"] == sym].sort_values("bucket_start").reset_index(drop=True) if len(joined) else pd.DataFrame()
        if g.empty:
            by_symbol[sym] = {"joined_rows": 0, "anchors": 0}
            continue
        logger.info("symbol=%s rows=%s", sym, len(g))
        res = run_symbol(g, cfg)
        all_feat.extend(res["features"])
        all_asg.extend(res["assignments"])
        all_oc.extend(res["outcomes"])
        by_symbol[sym] = {"joined_rows": int(len(g)), "anchors": int(res["n_anchors"])}

    summary = summarize_patterns(all_asg, all_oc, cfg)
    comparisons = pattern_comparisons(all_asg, all_oc, all_feat, cfg)
    coins = coin_summary(all_asg, all_oc, cfg)
    directions = direction_summary(summary)
    decision, rationale = decide(summary, comparisons, cfg)

    # pattern counts P1-P6
    asg_df = pd.DataFrame(all_asg)
    pattern_counts = {}
    if not asg_df.empty:
        primary = asg_df[~asg_df["pattern"].astype(str).str.startswith("COMBO::")]
        pattern_counts = primary["pattern"].value_counts().to_dict()

    return {
        "coverage": cov,
        "joined_rows": int(len(joined)),
        "features": all_feat,
        "assignments": all_asg,
        "outcomes": all_oc,
        "summary": summary.to_dict(orient="records") if not summary.empty else [],
        "comparisons": comparisons.to_dict(orient="records") if not comparisons.empty else [],
        "coin_summary": coins.to_dict(orient="records") if not coins.empty else [],
        "direction_summary": directions.to_dict(orient="records") if not directions.empty else [],
        "pattern_counts": pattern_counts,
        "n_feature_rows": len(all_feat),
        "by_symbol": by_symbol,
        "decision": decision,
        "decision_rationale": rationale,
        "config_hash": cfg.config_hash(),
        "cfg": cfg.to_dict(),
        "db_writes": False,
    }
