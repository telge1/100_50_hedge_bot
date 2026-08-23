"""Time / identity / existing verdict base fields (not market-derived numerics)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..diagnostics_analysis import coin_bucket
from .causality import as_utc
from .feature_value import FeatureValue, missing, ok


def _session(hour: int) -> str:
    if 0 <= hour < 8:
        return "ASIA"
    if 8 <= hour < 16:
        return "EU"
    return "US"


def compute_base_features(candidate: dict[str, Any], trade: dict[str, Any]) -> dict[str, FeatureValue]:
    dec = as_utc(candidate["decision_at"])
    hour = dec.hour
    feats: dict[str, FeatureValue] = {}

    def lit(name: str, value: Any, source: str = "checkpoint") -> FeatureValue:
        if value is None or value == "":
            return missing(name, reason="ABSENT_IN_CHECKPOINT", status="MISSING", source=source, asof=dec)
        return ok(name, value, asof=dec, window_start=None, window_end=None, source=source)

    feats["symbol"] = lit("symbol", str(candidate.get("symbol") or trade.get("symbol")).upper())
    feats["candidate_id"] = lit("candidate_id", candidate.get("candidate_id") or trade.get("candidate_id"))
    feats["cross_episode_id"] = lit("cross_episode_id", candidate.get("cross_episode_id") or trade.get("cross_episode_id"))
    feats["direction"] = lit("direction", candidate.get("direction") or trade.get("direction"))
    feats["decision_at"] = lit("decision_at", as_utc(candidate["decision_at"]).isoformat())
    feats["entry_at"] = lit("entry_at", trade.get("entry_at") or candidate.get("entry_at"))
    feats["utc_hour"] = ok("utc_hour", hour, asof=dec, window_start=None, window_end=None, source="derived")
    feats["utc_day_of_week"] = ok("utc_day_of_week", dec.weekday(), asof=dec, window_start=None, window_end=None, source="derived")
    feats["session_bucket"] = ok("session_bucket", _session(hour), asof=dec, window_start=None, window_end=None, source="derived")
    feats["coin_bucket"] = ok(
        "coin_bucket",
        coin_bucket(str(candidate.get("symbol") or trade.get("symbol"))),
        asof=dec,
        window_start=None,
        window_end=None,
        source="diagnostics_analysis.coin_bucket",
    )

    verdict_map = {
        "existing_trade_flow_verdict": "trade_flow_verdict",
        "existing_orderbook_verdict": "orderbook_verdict",
        "existing_liquidity_verdict": "liquidity_location_verdict",
        "existing_volatility_verdict": "volatility_verdict",
        "existing_fake_impulse_verdict": "fake_impulse_verdict",
        "existing_core_research_verdict": "core_research_verdict",
        "production_gate_verdict": "production_gate_verdict",
        "coverage_segment": "coverage_segment",
    }
    for feat_name, src_key in verdict_map.items():
        feats[feat_name] = lit(feat_name, candidate.get(src_key))
    return feats
