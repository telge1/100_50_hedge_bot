"""Read-only audit: ema_only must not load orderbook / trades / OI / liq."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

from orderbook_analyse.ema_zone_microstructure_confirmation.continuous_engine import (
    process_symbol_stream,
)
from orderbook_analyse.ema_zone_microstructure_confirmation.continuous_runner import (
    candle_analysis_samples,
    run_symbol,
)
from orderbook_analyse.ema_zone_microstructure_confirmation.coverage import probe_symbol_coverage
from orderbook_analyse.ema_zone_microstructure_confirmation.research_layers import (
    COMPUTATION_MODE_EMA_ONLY,
    COMPUTATION_MODE_EMA_PLUS_MICRO,
)
from orderbook_analyse.ema_zone_microstructure_confirmation.regime import prepare_bars_with_ema200


def _bars(n_min: int = 2500, px0: float = 100_000.0) -> pd.DataFrame:
    times = pd.date_range("2026-08-20", periods=n_min, freq="1min", tz="UTC")
    px = px0
    rows = []
    for t in times:
        px *= 1.00001
        rows.append({"open_time": t, "open": px, "high": px * 1.0001, "low": px * 0.9999, "close": px})
    return prepare_bars_with_ema200(pd.DataFrame(rows))


def test_ema_only_coverage_requires_candles_only(monkeypatch):
    calls: list[str] = []

    def _track(name):
        def _fn(*args, **kwargs):
            calls.append(name)
            return []

        return _fn

    monkeypatch.setattr(
        "orderbook_analyse.ema_zone_microstructure_confirmation.coverage.list_closed_segments",
        _track("list_closed_segments"),
    )
    monkeypatch.setattr(
        "orderbook_analyse.ema_zone_microstructure_confirmation.coverage.load_clickhouse_settings",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "orderbook_analyse.ema_zone_microstructure_confirmation.coverage.get_clickhouse_client",
        lambda: MagicMock(
            query=lambda sql, parameters=None, settings=None: MagicMock(
                result_rows=[(
                    datetime(2026, 1, 1, tzinfo=timezone.utc),
                    datetime(2026, 1, 2, tzinfo=timezone.utc),
                    1440,
                )]
            )
        ),
    )

    cov = probe_symbol_coverage(
        symbol="DOGEUSDT",
        raw_root=Path("/tmp/unused"),
        computation_mode=COMPUTATION_MODE_EMA_ONLY,
    )
    assert cov["status"] == "OK"
    assert cov["data_basis"] == "candles_1m"
    assert cov["orderbook_required"] is False
    assert cov["computation_mode"] == COMPUTATION_MODE_EMA_ONLY
    assert "list_closed_segments" not in calls


def test_ema_plus_micro_coverage_still_probes_orderbook(monkeypatch):
    calls: list[str] = []

    monkeypatch.setattr(
        "orderbook_analyse.ema_zone_microstructure_confirmation.coverage.list_closed_segments",
        lambda *a, **k: calls.append("list_closed_segments") or [],
    )
    monkeypatch.setattr(
        "orderbook_analyse.ema_zone_microstructure_confirmation.coverage.load_clickhouse_settings",
        lambda *a, **k: None,
    )

    def _client():
        def _q(sql, parameters=None, settings=None):
            if "candles_1m" in sql:
                return MagicMock(
                    result_rows=[(
                        datetime(2026, 1, 1, tzinfo=timezone.utc),
                        datetime(2026, 1, 2, tzinfo=timezone.utc),
                        1440,
                    )]
                )
            if "liquidity" in sql:
                return MagicMock(result_rows=[[0]])
            return MagicMock(result_rows=[(None, None, 0)])

        return MagicMock(query=_q)

    monkeypatch.setattr(
        "orderbook_analyse.ema_zone_microstructure_confirmation.coverage.get_clickhouse_client",
        _client,
    )

    cov = probe_symbol_coverage(
        symbol="DOGEUSDT",
        raw_root=Path("/tmp/unused"),
        computation_mode=COMPUTATION_MODE_EMA_PLUS_MICRO,
    )
    assert cov["orderbook_required"] is True
    assert "list_closed_segments" in calls


def test_run_symbol_ema_only_skips_orderbook_loaders(monkeypatch):
    bars = _bars()
    candles = pd.DataFrame(
        {
            "open_time": bars["open_time"],
            "open": bars["open"],
            "high": bars["high"],
            "low": bars["low"],
            "close": bars["close"],
        }
    )
    start = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    end = datetime(2026, 8, 20, 14, 0, tzinfo=timezone.utc)
    coverage = {
        "status": "OK",
        "discovery_start": start.isoformat().replace("+00:00", "Z"),
        "discovery_end": end.isoformat().replace("+00:00", "Z"),
    }
    invoked: list[str] = []

    monkeypatch.setattr(
        "orderbook_analyse.ema_zone_microstructure_confirmation.continuous_runner.load_clickhouse_settings",
        lambda: None,
    )
    monkeypatch.setattr(
        "orderbook_analyse.ema_zone_microstructure_confirmation.continuous_runner.get_clickhouse_client",
        lambda: MagicMock(),
    )
    monkeypatch.setattr(
        "orderbook_analyse.ema_zone_microstructure_confirmation.continuous_runner.load_candles_1m",
        lambda *a, **k: invoked.append("load_candles_1m") or candles,
    )
    monkeypatch.setattr(
        "orderbook_analyse.ema_zone_microstructure_confirmation.continuous_runner.fetch_oi_1m",
        lambda *a, **k: invoked.append("fetch_oi_1m") or pd.DataFrame(),
    )
    monkeypatch.setattr(
        "orderbook_analyse.ema_zone_microstructure_confirmation.continuous_runner.fetch_liquidations",
        lambda *a, **k: invoked.append("fetch_liquidations") or pd.DataFrame(),
    )
    monkeypatch.setattr(
        "orderbook_analyse.ema_zone_microstructure_confirmation.continuous_runner.replay_analysis_samples",
        lambda *a, **k: invoked.append("replay_analysis_samples") or [],
    )
    monkeypatch.setattr(
        "orderbook_analyse.ema_zone_microstructure_confirmation.continuous_runner.make_trades_loader",
        lambda *a, **k: invoked.append("make_trades_loader") or (lambda _a, _b: pd.DataFrame()),
    )
    monkeypatch.setattr(
        "orderbook_analyse.ema_zone_microstructure_confirmation.continuous_runner.process_symbol_stream",
        lambda **kwargs: invoked.append("process_symbol_stream")
        or {
            "candidate_events": [],
            "zone_watch_events": [],
            "ema_setup_events": [{"setup_id": "s1"}],
            "microstructure_confirmation_events": [],
        },
    )

    result = run_symbol(
        symbol="DOGEUSDT",
        raw_root=Path("/tmp/ob"),
        coverage=coverage,
        computation_mode=COMPUTATION_MODE_EMA_ONLY,
    )
    assert result["status"] == "OK"
    assert result["quality"]["data_basis"] == "candles_1m"
    assert result["quality"]["touch_price_basis"] == "candle_ohlc_1m"
    assert result["quality"]["orderbook_loaded"] is False
    assert "load_candles_1m" in invoked
    assert "fetch_oi_1m" not in invoked
    assert "fetch_liquidations" not in invoked
    assert "replay_analysis_samples" not in invoked
    assert "make_trades_loader" not in invoked
    assert "process_symbol_stream" in invoked


def test_candle_analysis_samples_use_close_not_orderbook():
    bars = _bars()
    candles = pd.DataFrame(
        {
            "open_time": bars["open_time"].iloc[-120:],
            "open": bars["open"].iloc[-120:],
            "high": bars["high"].iloc[-120:],
            "low": bars["low"].iloc[-120:],
            "close": bars["close"].iloc[-120:],
        }
    )
    start = candles["open_time"].iloc[10].to_pydatetime()
    end = candles["open_time"].iloc[-1].to_pydatetime() + pd.Timedelta(minutes=1)
    samples = candle_analysis_samples(candles_1m=candles, bars_5m=bars, start=start, end=end)
    assert samples
    assert all(s.source_file == "candles_1m_ohlc" for s in samples)
    assert all(s.bid_wall is None and s.ask_wall is None for s in samples)
    assert all(s.candle_low is not None and s.candle_high is not None for s in samples)


def test_incomplete_coverage_sources_are_json_safe():
    import json
    from datetime import datetime, timezone

    from orderbook_analyse.ema_zone_microstructure_confirmation.coverage import (
        SourceSpan,
        _source_span_dict,
    )

    span = SourceSpan(
        "candles_1m",
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        datetime(2026, 1, 2, tzinfo=timezone.utc),
        10,
        "OK",
    )
    payload = {"sources": [_source_span_dict(span)]}
    json.dumps(payload)


def test_process_symbol_stream_ema_only_never_calls_trades_loader():
    bars = _bars()
    base = int(datetime(2026, 8, 25, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)
    row = bars.iloc[-1]
    ema20 = float(row["ema20"])
    ema59 = float(row["ema59"])
    atr = float(row["atr"]) if float(row["atr"]) > 0 else 30.0
    from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.zone_replay import (
        AnalysisSample,
    )

    samples = [
        AnalysisSample(
            ts_ms=base + i * 60_000,
            mid=ema20 if i >= 5 else ema20 + 100,
            best_bid=ema20,
            best_ask=ema20 + 0.2,
            bid_levels=200,
            ask_levels=200,
            genuine=True,
            seq_gap=False,
            carried_forward=False,
            warmup=False,
            ema20=ema20,
            ema59=ema59,
            atr=atr,
            bid_wall=None,
            ask_wall=None,
            ask_in_ema20=None,
            bid_in_ema20=None,
            ask_in_ema59=None,
            bid_in_ema59=None,
            source_file="candles_1m_close",
        )
        for i in range(30)
    ]
    called = {"n": 0}

    def trades_loader(_a, _b):
        called["n"] += 1
        return pd.DataFrame()

    out = process_symbol_stream(
        symbol="BTCUSDT",
        samples=samples,
        bars=bars,
        trades_loader=trades_loader,
        oi=pd.DataFrame(),
        liq=pd.DataFrame(),
        tick=0.1,
        discovery_start_ms=base,
        discovery_end_ms=base + 30 * 60_000,
        computation_mode=COMPUTATION_MODE_EMA_ONLY,
    )
    assert called["n"] == 0
    assert out["microstructure_confirmation_events"] == []
    for ev in out["ema_setup_events"]:
        assert str(ev.get("candidate_direction") or "NONE").upper() in {"", "NONE"}
