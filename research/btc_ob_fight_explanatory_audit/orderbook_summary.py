"""Summarize orderbook facts from run_017 for phase context."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from .config import RUN_017


def summarize_orderbook_from_run_017() -> dict[str, Any]:
    run = RUN_017
    wall = json.loads((run / "wall_summary.json").read_text()) if (run / "wall_summary.json").exists() else {}
    consumption = (
        json.loads((run / "consumption_metrics_detail.json").read_text())
        if (run / "consumption_metrics_detail.json").exists()
        else {}
    )
    profile_edge_ask = next(
        (
            r
            for r in consumption.get("rows", [])
            if r.get("scope") == "PROFILE_EDGE_ZONE" and r.get("side") == "ASK"
        ),
        {},
    )
    nearby = json.loads((run / "nearby_liquidity_increase_metrics.json").read_text()) if (run / "nearby_liquidity_increase_metrics.json").exists() else {}
    obs_rows = list(csv.DictReader((run / "edge_observability_detail.csv").open())) if (run / "edge_observability_detail.csv").exists() else []

    upper_visit_obs = [
        r for r in obs_rows
        if r.get("edge") == "UPPER" and r.get("time_context") == "EDGE_VISIT_ACTIVE"
    ]
    return {
        "source_run": str(run),
        "trade_associated_ask_decreases_profile_edge_zone": profile_edge_ask.get("trade_associated_count"),
        "trade_associated_ask_decreases_wall_total": (wall.get("trade_associated_decreases") or {}).get("ask"),
        "trade_associated_bid_decreases": (wall.get("trade_associated_decreases") or {}).get("bid"),
        "nearby_ask_increases": nearby.get("ask_count"),
        "nearby_bid_increases": nearby.get("bid_count"),
        "nearby_unknown": nearby.get("unknown_count"),
        "exact_refills": 0,
        "upper_edge_visit_observability": upper_visit_obs[:5],
        "note": "Phase-level OB breakdown requires timestamp join to wall transitions; aggregate counts from run_017 wall_summary",
    }


def orderbook_phase_summary_rows() -> list[dict[str, Any]]:
    ob = summarize_orderbook_from_run_017()
    return [
        {
            "phase": "FULL_ATTACK_WINDOW",
            "scope": "run_017_aggregate",
            "trade_associated_ask_decreases": ob.get("trade_associated_ask_decreases_profile_edge_zone"),
            "trade_associated_bid_decreases": ob.get("trade_associated_bid_decreases"),
            "nearby_ask_increases": ob.get("nearby_ask_increases"),
            "nearby_bid_increases": ob.get("nearby_bid_increases"),
            "observability_note": "Edge zone mostly PARTIALLY_OBSERVABLE / outside book in fight-time contexts",
        }
    ]
