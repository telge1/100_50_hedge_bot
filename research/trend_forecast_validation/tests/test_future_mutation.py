"""Future-mutation: changing candles after N must not alter results ≤ N."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd

from research.trend_forecast_validation.causal_replay import run_causal_scanner_replay
from research.trend_forecast_validation.config import default_config
from research.trend_forecast_validation.signal_extractor import extract_forecast_signals


def _ohlcv(n: int = 700) -> pd.DataFrame:
    base = datetime(2026, 1, 15, tzinfo=timezone.utc)
    rows = []
    px = 7.0
    for i in range(n):
        px *= 1.0 + (0.001 if (i // 25) % 2 == 0 else -0.0008)
        ts = pd.Timestamp(base) + pd.Timedelta(minutes=5 * i)
        rows.append(
            {
                "timestamp": ts,
                "open": px,
                "high": px * 1.002,
                "low": px * 0.998,
                "close": px,
                "volume": 10.0,
            }
        )
    return pd.DataFrame(rows)


def test_future_mutation_does_not_change_past() -> None:
    cfg = default_config()
    n = 400
    base = _ohlcv(700)
    mutated = base.copy()
    # Extreme future mutation after n
    mutated.loc[n:, "high"] = mutated.loc[n:, "high"] * 5.0
    mutated.loc[n:, "low"] = mutated.loc[n:, "low"] * 0.2
    mutated.loc[n:, "close"] = mutated.loc[n:, "high"]

    a, _ = run_causal_scanner_replay(base, cfg)
    b, _ = run_causal_scanner_replay(mutated, cfg)
    cols = [
        c
        for c in [
            "protected_structure_state",
            "major_direction",
            "protected_high",
            "protected_low",
            "external_bos_up",
            "external_bos_down",
            "choch_side",
        ]
        if c in a.columns
    ]
    left = a.iloc[:n][cols].reset_index(drop=True)
    right = b.iloc[:n][cols].reset_index(drop=True)
    for c in cols:
        if pd.api.types.is_float_dtype(left[c]) or pd.api.types.is_numeric_dtype(left[c]):
            assert np.allclose(
                pd.to_numeric(left[c], errors="coerce").fillna(0),
                pd.to_numeric(right[c], errors="coerce").fillna(0),
                equal_nan=True,
                atol=1e-9,
            ), c
        else:
            assert left[c].astype(str).equals(right[c].astype(str)), c

    sa = extract_forecast_signals(a.iloc[:n].copy(), cfg)
    sb = extract_forecast_signals(b.iloc[:n].copy(), cfg)
    # Compare detected timestamps + types on prefix-only extraction
    if not sa.empty or not sb.empty:
        ka = set(zip(sa.get("detected_timestamp", []), sa.get("signal_type", []))) if not sa.empty else set()
        kb = set(zip(sb.get("detected_timestamp", []), sb.get("signal_type", []))) if not sb.empty else set()
        # Signals whose detected bar < n should match; filter by bar_index if present
        if "bar_index" in sa.columns:
            sa = sa.loc[sa["bar_index"] < n]
            sb = sb.loc[sb["bar_index"] < n]
            ka = set(zip(sa["detected_timestamp"], sa["signal_type"]))
            kb = set(zip(sb["detected_timestamp"], sb["signal_type"]))
            assert ka == kb
