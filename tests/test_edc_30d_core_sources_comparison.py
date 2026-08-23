"""Tests for 30d core-sources comparison."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from orderbook_analyse.ema_dual_cross_multisource.config import EMA_DUAL_CROSS_DEFAULTS
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.core_sources_research_policy import (
    apply_core_sources_research,
    apply_production_gate,
    assign_coverage_segment,
    core_research_policy_document,
)
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.mfe_mae import compute_mfe_mae_horizon
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.xrp_30d_core_sources_comparison_runner import (
    GROUP_MAP,
    MODE_IDS,
    TIMEFRAMES,
    _monotonic_ok,
)


def test_policy_version():
    assert core_research_policy_document()["policy_version"] == "AVAILABLE_CORE_SOURCES_RESEARCH_30D_V1"


def test_core_research_never_allow():
    cov = {
        "candles": {"status": "VALID"},
        "public_trades_cross": {"status": "VALID"},
        "orderbook_ob200_v3": {"status": "VALID"},
        "liquidity_locations": {"status": "VALID"},
    }
    v, _ = apply_core_sources_research(direction="BULLISH", features={}, coverage=cov)
    assert not v.startswith("ALLOW")
    assert v.startswith("CORE_RESEARCH_")


def test_missing_oi_not_neutral_in_segment():
    cov = {
        "candles": {"status": "VALID"},
        "public_trades_cross": {"status": "VALID"},
        "orderbook_ob200_v3": {"status": "VALID"},
        "liquidity_locations": {"status": "VALID"},
        "open_interest": {"status": "MISSING"},
        "liquidations": {"status": "MISSING"},
    }
    assert assign_coverage_segment(cov) == "CORE_FULL_OI_LIQ_MISSING"


def test_full_multisource_segment():
    cov = {
        "candles": {"status": "VALID"},
        "public_trades_cross": {"status": "VALID"},
        "orderbook_ob200_v3": {"status": "VALID"},
        "liquidity_locations": {"status": "VALID"},
        "open_interest": {"status": "VALID"},
        "liquidations": {"status": "VALID"},
    }
    assert assign_coverage_segment(cov) == "FULL_MULTISOURCE"


def test_core_incomplete_missing_ob():
    cov = {
        "candles": {"status": "VALID"},
        "public_trades_cross": {"status": "VALID"},
        "orderbook_ob200_v3": {"status": "MISSING"},
        "liquidity_locations": {"status": "VALID"},
    }
    assert assign_coverage_segment(cov) == "CORE_INCOMPLETE"
    v, _ = apply_core_sources_research(direction="BULLISH", features={}, coverage=cov)
    assert v == "CORE_RESEARCH_INSUFFICIENT"


def test_production_requires_oi_liq():
    cov = {
        "candles": {"status": "VALID"},
        "public_trades_cross": {"status": "VALID"},
        "orderbook_ob200_v3": {"status": "VALID"},
        "liquidity_locations": {"status": "VALID"},
        "open_interest": {"status": "MISSING"},
        "liquidations": {"status": "MISSING"},
        "coverage_gate": "INCONCLUSIVE_DATA",
    }
    v, reasons, _ = apply_production_gate(direction="BULLISH", features={}, coverage=cov)
    assert v == "INCONCLUSIVE_DATA"
    assert EMA_DUAL_CROSS_DEFAULTS.require_oi_for_allow is True


def test_mfe_monotonicity():
    t0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    rows = []
    for i in range(300):
        px = 1.0 + i * 0.0001
        rows.append(
            {
                "open_time": (t0 + timedelta(minutes=i)).replace(tzinfo=None),
                "open": px,
                "high": px + 0.002,
                "low": px - 0.001,
                "close": px,
                "volume": 1,
            }
        )
    df = pd.DataFrame(rows)
    entry = t0 + timedelta(minutes=5)
    row = {}
    for label, h in (("1h", 60), ("2h", 120), ("4h", 240)):
        oc = compute_mfe_mae_horizon(df, direction="BULLISH", entry_at=entry, entry_price=1.0, horizon_min=h)
        row[f"mfe_{label}_pct"] = oc["mfe_pct"]
        row[f"mae_{label}_pct"] = oc["mae_pct"]
    assert _monotonic_ok(row)


def test_mae_first_same_bar():
    t0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    df = pd.DataFrame(
        [
            {
                "open_time": t0.replace(tzinfo=None),
                "open": 100.0,
                "high": 100.3,
                "low": 99.7,
                "close": 100.0,
                "volume": 1,
            }
        ]
    )
    oc = compute_mfe_mae_horizon(df, direction="BULLISH", entry_at=t0, entry_price=100.0, horizon_min=15)
    assert oc["first_extreme"] == "MAE_FIRST"


def test_groups_disjoint_core_verdicts():
    c = {"core_research_verdict": "CORE_RESEARCH_SUPPORTIVE", "coverage_segment": "CORE_FULL_OI_LIQ_MISSING", "production_gate_verdict": "INCONCLUSIVE_DATA"}
    assert GROUP_MAP["CORE_RESEARCH_SUPPORTIVE"](c)
    assert not GROUP_MAP["CORE_RESEARCH_ADVERSE"](c)


def test_frozen_mode_timeframes():
    assert len(MODE_IDS) == 3
    assert TIMEFRAMES == ("5m", "15m", "30m")
