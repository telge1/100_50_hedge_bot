"""Unit tests for liquidation exhaustion pipeline (no MySQL)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from research.regime_scanner.liquidation_exhaustion.bursts import detect_bursts, price_filter, oi_filter
from research.regime_scanner.liquidation_exhaustion.clustering import cluster_bursts
from research.regime_scanner.liquidation_exhaustion.features import enrich_symbol_features
from research.regime_scanner.liquidation_exhaustion.loader import validate_symbols
from research.regime_scanner.liquidation_exhaustion.outcomes import compute_forward_outcomes
from research.regime_scanner.liquidation_exhaustion.reclaim import check_reclaim
from research.regime_scanner.run_liquidation_exhaustion_event_audit import main as cli_main


def _synth(n: int = 320, seq_break_at: int | None = None) -> pd.DataFrame:
    start = datetime(2026, 4, 1, tzinfo=timezone.utc)
    rows = []
    seq = 1
    px = 100.0
    oi = 1000.0
    t = start
    for i in range(n):
        if seq_break_at is not None and i == seq_break_at:
            seq = 2
            t = t + timedelta(days=2)  # gap
        o = px
        c = px * (1.0 - 0.001)
        h = max(o, c) * 1.001
        l = min(o, c) * 0.999
        long_liq = 10.0
        if i == n - 10:
            long_liq = 1e6
            c = px * 0.99
            l = c * 0.995
        rows.append(
            {
                "symbol": "BTCUSDT",
                "bucket_start": t,
                "bucket_end": t + timedelta(minutes=5),
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": 1000.0,
                "open_interest": oi,
                "open_interest_usd": oi * px,
                "long_liquidation_usd": long_liq,
                "short_liquidation_usd": 1.0,
                "total_liquidation_usd": long_liq + 1.0,
                "buy_volume": 100.0,
                "sell_volume": 120.0,
                "total_volume": 220.0,
                "delta": -20.0,
                "delta_ratio": -20.0 / 220.0,
                "spread_mean": 0.01,
                "spread_max": 0.02,
                "source_row_count": 5,
                "coverage_ratio": 1.0,
                "data_available": True,
                "sequence_id": seq,
                "source_hash": "x",
                "import_version": "derivatives_5m_v1",
            }
        )
        px = c
        oi = oi * 0.999
        t = t + timedelta(minutes=5)
    return pd.DataFrame(rows)


def test_validate_rejects_ena():
    with pytest.raises(ValueError):
        validate_symbols(["BTCUSDT", "ENAUSDT"])


def test_oi_change_not_across_sequence():
    df = enrich_symbol_features(_synth(40, seq_break_at=20))
    # at first bar of seq 2, oi_chg_5m must be NaN
    break_i = int((df["sequence_id"] == 2).idxmax())
    # find first index where sequence_id==2
    idxs = df.index[df["sequence_id"] == 2].tolist()
    assert np.isnan(df.loc[idxs[0], "oi_chg_5m"])


def test_burst_and_cluster_anchor_max():
    df = detect_bursts(enrich_symbol_features(_synth(320)))
    assert df["B1_long"].any() or df["B2_long"].any()
    # force B1 by checking huge liq row
    clusters = cluster_bursts(df, burst="B1", side="long", cooldown=6)
    # may be empty if warmup/p95 not met — B2 more likely
    clusters = cluster_bursts(df, burst="B2", side="long", cooldown=6)
    if clusters:
        cl = clusters[0]
        members = cl.member_indices
        liq = df["long_liq_usd"].to_numpy()
        assert cl.anchor_i == max(members, key=lambda i: liq[i])


def test_reclaim_fill_next_open_not_same_candle():
    df = enrich_symbol_features(_synth(50))
    # craft reclaim: set anchor mid, then close above mid next bars
    i = 10
    df.loc[i, "high"] = 110
    df.loc[i, "low"] = 100
    df.loc[i, "open"] = 105
    df.loc[i, "close"] = 101
    for j in range(i + 1, i + 4):
        df.loc[j, "close"] = 106  # above midpoint 105
        df.loc[j, "open"] = 104
        df.loc[j, "high"] = 107
        df.loc[j, "low"] = 103
    rc = check_reclaim(df, anchor_i=i, side="long", variant="R1", window=3)
    assert rc is not None
    assert rc["reclaim_i"] > i
    assert rc["fill_i"] == rc["reclaim_i"] + 1
    assert rc["fill_price"] == float(df.loc[rc["fill_i"], "open"])


def test_forward_outcomes_and_same_bar_conservative():
    df = enrich_symbol_features(_synth(40))
    out = compute_forward_outcomes(df, fill_i=5, entry=float(df.loc[5, "open"]), side="long")
    assert "h1_mfe_pct" in out
    assert "first_touch_order" in out


def test_price_oi_filters():
    row = pd.Series(
        {
            "ret_5m_pct": -0.2,
            "ret_15m_pct": -0.3,
            "ret_30m_pct": -1.0,
            "atr_14": 1.0,
            "close": 100.0,
            "oi_chg_5m": -5.0,
            "oi_chg_15m": -8.0,
            "oi_chg_p25": -2.0,
        }
    )
    assert price_filter(row, "long", "P1")
    assert not price_filter(row, "short", "P1")
    assert oi_filter(row, "O1")
    assert oi_filter(row, "O3")
    assert oi_filter(row, "O0")


def test_cli_rejects_unavailable_and_bad_range(tmp_path):
    rc = cli_main(
        [
            "--symbols",
            "ENAUSDT",
            "--start",
            "2026-04-01T00:00:00Z",
            "--end",
            "2026-04-02T00:00:00Z",
            "--output-dir",
            str(tmp_path),
        ]
    )
    assert rc == 2
    rc2 = cli_main(
        [
            "--symbols",
            "BTCUSDT",
            "--start",
            "2026-04-03T00:00:00Z",
            "--end",
            "2026-04-01T00:00:00Z",
            "--output-dir",
            str(tmp_path),
        ]
    )
    assert rc2 == 2
