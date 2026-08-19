"""Unit tests for the 49-coin Orderbook V2 rollout helpers and shell script.

No ClickHouse writes, no downloads, no live pilot.
"""
from __future__ import annotations

import importlib.util
import json
from datetime import date, datetime
from pathlib import Path

LIB_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "orderbook_v2_49_rollout_lib.py"
)
SH_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "run_orderbook_v2_49_coin_rollout.sh"
)


def _load_lib():
    spec = importlib.util.spec_from_file_location("orderbook_v2_49_rollout_lib", LIB_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


lib = _load_lib()


class FakeQuery:
    def __init__(self, rows):
        self.result_rows = rows


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)

    def query(self, sql, parameters=None):
        if not self.responses:
            raise AssertionError(f"unexpected query: {sql[:80]}")
        return FakeQuery(self.responses.pop(0))


def test_symbol_set_is_exactly_49_without_ada_btc_xau():
    lib.validate_symbol_set()
    assert len(lib.SYMBOLS_49) == 49
    assert len(set(lib.SYMBOLS_49)) == 49
    assert "ADAUSDT" not in lib.SYMBOLS_49
    assert "BTCUSDT" not in lib.SYMBOLS_49
    assert "XAUUSDT" not in lib.SYMBOLS_49


def test_check_window_ok_and_bad():
    ok, _ = lib.check_window(
        [date(2026, 8, d) for d in (17, 16, 15, 14, 13, 12, 11)]
    )
    assert ok
    bad, _ = lib.check_window(
        [date(2026, 8, d) for d in (18, 17, 16, 15, 14, 13, 12)]
    )
    assert not bad


def test_write_progress_atomic(tmp_path):
    path = tmp_path / "progress.json"
    lib.write_progress_atomic(path, {"rollout_status": "PREPARED", "n": 1})
    assert json.loads(path.read_text())["rollout_status"] == "PREPARED"
    assert not path.with_name("progress.json.tmp").exists()


def test_classify_not_imported():
    client = FakeClient([[(0, 0, 0, 0)], [], []])
    klass, _ = lib.classify_symbol(client, "ETHUSDT")
    assert klass == "NOT_IMPORTED"


def test_classify_complete_skipped():
    days = [(date(2026, 8, d), "COMPLETE", "ob200_v3", 86400) for d in range(11, 18)]
    client = FakeClient(
        [
            [(604800, 604800, 1, 0)],
            [("ob200_v3", 604800)],
            days,
        ]
    )
    klass, reason = lib.classify_symbol(client, "ETHUSDT")
    assert klass == "COMPLETE_V3"
    assert reason == "skip_complete"


def test_classify_legacy_ob200_v2_is_not_complete_v3():
    """Historical shifted-window v2 rows must not be treated as a completed v3 import."""
    days = [(date(2026, 8, d), "COMPLETE", "ob200_v2", 86400) for d in range(11, 18)]
    client = FakeClient(
        [
            [(604800, 604800, 1, 0)],
            [("ob200_v2", 604800)],
            days,
        ]
    )
    klass, reason = lib.classify_symbol(client, "ETHUSDT")
    assert klass != "COMPLETE_V3"
    assert klass == "INCONSISTENT"
    assert "ob200_v2" in reason


def test_classify_partial_or_failed_is_inconsistent():
    client = FakeClient(
        [
            [(100, 100, 1, 0)],
            [("ob200_v2", 100)],
            [(date(2026, 8, 11), "FAILED", "ob200_v2", 0)],
        ]
    )
    klass, _ = lib.classify_symbol(client, "ETHUSDT")
    assert klass == "INCONSISTENT"


def test_audit_pass_and_fail(monkeypatch):
    monkeypatch.setattr(lib, "collector_ok", lambda pid=147111: (True, "ok"))
    days = [(date(2026, 8, d), 86400, 86400) for d in range(11, 18)]
    manifest = [(date(2026, 8, d), "COMPLETE", "ob200_v3", 86400) for d in range(11, 18)]
    first = datetime(2026, 8, 11, 0, 0, 0)
    last = datetime(2026, 8, 17, 23, 59, 59)
    client = FakeClient(
        [
            [(604800, 604800, 0, first, last, 604000, 800)],
            days,
            [("ob200_v3", 604800)],
            [(0,)],
            manifest,
            [(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 800)],
            [(0,)],
            [(0, 0)],
        ]
    )
    ok, reason = lib.audit_symbol(client, "ETHUSDT")
    assert ok, reason

    client_fail = FakeClient(
        [
            [(10, 10, 1, first, last, 10, 0)],
            days[:1],
            [("ob200_v1", 10)],
            [(1,)],
            [],
            [(1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)],
            [(0,)],
            [(0, 0)],
        ]
    )
    ok2, failed = lib.audit_symbol(client_fail, "ETHUSDT")
    assert not ok2
    assert "logical" in failed


def test_collector_guard_missing():
    ok, reason = lib.collector_ok(pid=99999999)
    assert not ok
    assert "missing" in reason


def test_shell_script_safety_and_counts():
    text = SH_PATH.read_text(encoding="utf-8")
    assert "set -u" in text
    assert "set -o pipefail" in text
    assert "--optimize-final" not in text
    assert "OPTIMIZE TABLE" not in text
    assert "orderbook_deltas" not in text
    assert "DELETE FROM" not in text
    assert "TRUNCATE" not in text
    assert "DROP TABLE" not in text
    assert '--symbol "${OB_SYMBOL}"' in text
    assert "--days 7" in text
    assert "flock" in text
    assert '${OB_SYMBOL}_OB_V2_7D_PILOT_PASSED' in text
    assert "PASSED_WITH_WARNINGS" in text
    assert "SKIP_COMPLETE" in text
    assert "COMPLETE_V3" in text
    assert "COMPLETE_V2" not in text
    assert 'EXPECTED_PARSER="ob200_v2"' not in text
    assert "from orderbook_analyse.orderbook_v2 import PARSER_VERSION" in text
    assert "STOPPED_WINDOW_CHANGED" in text
    assert "STOPPED_COLLECTOR_GUARD" in text
    assert "STOPPED_AUDIT_FAILED" in text
    assert "STOPPED_IMPORT_FAILED" in text
    assert "[o]rderbook_analyse.orderbook_v2.pilot" in text


def test_decision_parser_accepts_only_exact_passed(tmp_path):
    log = tmp_path / "ETHUSDT.log"
    log.write_text(
        "=== ORDERBOOK_V2 PILOT: ETHUSDT 7d ===\n"
        "DECISION: ETHUSDT_OB_V2_7D_PILOT_PASSED_WITH_WARNINGS\n",
        encoding="utf-8",
    )
    text = log.read_text()
    assert not any(
        line == "DECISION: ETHUSDT_OB_V2_7D_PILOT_PASSED" for line in text.splitlines()
    )


def test_env_presence_does_not_print_values(monkeypatch):
    monkeypatch.setenv("CLICKHOUSE_HOST", "secret-host")
    monkeypatch.setenv("CLICKHOUSE_HTTP_PORT", "8123")
    monkeypatch.setenv("CLICKHOUSE_DATABASE", "secret-db")
    monkeypatch.setenv("CLICKHOUSE_USER", "secret-user")
    presence = lib.env_presence(load_env_file=False)
    dumped = json.dumps(presence)
    assert "secret-host" not in dumped
    assert presence["CLICKHOUSE_HOST"] == "SET"


def test_expected_parser_is_ob200_v3_from_package():
    from orderbook_analyse.orderbook_v2 import PARSER_VERSION

    assert PARSER_VERSION == "ob200_v3"
    assert lib.EXPECTED_PARSER == PARSER_VERSION
    assert lib.EXPECTED_PARSER == "ob200_v3"


def test_progress_payload_records_ob200_v3(tmp_path):
    path = tmp_path / "progress.json"
    lib.write_progress_atomic(
        path,
        {
            "rollout_status": "PREPARED",
            "parser_version": lib.EXPECTED_PARSER,
        },
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["parser_version"] == "ob200_v3"


def test_audit_rejects_legacy_ob200_v2_parser(monkeypatch):
    monkeypatch.setattr(lib, "collector_ok", lambda pid=147111: (True, "ok"))
    days = [(date(2026, 8, d), 86400, 86400) for d in range(11, 18)]
    manifest = [(date(2026, 8, d), "COMPLETE", "ob200_v2", 86400) for d in range(11, 18)]
    first = datetime(2026, 8, 11, 0, 0, 0)
    last = datetime(2026, 8, 17, 23, 59, 59)
    client = FakeClient(
        [
            [(604800, 604800, 0, first, last, 604000, 800)],
            days,
            [("ob200_v2", 604800)],
            [(0,)],
            manifest,
            [(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 800)],
            [(0,)],
            [(0, 0)],
        ]
    )
    ok, failed = lib.audit_symbol(client, "ETHUSDT")
    assert not ok
    assert "parser" in failed.split(",")
