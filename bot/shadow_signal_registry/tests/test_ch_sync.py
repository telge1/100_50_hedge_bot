"""ClickHouse sync unit tests (mock client)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import bot.shadow_signal_registry.store as store_mod
from bot.shadow_signal_registry.ch_config import apply_live_scanner_runtime_env, shadow_ch_sync_enabled
from bot.shadow_signal_registry.ch_sync import (
    LONG_COLUMNS,
    SHORT_COLUMNS,
    content_hash,
    flush_ch_sync_hook,
    pending_path,
    snapshot_to_ch_row,
    sync_snapshots_to_clickhouse,
    version_from_updated_at,
)
from bot.shadow_signal_registry.registry import ShadowRegistry


class MockChClient:
    def __init__(self) -> None:
        self.inserts: list[tuple[str, list, list]] = []
        self.fail_next = False

    def insert(self, table: str, data: list, column_names: list[str]) -> None:
        if self.fail_next:
            raise RuntimeError("ch down")
        self.inserts.append((table, data, column_names))

    def command(self, cmd: str) -> None:
        pass

    def query(self, sql: str, parameters: dict | None = None):
        return type("R", (), {"column_names": [], "result_rows": []})()

    def close(self) -> None:
        pass


def _row(side: str = "short") -> dict:
    base = {
        "signal_id": "sid1",
        "strategy_name": "s",
        "strategy_version": "v1",
        "symbol": "TUTUSDT",
        "side": side.upper(),
        "decision_time": "2026-10-04T18:30:00+00:00",
        "detected_at": "2026-10-04T18:30:00+00:00",
        "exit_time": "2026-10-04T18:50:00+00:00",
        "signal_status": "ALLOWED",
        "allowed": True,
        "blocked": False,
        "hypothetical": False,
        "block_reason": "NONE",
        "entry_price": 0.024398,
        "initial_sl": 0.02467124,
        "active_sl": 0.02467124,
        "tp": 0.024075,
        "pool_id": "p",
        "tracking_status": "CLOSED",
        "outcome": "SL",
        "pnl_pct": -1.119928,
        "mae_pct": 1.0,
        "mfe_pct": 0.1,
        "created_at": "2026-10-04T18:30:00+00:00",
        "updated_at": "2026-10-04T18:50:00+00:00",
        "causality_status": "PASS",
    }
    if side == "long":
        base.update({"ladder24_pass": True, "be_triggered": False})
    else:
        base.update({"e1r_state": "INACTIVE", "floor_guard_state": "PASS"})
    return base


def test_long_schema_mapping() -> None:
    ch = snapshot_to_ch_row("long", _row("long"))
    assert set(ch.keys()) == set(LONG_COLUMNS)
    assert ch["ladder24_pass"] == 1


def test_short_schema_mapping() -> None:
    ch = snapshot_to_ch_row("short", _row("short"))
    assert set(ch.keys()) == set(SHORT_COLUMNS)
    assert "ladder24_pass" not in ch


def test_version_from_updated_at_deterministic() -> None:
    ts = "2026-10-04T18:50:00+00:00"
    v1 = version_from_updated_at(ts)
    v2 = version_from_updated_at(ts)
    assert v1 == v2
    assert v1 > 0


def test_version_collision_distinct_timestamps() -> None:
    a = version_from_updated_at("2026-10-04T18:30:00+00:00")
    b = version_from_updated_at("2026-10-04T18:30:00.001+00:00")
    assert a != b


def test_content_hash_unchanged_skips_insert(monkeypatch) -> None:
    monkeypatch.setenv("SHADOW_CH_SYNC_ENABLED", "1")
    client = MockChClient()
    hashes: dict[str, str] = {}
    row = _row()
    hashes["sid1"] = content_hash(row)
    sync_snapshots_to_clickhouse("short", [row], hashes, client=client, force=False)
    assert len(client.inserts) == 0


def test_content_hash_changed_inserts(monkeypatch) -> None:
    monkeypatch.setenv("SHADOW_CH_SYNC_ENABLED", "1")
    client = MockChClient()
    hashes: dict[str, str] = {}
    row = _row()
    sync_snapshots_to_clickhouse("short", [row], hashes, client=client, force=False)
    assert len(client.inserts) == 1
    assert hashes["sid1"] == content_hash(row)


def test_ch_failure_pending_spool(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SHADOW_CH_SYNC_ENABLED", "1")
    monkeypatch.setattr(store_mod, "RUNTIME", tmp_path)
    client = MockChClient()
    client.fail_next = True
    hashes: dict[str, str] = {}
    sync_snapshots_to_clickhouse("short", [_row()], hashes, client=client)
    assert pending_path("short").is_file()


def test_pending_retry_success(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SHADOW_CH_SYNC_ENABLED", "1")
    monkeypatch.setattr(store_mod, "RUNTIME", tmp_path)
    client = MockChClient()
    client.fail_next = True
    hashes: dict[str, str] = {}
    sync_snapshots_to_clickhouse("short", [_row()], hashes, client=client)
    client.fail_next = False
    sync_snapshots_to_clickhouse("short", [_row()], hashes, client=client)
    assert not pending_path("short").is_file()
    assert len(client.inserts) >= 1


def test_flush_hook_no_exception(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(store_mod, "RUNTIME", tmp_path)
    reg = ShadowRegistry("short")
    flush_ch_sync_hook("short", [], reg.ch_last_hash)


def test_live_scanner_runtime_env_enables_ch_sync(monkeypatch) -> None:
    monkeypatch.delenv("SHADOW_CH_SYNC_ENABLED", raising=False)
    apply_live_scanner_runtime_env()
    assert shadow_ch_sync_enabled()


def test_no_orders_ch_package() -> None:
    root = Path(__file__).resolve().parents[1]
    for p in root.glob("ch*.py"):
        text = p.read_text(encoding="utf-8").lower()
        assert "create_order" not in text
