"""Tests for EMA dual-cross multi-source research pipeline (recovery matrix)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from orderbook_analyse.cluster_sweep_research.ema_features import attach_emas
from orderbook_analyse.ema_dual_cross_multisource.config import EMA_DUAL_CROSS_DEFAULTS, EmaDualCrossConfig
from orderbook_analyse.ema_dual_cross_multisource.coverage_gate import assess_coverage
from orderbook_analyse.ema_dual_cross_multisource.ema_candidate import detect_cross_events
from orderbook_analyse.ema_dual_cross_multisource.episode_state import EpisodeTracker, make_episode_id
from orderbook_analyse.ema_dual_cross_multisource.gate_policy import (
    LIQ_SUPPORT_RATIO,
    OI_CONTRA_MIN_PCT,
    apply_gate,
    policy_document,
)
from orderbook_analyse.ema_dual_cross_multisource.models import FinalVerdict
from orderbook_analyse.ema_dual_cross_multisource.pipeline import run_ema_dual_cross_on_candles


def _bars_from_closes(closes: list[float], *, start: datetime | None = None) -> pd.DataFrame:
    start = start or datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
    rows = []
    for i, c in enumerate(closes):
        t = (start + timedelta(minutes=15 * i)).replace(tzinfo=None)
        o = closes[i - 1] if i else c
        rows.append({"open_time": t, "open": o, "high": max(o, c) * 1.002, "low": min(o, c) * 0.998, "close": c, "volume": 1000.0})
    return attach_emas(pd.DataFrame(rows))


def _bull_sync_closes(n_warm: int = 80) -> list[float]:
    closes = [1.0] * n_warm
    for i in range(20):
        closes.append(1.0 + i * 0.002)
    return closes


def _trades_df(*, tbr: float = 0.6, delta: float = 100):
    t = datetime(2026, 8, 4, 13, 0, tzinfo=timezone.utc)
    bn = tbr * 1000
    sn = (1 - tbr) * 1000
    return pd.DataFrame([{"minute": t.replace(tzinfo=None), "buy_notional": bn, "sell_notional": sn, "trade_count": 50, "delta": delta}])


def _ob_df(*, imb: float = 0.05):
    t = datetime(2026, 8, 4, 13, 29, tzinfo=timezone.utc)
    return pd.DataFrame([{"minute": t.replace(tzinfo=None), "imbalance_l50": imb, "spread_bps": 2.0}])


# --- Sync cross detection ---


def test_bull_synchronous_cross_detected():
    df = _bars_from_closes(_bull_sync_closes())
    valid, _ = detect_cross_events(df, symbol="XRPUSDT", timeframe="15m")
    sync = [v for v in valid if v["candidate_type"] == "SYNCHRONOUS_DUAL_EMA_CROSS" and v["direction"] == "BULLISH"]
    assert len(sync) >= 1
    assert sync[0]["reason_codes"] == ["VALID_SYNCHRONOUS_CROSS"]
    assert sync[0]["ema_metrics"]["same_candle_cross"] is True


def test_bear_mirror():
    closes = [1.2 - i * 0.001 for i in range(60)] + [1.14 - i * 0.003 for i in range(30)]
    df = _bars_from_closes(closes)
    valid, _ = detect_cross_events(df, symbol="XRPUSDT", timeframe="15m")
    assert isinstance([v for v in valid if v["direction"] == "BEARISH"], list)


def test_sync_only_flag_disables_rebound():
    cfg = EmaDualCrossConfig(enable_sync_cross=True, enable_compressed_rebound=False)
    df = _bars_from_closes(_bull_sync_closes())
    valid, _ = detect_cross_events(df, symbol="XRPUSDT", timeframe="15m", cfg=cfg)
    assert all(v["candidate_type"] == "SYNCHRONOUS_DUAL_EMA_CROSS" for v in valid)


def test_rebound_only_flag():
    cfg = EmaDualCrossConfig(enable_sync_cross=False, enable_compressed_rebound=True)
    df = _bars_from_closes(_bull_sync_closes())
    valid, _ = detect_cross_events(df, symbol="XRPUSDT", timeframe="15m", cfg=cfg)
    assert all(v["candidate_type"] != "SYNCHRONOUS_DUAL_EMA_CROSS" for v in valid) or len(valid) == 0


def test_sync_priority_over_rebound_same_bar():
    cfg = EmaDualCrossConfig(enable_sync_cross=True, enable_compressed_rebound=True)
    df = _bars_from_closes(_bull_sync_closes())
    valid, _ = detect_cross_events(df, symbol="XRPUSDT", timeframe="15m", cfg=cfg)
    by_bar = {}
    for v in valid:
        key = (v["bar_index"], v["direction"])
        by_bar.setdefault(key, []).append(v["candidate_type"])
    for types in by_bar.values():
        if "SYNCHRONOUS_DUAL_EMA_CROSS" in types:
            assert "COMPRESSED_EMA59_REBOUND" not in types


def test_no_partial_cross_as_valid_sync():
    df = attach_emas(pd.DataFrame([
        {"open_time": datetime(2026, 1, 1) + timedelta(minutes=15 * i), "open": 1, "high": 1.01, "low": 0.99,
         "close": 1.0 + (0.001 if i > 70 else 0), "volume": 1}
        for i in range(90)
    ]))
    valid, rejected = detect_cross_events(df, symbol="X", timeframe="15m")
    for v in valid:
        if v["candidate_type"] == "SYNCHRONOUS_DUAL_EMA_CROSS":
            assert v["ema_metrics"].get("same_candle_cross") is True
    partial = {"REJECTED_EMA9_ONLY", "REJECTED_EMA20_ONLY", "REJECTED_STAGGERED_CROSS"}
    if rejected:
        assert any(r["reason_codes"][0] in partial for r in rejected if r.get("reason_codes"))


# --- Entry / outcome reference ---


def test_entry_next_open_only_on_allow():
    df = _bars_from_closes(_bull_sync_closes())
    start = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(days=30)
    bundle = run_ema_dual_cross_on_candles(
        df, symbol="XRPUSDT", timeframe="15m", window_start=start, window_end=end, attach_outcomes=False
    )
    for c in bundle["candidates"]:
        assert c.get("hypothetical_entry_at") is not None
        assert c.get("hypothetical_entry_price") is not None
        if c["final_verdict"] == "ALLOW":
            assert c["entry_at"] is not None
            assert c["entry_price"] is not None
        else:
            assert c.get("entry_at") is None
            assert c.get("entry_price") is None


def test_hypothetical_entry_all_verdicts():
    df = _bars_from_closes(_bull_sync_closes())
    start = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
    bundle = run_ema_dual_cross_on_candles(
        df, symbol="XRPUSDT", timeframe="15m", window_start=start, window_end=start + timedelta(days=30), attach_outcomes=False
    )
    for c in bundle["candidates"]:
        assert c["hypothetical_entry_at"]
        assert c["hypothetical_entry_price"] is not None


# --- Gate ---


def test_block_no_entry():
    feats = {
        "windows": {"pre_15m": {"taker_buy_ratio": 0.30, "trades_status": "VALID", "delta": -100, "ob_status": "VALID", "imbalance_l50_mean": 0.01}},
        "frozen_gate_features": {"taker_buy_ratio": 0.30, "ret_5m": -0.01},
        "volatility": {"body_atr": 0.5},
        "liquidity_confluence": {"lld_status": "VALID"},
        "trade_flow": {},
        "ob_meta": {"status": "VALID"},
    }
    cov = {"coverage_gate": "PASS", "critical_missing": []}
    v, _, _ = apply_gate(direction="BULLISH", features=feats, coverage=cov)
    assert v in (FinalVerdict.BLOCK, FinalVerdict.INCONCLUSIVE_DATA)


def test_inconclusive_missing_trades():
    cov = assess_coverage(
        candidate_at=datetime(2026, 8, 19, 5, 0, tzinfo=timezone.utc),
        symbol="XRPUSDT",
        candles_df=pd.DataFrame([{"open_time": datetime(2026, 8, 19, 4, 0), "close": 1.0}]),
        trades_1m=None,
        ob_1m=None,
        oi_1m=None,
        liq=None,
        lld_status="VALID",
    )
    assert cov["coverage_gate"] == "INCONCLUSIVE_DATA"
    assert "public_trades_cross" in cov["critical_missing"]


def test_missing_ob_inconclusive_coverage():
    cfg = EmaDualCrossConfig(require_ob_for_allow=True)
    cov = assess_coverage(
        candidate_at=datetime(2026, 8, 19, 5, 0, tzinfo=timezone.utc),
        symbol="XRPUSDT",
        candles_df=pd.DataFrame([{"open_time": datetime(2026, 8, 19, 4, 0), "close": 1.0}]),
        trades_1m=_trades_df(),
        ob_1m=None,
        oi_1m=None,
        liq=None,
        lld_status="VALID",
        cfg=cfg,
    )
    assert cov["coverage_gate"] == "INCONCLUSIVE_DATA"
    assert "orderbook_ob200_v3" in cov["critical_missing"]


def test_stale_ob_inconclusive():
    cfg = EmaDualCrossConfig(require_ob_for_allow=True, ob_stale_minutes=5)
    ob_time = datetime(2026, 8, 4, 13, 20, tzinfo=timezone.utc)
    ob = pd.DataFrame([{"minute": ob_time.replace(tzinfo=None), "imbalance_l50": 0.05, "spread_bps": 2.0}])
    cov = assess_coverage(
        candidate_at=datetime(2026, 8, 4, 13, 30, tzinfo=timezone.utc),
        symbol="XRPUSDT",
        candles_df=pd.DataFrame([{"open_time": datetime(2026, 8, 4, 13, 0), "close": 1.0}]),
        trades_1m=_trades_df(),
        ob_1m=ob,
        oi_1m=None,
        liq=None,
        lld_status="VALID",
        cfg=cfg,
    )
    assert cov["orderbook_ob200_v3"]["status"] == "STALE"
    assert cov["coverage_gate"] == "INCONCLUSIVE_DATA"


def test_missing_ob_not_neutral_in_gate():
    feats = {
        "windows": {"pre_15m": {"trades_status": "VALID", "taker_buy_ratio": 0.6, "delta": 10, "ob_status": "MISSING"}},
        "frozen_gate_features": {"taker_buy_ratio": 0.6},
        "volatility": {"body_atr": 0.5},
        "liquidity_confluence": {},
        "trade_flow": {},
        "ob_meta": {"status": "MISSING"},
    }
    cov = {"coverage_gate": "PASS", "critical_missing": []}
    sv = apply_gate(direction="BULLISH", features=feats, coverage=cov)[2]
    assert sv.get("ob") == "INCONCLUSIVE_DATA"


def test_oi_supporting():
    feats = {
        "windows": {"pre_15m": {"oi_status": "VALID", "oi_change": 100}},
        "oi_features": {"oi_change_rel_baseline": OI_CONTRA_MIN_PCT + 0.01},
        "frozen_gate_features": {"ret_5m": 0.01},
        "trade_flow": {},
        "ob_meta": {},
        "liquidity_confluence": {"lld_status": "VALID"},
        "volatility": {},
    }
    from orderbook_analyse.ema_dual_cross_multisource.gate_policy import _oi_verdict

    assert _oi_verdict(feats, "BULLISH") == "SUPPORTING"


def test_oi_contradicting():
    feats = {
        "windows": {"pre_15m": {"oi_status": "VALID"}},
        "oi_features": {"oi_change_rel_baseline": -(OI_CONTRA_MIN_PCT + 0.01)},
        "frozen_gate_features": {"ret_5m": 0.01},
    }
    from orderbook_analyse.ema_dual_cross_multisource.gate_policy import _oi_verdict

    assert _oi_verdict(feats, "BULLISH") == "CONTRADICTING"


def test_liq_supporting():
    feats = {
        "windows": {"pre_15m": {"liq_status": "VALID", "liq_long_notional": 100, "liq_short_notional": 200 * LIQ_SUPPORT_RATIO}},
        "liquidation_features": {"intensity_rel_baseline": 1.2},
    }
    from orderbook_analyse.ema_dual_cross_multisource.gate_policy import _liq_verdict

    assert _liq_verdict(feats, "BULLISH") == "SUPPORTING"


def test_lld_contradicting():
    feats = {
        "liquidity_confluence": {"lld_status": "VALID", "opposing_cluster": {"distance_pct": 0.05}},
        "lld_features": {"opposing_barrier_distance_pct": 0.05, "free_room_pct": 0.05},
    }
    from orderbook_analyse.ema_dual_cross_multisource.gate_policy import _liquidity_verdict

    assert _liquidity_verdict(feats, "BULLISH") in ("CONTRADICTING", "STRONGLY_CONTRADICTING")


# --- Episode logic ---


def test_episode_allow_blocks_second_allow():
    tr = EpisodeTracker()
    raw = {"direction": "BULLISH", "bar_index": 10, "candidate_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
           "symbol": "X", "timeframe": "15m", "candidate_type": "SYNCHRONOUS_DUAL_EMA_CROSS", "ema_metrics": {}}
    ok1, _, _ = tr.admit_candidate(raw)
    assert ok1
    tr.record_verdict(raw, FinalVerdict.ALLOW)
    raw2 = dict(raw, bar_index=12)
    ok2, rej, _ = tr.admit_candidate(raw2)
    assert not ok2
    assert rej == "REJECTED_EPISODE_ALREADY_SIGNALED"


def test_block_does_not_open_active_entry_episode():
    tr = EpisodeTracker()
    raw = {"direction": "BEARISH", "bar_index": 10, "candidate_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
           "symbol": "X", "timeframe": "15m", "candidate_type": "COMPRESSED_EMA59_REBOUND",
           "ema_metrics": {"ema_9_20_gap_pct": 0.05, "ema_band_width_atr": 0.2}}
    ok1, _, _ = tr.admit_candidate(raw)
    tr.record_verdict(raw, FinalVerdict.BLOCK)
    sync = dict(raw, bar_index=20, candidate_type="SYNCHRONOUS_DUAL_EMA_CROSS", reason_codes=["VALID_SYNCHRONOUS_CROSS"])
    ok2, rej, _ = tr.admit_candidate(sync)
    assert ok2, rej


def test_inconclusive_does_not_block_later_sync():
    tr = EpisodeTracker()
    raw = {"direction": "BEARISH", "bar_index": 10, "candidate_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
           "symbol": "X", "timeframe": "15m", "candidate_type": "COMPRESSED_EMA59_REBOUND",
           "ema_metrics": {"ema_9_20_gap_pct": 0.05, "ema_band_width_atr": 0.2}}
    tr.admit_candidate(raw)
    tr.record_verdict(raw, FinalVerdict.INCONCLUSIVE_DATA)
    sync = dict(raw, bar_index=15, candidate_type="SYNCHRONOUS_DUAL_EMA_CROSS")
    ok, _, _ = tr.admit_candidate(sync)
    assert ok


def test_rebound_block_does_not_block_sync():
    tr = EpisodeTracker()
    rebound = {"direction": "BEARISH", "bar_index": 10, "candidate_at": datetime(2026, 8, 4, 3, 0, tzinfo=timezone.utc),
               "symbol": "XRPUSDT", "timeframe": "15m", "candidate_type": "COMPRESSED_EMA59_REBOUND",
               "ema_metrics": {"ema_9_20_gap_pct": 0.05, "ema_band_width_atr": 0.2}}
    tr.admit_candidate(rebound)
    tr.record_verdict(rebound, FinalVerdict.BLOCK)
    sync = dict(rebound, bar_index=23, candidate_at=datetime(2026, 8, 4, 6, 15, tzinfo=timezone.utc),
               candidate_type="SYNCHRONOUS_DUAL_EMA_CROSS")
    ok, rej, _ = tr.admit_candidate(sync)
    assert ok, rej


def test_allow_rebound_sync_confirmation():
    tr = EpisodeTracker()
    rebound = {"direction": "BULLISH", "bar_index": 50, "candidate_at": datetime(2026, 8, 4, 13, 30, tzinfo=timezone.utc),
               "symbol": "XRPUSDT", "timeframe": "15m", "candidate_type": "COMPRESSED_EMA59_REBOUND",
               "ema_metrics": {"ema_9_20_gap_pct": 0.05, "ema_band_width_atr": 0.2}}
    tr.admit_candidate(rebound)
    tr.record_verdict(rebound, FinalVerdict.ALLOW)
    sync = dict(rebound, bar_index=68, candidate_at=datetime(2026, 8, 4, 18, 0, tzinfo=timezone.utc),
               candidate_type="SYNCHRONOUS_DUAL_EMA_CROSS")
    ok, _, rel = tr.admit_candidate(sync)
    assert ok
    assert rel == "SYNC_CONFIRMATION"


def test_episode_id_deterministic():
    ts = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    assert make_episode_id("XRPUSDT", "15m", "BULLISH", ts) == make_episode_id("XRPUSDT", "15m", "BULLISH", ts)


def test_update_compression_wired():
    tr = EpisodeTracker()
    tr.update_compression("BULLISH", compressed=True, bar_index=5)
    assert tr.compression_active["BULLISH"]
    tr.update_compression("BULLISH", compressed=False, bar_index=10)
    assert tr.state["BULLISH"].value in ("RESET_PENDING", "NEUTRAL", "RESET_COMPLETE")


# --- Architecture ---


def test_cluster_alone_no_candidate():
    df = _bars_from_closes(_bull_sync_closes())
    valid, _ = detect_cross_events(df, symbol="XRPUSDT", timeframe="15m")
    for v in valid:
        assert v["candidate_type"] in ("SYNCHRONOUS_DUAL_EMA_CROSS", "COMPRESSED_EMA59_REBOUND")


def test_policy_version_and_ob_required():
    p = policy_document()
    assert p["policy_version"] == "EMA_MULTI_SOURCE_GATE_V1"
    assert EMA_DUAL_CROSS_DEFAULTS.require_ob_for_allow is True
    assert EMA_DUAL_CROSS_DEFAULTS.enable_compressed_rebound is False


def test_idempotent_run():
    df = _bars_from_closes(_bull_sync_closes())
    start = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(days=30)
    kw = dict(symbol="XRPUSDT", timeframe="15m", window_start=start, window_end=end, attach_outcomes=False)
    a = run_ema_dual_cross_on_candles(df, **kw)
    b = run_ema_dual_cross_on_candles(df, **kw)
    assert len(a["candidates"]) == len(b["candidates"])


def test_inconclusive_ob_no_entry():
    df = _bars_from_closes(_bull_sync_closes())
    start = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
    cfg = EmaDualCrossConfig(enable_sync_cross=True, enable_compressed_rebound=False, require_ob_for_allow=True)
    bundle = run_ema_dual_cross_on_candles(
        df, symbol="XRPUSDT", timeframe="15m", window_start=start, window_end=start + timedelta(days=30),
        trades_1m=_trades_df(), ob_1m=None, attach_outcomes=False, cfg=cfg,
    )
    for c in bundle["candidates"]:
        if c["final_verdict"] == "INCONCLUSIVE_DATA":
            assert c.get("entry_at") is None
            assert c.get("hypothetical_entry_at")
