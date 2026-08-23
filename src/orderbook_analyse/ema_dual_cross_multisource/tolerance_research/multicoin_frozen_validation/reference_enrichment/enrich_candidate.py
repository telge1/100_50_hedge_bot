"""Orchestrate pure causal enrichment for one candidate (no DB)."""

from __future__ import annotations

from typing import Any

import pandas as pd

from .causality import as_utc
from .ema_structure import compute_ema_structure_features
from .feature_value import flatten_features
from .flow_features import compute_flow_features
from .labels import extract_labels
from .lld_features import compute_lld_features
from .mfe_mae_labels import compute_mfe_mae_labels
from .oi_liq_features import compute_oi_liq_features
from .orderbook_features import compute_orderbook_features
from .price_atr import compute_price_atr_features
from .regime import compute_regime_features
from .time_base import compute_base_features


def enrich_candidate_row(
    candidate: dict[str, Any],
    trade: dict[str, Any],
    *,
    candles_5m: pd.DataFrame,
    candles_1m: pd.DataFrame | None = None,
    trades: pd.DataFrame | None = None,
    ob_1s: pd.DataFrame | None = None,
    oi_1m: pd.DataFrame | None = None,
    liq: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """Build one export row: feature__* + label__*. Never mutates candidate/trade."""
    direction = str(candidate.get("direction") or trade.get("direction"))
    decision_at = as_utc(candidate["decision_at"])

    feats = {}
    feats.update(compute_base_features(candidate, trade))
    feats.update(compute_price_atr_features(candles_5m, decision_at))
    feats.update(compute_ema_structure_features(candles_5m, decision_at, direction))
    feats.update(compute_regime_features(candles_5m, decision_at))
    feats.update(compute_orderbook_features(ob_1s, decision_at, direction))
    feats.update(compute_flow_features(trades, decision_at, direction))
    feats.update(compute_oi_liq_features(oi_1m, liq, decision_at))
    feats.update(compute_lld_features(decision_at))

    row: dict[str, Any] = {}
    row.update(flatten_features(feats))
    row.update(extract_labels(trade))
    row.update(compute_mfe_mae_labels(candles_1m, trade))
    row["candidate_id"] = trade.get("candidate_id") or candidate.get("candidate_id")
    row["symbol"] = str(candidate.get("symbol") or trade.get("symbol")).upper()
    row["setup_id"] = row["candidate_id"]
    return row


def coverage_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    from . import constants as C

    field_missing: dict[str, int] = {}
    ok_count = 0
    for row in rows:
        for k, v in row.items():
            if not k.startswith(C.FEATURE_PREFIX):
                continue
            if k.endswith("__coverage_status"):
                base = k[: -len("__coverage_status")]
                if v != "OK":
                    field_missing[base] = field_missing.get(base, 0) + 1
                else:
                    ok_count += 1
    return {"n_rows": len(rows), "n_feature_ok_cells": ok_count, "missing_by_field": field_missing}
