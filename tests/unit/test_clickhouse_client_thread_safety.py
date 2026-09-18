"""ClickHouse session must not be shared across concurrent writers."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from signal_generator.db.client import ClickHouseClient


class _FakeRawClient:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self._gate = threading.Lock()
        self.queries = 0

    def query(self, sql, parameters=None):  # noqa: ANN001
        with self._gate:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.02)
        with self._gate:
            self.active -= 1
            self.queries += 1
        return type("R", (), {"result_rows": [("1", "db", "u")]})()

    def command(self, sql, parameters=None):  # noqa: ANN001
        return self.query(sql, parameters)

    def insert(self, **kwargs):  # noqa: ANN003
        return self.query("INSERT")

    def close(self) -> None:
        return None


def test_clickhouse_client_serializes_concurrent_queries():
    raw = _FakeRawClient()
    client = ClickHouseClient(raw, database="signal_generator")

    def _hit(_: int) -> None:
        client.query("SELECT 1")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_hit, range(16)))

    assert raw.queries == 16
    assert raw.max_active == 1


def test_ensure_trade_buffer_uses_dedicated_clickhouse_client(monkeypatch):
    from signal_generator.bybit.live import collector as coll

    monkeypatch.setattr(coll, "assert_shadow_only", lambda: None)
    monkeypatch.setattr(coll, "get_clickhouse_settings", lambda: object())
    monkeypatch.setattr(coll, "CandleRepository", lambda ch: object())
    monkeypatch.setattr(coll, "SignalRepository", lambda ch: object())
    monkeypatch.setattr(coll, "ProcessingStateRepository", lambda ch: object())

    main = object()
    public = object()
    created: list[object] = []

    def _from_settings(_settings):  # noqa: ANN001
        created.append(public)
        return public

    monkeypatch.setattr(coll.ClickHouseClient, "from_settings", _from_settings)

    class _Repo:
        def __init__(self, client) -> None:  # noqa: ANN001
            self.client = client

    monkeypatch.setattr(coll, "CanonicalPublicTradeRepository", _Repo)

    c = coll.Live1mCollector(
        ch=main,  # type: ignore[arg-type]
        candle_symbols=["BTCUSDT", "ETHUSDT"],
        signal_symbols=[],
        enable_signals=False,
        enable_public_trades=True,
        public_trade_symbols=["BTCUSDT", "ETHUSDT"],
    )
    buf = c._ensure_trade_buffer()
    assert buf is not None
    assert c._ch_public is public
    assert buf.repo.client is public
    assert buf.repo.client is not main
    assert created == [public]
    # Second call reuses the same dedicated client/buffer
    assert c._ensure_trade_buffer() is buf
    assert created == [public]
