"""Contract tests for DOGE reference replay (no hardcoded market outcomes)."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.doge_reference_replay import (
    build_pool_parity_rows,
    pool_from_engine_type,
    run_doge_reference_replay,
)
from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.models import (
    CandidateState,
    PoolRecord,
    ScannerCandidate,
    _utc_naive,
)
from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.pools import pool_from_engine


class _FakePool:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _pool_row(pool_id: str, *, side: str, lower: float, upper: float, known: datetime) -> dict:
    p = _FakePool(
        pool_id=pool_id,
        symbol="DOGEUSDT",
        timeframe="15m",
        side="upper" if side == "ASK" else "lower",
        bottom_price=lower,
        top_price=upper,
        strength=1.0,
        created_timestamp=known,
        source_timestamp=known - timedelta(minutes=15),
        invalidated_timestamp=None,
        active=True,
    )
    pr = pool_from_engine(p)
    return {
        "pool_id": pool_id,
        "symbol": "DOGEUSDT",
        "timeframe": "15m",
        "side": side,
        "lower_edge": pr.lower_edge,
        "upper_edge": pr.upper_edge,
        "midpoint": pr.midpoint,
        "known_at": pr.known_at.isoformat(),
        "source_timestamp": pr.source_timestamp.isoformat(),
        "component_count": 1,
        "chart_overlay_start": pr.known_at.isoformat(),
        "scanner_seen_at": pr.known_at.isoformat(),
    }


def test_pool_from_engine_type_roundtrip():
    known = datetime(2026, 8, 28, 3, 30)
    row = _pool_row("lld:DOGEUSDT:15m:upper:x", side="ASK", lower=0.088, upper=0.0883, known=known)
    pr = pool_from_engine_type(row)
    assert pr.pool_id == row["pool_id"]
    assert pr.known_at == _utc_naive(known)


def test_chart_scanner_parity_same_pool_id(monkeypatch):
    known = datetime(2026, 8, 28, 3, 30)
    pid = "lld:DOGEUSDT:15m:upper:test"
    fake = _FakePool(
        pool_id=pid,
        symbol="DOGEUSDT",
        timeframe="15m",
        side="upper",
        bottom_price=0.088,
        top_price=0.0883,
        strength=1.0,
        created_timestamp=known,
        source_timestamp=known,
        invalidated_timestamp=None,
        active=True,
        created_index=1,
        source_index=0,
        source_high=0.0883,
        source_low=0.088,
        source_volume=1.0,
        metadata={},
    )

    def fake_lld(*a, **k):
        class R:
            pools = [fake]

        return R()

    def fake_load(*a, **k):
        return {"15m": [pool_from_engine(fake)], "30m": [], "1h": []}

    candles = {
        "15m": pd.DataFrame(
            {"open_time": [known], "open": [0.088], "high": [0.088], "low": [0.088], "close": [0.088], "volume": [1]}
        ),
        "30m": pd.DataFrame(),
        "1h": pd.DataFrame(),
        "1m": pd.DataFrame(),
        "5m": pd.DataFrame(),
    }
    monkeypatch.setattr(
        "orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.doge_reference_replay.run_lld_pools",
        fake_lld,
    )
    monkeypatch.setattr(
        "orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.doge_reference_replay.load_pools_at",
        fake_load,
    )
    rows = build_pool_parity_rows(candles, symbol="DOGEUSDT", sample_times=[known + timedelta(hours=1)])
    assert rows
    assert rows[0]["parity_ok"] is True
    assert rows[0]["chart_known_at"] == rows[0]["scanner_known_at"]


def test_late_pool_not_known_before_approach():
    known = datetime(2026, 8, 28, 5, 0)
    approach = datetime(2026, 8, 28, 4, 0)
    pr = PoolRecord(
        pool_id="late",
        symbol="DOGEUSDT",
        timeframe="30m",
        side="ASK",
        lower_edge=0.088,
        upper_edge=0.089,
        midpoint=0.0885,
        component_count=1,
        strength=1.0,
        known_at=known,
        invalidated_at=None,
        source_timestamp=known,
    )
    assert not pr.is_known_before(approach)


def test_features_end_at_decision_not_outcome():
    known = datetime(2026, 8, 28, 3, 30)
    pool = PoolRecord(
        pool_id="p",
        symbol="DOGEUSDT",
        timeframe="15m",
        side="ASK",
        lower_edge=0.088,
        upper_edge=0.0883,
        midpoint=0.08815,
        component_count=1,
        strength=1.0,
        known_at=known,
        invalidated_at=None,
        source_timestamp=known,
    )
    c = ScannerCandidate(
        setup_id="s",
        setup_type="A_PLUS_PULLBACK_SHORT",
        symbol="DOGEUSDT",
        direction="SHORT",
        state=CandidateState.CONFIRMED,
        entry_pool=pool,
        target_pool=pool,
        confirmation_at=datetime(2026, 8, 28, 4, 30),
        signal_at=datetime(2026, 8, 28, 4, 30),
        entry_price=0.0879,
    )
    d = c.to_dict()
    assert "pnl" not in d
    assert "outcome" not in d
    assert d["confirmation_at"] == d["signal_at"]


def test_replay_writes_immutable_output(tmp_path: Path, monkeypatch):
    out_root = tmp_path / "results"
    out_root.mkdir()
    monkeypatch.setattr(
        "orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.doge_reference_replay.DEFAULT_OUT_DIR",
        str(out_root / "a_plus_liquidity_pool_signal_scanner_v1"),
    )
    monkeypatch.setattr(
        "orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.doge_reference_replay.get_clickhouse_client",
        lambda: MagicMock(),
    )
    monkeypatch.setattr(
        "orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.doge_reference_replay.build_candles_by_tf",
        lambda *a, **k: {
            "1m": pd.DataFrame(
                {
                    "open_time": [datetime(2026, 8, 28, 0, 0)],
                    "open": [0.09],
                    "high": [0.09],
                    "low": [0.09],
                    "close": [0.09],
                    "volume": [1],
                }
            ),
            "5m": pd.DataFrame(),
            "15m": pd.DataFrame(),
            "30m": pd.DataFrame(),
            "1h": pd.DataFrame(),
        },
    )
    monkeypatch.setattr(
        "orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.doge_reference_replay.run_scanner",
        lambda **k: {"confirmed": [], "invalidated": [], "candidates": [], "n_confirmed": 0, "n_invalidated": 0},
    )
    monkeypatch.setattr(
        "orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.doge_reference_replay.audit_window_funnel",
        lambda *a, **k: __import__(
            "orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.doge_reference_replay",
            fromlist=["FunnelTracker"],
        ).FunnelTracker(),
    )
    monkeypatch.setattr(
        "orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.doge_reference_replay.identify_pullback_short_reference",
        lambda *a, **k: {"found": False},
    )
    monkeypatch.setattr(
        "orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.doge_reference_replay.identify_terminal_long_reference",
        lambda *a, **k: {"found": False},
    )
    res = run_doge_reference_replay()
    out = Path(res["out_dir"])
    assert (out / "replay_manifest.json").is_file()
    assert (out / "report.md").is_file()
    with pytest.raises(FileExistsError):
        run_doge_reference_replay()
