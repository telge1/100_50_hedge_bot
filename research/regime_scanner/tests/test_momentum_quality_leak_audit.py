"""Tests for momentum quality leak audit (research-only)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from research.regime_scanner.momentum import (
    body_to_range_ratio,
    close_location_ratio,
    default_momentum_config,
)
from research.regime_scanner.momentum_quality_leak_audit import (
    document_current_momentum_logic,
    load_leak_ids,
    match_score,
    wick_shares,
)

MULTIWEEK = Path(
    "research/backtests/results/regime_scanner_pipeline_counterfactual_multiweek"
)


@pytest.mark.skipif(not (MULTIWEEK / "multiweek_remaining_weak_entry_leaks.csv").exists(), reason="multiweek artifacts missing")
def test_loads_exactly_eleven_leaks() -> None:
    ids = load_leak_ids(MULTIWEEK)
    assert len(ids) == 11
    assert len(set(ids)) == 11
    df = pd.read_csv(MULTIWEEK / "multiweek_remaining_weak_entry_leaks.csv")
    assert set(df["leak_category"]) == {"MOMENTUM_QUALITY_LEAK"}
    # Must not silently include goods from outcomes
    out = pd.read_csv(MULTIWEEK / "multiweek_entry_outcomes.csv")
    m3 = out[out.multi_variant == "M3"]
    leak_m3 = m3[m3.setup_id.isin(ids)]
    assert len(leak_m3) == 11
    assert (leak_m3.entry_quality == "weak").all()


@pytest.mark.skipif(not (MULTIWEEK / "multiweek_entry_outcomes.csv").exists(), reason="multiweek artifacts missing")
def test_m0_timestamps_align_with_momentum_csv() -> None:
    ids = load_leak_ids(MULTIWEEK)
    out = pd.read_csv(MULTIWEEK / "multiweek_entry_outcomes.csv")
    mom = pd.read_csv(
        "research/backtests/results/regime_scanner_pipeline_audit_aptusdt_2026_h1/momentum_confirmations.csv"
    )
    pa = pd.read_csv(
        "research/backtests/results/regime_scanner_pipeline_audit_aptusdt_2026_h1/price_action_confirmations.csv"
    )
    m0 = out[out.multi_variant == "M0"]
    for sid in ids:
        o = m0[m0.setup_id == sid].iloc[0]
        m = mom[mom.setup_id == sid].iloc[0]
        p = pa[pa.setup_id == sid].iloc[0]
        assert pd.Timestamp(o.entry_timestamp) == pd.Timestamp(m.confirmation_timestamp)
        assert pd.Timestamp(p.structure_break_timestamp) == pd.Timestamp(
            m.pa_structure_break_timestamp
        )


def test_momentum_logic_doc_states_no_ema_di() -> None:
    doc = document_current_momentum_logic()
    assert "NOT used" in doc["q6_ema9_ema20"]
    assert "NOT used" in doc["q7_di_adx"]
    assert doc["config"]["volume_filter_enabled"] is False
    cfg = default_momentum_config()
    assert cfg.confirmation_window_candles == 3
    assert cfg.allow_confirmation_on_break_candle is True
    assert cfg.min_body_to_range_ratio == 0.50
    assert cfg.require_structure_level_hold is True


def test_wick_body_close_location() -> None:
    candle = {"open": 1.0, "high": 1.2, "low": 0.9, "close": 1.15}
    assert body_to_range_ratio(candle) == pytest.approx(0.15 / 0.3)
    assert close_location_ratio(candle, side="long") == pytest.approx((1.15 - 0.9) / 0.3)
    w = wick_shares(candle, "long")
    assert w["opp_wick_share"] == pytest.approx((min(1.0, 1.15) - 0.9) / 0.3)


def test_match_requires_same_side() -> None:
    leak = {
        "side": "long",
        "market_phase": "quiet_trend",
        "setup_type": "x",
        "pa_pattern_type": "y",
        "entry_timestamp": "2026-01-06T23:50:00+00:00",
        "atr_pct": 1.0,
    }
    bad = dict(leak, side="short", entry_timestamp="2026-01-07T00:00:00+00:00")
    good = dict(leak, entry_timestamp="2026-01-07T01:00:00+00:00")
    assert match_score(leak, bad) < 0
    assert match_score(leak, good) > 0


def test_no_lookahead_in_confirm_age_ordering() -> None:
    # ages increase only after PA
    from research.regime_scanner.momentum_quality_leak_audit import extract_confirm_candles

    rows = [
        {
            "phase": "pa_to_entry",
            "decision_time": "2026-01-06T23:40:00+00:00",
            "confirm_age_if_in_window": 0,
        },
        {
            "phase": "pa_to_entry",
            "decision_time": "2026-01-06T23:45:00+00:00",
            "confirm_age_if_in_window": 1,
        },
        {
            "phase": "pa_to_entry",
            "decision_time": "2026-01-06T23:50:00+00:00",
            "confirm_age_if_in_window": 2,
        },
    ]
    conf = extract_confirm_candles(
        rows,
        pd.Timestamp("2026-01-06T23:40:00+00:00"),
        pd.Timestamp("2026-01-06T23:50:00+00:00"),
    )
    assert conf["c0_break"]["confirm_age_if_in_window"] == 0
    assert conf["c1"]["confirm_age_if_in_window"] == 1
    assert conf["n_candles_pa_to_mom"] == 3
