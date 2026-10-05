"""Production ClickHouse must not be touched by default test runs."""

from __future__ import annotations

import ast
from pathlib import Path

from bot.shadow_signal_registry.ch_config import shadow_ch_sync_enabled
from bot.shadow_signal_registry.ch_sync import sync_snapshots_to_clickhouse
from bot.shadow_signal_registry.tests.test_ch_sync import MockChClient, _row


def test_ch_sync_disabled_by_default_in_test_session() -> None:
    assert shadow_ch_sync_enabled() is False


def test_ch_sync_tests_use_mock_client_not_production(monkeypatch) -> None:
    """Enabling sync in a test must not call real ClickHouse (_get_client)."""
    monkeypatch.setenv("SHADOW_CH_SYNC_ENABLED", "1")
    client = MockChClient()
    hashes: dict[str, str] = {}
    sync_snapshots_to_clickhouse("short", [_row()], hashes, client=client, force=False)
    assert len(client.inserts) == 1
    assert "live_forward" in client.inserts[0][0]


def test_flush_snapshots_test_must_not_rely_on_tmp_path_only() -> None:
    """Documented root cause: tmp_path isolates registry files, not CH sync."""
    path = Path(__file__).resolve().parents[1] / "tests" / "test_registry_hardening.py"
    text = path.read_text(encoding="utf-8")
    assert "test_snapshot_schema_block_reason_never_null_on_flush" in text
    assert 'monkeypatch.setenv("SHADOW_CH_SYNC_ENABLED", "0")' in text


def test_ch_sync_module_tests_never_import_get_client_in_test_ch_sync() -> None:
    test_file = Path(__file__).resolve().parent / "test_ch_sync.py"
    tree = ast.parse(test_file.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "_get_client":
                raise AssertionError("test_ch_sync.py must not call _get_client()")
            if isinstance(func, ast.Name) and func.id == "_get_client":
                raise AssertionError("test_ch_sync.py must not call _get_client()")
