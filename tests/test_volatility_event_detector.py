"""Unit tests for research/volatility_event_detector (no ClickHouse)."""

from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "research"))

from volatility_event_detector.causality import (  # noqa: E402
    assert_no_lookahead_baseline,
    classify_severity,
    compute_volatility_frame,
    direction_from_return,
)
from volatility_event_detector.cli import validate_single_symbol  # noqa: E402
from volatility_event_detector.episodes import (  # noqa: E402
    RawEvent,
    cluster_episodes,
    raw_events_from_frame,
)


def _candle_frame(n: int = 1600, start: datetime | None = None) -> pd.DataFrame:
    start = start or datetime(2026, 8, 11, 0, 0, 0)
    times = [start + timedelta(minutes=i) for i in range(n)]
    rng = np.random.default_rng(0)
    rets = rng.normal(0, 0.0005, size=n)
    rets[1440 + 60] = 0.02
    close = 1.0 * np.exp(np.cumsum(rets))
    return pd.DataFrame(
        {
            "open_time": times,
            "open": close,
            "high": close * 1.001,
            "low": close * 0.999,
            "close": close,
        }
    )


def test_baseline_excludes_current_and_has_no_lookahead():
    df = _candle_frame()
    frame = compute_volatility_frame(df)
    rv = frame["rv_1m"].to_numpy()
    base = frame["rv_1m_baseline_24h"].to_numpy()
    assert np.all(np.isnan(base[:1440]))
    for i in (1440, 1445, 1490):
        window = rv[i - 1440 : i]
        expected = float(np.nanmedian(window))
        assert math.isclose(float(base[i]), expected, rel_tol=1e-9, abs_tol=1e-15)
        assert len(window) == 1440
    assert_no_lookahead_baseline(frame["rv_1m"], frame["rv_1m_baseline_24h"])


def test_current_rv_not_in_own_baseline_construction():
    n = 1445
    times = [datetime(2026, 8, 11) + timedelta(minutes=i) for i in range(n)]
    close = np.ones(n, dtype=float)
    for i in range(1, n):
        close[i] = close[i - 1] * math.exp(0.001)
    close[-1] = close[-2] * math.exp(1.0)
    df = pd.DataFrame(
        {"open_time": times, "open": close, "high": close, "low": close, "close": close}
    )
    frame = compute_volatility_frame(df)
    last_base = float(frame["rv_1m_baseline_24h"].iloc[-1])
    last_rv = float(frame["rv_1m"].iloc[-1])
    assert last_rv > 0.5
    assert last_base < 0.01
    prev = frame["rv_1m"].iloc[-1441:-1].to_numpy()
    assert math.isclose(last_base, float(np.nanmedian(prev)), rel_tol=1e-9)


@pytest.mark.parametrize(
    "ratio,expected",
    [
        (1.9, None),
        (2.0, "2x"),
        (2.5, "2x"),
        (3.0, "3x"),
        (4.9, "3x"),
        (5.0, "5x"),
        (9.9, "5x"),
        (10.0, "10x"),
        (25.0, "10x"),
        (float("nan"), None),
        (float("inf"), None),
    ],
)
def test_severity_classification(ratio, expected):
    assert classify_severity(ratio) == expected


@pytest.mark.parametrize(
    "ret,expected",
    [
        (0.01, "PUMP"),
        (-0.01, "DUMP"),
        (0.0, None),
        (float("nan"), None),
    ],
)
def test_direction(ret, expected):
    assert direction_from_return(ret) == expected


def test_episode_clustering_gap_and_direction():
    base = datetime(2026, 8, 12, 10, 0, 0)

    def ev(minute: int, direction: str, ratio: float) -> RawEvent:
        return RawEvent(
            symbol="ADAUSDT",
            event_time=base + timedelta(minutes=minute),
            direction=direction,
            severity="3x",
            vol_ratio_max=ratio,
            rv_1m_ratio=ratio,
            rv_5m_ratio=None,
            rv_15m_ratio=None,
            log_return_1m=0.01 if direction == "PUMP" else -0.01,
            price_change_1m=None,
            price_change_3m=None,
            price_change_5m=None,
            price_change_15m=None,
            price_change_30m=None,
        )

    events = [
        ev(0, "PUMP", 3.0),
        ev(2, "PUMP", 5.0),
        ev(6, "PUMP", 2.5),
        ev(7, "DUMP", 4.0),
    ]
    eps = cluster_episodes(events)
    assert len(eps) == 3
    assert eps[0]["event_peak"] == base + timedelta(minutes=2)
    assert eps[0]["vol_ratio_max"] == 5.0
    assert eps[0]["raw_event_count"] == 2
    assert eps[2]["direction"] == "DUMP"


def test_post_event_values_do_not_affect_detection():
    df = _candle_frame()
    frame = compute_volatility_frame(df)
    idx = 1450
    past_ratio = float(frame.loc[idx, "vol_ratio_max"])
    df2 = df.copy()
    df2.loc[idx + 1 :, "close"] = df2.loc[idx + 1 :, "close"] * 10.0
    frame2 = compute_volatility_frame(df2)
    assert math.isclose(float(frame2.loc[idx, "vol_ratio_max"]), past_ratio, rel_tol=1e-12)


def test_division_by_zero_nan_inf_safe():
    times = [datetime(2026, 8, 11) + timedelta(minutes=i) for i in range(10)]
    close = [1.0, 0.0, 1.0, -1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0]
    df = pd.DataFrame(
        {"open_time": times, "open": close, "high": close, "low": close, "close": close}
    )
    frame = compute_volatility_frame(df)
    assert not np.isinf(frame["rv_1m_ratio"].fillna(0)).any()
    assert classify_severity(float("inf")) is None
    assert classify_severity(float("nan")) is None


def test_cli_blocks_universe_run():
    with pytest.raises(SystemExit):
        validate_single_symbol("ALL")
    with pytest.raises(SystemExit):
        validate_single_symbol("universe51")
    with pytest.raises(SystemExit):
        validate_single_symbol("ADAUSDT,BTCUSDT")
    with pytest.raises(SystemExit):
        validate_single_symbol("ADA BTC")
    assert validate_single_symbol("adausdt") == "ADAUSDT"


def test_raw_events_only_in_evaluation_window():
    df = _candle_frame()
    frame = compute_volatility_frame(df)
    frame.loc[100, "vol_ratio_max"] = 5.0
    frame.loc[100, "severity"] = "5x"
    frame.loc[100, "direction"] = "PUMP"
    frame.loc[1460, "vol_ratio_max"] = 5.0
    frame.loc[1460, "severity"] = "5x"
    frame.loc[1460, "direction"] = "DUMP"
    warm_end = datetime(2026, 8, 11, 23, 59)
    ev_start = datetime(2026, 8, 12, 0, 0)
    ev_end = datetime(2026, 8, 12, 23, 59)
    events = raw_events_from_frame(
        frame, symbol="ADAUSDT", evaluation_start=ev_start, evaluation_end=ev_end
    )
    assert all(ev_start <= e.event_time <= ev_end for e in events)
    assert all(e.event_time > warm_end for e in events)
