"""Orchestrate Level-Context × Orderflow Absorption V1 audit (read-only)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import pandas as pd

from research.regime_scanner.liquidation_exhaustion.loader import load_joined_5m
from research.regime_scanner.orderflow_absorption.config import AbsorptionConfig
from research.regime_scanner.orderflow_absorption.features import compute_feature_rows, enrich_frame
from research.regime_scanner.orderflow_absorption.patterns import assignment_rows
from research.regime_scanner.orderflow_absorption_level.config import LevelAbsorptionConfig, default_config
from research.regime_scanner.orderflow_absorption_level.confirmations import build_confirmation_events
from research.regime_scanner.orderflow_absorption_level.controls import (
    build_control_assignments,
    build_flow_control_events_from_assignments,
    build_k2_touch_events,
    build_treatment_assignments,
    compute_atr_tercile_edges,
    match_control_pairs,
)
from research.regime_scanner.orderflow_absorption_level.events import build_absorption_level_events
from research.regime_scanner.orderflow_absorption_level.level_assign import assign_levels_to_anchors
from research.regime_scanner.orderflow_absorption_level.levels_build import build_level_inventory
from research.regime_scanner.orderflow_absorption_level.outcomes_level import compute_event_outcomes
from research.regime_scanner.orderflow_absorption_level.summarize import (
    coin_summary,
    confirmation_summary,
    control_comparison,
    decide,
    distance_bucket_summary,
    equal_coin_summary,
    event_summary,
    level_type_summary,
    median_coin_summary,
    treatment_summary,
)

logger = logging.getLogger(__name__)


def _absorption_cfg(cfg: LevelAbsorptionConfig) -> AbsorptionConfig:
    return AbsorptionConfig(
        lookbacks=cfg.lookbacks,
        horizons=cfg.horizons,
        move_thresholds=cfg.move_thresholds,
        f1_abs=cfg.f1_abs,
        normal_progress_abs=cfg.normal_progress_abs,
        weak_progress_abs=cfg.weak_progress_abs,
        import_version=cfg.import_version,
    )


def _filter_pattern_assignments(
    assigns: list[dict[str, Any]],
    cfg: LevelAbsorptionConfig,
    *,
    patterns: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    pats = patterns if patterns is not None else cfg.patterns
    out = []
    for a in assigns:
        if str(a.get("pattern")) not in pats:
            continue
        if str(a.get("flow_rule")) not in cfg.flow_rules:
            continue
        if int(a.get("lookback") or 0) not in cfg.lookbacks:
            continue
        out.append(a)
    return out


def run_symbol(df: pd.DataFrame, cfg: LevelAbsorptionConfig) -> dict[str, Any]:
    symbol = str(df["symbol"].iloc[0])
    abs_cfg = _absorption_cfg(cfg)
    enriched = enrich_frame(df, abs_cfg)
    feats = compute_feature_rows(enriched, abs_cfg)
    _stated, assigns = assignment_rows(feats, abs_cfg)

    inventory = build_level_inventory(enriched, symbol=symbol, cfg=cfg)

    # Primary patterns + K3 controls (C1/C2)
    focus_patterns = tuple(dict.fromkeys([*cfg.patterns, "C1", "C2"]))
    focus_assigns = _filter_pattern_assignments(assigns, cfg, patterns=focus_patterns)
    # Also need C1/C2 with F1 + lookback — filter already applies flow/lookback

    level_asg = assign_levels_to_anchors(enriched, focus_assigns, inventory, cfg)

    abs_level_asg = [a for a in level_asg if str(a["pattern"]) in cfg.patterns]
    events = build_absorption_level_events(
        enriched, abs_level_asg, patterns=cfg.patterns, cfg=cfg
    )

    # K3 events
    c2_events = build_flow_control_events_from_assignments(level_asg, enriched, pattern="C2", cfg=cfg)
    c1_events = build_flow_control_events_from_assignments(level_asg, enriched, pattern="C1", cfg=cfg)

    # K2: level touch without A4/A2
    a4_idx = {int(a["anchor_index"]) for a in abs_level_asg if a["pattern"] == "A4"}
    a2_idx = {int(a["anchor_index"]) for a in abs_level_asg if a["pattern"] == "A2"}
    k2_support = build_k2_touch_events(
        enriched, inventory, a4_idx, side="support", pattern_label="K2_SUPPORT", cfg=cfg, symbol=symbol
    )
    k2_resist = build_k2_touch_events(
        enriched, inventory, a2_idx, side="resistance", pattern_label="K2_RESISTANCE", cfg=cfg, symbol=symbol
    )

    treatments = build_treatment_assignments(events)
    controls = build_control_assignments(
        events,
        c2_support_events=c2_events,
        c1_resistance_events=c1_events,
        k2_support_events=k2_support,
        k2_resistance_events=k2_resist,
    )

    confirmations = build_confirmation_events(enriched, events, cfg)
    # also outcomes for K3/K2 control events as R0 only
    ctrl_conf: list[dict[str, Any]] = []
    for ev in c2_events + c1_events + k2_support + k2_resist:
        ctrl_conf.append(
            {
                **ev,
                "confirmation_type": "R0",
                "confirmation_id": f"{ev['event_id']}|R0",
                "confirmation_ok": True,
                "confirmation_reason": "control_r0",
            }
        )
    outcomes = compute_event_outcomes(enriched, confirmations + ctrl_conf, cfg)

    return {
        "symbol": symbol,
        "enriched": enriched,
        "level_inventory": inventory,
        "anchor_level_assignments": abs_level_asg,
        "events": events,
        "treatment_assignments": treatments,
        "control_assignments": controls,
        "confirmation_events": confirmations,
        "outcomes": outcomes,
        "c2_events": c2_events,
        "c1_events": c1_events,
        "k2_support": k2_support,
        "k2_resist": k2_resist,
        "n_feats": len(feats),
    }


def run_audit(
    *,
    symbols: list[str],
    start: datetime,
    end: datetime,
    cfg: LevelAbsorptionConfig | None = None,
) -> dict[str, Any]:
    cfg = cfg or default_config()
    joined = load_joined_5m(
        symbols=symbols, start=start, end=end, import_version=cfg.import_version
    )
    cov_rows = []
    if not joined.empty:
        for sym, g in joined.groupby("symbol", sort=True):
            cov_rows.append(
                {
                    "symbol": str(sym),
                    "joined_rows": int(len(g)),
                    "min_bucket": str(g["bucket_start"].min()),
                    "max_bucket": str(g["bucket_start"].max()),
                }
            )

    all_inv: list[dict[str, Any]] = []
    all_asg: list[dict[str, Any]] = []
    all_events: list[dict[str, Any]] = []
    all_treat: list[dict[str, Any]] = []
    all_ctrl: list[dict[str, Any]] = []
    all_conf: list[dict[str, Any]] = []
    all_oc: list[dict[str, Any]] = []
    treat_for_match: list[dict[str, Any]] = []
    ctrl_for_match: list[dict[str, Any]] = []
    df_by_symbol: dict[str, pd.DataFrame] = {}
    atr_edges: dict[str, tuple[float, float] | None] = {}

    for sym in symbols:
        g = (
            joined[joined["symbol"] == sym].sort_values("bucket_start").reset_index(drop=True)
            if len(joined)
            else pd.DataFrame()
        )
        if g.empty:
            logger.info("symbol=%s rows=0", sym)
            continue
        logger.info("symbol=%s rows=%s", sym, len(g))
        res = run_symbol(g, cfg)
        df_by_symbol[sym] = res["enriched"]
        atr_edges[sym] = compute_atr_tercile_edges(res["enriched"])
        all_inv.extend(res["level_inventory"])
        all_asg.extend(res["anchor_level_assignments"])
        all_events.extend(res["events"])
        all_treat.extend(res["treatment_assignments"])
        all_ctrl.extend(res["control_assignments"])
        all_conf.extend(res["confirmation_events"])
        all_oc.extend(res["outcomes"])

        for ev in res["events"]:
            labs = [t["treatment"] for t in res["treatment_assignments"] if t["event_id"] == ev["event_id"]]
            if "A4_AT_ANY_SUPPORT" in labs or "A2_AT_ANY_RESISTANCE" in labs:
                treat_for_match.append(ev)
            if "A4_NO_SUPPORT" in labs or "A2_NO_RESISTANCE" in labs or "A4_FAR_FROM_SUPPORT" in labs or "A2_FAR_FROM_RESISTANCE" in labs:
                ctrl_for_match.append(ev)

    pairs, unmatched = match_control_pairs(
        treat_for_match, ctrl_for_match, df_by_symbol=df_by_symbol, atr_edges_by_symbol=atr_edges
    )

    oc_df = pd.DataFrame(all_oc)
    ev_sum = event_summary(oc_df, cfg)
    treat_sum = treatment_summary(oc_df, all_events, cfg)
    ctrl_cmp = control_comparison(oc_df, all_events, cfg)
    lt_sum = level_type_summary(oc_df, cfg)
    dist_sum = distance_bucket_summary(oc_df, cfg)
    conf_sum = confirmation_summary(oc_df, cfg)
    coins = coin_summary(oc_df, all_events, cfg)
    eq = equal_coin_summary(coins)
    med = median_coin_summary(coins)

    decision, rationale = decide(
        treatment_df=treat_sum,
        comparison_df=ctrl_cmp,
        confirmation_df=conf_sum,
        coin_df=coins,
        level_type_df=lt_sum,
        cfg=cfg,
    )

    levels_by_type: dict[str, int] = {}
    if all_inv:
        for r in all_inv:
            levels_by_type[str(r["level_type"])] = levels_by_type.get(str(r["level_type"]), 0) + 1

    conf_counts: dict[str, int] = {}
    for c in all_conf:
        conf_counts[str(c["confirmation_type"])] = conf_counts.get(str(c["confirmation_type"]), 0) + 1

    event_counts = {
        "total": len(all_events),
        "by_pattern": {},
    }
    for e in all_events:
        p = str(e["pattern"])
        event_counts["by_pattern"][p] = event_counts["by_pattern"].get(p, 0) + 1

    cfg_dict = cfg.to_dict()
    cfg_dict["symbols"] = tuple(symbols)

    return {
        "cfg": cfg_dict,
        "config_hash": cfg.config_hash(),
        "joined_rows": int(len(joined)),
        "coverage_rows": cov_rows,
        "level_inventory": all_inv,
        "anchor_level_assignments": all_asg,
        "events": all_events,
        "treatment_assignments": all_treat,
        "control_assignments": all_ctrl,
        "matched_control_pairs": pairs + unmatched,
        "confirmation_events": all_conf,
        "outcomes": all_oc,
        "event_summary": ev_sum.to_dict(orient="records") if not ev_sum.empty else [],
        "treatment_summary": treat_sum.to_dict(orient="records") if not treat_sum.empty else [],
        "control_comparison": ctrl_cmp.to_dict(orient="records") if not ctrl_cmp.empty else [],
        "level_type_summary": lt_sum.to_dict(orient="records") if not lt_sum.empty else [],
        "distance_bucket_summary": dist_sum.to_dict(orient="records") if not dist_sum.empty else [],
        "confirmation_summary": conf_sum.to_dict(orient="records") if not conf_sum.empty else [],
        "coin_summary": coins.to_dict(orient="records") if not coins.empty else [],
        "equal_coin_summary": eq.to_dict(orient="records") if not eq.empty else [],
        "median_coin_summary": med.to_dict(orient="records") if not med.empty else [],
        "n_levels": len(all_inv),
        "levels_by_type": levels_by_type,
        "n_events": len(all_events),
        "event_counts": event_counts,
        "confirmation_counts": conf_counts,
        "outcome_counts": {"total": len(all_oc)},
        "row_counts": {
            "joined": int(len(joined)),
            "levels": len(all_inv),
            "anchor_assignments": len(all_asg),
            "events": len(all_events),
            "confirmations": len(all_conf),
            "outcomes": len(all_oc),
        },
        "decision": decision,
        "decision_rationale": rationale,
        "recommendation": (
            "Full 3-coin run is prepared; do not start until smoke integrity is green."
            if decision
            else ""
        ),
        "db_writes": False,
        "absorption_unchanged": True,
        "known_leakage": False,
        "causality_flags": {
            "confirmation_strict_lt_anchor": True,
            "atr_reference_t_minus_1": True,
            "outcomes_from_entry_plus_1": True,
            "no_future_level_state_rewrite": True,
            "sequence_gap_resets_levels": True,
        },
    }
