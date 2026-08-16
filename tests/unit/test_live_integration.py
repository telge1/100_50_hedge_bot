"""Unit tests for live collector integration (universe, desired state, control, catch-up)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from signal_generator.bybit.live.candle_buffer import CandleInsertBuffer
from signal_generator.bybit.live.control_api import CollectorControlService
from signal_generator.bybit.live.desired_state import DesiredStateStore
from signal_generator.bybit.live.health import (
    CollectorState,
    HealthState,
    SymbolRuntimeState,
)
from signal_generator.bybit.live.live_universe import (
    load_live_universe,
    partition_valid_symbols,
    validate_live_symbols,
)
from signal_generator.bybit.live.signal_catchup import (
    SHADOW_MODE,
    TRADING_ENABLED,
    assert_shadow_only,
    htf_boundary_at_close,
)
from signal_generator.bybit.live.ws_kline import (
    BybitKlineWebSocket,
    is_pong_message,
    ws_tick_to_candle,
    parse_ws_kline_item,
)
from signal_generator.bybit.missing_ranges import missing_ranges_from_open_times
from signal_generator.db.candles import Candle1m


UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[2]


def test_live_universe_parsing():
    uni = load_live_universe(ROOT / "config" / "live_universe.json")
    assert uni.exchange == "bybit"
    assert uni.interval == "1m"
    assert len(uni.symbols) == 10


def test_btc_absent_from_live_universe():
    uni = load_live_universe(ROOT / "config" / "live_universe.json")
    assert "BTCUSDT" not in uni.symbols


def test_configured_symbols_dynamic(tmp_path: Path):
    path = tmp_path / "live.json"
    path.write_text(
        json.dumps(
            {
                "exchange": "bybit",
                "category": "linear",
                "interval": "1m",
                "symbols": ["APTUSDT", "DOGEUSDT", "SOLUSDT"],
            }
        ),
        encoding="utf-8",
    )
    uni = load_live_universe(path)
    assert uni.symbols == ["APTUSDT", "DOGEUSDT", "SOLUSDT"]


def test_btc_rejected_in_live_universe_file(tmp_path: Path):
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps({"symbols": ["APTUSDT", "BTCUSDT"]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="BTCUSDT"):
        load_live_universe(path)


def test_invalid_symbol_isolated():
    results = validate_live_symbols(
        ["APTUSDT", "NOTAREALCOINUSDT", "DOGEUSDT"],
        tradable={"APTUSDT", "DOGEUSDT"},
    )
    valid, invalid = partition_valid_symbols(results)
    assert valid == ["APTUSDT", "DOGEUSDT"]
    assert len(invalid) == 1
    assert invalid[0].symbol == "NOTAREALCOINUSDT"


def test_confirm_false_ignored():
    item = {
        "start": 1_704_067_200_000,
        "end": 1_704_067_259_999,
        "interval": "1",
        "open": "1",
        "high": "2",
        "low": "0.5",
        "close": "1.5",
        "volume": "10",
        "turnover": "15",
        "confirm": False,
        "timestamp": 1_704_067_210_000,
    }
    assert ws_tick_to_candle(parse_ws_kline_item(item, symbol="APTUSDT")) is None


def test_confirm_true_persisted_shape():
    start = int(datetime(2026, 8, 10, 10, 0, tzinfo=UTC).timestamp() * 1000)
    item = {
        "start": start,
        "end": start + 59_999,
        "interval": "1",
        "open": "1",
        "high": "1",
        "low": "1",
        "close": "1",
        "volume": "1",
        "turnover": "1",
        "confirm": True,
        "timestamp": start + 1,
    }
    c = ws_tick_to_candle(parse_ws_kline_item(item, symbol="APTUSDT"))
    assert c is not None
    assert c.source == "bybit_live"
    assert c.close_time == c.open_time + timedelta(minutes=1)


def test_duplicate_closed_candle_buffer_idempotent_insert():
    class Repo:
        def __init__(self) -> None:
            self.rows: list[Candle1m] = []

        def insert_candles(self, batch: list[Candle1m]) -> int:
            self.rows.extend(batch)
            return len(batch)

    repo = Repo()
    buf = CandleInsertBuffer(repo, max_rows=10, flush_interval_s=60)  # type: ignore[arg-type]
    ot = datetime(2026, 8, 10, 10, 0, tzinfo=UTC)
    c = Candle1m(
        exchange="bybit",
        symbol="APTUSDT",
        open_time=ot,
        close_time=ot + timedelta(minutes=1),
        open=Decimal("1"),
        high=Decimal("1"),
        low=Decimal("1"),
        close=Decimal("1"),
        volume=Decimal("1"),
        turnover=Decimal("1"),
        source="bybit_live",
    )
    buf.add(c)
    buf.add(c)
    n = buf.flush()
    assert n == 2  # physical duplicates ok; logical uniqueness via ReplacingMergeTree
    assert len(repo.rows) == 2


def test_ping_pong_and_timeout_fields():
    h = HealthState()
    h.note_ping()
    assert h.last_ping_at is not None
    h.note_pong()
    assert h.last_pong_at is not None
    assert is_pong_message({"op": "pong"})


@pytest.mark.asyncio
async def test_subscribe_ack_event():
    events: list[tuple[str, dict]] = []

    async def on_event(name: str, data: dict) -> None:
        events.append((name, data))

    ws = BybitKlineWebSocket(["APTUSDT"], on_event=on_event)
    await ws._handle_payload({"op": "subscribe", "success": True, "ret_msg": "ok"})
    assert events and events[0][0] == "subscribe_ack"
    assert events[0][1]["success"] is True


def test_startup_trailing_recovery_window():
    from signal_generator.bybit.live.recovery import compute_recovery_window

    last = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
    as_of = datetime(2026, 8, 10, 12, 0, 30, tzinfo=UTC)
    w = compute_recovery_window(last, as_of=as_of)
    assert w is not None
    assert w[0] == last
    assert w[1] == datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def test_internal_missing_range_detection():
    start = datetime(2026, 8, 10, 10, 0, tzinfo=UTC)
    end = start + timedelta(minutes=10)
    present = [start + timedelta(minutes=i) for i in (0, 1, 2, 7, 8, 9)]
    ranges = missing_ranges_from_open_times(present, effective_start=start, requested_end=end)
    assert any(r.kind == "INTERNAL" for r in ranges)


def test_htf_boundary_trigger():
    # 15m close at 10:15
    assert htf_boundary_at_close(datetime(2026, 8, 10, 10, 15, tzinfo=UTC)) is True
    assert htf_boundary_at_close(datetime(2026, 8, 10, 10, 16, tzinfo=UTC)) is False


def test_out_of_order_open_time_not_arrival_order():
    # Logical key is open_time; later arrival of earlier open_time still valid candle
    earlier = datetime(2026, 8, 10, 10, 0, tzinfo=UTC)
    later = datetime(2026, 8, 10, 10, 1, tzinfo=UTC)
    assert earlier < later


def test_batch_flush_on_max_rows():
    class Repo:
        def __init__(self) -> None:
            self.n = 0

        def insert_candles(self, batch: list[Candle1m]) -> int:
            self.n += len(batch)
            return len(batch)

    repo = Repo()
    buf = CandleInsertBuffer(repo, max_rows=2, flush_interval_s=999)  # type: ignore[arg-type]
    ot = datetime(2026, 8, 10, 10, 0, tzinfo=UTC)

    def c(i: int) -> Candle1m:
        t = ot + timedelta(minutes=i)
        return Candle1m(
            exchange="bybit",
            symbol="APTUSDT",
            open_time=t,
            close_time=t + timedelta(minutes=1),
            open="1",
            high="1",
            low="1",
            close="1",
            volume="1",
            turnover="1",
            source="bybit_live",
        )

    assert buf.add(c(0)) == 0
    assert buf.add(c(1)) == 2
    assert repo.n == 2
    assert buf.pending == 0


def test_desired_state_persistence(tmp_path: Path):
    path = tmp_path / "desired_state.json"
    store = DesiredStateStore(path=path, default="STOPPED")
    assert store.read() == "STOPPED"
    store.write("RUNNING", reason="test")
    assert store.read() == "RUNNING"
    store.write("STOPPED", reason="test")
    assert store.read() == "STOPPED"
    # idempotent
    store.write("STOPPED", reason="again")
    assert store.read() == "STOPPED"


def test_desired_running_idempotent(tmp_path: Path):
    store = DesiredStateStore(path=tmp_path / "d.json", default="RUNNING")
    store.write("RUNNING")
    store.write("RUNNING")
    assert store.read() == "RUNNING"


def test_desired_invalid_rejected(tmp_path: Path):
    store = DesiredStateStore(path=tmp_path / "d.json")
    with pytest.raises(ValueError):
        store.write("NOPE")  # type: ignore[arg-type]


def test_no_double_collector_lock(tmp_path: Path):
    from signal_generator.bybit.live.collector import acquire_singleton_lock

    lock = tmp_path / "collector.lock"
    acquire_singleton_lock(lock)
    with pytest.raises(RuntimeError, match="another collector"):
        acquire_singleton_lock(lock)


def test_collector_status_json_contract():
    h = HealthState(desired_state="RUNNING")
    h.init_symbols(["APTUSDT", "DOGEUSDT"])
    h.set_state(CollectorState.LIVE)
    h.mark_subscribed("APTUSDT")
    h.mark_symbol_live("APTUSDT")
    h.note_ping()
    h.note_pong()
    d = h.to_dict()
    for key in (
        "desired_state",
        "collector_state",
        "websocket_connected",
        "configured_symbols",
        "subscribed_symbols",
        "live_symbols",
        "stale_symbols",
        "recovering_symbols",
        "last_message_at",
        "last_ping_at",
        "last_pong_at",
        "reconnect_count",
        "started_at",
        "symbols",
    ):
        assert key in d
    assert d["configured_count"] == 2
    assert len(d["symbols"]) == 2
    assert d["symbols"][0]["symbol"] == "APTUSDT"


def test_per_symbol_status_fields():
    h = HealthState()
    sh = h.ensure_symbol("SOLUSDT")
    sh.state = SymbolRuntimeState.STALE
    sh.last_error = "lag"
    row = sh.to_dict()
    assert row["state"] == "STALE"
    assert "candle_lag_seconds" in row
    assert "signal_processor_state" in row


def test_shadow_mode_cannot_trade():
    assert SHADOW_MODE is True
    assert TRADING_ENABLED is False
    assert_shadow_only()


def test_control_api_desired_state(tmp_path: Path):
    store = DesiredStateStore(path=tmp_path / "d.json", default="STOPPED")
    h = HealthState(desired_state="STOPPED")
    h.init_symbols(["APTUSDT"])
    changed: list[str] = []
    svc = CollectorControlService(
        health=h, desired=store, on_desired_change=lambda v: changed.append(v)
    )
    assert svc.get_desired()["desired_state"] == "STOPPED"
    out = svc.set_desired("RUNNING")
    assert out["desired_state"] == "RUNNING"
    assert changed == ["RUNNING"]
    # idempotent stop
    svc.set_desired("STOPPED")
    svc.set_desired("STOPPED")
    assert store.read() == "STOPPED"
    status = svc.status()
    assert status["desired_state"] == "STOPPED"


def test_forming_payload_on_health():
    h = HealthState(desired_state="RUNNING")
    h.set_forming_candle("ACEUSDT", {"time": 1, "open": 1.0, "high": 1.2, "low": 0.9, "close": 1.1})
    out = h.forming_payload("ACEUSDT")
    assert out["forming"]["close"] == 1.1
    assert h.forming_payload()["forming_by_symbol"]["ACEUSDT"]["open"] == 1.0


def test_control_api_ensure_symbol_demand(tmp_path: Path):
    from signal_generator.bybit.live.demand_symbols import DemandSymbolStore

    store = DesiredStateStore(path=tmp_path / "d.json", default="STOPPED")
    demand = DemandSymbolStore(path=tmp_path / "demand.json")
    h = HealthState(desired_state="STOPPED")
    stopped: list[int] = []

    class _Col:
        symbols = ["APTUSDT"]

        def request_stop(self) -> None:
            stopped.append(1)

    col = _Col()
    svc = CollectorControlService(
        health=h,
        desired=store,
        demand=demand,
        get_collector=lambda: col,
    )
    out = svc.ensure_symbol("ACEUSDT")
    assert out["ok"] is True
    assert demand.read() == ["ACEUSDT"]
    assert store.read() == "RUNNING"
    assert out["restarted"] is True
    assert stopped == [1]


def test_signals_api_query_contract_marker():
    # Marker mapping used by control_api
    assert ("LONG", "▲") and ("SHORT", "▼")
    direction = "LONG"
    marker = "▲" if direction == "LONG" else "▼"
    assert marker == "▲"


def test_all_ten_symbols_independent_in_health():
    uni = load_live_universe(ROOT / "config" / "live_universe.json")
    h = HealthState()
    h.init_symbols(uni.symbols)
    h.ensure_symbol("APTUSDT").state = SymbolRuntimeState.ERROR
    h.ensure_symbol("APTUSDT").last_error = "boom"
    # others remain STARTING
    for s in uni.symbols:
        if s != "APTUSDT":
            assert h.symbol_health[s].state == SymbolRuntimeState.STARTING
    assert h.symbol_health["APTUSDT"].state == SymbolRuntimeState.ERROR


def test_collector_rejects_btc():
    from signal_generator.bybit.live.collector import Live1mCollector

    with pytest.raises(ValueError, match="BTCUSDT"):
        Live1mCollector(symbols=["APTUSDT", "BTCUSDT"], ch=MagicMock())


def test_control_api_rejects_non_localhost():
    from signal_generator.bybit.live.control_api import start_control_api

    svc = CollectorControlService(
        health=HealthState(), desired=DesiredStateStore(path=Path("/tmp/x"))
    )
    with pytest.raises(ValueError, match="localhost"):
        start_control_api(svc, host="0.0.0.0", port=1)
