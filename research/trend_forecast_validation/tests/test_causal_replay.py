"""Causal replay / prefix invariance tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from research.trend_forecast_validation.causal_replay import (
    prefix_invariance_check,
    run_causal_scanner_replay,
)
from research.trend_forecast_validation.config import default_config


def _synthetic_ohlcv(n: int = 800) -> pd.DataFrame:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    px = 10.0
    for i in range(n):
        # mild trend + oscillation so structure can form
        px = px * (1.0 + (0.0015 if (i // 20) % 2 == 0 else -0.0012))
        ts = pd.Timestamp(base) + pd.Timedelta(minutes=5 * i)
        rows.append(
            {
                "timestamp": ts,
                "open": px,
                "high": px * 1.002,
                "low": px * 0.998,
                "close": px,
                "volume": 1000.0,
            }
        )
    return pd.DataFrame(rows)


def test_prefix_invariance_on_synthetic() -> None:
    cfg = default_config()
    df = _synthetic_ohlcv(900)
    # Use shortened calendar so warmup masks don't matter for structure equality
    result = prefix_invariance_check(df, n=500, cfg=cfg)
    assert result["equal"], result


def test_replay_deterministic() -> None:
    cfg = default_config()
    df = _synthetic_ohlcv(600)
    a, _ = run_causal_scanner_replay(df, cfg)
    b, _ = run_causal_scanner_replay(df, cfg)
    cols = ["protected_structure_state", "major_direction", "external_bos_up", "external_bos_down"]
    assert a[cols].equals(b[cols])
