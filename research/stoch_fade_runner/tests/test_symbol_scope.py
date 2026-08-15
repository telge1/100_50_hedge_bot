from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from research.stoch_fade_runner.audits import classify_parity, load_production_signals
from research.stoch_fade_runner.cli import main
from research.stoch_fade_runner.snapshot import capture_snapshot, coin_scope_equal
from research.stoch_fade_runner.config import REQUESTED_SIGNAL_END_EXCLUSIVE, REQUESTED_SIGNAL_START


class FakeResult:
    def __init__(self, rows=None, column_names=None):
        self.result_rows = rows or []
        self.column_names = column_names or []


class RecordingClient:
    database = "signal_generator"

    def __init__(self):
        self.calls: list[tuple[str, dict | None]] = []

    def query(self, sql, parameters=None):
        self.calls.append((sql, parameters))
        lowered = sql.lower()
        if lowered.strip().startswith("select count()") and "where" not in lowered:
            return FakeResult([(99,)])
        if "signal_outcomes" in lowered:
            return FakeResult([(0, 0)])
        if "signal_processing_state" in lowered and "select symbol" in lowered:
            return FakeResult([])
        if "candles_1m" in lowered:
            if "group by exchange" in lowered:
                return FakeResult([("bybit", 10, 10)])
            if "datediff" in lowered or "duplicate_count" in lowered:
                t0 = datetime(2025, 12, 11, tzinfo=timezone.utc)
                t1 = datetime(2026, 8, 15, 9, 41, tzinfo=timezone.utc)
                return FakeResult([(t0, t1, 10, 10, 0, 0)])
            t0 = datetime(2025, 12, 11, tzinfo=timezone.utc)
            t1 = datetime(2026, 8, 15, 9, 41, tzinfo=timezone.utc)
            return FakeResult([(t0, t1, 10, 10)])
        if "from signal_generator.signals" in lowered or "signals final" in lowered:
            if "tostring(signal_id)" in lowered:
                return FakeResult([], column_names=[
                    "signal_id", "symbol", "timeframe", "direction", "signal_type",
                    "candle_open_time", "generated_at", "tier_a", "strategy_version", "metadata",
                ])
            return FakeResult([(0, 0, None, None)])
        return FakeResult([(0,)])


def test_snapshot_uses_explicit_aave_and_bonk_not_pepe() -> None:
    start = REQUESTED_SIGNAL_START
    end = REQUESTED_SIGNAL_END_EXCLUSIVE
    for symbol in ("AAVEUSDT", "1000BONKUSDT"):
        client = RecordingClient()
        snap = capture_snapshot(client, label="before", symbol=symbol, start=start, end=end)
        assert snap["scope_symbol"] == symbol
        assert snap["symbol"] == symbol
        assert snap["signals"]["symbol"] == symbol
        assert snap["window_candles"]["symbol"] == symbol
        assert snap["candles"]["symbol"] == symbol
        assert snap["outcomes"]["scope_symbol"] == symbol
        assert snap["outcomes"]["scope"] == "symbol_via_signal_id_join"
        assert snap["scope_start"] == "2025-12-11T00:00:00Z"
        assert snap["scope_end_exclusive"] == "2026-08-15T09:42:00Z"
        symbols = [p.get("symbol") for _, p in client.calls if p and "symbol" in p]
        assert symbols
        assert all(s == symbol for s in symbols)
        assert "1000PEPEUSDT" not in symbols


def test_snapshot_before_after_same_scope() -> None:
    client = RecordingClient()
    start, end = REQUESTED_SIGNAL_START, REQUESTED_SIGNAL_END_EXCLUSIVE
    before = capture_snapshot(client, label="before", symbol="AAVEUSDT", start=start, end=end)
    after = capture_snapshot(client, label="after", symbol="AAVEUSDT", start=start, end=end)
    assert before["scope_symbol"] == after["scope_symbol"] == "AAVEUSDT"
    assert before["scope_start"] == after["scope_start"]
    assert before["scope_end_exclusive"] == after["scope_end_exclusive"]
    assert coin_scope_equal(before, after)


def test_global_candle_delta_is_not_coin_write() -> None:
    client = RecordingClient()
    start, end = REQUESTED_SIGNAL_START, REQUESTED_SIGNAL_END_EXCLUSIVE
    before = capture_snapshot(client, label="before", symbol="AAVEUSDT", start=start, end=end)
    after = dict(before)
    after["global_control_counts"] = dict(before["global_control_counts"])
    after["global_control_counts"]["candles_1m"] = before["global_control_counts"]["candles_1m"] + 1
    after["global_table_counts"] = after["global_control_counts"]
    assert coin_scope_equal(before, after)
    assert after["global_counts_are_not_coin_writes"] is True


def test_load_production_and_parity_scoped_to_aave() -> None:
    client = RecordingClient()
    rows = load_production_signals(client, symbol="AAVEUSDT")
    assert rows == []
    assert all(p["symbol"] == "AAVEUSDT" for _, p in client.calls if p and "symbol" in p)
    pepe = {
        "signal_id": "pepe-1",
        "symbol": "1000PEPEUSDT",
        "timeframe": "15m",
        "direction": "LONG",
        "signal_type": "wave_fade",
        "candle_open_time": "2026-08-01T00:00:00Z",
        "tier_a": True,
        "strategy_version": "wave_fade_no_be50_v1",
    }
    aave_r = {
        "signal_id": "aave-r",
        "symbol": "AAVEUSDT",
        "timeframe": "15m",
        "direction": "LONG",
        "signal_type": "wave_fade",
        "candle_open_time": "2026-01-01T00:00:00Z",
        "confirmation_available_at": "2026-01-01T00:15:00Z",
        "tier_a": True,
        "strategy_version": "wave_fade_frozen_f16ae32",
    }
    out = classify_parity([aave_r], [pepe], scope_symbol="AAVEUSDT")
    assert out["scope_symbol"] == "AAVEUSDT"
    assert out["production_keys"] == 0
    assert out["intersection"] == 0
    assert out["production_only"] == 0
    assert out["research_only"] == 1
    assert out["research_keys"] == 1
    assert 423 not in out.values()


def test_cli_aave_dry_run_selected_symbols(tmp_path) -> None:
    rc = main(["--dry-run-empty", "--symbol", "AAVEUSDT", "--out-root", str(tmp_path)])
    assert rc == 0
    run_dir = next(p for p in tmp_path.iterdir() if p.is_dir())
    man = json.loads((run_dir / "run_manifest.json").read_text())
    assert man["selected_symbols"] == ["AAVEUSDT"]
    assert man["selected_symbol"] == "AAVEUSDT"
    assert "canary_symbol" not in man
    assert man["default_canary_symbol"] == "1000PEPEUSDT"
    assert man["default_canary_symbol_is_not_run_symbol"] is True
