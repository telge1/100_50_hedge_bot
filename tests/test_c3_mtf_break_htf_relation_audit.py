"""Tests for causal 5m break ↔ HTF relation audit."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from orderbook_analyse.c3_mtf_break_htf_relation_audit import (
    MATCH_BPS,
    attach_catalog_outcomes,
    attach_htf_context,
    build_event_universe,
    classify_relation_class,
    levels_match,
    make_event_id,
    parse_ts,
    run_mtf_break_htf_relation_audit,
)

ROOT = Path(__file__).resolve().parents[1]
MTF_DIR = ROOT / "results" / "trend_scanner_multitimeframe_structure"
OUT_DIR = ROOT / "results" / "c3_mtf_break_htf_relation_audit"
PL_CATALOG = ROOT / "results" / "c3_protected_low_historical_event_catalog"
PH_CATALOG = ROOT / "results" / "c3_protected_high_historical_event_catalog"


def test_match_bps_documented_constant() -> None:
    assert MATCH_BPS == 1.0
    assert levels_match(100.0, 100.0)
    # 1.0 bps of 100 = 0.01
    assert levels_match(100.0, 100.0 + 0.01)
    assert not levels_match(100.0, 100.0 + 0.02)


def test_asof_never_future() -> None:
    signal = datetime(2026, 7, 31, 2, 30, tzinfo=timezone.utc)
    events = pd.DataFrame(
        [
            {
                "event_id": "APTUSDT_PL_20260731T023000_0p5689",
                "symbol": "APTUSDT",
                "event_type": "PROTECTED_LOW_BREAK",
                "level": 0.5689,
                "level_r8": 0.5689,
                "break_candle_open": datetime(2026, 7, 31, 2, 25, tzinfo=timezone.utc),
                "signal_available_at": signal,
                "break_close": 0.5687,
                "choch": True,
                "trend_segment_id": "s",
                "major_direction": 1,
                "in_warmup": False,
                "rebreak_flag": False,
            }
        ]
    )
    mtf = pd.DataFrame(
        [
            {
                "symbol": "APTUSDT",
                "timeframe": "5m",
                "available_at": signal,
                "candle_open_ts": datetime(2026, 7, 31, 2, 25, tzinfo=timezone.utc),
                "timestamp": datetime(2026, 7, 31, 2, 25, tzinfo=timezone.utc),
                "available_at_1h": datetime(2026, 7, 31, 2, 0, tzinfo=timezone.utc),
                "protected_low_1h": 0.5689,
                "protected_high_1h": None,
                "major_direction_1h": 1,
                "close_break_protected_down_1h": False,
                "close_break_protected_up_1h": False,
                "available_at_4h": datetime(2026, 7, 31, 0, 0, tzinfo=timezone.utc),
                "protected_low_4h": None,
                "protected_high_4h": 0.6126,
                "major_direction_4h": -1,
                "close_break_protected_down_4h": False,
                "close_break_protected_up_4h": False,
            }
        ]
    )
    out = attach_htf_context(events, mtf)
    assert not bool(out.iloc[0]["future_violation"])
    assert parse_ts(out.iloc[0]["available_at_1h"]) <= signal
    assert parse_ts(out.iloc[0]["available_at_4h"]) <= signal


def test_pl_and_ph_both_in_universe() -> None:
    pl = pd.DataFrame(
        [
            {
                "symbol": "APTUSDT",
                "timeframe": "5m",
                "event_side": "protected_low_break",
                "candle_open_ts": "2026-07-31T02:25:00Z",
                "available_at": "2026-07-31T02:30:00Z",
                "level": 0.5689,
                "close": 0.5687,
                "choch": True,
                "require_choch": True,
                "trend_segment_id": "s1",
                "major_direction": 1,
                "in_warmup": False,
            }
        ]
    )
    ph = pd.DataFrame(
        [
            {
                "symbol": "APTUSDT",
                "timeframe": "5m",
                "event_side": "protected_high_break",
                "candle_open_ts": "2026-07-31T03:00:00Z",
                "available_at": "2026-07-31T03:05:00Z",
                "level": 0.57,
                "close": 0.571,
                "choch": True,
                "require_choch": True,
                "trend_segment_id": "s2",
                "major_direction": -1,
                "in_warmup": False,
            }
        ]
    )
    uni = build_event_universe(pl, ph, symbols=["APTUSDT"])
    assert set(uni["event_type"]) == {"PROTECTED_LOW_BREAK", "PROTECTED_HIGH_BREAK"}


def test_rebreak_flag_deterministic() -> None:
    pl = pd.DataFrame(
        [
            {
                "symbol": "APTUSDT",
                "timeframe": "5m",
                "candle_open_ts": "2026-07-01T00:00:00Z",
                "available_at": "2026-07-01T00:05:00Z",
                "level": 1.0,
                "close": 0.99,
                "choch": True,
                "require_choch": True,
                "trend_segment_id": "a",
                "major_direction": 1,
                "in_warmup": False,
            },
            {
                "symbol": "APTUSDT",
                "timeframe": "5m",
                "candle_open_ts": "2026-07-02T00:00:00Z",
                "available_at": "2026-07-02T00:05:00Z",
                "level": 1.0,
                "close": 0.98,
                "choch": True,
                "require_choch": True,
                "trend_segment_id": "b",
                "major_direction": 1,
                "in_warmup": False,
            },
        ]
    )
    ph = pl.iloc[0:0].copy()
    uni = build_event_universe(pl, ph, symbols=["APTUSDT"])
    assert list(uni.sort_values("signal_available_at")["rebreak_flag"]) == [False, True]
    uni2 = build_event_universe(pl, ph, symbols=["APTUSDT"])
    assert list(uni["rebreak_flag"]) == list(uni2["rebreak_flag"])


def test_outcome_join_only_from_catalogs() -> None:
    events = pd.DataFrame(
        [
            {
                "event_id": "APTUSDT_PL_20260731T023000_0p5689",
                "symbol": "APTUSDT",
                "event_type": "PROTECTED_LOW_BREAK",
                "level": 0.5689,
                "level_r8": 0.5689,
                "signal_available_at": "2026-07-31T02:30:00Z",
                "relation_class": "MATCH_1H_ONLY",
            },
            {
                "event_id": "BTCUSDT_PL_20260101T000500_1p0",
                "symbol": "BTCUSDT",
                "event_type": "PROTECTED_LOW_BREAK",
                "level": 1.0,
                "level_r8": 1.0,
                "signal_available_at": "2026-01-01T00:05:00Z",
                "relation_class": "LOCAL_5M_ONLY",
            },
        ]
    )
    pl_dec = pd.DataFrame(
        [
            {
                "event_id": "APTUSDT_PL_20260731T023000_0p5689",
                "symbol": "APTUSDT",
                "protected_low": 0.5689,
                "break_available_at": "2026-07-31T02:30:00Z",
                "outcome": "BREAKDOWN_CONFIRMED",
                "decision_ts": "2026-07-31T02:45:00Z",
                "minutes_after_break": 15.0,
            }
        ]
    )
    ph_dec = pd.DataFrame(
        columns=[
            "event_id",
            "symbol",
            "protected_high",
            "break_available_at",
            "outcome",
            "decision_ts",
            "minutes_after_break",
        ]
    )
    out = attach_catalog_outcomes(events, pl_dec, ph_dec)
    apt = out[out["symbol"] == "APTUSDT"].iloc[0]
    btc = out[out["symbol"] == "BTCUSDT"].iloc[0]
    assert apt["outcome"] == "BREAKDOWN_CONFIRMED"
    assert apt["persistence"] == "HOLD_CONTINUATION"
    assert apt["outcome_source"] == "catalog"
    assert btc["outcome"] == "n/a"
    assert btc["persistence"] == "n/a"


def test_relation_priority() -> None:
    both = classify_relation_class(
        event_type="PROTECTED_LOW_BREAK",
        event_level=1.0,
        pl_1h=1.0,
        ph_1h=1.1,
        pl_4h=1.0,
        ph_4h=1.2,
    )
    assert both["relation_class"] == "MATCH_1H_AND_4H"
    only1 = classify_relation_class(
        event_type="PROTECTED_LOW_BREAK",
        event_level=1.0,
        pl_1h=1.0,
        ph_1h=None,
        pl_4h=None,
        ph_4h=None,
    )
    assert only1["relation_class"] == "MATCH_1H_ONLY"
    cross = classify_relation_class(
        event_type="PROTECTED_LOW_BREAK",
        event_level=1.0,
        pl_1h=None,
        ph_1h=1.0,
        pl_4h=None,
        ph_4h=None,
    )
    assert cross["relation_class"] == "MATCH_1H_CROSS"
    local = classify_relation_class(
        event_type="PROTECTED_LOW_BREAK",
        event_level=1.0,
        pl_1h=0.9,
        ph_1h=1.1,
        pl_4h=0.8,
        ph_4h=1.2,
    )
    assert local["relation_class"] == "LOCAL_5M_ONLY"


@pytest.mark.skipif(not MTF_DIR.exists(), reason="MTF artefacts missing")
def test_apt_002_and_apt_003_live_artefacts() -> None:
    pl = pd.read_csv(MTF_DIR / "protected_low_break_events.csv")
    ph = pd.read_csv(MTF_DIR / "protected_high_break_events.csv")
    uni = build_event_universe(pl, ph, symbols=["APTUSDT"])
    mtf = pd.read_parquet(MTF_DIR / "structure_states_multitimeframe.parquet")
    h1 = pd.read_parquet(MTF_DIR / "structure_states_1h.parquet")
    h4 = pd.read_parquet(MTF_DIR / "structure_states_4h.parquet")
    ctx = attach_htf_context(uni, mtf, h1, h4)

    apt002 = ctx[
        (ctx["symbol"] == "APTUSDT")
        & (ctx["signal_available_at"] == "2026-07-31T02:30:00Z")
        & (ctx["level_r8"] == 0.5689)
    ]
    assert len(apt002) == 1
    r2 = apt002.iloc[0]
    assert r2["relation_class"] == "MATCH_1H_ONLY" or bool(r2["against_active_1h_pl"])
    assert bool(r2["against_active_1h_pl"]) is True or r2["relation_class"] == "MATCH_1H_ONLY"
    assert not bool(r2["future_violation"])

    apt003 = ctx[
        (ctx["symbol"] == "APTUSDT")
        & (ctx["signal_available_at"] == "2026-08-02T03:55:00Z")
        & (ctx["level_r8"] == 0.5613)
    ]
    assert len(apt003) == 1
    r3 = apt003.iloc[0]
    assert r3["relation_class"] == "LOCAL_5M_ONLY" or not bool(r3["match_1h_same_side"])
    assert r3["protected_low_1h"] is None or (isinstance(r3["protected_low_1h"], float) and pd.isna(r3["protected_low_1h"]))


@pytest.mark.skipif(
    not (MTF_DIR.exists() and PL_CATALOG.exists() and PH_CATALOG.exists()),
    reason="artefacts missing",
)
def test_runner_artefacts_if_present() -> None:
    if not OUT_DIR.exists() or not (OUT_DIR / "decision.json").exists():
        pytest.skip("audit not run yet")
    # keep_default_na=False: literal "n/a" must not become NaN
    uni = pd.read_csv(OUT_DIR / "event_universe.csv", keep_default_na=False)
    assert "PROTECTED_LOW_BREAK" in set(uni["event_type"])
    assert "PROTECTED_HIGH_BREAK" in set(uni["event_type"])
    assert int(pd.to_numeric(uni["future_violation"]).sum()) == 0
    # outcomes only n/a or catalog labels
    assert set(uni["outcome_source"].unique()) <= {"n/a", "catalog"}
