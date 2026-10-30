"""Phase 2D — support strength, SL@Q, recovery modes."""

from __future__ import annotations

import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG))
SHORT = PKG.parents[1] / "phase_short_main_v2_combined_exit_v1"
sys.path.insert(0, str(SHORT))

from long_research_config_v2d_v1 import LongResearchConfigV2DV1  # noqa: E402
from long_risk_audit_v1 import risk_pct, structural_long_sl  # noqa: E402
from long_support_selection_v2d_v1 import select_support_v2d  # noqa: E402
from tests.fixtures_v1 import snap  # noqa: E402


def test_v2d_sl_at_q_upper_skips_weak_neighbor():
    s = snap(
        [
            (0.100, 0.101, "a|b"),
            (0.095, 0.096, "c|d"),
            (0.088, 0.090, "e|f|g"),
        ]
    )
    cfg = LongResearchConfigV2DV1("V2D_STRONG", "DEEPER_Q_UPPER", "NEXT_BAR")
    sel, reason = select_support_v2d(s, market_price=0.102, config=cfg)
    assert reason is None and sel is not None
    assert sel.region.upper_edge == 0.096
    assert sel.structural_sl == 0.09
    assert sel.structural_sl != structural_long_sl(sel.region.lower_edge)
    rp = risk_pct(sel.entry_limit, sel.structural_sl)
    assert rp is not None and rp > 1.0


def test_v1_baseline_still_legacy_sl_with_v2d_wrapper():
    s = snap([(0.100, 0.101, "a|b"), (0.095, 0.096, "c|d")])
    cfg = LongResearchConfigV2DV1("V1_BASELINE", "LEGACY_BELOW_P_LOWER", "NEXT_BAR")
    sel, _ = select_support_v2d(s, market_price=0.102, config=cfg)
    assert sel is not None
    assert abs(sel.structural_sl - sel.region.lower_edge * 0.998) < 1e-12


def test_strong_prefers_higher_strength_when_both_gap_qualified():
    s = snap(
        [
            (0.100, 0.101, "a|b"),
            (0.098, 0.099, "c|d|e"),
            (0.095, 0.096, "f|g"),
        ]
    )
    v1, _ = select_support_v2d(
        s,
        market_price=0.102,
        config=LongResearchConfigV2DV1("V1_BASELINE", "LEGACY_BELOW_P_LOWER", "NEXT_BAR"),
    )
    v2, _ = select_support_v2d(
        s,
        market_price=0.102,
        config=LongResearchConfigV2DV1("V2D_STRONG", "DEEPER_Q_UPPER", "NEXT_BAR"),
    )
    assert v1 is not None and v2 is not None
    assert v1.region.upper_edge == 0.101
    assert v2.region.upper_edge == 0.099
