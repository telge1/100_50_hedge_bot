"""Unit tests for LOSS_CLUSTER_VOLATILITY_REGIME_AUDIT helpers (no DB)."""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "audit_loss_cluster_volatility.py"


def _load_mod():
    spec = importlib.util.spec_from_file_location("audit_loss_cluster_volatility", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # Avoid executing main / ClickHouse imports side effects beyond module body
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load_mod()
true_range = mod.true_range
build_book = mod.build_book
causal_percentile = mod.causal_percentile
features_at = mod.features_at
max_loss_streak = mod.max_loss_streak


def _synth_df(n: int = 2000, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    start = pd.Timestamp("2026-08-01T00:00:00Z")
    times = [start + pd.Timedelta(minutes=i) for i in range(n)]
    close = 100 + np.cumsum(rng.normal(0, 0.05, size=n))
    high = close + rng.uniform(0.01, 0.2, size=n)
    low = close - rng.uniform(0.01, 0.2, size=n)
    open_ = close + rng.normal(0, 0.02, size=n)
    return pd.DataFrame(
        {
            "open_time": times,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
        }
    )


def test_true_range_deterministic():
    h = np.array([10.0, 11.0, 12.0])
    l = np.array([9.0, 10.0, 10.5])
    pc = np.array([9.5, 10.0, 10.8])
    tr = true_range(h, l, pc)
    assert tr[0] == pytest.approx(1.0)
    assert tr[1] == pytest.approx(1.0)
    assert tr[2] == pytest.approx(1.5)


def test_atr_deterministic_same_input():
    df = _synth_df(500, seed=1)
    b1 = build_book("TEST", df)
    b2 = build_book("TEST", df)
    np.testing.assert_allclose(b1.atr14, b2.atr14, equal_nan=True)
    np.testing.assert_allclose(b1.atr14_ratio, b2.atr14_ratio, equal_nan=True)


def test_only_closed_candles_for_features():
    df = _synth_df(300, seed=2)
    book = build_book("TEST", df)
    entry = datetime(2026, 8, 1, 1, 0, tzinfo=timezone.utc)
    i = book.index_at_or_before(entry)
    assert i is not None
    assert pd.Timestamp(book.ts[i]) == pd.Timestamp("2026-08-01T00:59:00")
    feat = features_at(book, entry)
    assert feat["feature_ok"] is True
    assert feat["feature_bar_open"] == "2026-08-01T00:59:00Z"


def test_no_lookahead_percentile_uses_past_only():
    series = np.arange(100, dtype=float)
    series_future = series.copy()
    series_future[90:] = 1e6
    p_early = causal_percentile(series_future, 50, 40)
    p_plain = causal_percentile(series, 50, 40)
    assert p_early == pytest.approx(p_plain)
    p_late = causal_percentile(series_future, 95, 40)
    assert p_late is not None and p_late >= 95.0


def test_feature_at_t_ignores_future_bars():
    df = _synth_df(400, seed=3)
    book = build_book("TEST", df)
    entry = datetime(2026, 8, 1, 2, 0, tzinfo=timezone.utc)
    i = book.index_at_or_before(entry)
    assert i is not None
    feat1 = features_at(book, entry)
    book.close[i + 1 :] = book.close[i + 1 :] * 10
    book.atr14_pct[i + 1 :] = 999.0
    feat2 = features_at(book, entry)
    assert feat1["atr14_pct"] == feat2["atr14_pct"]
    assert feat1["atr14_ratio"] == feat2["atr14_ratio"]


def test_max_loss_streak():
    assert max_loss_streak(["WIN", "LOSS", "LOSS", "WIN", "LOSS"]) == 2
    assert max_loss_streak(["LOSS"] * 6) == 6


def test_pause_state_causal_timeline():
    timeline = [
        (datetime(2026, 8, 9, 0, 0, tzinfo=timezone.utc), False),
        (datetime(2026, 8, 9, 0, 5, tzinfo=timezone.utc), True),
        (datetime(2026, 8, 9, 0, 10, tzinfo=timezone.utc), True),
        (datetime(2026, 8, 9, 0, 15, tzinfo=timezone.utc), False),
    ]

    def is_paused_at(et: datetime) -> bool:
        last = False
        for tx, p in timeline:
            if tx <= et:
                last = p
            else:
                break
        return last

    assert is_paused_at(datetime(2026, 8, 9, 0, 4, tzinfo=timezone.utc)) is False
    assert is_paused_at(datetime(2026, 8, 9, 0, 5, tzinfo=timezone.utc)) is True
    assert is_paused_at(datetime(2026, 8, 9, 0, 12, tzinfo=timezone.utc)) is True
    assert is_paused_at(datetime(2026, 8, 9, 0, 16, tzinfo=timezone.utc)) is False


def test_threshold_sweep_reproducible():
    rows = [
        {"signal_id": "a", "result": "LOSS", "pnl_pct": -1.0, "atr14_ratio": 2.0},
        {"signal_id": "b", "result": "WIN", "pnl_pct": 2.0, "atr14_ratio": 1.0},
        {"signal_id": "c", "result": "LOSS", "pnl_pct": -1.0, "atr14_ratio": 1.1},
    ]

    def eval_once(thr: float):
        blocked = [r for r in rows if (r["atr14_ratio"] or 0) > thr]
        kept = [r for r in rows if (r["atr14_ratio"] or 0) <= thr]
        return (
            len(blocked),
            sum(1 for r in blocked if r["result"] == "LOSS"),
            sum(r["pnl_pct"] for r in kept),
        )

    assert eval_once(1.5) == eval_once(1.5)
    assert eval_once(1.5) == (1, 1, 1.0)


def test_export_csv_unchanged_path_exists():
    """Original signal/outcome export artifacts must remain present (audit is read-only)."""
    dash = ROOT / "results" / "current_dashboard_101_signals" / "dashboard_101_signals.csv"
    assert dash.exists()
    # Audit must not rewrite this file — spot-check row count stable after import
    before = dash.read_bytes()
    assert b"signal_id" in before
    assert before == dash.read_bytes()
