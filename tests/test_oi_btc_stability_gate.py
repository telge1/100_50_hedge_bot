"""Focused BTCUSDT stability-gate tests. No live trading, no extra CH writes."""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from orderbook_analyse.oi_liquidation_collector.collector import OILiquidationCollector
from orderbook_analyse.oi_liquidation_collector.health_logic import (
    DeadConnection,
    is_pong_payload,
    liquidation_stream_healthy,
    market_data_stale,
    next_backoff,
    pong_timed_out,
    resubscribe_topics,
)
from orderbook_analyse.oi_liquidation_collector.locks import (
    SingleInstanceLock,
    cmdline_is_collector_python,
)
from orderbook_analyse.oi_liquidation_collector.logic import OIState, floor_5s
from orderbook_analyse.oi_liquidation_collector.settings import OICollectorSettings
from orderbook_analyse.oi_liquidation_collector.universe import UniversePlan


RECEIVED = datetime(2026, 8, 18, 15, 0, 0, tzinfo=timezone.utc)


def _settings() -> OICollectorSettings:
    return OICollectorSettings(
        bybit_ws_url="wss://stream.bybit.com/v5/public/linear",
        bybit_rest_url="https://api.bybit.com",
        clickhouse_host="127.0.0.1",
        clickhouse_http_port=8123,
        clickhouse_database="orderbook_analysis",
        clickhouse_user="u",
        clickhouse_password="x",
        universe_path=Path("/tmp/u.json"),
        lock_path=Path("/tmp/x.lock"),
        pid_path=Path("/tmp/x.pid"),
        ping_interval_sec=20.0,
        ping_timeout_sec=20.0,
        stale_data_sec=15.0,
        reconnect_initial_sec=1.0,
        reconnect_max_sec=4.0,
    )


def _plan() -> UniversePlan:
    return UniversePlan(
        source_path="x",
        requested=("BTCUSDT",),
        supported=("BTCUSDT",),
        subscribed=("BTCUSDT",),
        decisions=[],
        universe_hash="h",
    )


class _NullWriter:
    rows_inserted = 0
    insert_errors = 0
    queue_drops = 0
    queue_size = 0

    async def enqueue(self, table, recs):
        return 0


def _collector() -> OILiquidationCollector:
    return OILiquidationCollector(_settings(), _plan(), _NullWriter())  # type: ignore[arg-type]


def test_ping_is_recorded_when_sent() -> None:
    col = _collector()
    sent = col.note_ping_sent(now_mono=10.0)
    assert sent["op"] == "ping"
    assert col.stats.ping_count == 1
    assert col.stats.last_ping_mono == 10.0


def test_pong_updates_health_latency() -> None:
    col = _collector()
    col.note_ping_sent(now_mono=10.0)
    latency = col.note_pong(now_mono=10.05)
    assert latency == 50
    assert col.stats.ping_ok is True
    assert col.stats.pong_count == 1
    assert col.stats.pong_latency_ms == 50
    assert is_pong_payload({"op": "ping", "ret_msg": "pong", "success": True})


def test_pong_timeout_triggers_reconnect() -> None:
    col = _collector()
    col.note_ping_sent(now_mono=0.0)
    with pytest.raises(DeadConnection, match="pong_timeout"):
        col.check_liveness(now_mono=20.0)
    assert pong_timed_out(last_ping_mono=0.0, last_pong_mono=None, now_mono=20.0, timeout_sec=20.0)


def test_socket_close_is_reconnect_reason() -> None:
    col = _collector()
    st = col.states["BTCUSDT"]
    st.apply_ticker(
        {"type": "snapshot", "ts": 1, "data": {"symbol": "BTCUSDT", "openInterest": "1", "openInterestValue": "2"}},
        received_at=RECEIVED,
    )
    assert st.valid
    col.mark_disconnect("ConnectionClosed")
    assert st.valid is False
    assert col.stats.ws_connected is False


def test_backoff_is_capped() -> None:
    assert next_backoff(1.0, initial=1.0, cap=4.0) == 2.0
    assert next_backoff(2.0, initial=1.0, cap=4.0) == 4.0
    assert next_backoff(4.0, initial=1.0, cap=4.0) == 4.0
    assert next_backoff(8.0, initial=1.0, cap=4.0) == 4.0


def test_resubscribe_both_btc_topics() -> None:
    topics = resubscribe_topics(["BTCUSDT"])
    assert topics == ["tickers.BTCUSDT", "allLiquidation.BTCUSDT"]


def test_oi_invalidated_on_disconnect_no_stale_snapshot() -> None:
    col = _collector()
    st = col.states["BTCUSDT"]
    st.apply_ticker(
        {"type": "snapshot", "ts": 1, "data": {"symbol": "BTCUSDT", "openInterest": "1", "openInterestValue": "2"}},
        received_at=RECEIVED,
    )
    col.mark_disconnect("pong_timeout")
    assert st.valid is False
    assert st.snapshot_5s_row(bucket_time=floor_5s(RECEIVED), now=RECEIVED, collector_instance_id="t") is None


def test_new_snapshot_revalidates_delta_does_not() -> None:
    st = OIState(symbol="BTCUSDT")
    st.invalidate()
    delta = st.apply_ticker(
        {"type": "delta", "ts": 2, "data": {"symbol": "BTCUSDT", "openInterest": "9", "openInterestValue": "3"}},
        received_at=RECEIVED,
    )
    assert delta["action"] == "ignored_no_snapshot"
    assert st.valid is False
    snap = st.apply_ticker(
        {"type": "snapshot", "ts": 3, "data": {"symbol": "BTCUSDT", "openInterest": "7", "openInterestValue": "4"}},
        received_at=RECEIVED,
    )
    assert snap["action"] == "initialized"
    assert st.valid is True


def test_single_instance_guard(tmp_path: Path) -> None:
    a = SingleInstanceLock(tmp_path / "c.lock", tmp_path / "c.pid")
    b = SingleInstanceLock(tmp_path / "c.lock", tmp_path / "c.pid")
    a.acquire()
    with pytest.raises(RuntimeError):
        b.acquire()
    a.release()
    b.acquire()
    b.release()


def test_cmdline_ignores_bash_wrapper_and_backfill() -> None:
    assert cmdline_is_collector_python(
        "/home/x/.venv/bin/python -m orderbook_analyse.oi_liquidation_collector --mode live",
        "python",
    )
    assert not cmdline_is_collector_python(
        "bash -c python -m orderbook_analyse.oi_liquidation_collector --mode live",
        "bash",
    )
    assert not cmdline_is_collector_python(
        "/home/x/.venv/bin/python -m orderbook_analyse.oi_liquidation_collector.backfill --days 30",
        "python",
    )


def test_liquidation_stream_healthy_without_events() -> None:
    assert liquidation_stream_healthy(
        ws_connected=True,
        subscription_confirmed=True,
        ping_ok=True,
        liq_topic_subscribed=True,
        last_liquidation_at=None,
    )
    assert not liquidation_stream_healthy(
        ws_connected=True,
        subscription_confirmed=False,
        ping_ok=True,
        liq_topic_subscribed=True,
    )


def test_no_trading_functions() -> None:
    src = inspect.getsource(OILiquidationCollector)
    for needle in ("place_order", "/v5/order", "private/", "signal_generator"):
        assert needle not in src


def test_stale_market_reconnect() -> None:
    assert market_data_stale(last_market_mono=0.0, now_mono=15.0, stale_sec=15.0)
    assert not market_data_stale(last_market_mono=0.0, now_mono=14.0, stale_sec=15.0)
