"""Unit tests for additive MTF trend permission layer (no C3.4B mutation)."""

from __future__ import annotations

import pandas as pd

from research.regime_scanner.trend_permission_layer import (
    TrendPermissionConfig,
    apply_mtf_permission_layer,
    classify_ema_regime_row,
    decide_mtf_permission,
    enrich_tf_permission_context,
    permission_invariants_ok,
    structure_direction_from_major,
    transition_state_from_structure,
)


def test_structure_direction_uses_major_not_choch_label():
    assert structure_direction_from_major(-1) == "BEARISH"
    assert structure_direction_from_major(1) == "BULLISH"
    assert structure_direction_from_major(0) == "UNKNOWN"
    tr = transition_state_from_structure(
        major_direction=-1, protected_structure_state="bullish_choch"
    )
    assert tr == "BULLISH_CHOCH_PENDING"
    assert structure_direction_from_major(-1) == "BEARISH"


def test_choch_hold_semantics_enrich():
    df = pd.DataFrame(
        {
            "close": [100.0, 101.0],
            "high": [101.0, 102.0],
            "low": [99.0, 100.0],
            "major_direction": [-1, -1],
            "protected_structure_state": ["bearish_structure", "bullish_choch"],
        }
    )
    cfg = TrendPermissionConfig(ema_warmup_bars=0)
    out = enrich_tf_permission_context(df, cfg=cfg)
    assert out.iloc[1]["structure_direction"] == "BEARISH"
    assert out.iloc[1]["transition_state"] == "BULLISH_CHOCH_PENDING"


def _ema_row(*, close, e9, e20, e59, e200, s9, s20):
    return {
        "close": close,
        "ema_9": e9,
        "ema_20": e20,
        "ema_59": e59,
        "ema_200": e200,
        "ema_9_slope": s9,
        "ema_20_slope": s20,
    }


def test_ema_regimes_strong_bull_bear_neutral_warmup():
    cfg = TrendPermissionConfig(ema_warmup_bars=5, ema_flat_threshold=0.01)
    u = classify_ema_regime_row(
        _ema_row(close=100, e9=99, e20=98, e59=97, e200=96, s9=1, s20=1),
        cfg=cfg,
        bar_index=1,
        structure_direction="BULLISH",
    )
    assert u["ema_regime"] == "UNKNOWN"

    strong = classify_ema_regime_row(
        _ema_row(close=110, e9=108, e20=105, e59=100, e200=90, s9=2.0, s20=1.5),
        cfg=cfg,
        bar_index=10,
        structure_direction="BULLISH",
    )
    assert strong["ema_regime"] == "STRONG_BULLISH"

    strong_b = classify_ema_regime_row(
        _ema_row(close=90, e9=92, e20=95, e59=100, e200=110, s9=-2.0, s20=-1.5),
        cfg=cfg,
        bar_index=10,
        structure_direction="BEARISH",
    )
    assert strong_b["ema_regime"] == "STRONG_BEARISH"

    flat = classify_ema_regime_row(
        _ema_row(close=100, e9=100.01, e20=100.0, e59=100.0, e200=100.0, s9=0.0, s20=0.0),
        cfg=cfg,
        bar_index=10,
        structure_direction="UNKNOWN",
    )
    assert flat["ema_regime"] == "NEUTRAL"


def _snap(**kwargs):
    base = {
        "available_at": "2026-08-01T12:00:00Z",
        "available_at_15m": "2026-08-01T12:00:00Z",
        "available_at_1h": "2026-08-01T12:00:00Z",
        "available_at_4h": "2026-08-01T12:00:00Z",
        "structure_direction": "BULLISH",
        "transition_state": "NONE",
        "ema_regime": "BULLISH",
        "major_direction": 1,
        "protected_structure_state": "bullish_structure",
        "external_bos_up": False,
        "external_bos_down": False,
        "structure_direction_15m": "BULLISH",
        "transition_state_15m": "NONE",
        "ema_regime_15m": "BULLISH",
        "major_direction_15m": 1,
        "structure_direction_1h": "BULLISH",
        "transition_state_1h": "NONE",
        "ema_regime_1h": "BULLISH",
        "major_direction_1h": 1,
        "structure_direction_4h": "BULLISH",
        "transition_state_4h": "NONE",
        "ema_regime_4h": "BULLISH",
        "major_direction_4h": 1,
    }
    base.update(kwargs)
    return base


def test_full_bullish_long_allowed():
    cfg = TrendPermissionConfig(block_on_missing_warmup=False, ema_warmup_bars=0)
    d = decide_mtf_permission(_snap(), cfg=cfg)
    assert d["trade_permission"] == "LONG_ALLOWED"
    assert d["long_allowed"] is True
    assert d["short_allowed"] is False
    assert permission_invariants_ok(d)


def test_bullish_htf_pullback_wait():
    cfg = TrendPermissionConfig(block_on_missing_warmup=False)
    d = decide_mtf_permission(
        _snap(
            structure_direction="BEARISH",
            major_direction=-1,
            protected_structure_state="bearish_structure",
            structure_direction_15m="BEARISH",
            major_direction_15m=-1,
            ema_regime_15m="BEARISH",
        ),
        cfg=cfg,
    )
    assert d["mtf_state"] == "BULLISH_HTF_PULLBACK"
    assert d["trade_permission"] == "WAIT_FOR_LONG_TRIGGER"
    assert d["long_allowed"] is False


def test_full_bearish_short_allowed_mirror():
    cfg = TrendPermissionConfig(block_on_missing_warmup=False)
    d = decide_mtf_permission(
        _snap(
            structure_direction="BEARISH",
            major_direction=-1,
            protected_structure_state="bearish_structure",
            ema_regime="BEARISH",
            structure_direction_15m="BEARISH",
            major_direction_15m=-1,
            ema_regime_15m="BEARISH",
            structure_direction_1h="BEARISH",
            major_direction_1h=-1,
            ema_regime_1h="BEARISH",
            structure_direction_4h="BEARISH",
            major_direction_4h=-1,
            ema_regime_4h="BEARISH",
        ),
        cfg=cfg,
    )
    assert d["trade_permission"] == "SHORT_ALLOWED"
    assert d["short_allowed"] is True
    assert d["long_allowed"] is False


def test_4h_bull_1h_bear_blocks():
    cfg = TrendPermissionConfig(block_on_missing_warmup=False)
    d = decide_mtf_permission(
        _snap(
            structure_direction_1h="BEARISH",
            major_direction_1h=-1,
            ema_regime_1h="BEARISH",
        ),
        cfg=cfg,
    )
    assert d["trade_permission"] == "BLOCK_BOTH"
    assert d["mtf_state"] == "MIXED_TIMEFRAMES"


def test_4h_unknown_blocks():
    cfg = TrendPermissionConfig(block_on_missing_warmup=False)
    d = decide_mtf_permission(
        _snap(structure_direction_4h="UNKNOWN", major_direction_4h=0),
        cfg=cfg,
    )
    assert d["trade_permission"] == "BLOCK_BOTH"


def test_htf_choch_pending_blocks():
    cfg = TrendPermissionConfig(block_on_missing_warmup=False)
    d = decide_mtf_permission(
        _snap(
            structure_direction_4h="BEARISH",
            major_direction_4h=-1,
            transition_state_4h="BULLISH_CHOCH_PENDING",
            protected_structure_state_4h="bullish_choch",
        ),
        cfg=cfg,
    )
    assert d["trade_permission"] == "BLOCK_BOTH"
    assert "choch" in d["permission_reason"]


def test_5m_cannot_override_4h_bear():
    cfg = TrendPermissionConfig(block_on_missing_warmup=False)
    d = decide_mtf_permission(
        _snap(
            structure_direction="BULLISH",
            major_direction=1,
            structure_direction_4h="BEARISH",
            major_direction_4h=-1,
            ema_regime_4h="BEARISH",
            structure_direction_1h="BEARISH",
            major_direction_1h=-1,
            ema_regime_1h="BEARISH",
            structure_direction_15m="BEARISH",
            major_direction_15m=-1,
        ),
        cfg=cfg,
    )
    assert d["trade_permission"] != "LONG_ALLOWED"
    assert d["long_allowed"] is False


def test_decision_available_at_is_max_of_sources():
    cfg = TrendPermissionConfig(block_on_missing_warmup=False)
    d = decide_mtf_permission(
        _snap(
            available_at="2026-08-01T10:00:00Z",
            available_at_15m="2026-08-01T10:15:00Z",
            available_at_1h="2026-08-01T11:00:00Z",
            available_at_4h="2026-08-01T12:00:00Z",
        ),
        cfg=cfg,
    )
    assert "12:00" in str(d["decision_available_at"])


def test_apply_layer_invariants():
    cfg = TrendPermissionConfig(block_on_missing_warmup=False)
    mtf = pd.DataFrame([_snap()])
    out = apply_mtf_permission_layer(mtf, cfg=cfg)
    assert bool(out.iloc[0]["long_allowed"]) is True
    assert bool(out.iloc[0]["short_allowed"]) is False
    assert permission_invariants_ok(out.iloc[0].to_dict())


def test_ema_strong_conflict_blocks():
    cfg = TrendPermissionConfig(block_on_missing_warmup=False, ema_conflict_policy="block")
    d = decide_mtf_permission(_snap(ema_regime_4h="STRONG_BEARISH"), cfg=cfg)
    assert d["trade_permission"] == "BLOCK_BOTH"


def test_c34b_not_redefined_in_permission_module():
    import research.regime_scanner.market_structure_c3_4b as c34b
    import research.regime_scanner.trend_permission_layer as tpl

    assert hasattr(c34b, "step_protected_structure_state")
    assert not hasattr(tpl, "step_protected_structure_state")
