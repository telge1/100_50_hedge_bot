"""Unit tests for fractal cycle wave efficiency analysis (synthetic; no MySQL)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_cycle_wave_analysis.analysis import decide_visibility
from orderbook_analyse.fractal_cycle_wave_analysis.indicators import attach_indicators
from orderbook_analyse.fractal_cycle_wave_analysis.waves import (
    segment_stoch_waves,
    summarize_tf_waves,
)


def _synth_ohlcv(n: int = 400, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    # trending up with noise → more efficient UP waves expected in aggregate
    rets = rng.normal(0.0005, 0.004, size=n)
    close = 10.0 * np.exp(np.cumsum(rets))
    high = close * (1.0 + rng.uniform(0.0005, 0.003, size=n))
    low = close * (1.0 - rng.uniform(0.0005, 0.003, size=n))
    open_ = np.r_[close[0], close[:-1]]
    ts = pd.date_range("2024-01-01", periods=n, freq="5min", tz="UTC")
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": rng.uniform(1, 10, size=n),
            "close_time": ts + pd.Timedelta(minutes=5),
            "available_at": ts + pd.Timedelta(minutes=5),
        }
    )


def test_indicators_and_wave_segmentation() -> None:
    raw = _synth_ohlcv()
    ind = attach_indicators(raw)
    assert "stoch_k" in ind.columns and "cci" in ind.columns and "ema20" in ind.columns
    waves = segment_stoch_waves(ind)
    assert not waves.empty
    assert set(waves["direction"].unique()).issubset({"UP", "DOWN"})
    assert (waves["n_bars"] >= 3).all()
    assert "directional_efficiency" in waves.columns
    assert "rsi_gt50_share" in waves.columns


def test_summarize_and_visibility_helper() -> None:
    raw = _synth_ohlcv(n=600, seed=3)
    ind = attach_indicators(raw)
    waves = segment_stoch_waves(ind)
    summary = summarize_tf_waves(waves, timeframe="5m")
    assert summary["n_waves"] > 0
    # Build fake multi-TF coherent summaries
    fake = []
    for tf in ("1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w", "1M"):
        fake.append(
            {
                "timeframe": tf,
                "asymmetry": {
                    "directionally_coherent": True,
                    "signed_up_mean": 0.2,
                    "signed_down_mean": -0.15,
                },
            }
        )
    vis = decide_visibility(fake)
    assert vis["decision"] == "FRACTAL_WAVE_EFFICIENCY_VISIBLE"

    weak = [
        {
            "timeframe": "5m",
            "asymmetry": {
                "directionally_coherent": True,
                "signed_up_mean": 0.01,
                "signed_down_mean": -0.01,
            },
        },
        {
            "timeframe": "15m",
            "asymmetry": {
                "directionally_coherent": True,
                "signed_up_mean": 0.01,
                "signed_down_mean": -0.01,
            },
        },
        {
            "timeframe": "1h",
            "asymmetry": {
                "directionally_coherent": True,
                "signed_up_mean": 0.01,
                "signed_down_mean": -0.01,
            },
        },
    ]
    vis2 = decide_visibility(weak)
    assert vis2["decision"] == "FRACTAL_WAVE_EFFICIENCY_WEAK"
