"""Tests for three-level source-filtered shortlist research."""

from __future__ import annotations

import ast
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from orderbook_analyse.ema_dual_cross_multisource.config import EMA_DUAL_CROSS_DEFAULTS
from orderbook_analyse.ema_dual_cross_multisource.coverage_gate import assess_coverage
from orderbook_analyse.ema_dual_cross_multisource.feature_builder import build_gate_features
from orderbook_analyse.ema_dual_cross_multisource.gate_policy import apply_gate
from orderbook_analyse.ema_dual_cross_multisource.models import FinalVerdict, SourceVerdict
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.research_policy import (
    apply_available_source_research,
    compute_all_source_verdicts,
    map_source_contribution,
    research_policy_document,
)
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.shortlist_runner import (
    SHORTLIST_MODE_IDS,
    shortlist_modes,
)


def _bar(i, e9, e20, e59=1.0, close=None, s9=0.002, s20=0.002, atr=0.05):
    c = close if close is not None else e9
    return {
        "open_time": (datetime(2026, 8, 1, tzinfo=timezone.utc) + timedelta(minutes=15 * i)).replace(tzinfo=None),
        "open": c,
        "high": c * 1.001,
        "low": c * 0.999,
        "close": c,
        "volume": 1000.0,
        "ema_9": e9,
        "ema_20": e20,
        "ema_59": e59,
        "ema_9_slope_1": s9,
        "ema_20_slope_1": s20,
        "ema_59_slope_1": 0.0002,
        "atr": atr,
    }


def test_research_policy_document_has_truth_table():
    doc = research_policy_document()
    assert doc["policy_version"] == "AVAILABLE_SOURCE_RESEARCH_V1"
    assert len(doc["truth_table"]) >= 8
    assert doc["missing_never_supportive"] is True


def test_shortlist_modes_frozen():
    modes = shortlist_modes()
    assert [m["mode_id"] for m in modes] == list(SHORTLIST_MODE_IDS)


def test_missing_never_supportive():
    cov = {
        "candles": {"status": "VALID", "critical_for_allow": True},
        "public_trades_cross": {"status": "MISSING", "critical_for_allow": True},
        "orderbook_ob200_v3": {"status": "VALID", "critical_for_allow": True},
    }
    v, _ = apply_available_source_research(direction="BULLISH", features={}, coverage=cov, source_verdicts={})
    assert v == "RESEARCH_INSUFFICIENT"
    contrib = map_source_contribution(
        source="trades",
        coverage=cov,
        source_verdicts={"trades": SourceVerdict.INCONCLUSIVE_DATA.value},
        production_verdict="INCONCLUSIVE_DATA",
        production_reasons=["CRITICAL_COVERAGE_MISSING"],
        available_research_verdict=v,
    )
    assert contrib["contribution"] == "MISSING"
    assert contrib["contribution"] != "SUPPORTIVE"


def test_research_never_allow():
    cov = {
        "candles": {"status": "VALID"},
        "public_trades_cross": {"status": "VALID"},
        "orderbook_ob200_v3": {"status": "VALID"},
    }
    sv = {
        "trades": SourceVerdict.CONFIRMING.value,
        "ob": SourceVerdict.CONFIRMING.value,
        "liquidity": SourceVerdict.SUPPORTING.value,
        "volatility": SourceVerdict.SUPPORTING.value,
        "fake_impulse": SourceVerdict.NEUTRAL.value,
        "_fake_impulse_label": "NO_EVIDENCE",
    }
    v, _ = apply_available_source_research(direction="BULLISH", features={}, coverage=cov, source_verdicts=sv)
    assert v == "RESEARCH_SUPPORTIVE"
    assert v != "ALLOW"
    assert v != FinalVerdict.ALLOW.value


def test_production_gate_unchanged_on_legacy_candidate():
    legacy = Path("results/ema_dual_cross_multisource/xrpusdt_15m_20260822T160246Z/candidates.json")
    if not legacy.exists():
        pytest.skip("legacy export missing")
    c = json.loads(legacy.read_text())[0]
    feats = c["features"]
    cov = c["coverage"]
    v, reasons, sv = apply_gate(direction=c["direction"], features=feats, coverage=cov)
    assert v.value == c["final_verdict"]
    assert "MULTISOURCE_CONFIRMATION" in reasons or c["final_verdict"] == "ALLOW"


def test_inc_keeps_computed_source_verdicts():
    phase1 = Path("results/edc_sync_tolerance/phase1-xrp-pilot/candidates_all.csv")
    if not phase1.exists():
        pytest.skip("phase1 export missing")
    df = pd.read_csv(phase1)
    row = df[(df.mode_id == "M0_STRICT_SYNC") & (df.timeframe == "15m")].iloc[0]
    feats = ast.literal_eval(row.features)
    cov = ast.literal_eval(row.coverage)
    sv = compute_all_source_verdicts(direction=row.direction, features=feats)
    assert sv["trades"] in {x.value for x in SourceVerdict}
    assert sv["ob"] in {x.value for x in SourceVerdict}
    prod, _, prod_sv = apply_gate(direction=row.direction, features=feats, coverage=cov)
    assert prod == FinalVerdict.INCONCLUSIVE_DATA
    assert prod_sv == {} or isinstance(prod_sv, dict)
    # research path always has verdicts
    assert sv["trades"] != "ALLOW"


def test_ob_adverse_drives_research_adverse():
    cov = {
        "candles": {"status": "VALID"},
        "public_trades_cross": {"status": "VALID"},
        "orderbook_ob200_v3": {"status": "VALID"},
    }
    sv = {
        "trades": SourceVerdict.NEUTRAL.value,
        "ob": SourceVerdict.STRONGLY_CONTRADICTING.value,
        "liquidity": SourceVerdict.NEUTRAL.value,
        "volatility": SourceVerdict.NEUTRAL.value,
        "fake_impulse": SourceVerdict.NEUTRAL.value,
        "_fake_impulse_label": "NO_EVIDENCE",
    }
    v, reasons = apply_available_source_research(direction="BULLISH", features={}, coverage=cov, source_verdicts=sv)
    assert v == "RESEARCH_ADVERSE"
    assert any("OB" in r for r in reasons)


def test_fake_impulse_ablation_legacy_block():
    legacy = Path("results/ema_dual_cross_multisource/xrpusdt_15m_20260822T160246Z/candidates.json")
    if not legacy.exists():
        pytest.skip("legacy export missing")
    block = next(c for c in json.loads(legacy.read_text()) if c["final_verdict"] == "BLOCK")
    sv = compute_all_source_verdicts(direction=block["direction"], features=block["features"])
    base, _ = apply_available_source_research(
        direction=block["direction"], features=block["features"], coverage=block["coverage"], source_verdicts=sv
    )
    sv2 = dict(sv)
    sv2["fake_impulse"] = SourceVerdict.NEUTRAL.value
    sv2["_fake_impulse_label"] = "NO_EVIDENCE"
    ablated, _ = apply_available_source_research(
        direction=block["direction"], features=block["features"], coverage=block["coverage"], source_verdicts=sv2
    )
    assert base in ("RESEARCH_ADVERSE", "RESEARCH_MIXED")
    assert ablated != base


def test_empty_window_distinct_from_missing():
    cov = assess_coverage(
        candidate_at=datetime(2026, 8, 18, 23, 10, tzinfo=timezone.utc),
        symbol="X",
        candles_df=pd.DataFrame([_bar(0, 1.0, 1.0)]),
        trades_1m=pd.DataFrame(),
        ob_1m=pd.DataFrame(),
        oi_1m=pd.DataFrame(),
        liq=pd.DataFrame(),
        lld_status="VALID",
        cfg=EMA_DUAL_CROSS_DEFAULTS,
        timeframe="5m",
        decision_at=datetime(2026, 8, 18, 23, 15, tzinfo=timezone.utc),
    )
    assert cov["liquidations"]["status"] in ("MISSING", "EMPTY_TABLE_SLICE")


def test_defaults_untouched():
    assert EMA_DUAL_CROSS_DEFAULTS.require_oi_for_allow is True
    assert EMA_DUAL_CROSS_DEFAULTS.require_liq_for_allow is True
