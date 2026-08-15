from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from research.stoch_fade_runner.audits import classify_parity, generation_key
from research.stoch_fade_runner.candles import _as_utc
from research.stoch_fade_runner.snapshot import _iso, _outcomes_payload

ROOT = Path(__file__).resolve().parents[3]


def test_naive_clickhouse_datetime_is_utc_not_local() -> None:
    naive = datetime(2025, 12, 11, 0, 0, 0)
    got = _as_utc(naive)
    assert got == datetime(2025, 12, 11, 0, 0, 0, tzinfo=timezone.utc)
    assert got != naive.astimezone(timezone.utc)
    assert _iso(naive) == "2025-12-11T00:00:00Z"


def test_aware_utc_passthrough() -> None:
    aware = datetime(2025, 12, 11, 0, 0, 0, tzinfo=timezone.utc)
    assert _as_utc(aware) == aware


def test_as_utc_epoch_identical_under_process_timezones() -> None:
    code = (
        "from datetime import datetime, timezone\n"
        "from research.stoch_fade_runner.candles import _as_utc\n"
        "samples = [\n"
        " datetime(2025, 12, 11, 0, 0, 0),\n"
        " datetime(2026, 3, 29, 1, 0, 0),\n"
        " datetime(2026, 3, 29, 2, 0, 0),\n"
        " datetime(2026, 7, 15, 12, 0, 0),\n"
        " datetime(2025, 10, 26, 0, 0, 0),\n"
        " datetime(2025, 10, 26, 1, 0, 0),\n"
        " datetime(2025, 10, 26, 2, 0, 0),\n"
        " datetime(2025, 12, 31, 23, 0, 0),\n"
        " datetime(2026, 1, 1, 0, 0, 0),\n"
        " datetime(2026, 1, 31, 23, 0, 0),\n"
        " datetime(2026, 2, 1, 0, 0, 0),\n"
        " datetime(2026, 8, 15, 8, 0, 0),\n"
        " datetime(2026, 8, 15, 12, 0, 0),\n"
        " datetime(2026, 8, 15, 16, 0, 0),\n"
        " datetime(2026, 8, 15, 20, 0, 0),\n"
        "]\n"
        "print(','.join(str(int(_as_utc(s).timestamp())) for s in samples))\n"
    )
    epochs = []
    for tz in ("UTC", "Europe/Paris", "Africa/Dar_es_Salaam"):
        env = {**os.environ, "TZ": tz, "PYTHONPATH": str(ROOT)}
        proc = subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        epochs.append(proc.stdout.strip())
    assert epochs[0] == epochs[1] == epochs[2]
    winter = int(epochs[0].split(",")[0])
    assert winter == int(datetime(2025, 12, 11, tzinfo=timezone.utc).timestamp())


def test_naive_utc_minute_series_has_no_dst_duplicates() -> None:
    start = datetime(2025, 10, 25, 0, 0, 0)
    seen = set()
    for i in range(72 * 60):
        ts = _as_utc(start + timedelta(minutes=i))
        epoch = int(ts.timestamp())
        assert epoch not in seen
        seen.add(epoch)
    assert len(seen) == 72 * 60


def test_clickhouse_source_does_not_drop_unique_utc_minutes(tmp_path=None) -> None:
    from research.stoch_fade_runner.candles import ClickHouseReadOnlyCandleSource, bind_readonly_fetcher
    from research.stoch_fade_runner.config import CANARY_SYMBOL

    start = datetime(2025, 10, 26, 0, 0, 0)

    def get_candles(symbol, start_t, end_t, *, exchange="bybit", interval="1m"):
        rows = []
        for i in range(180):
            ot = start + timedelta(minutes=i)
            rows.append(
                {
                    "open_time": ot,
                    "open": 1.0,
                    "high": 1.0,
                    "low": 1.0,
                    "close": 1.0,
                    "volume": 1.0,
                }
            )
        return rows

    src = ClickHouseReadOnlyCandleSource(bind_readonly_fetcher(get_candles))
    df = src.get_candles(
        CANARY_SYMBOL,
        datetime(2025, 10, 26, tzinfo=timezone.utc),
        datetime(2025, 10, 26, 3, tzinfo=timezone.utc),
    )
    assert src.last_stats["duplicate_count"] == 0
    assert src.last_stats["rows_removed_by_normalization"] == 0
    assert len(df) == 180
    assert df["open_time"].nunique() == 180


def test_generation_key_parity_ignores_strategy_version() -> None:
    research = [
        {
            "signal_id": "frozen-1",
            "symbol": "1000PEPEUSDT",
            "timeframe": "15m",
            "direction": "LONG",
            "signal_type": "wave_fade",
            "candle_open_time": "2026-08-01T00:00:00Z",
            "generated_at": "2026-08-01T00:15:00Z",
            "confirmation_available_at": "2026-08-01T00:15:00Z",
            "tier_a": True,
            "strategy_version": "wave_fade_frozen_f16ae32",
        }
    ]
    production = [
        {
            "signal_id": "live-1",
            "symbol": "1000PEPEUSDT",
            "timeframe": "15m",
            "direction": "LONG",
            "signal_type": "wave_fade",
            "candle_open_time": "2026-08-01T00:00:00Z",
            "generated_at": "2026-08-01T00:15:00Z",
            "tier_a": True,
            "strategy_version": "wave_fade_no_be50_v1",
            "metadata": '{"confirmation_available_at":"2026-08-01T00:15:00Z"}',
        }
    ]
    assert generation_key(research[0]) == generation_key(production[0])
    out = classify_parity(research, production, scope_symbol="1000PEPEUSDT")
    assert out["EXACT_MATCH"] == 0
    assert out["NOT_COMPARABLE_VERSION"] == 1
    assert out["generation_key_match"] == 1
    assert out["generation_key_field_mismatch"] == 0


def test_outcomes_payload_join_and_error() -> None:
    assert _outcomes_payload((3, 2), scope_symbol="AAVEUSDT")["scope"] == "symbol_via_signal_id_join"
    assert _outcomes_payload((3, 2), scope_symbol="AAVEUSDT")["scope_symbol"] == "AAVEUSDT"
    err = _outcomes_payload(("error", "no symbol"), scope_symbol="AAVEUSDT")
    assert err["error"] == "no symbol"
    glob = _outcomes_payload(("global_only", 9, 8, "fallback"), scope_symbol="AAVEUSDT")
    assert glob["count"] == 9
    assert glob["scope"] == "global_only"
