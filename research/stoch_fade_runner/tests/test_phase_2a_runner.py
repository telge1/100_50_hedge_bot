from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from research.stoch_fade_runner.candles import MemoryCandleSource
from research.stoch_fade_runner.cli import main
from research.stoch_fade_runner.config import (
    CANARY_SYMBOL,
    SOURCE_COMMIT_PIN,
    STRATEGY_ID,
    assert_frozen_pin,
)
from research.stoch_fade_runner.engine import evaluate_symbol
from research.stoch_fade_runner.guards import (
    assert_runner_has_no_production_writers,
    reject_forbidden_argv,
)


def test_frozen_pin_matches_signal_generator() -> None:
    assert_frozen_pin()
    assert STRATEGY_ID == "wave_fade_frozen_f16ae32"
    assert SOURCE_COMMIT_PIN == "f16ae32"


def test_runner_source_has_no_production_writers() -> None:
    assert_runner_has_no_production_writers()


def test_cli_rejects_cleanup_and_shadow_pipeline() -> None:
    assert reject_forbidden_argv(["--cleanup-first"]) == "FORBIDDEN_ARG:--cleanup-first"
    assert reject_forbidden_argv(["run_wave_fade_shadow_pipeline"]) is not None
    assert main(["--cleanup-first", "--dry-run-empty"]) == 2
    assert main(["--symbol", "ALL", "--dry-run-empty"]) == 2


def test_empty_candles_is_no_candle_data() -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 2, tzinfo=timezone.utc)
    out = evaluate_symbol(
        symbol=CANARY_SYMBOL,
        candle_source=MemoryCandleSource({}),
        signal_start=start,
        signal_end_exclusive=end,
    )
    assert out["status"] == "NO_CANDLE_DATA"
    assert out["signals"] == []
    assert out["side_effect_flags"]["writes_to_clickhouse"] is False
    assert out["side_effect_flags"]["cleanup_enabled"] is False


def test_incomplete_when_bars_only_before_window() -> None:
    start = datetime(2026, 1, 10, tzinfo=timezone.utc)
    end = datetime(2026, 1, 11, tzinfo=timezone.utc)
    rows = []
    t0 = datetime(2025, 12, 1, tzinfo=timezone.utc)
    for i in range(5):
        ot = t0 + timedelta(minutes=i)
        rows.append(
            {
                "open_time": ot,
                "open": 1.0,
                "high": 1.1,
                "low": 0.9,
                "close": 1.0,
                "volume": 1.0,
            }
        )
    src = MemoryCandleSource({CANARY_SYMBOL: pd.DataFrame(rows)})
    out = evaluate_symbol(
        symbol=CANARY_SYMBOL,
        candle_source=src,
        signal_start=start,
        signal_end_exclusive=end,
    )
    assert out["status"] == "INCOMPLETE_DATA"


def test_dry_run_cli_writes_artifacts_only(tmp_path: Path) -> None:
    rc = main(
        [
            "--dry-run-empty",
            "--symbol",
            CANARY_SYMBOL,
            "--out-root",
            str(tmp_path),
            "--signal-start",
            "2026-01-01T00:00:00Z",
            "--signal-end-exclusive",
            "2026-01-02T00:00:00Z",
        ]
    )
    assert rc == 0
    run_dirs = [p for p in tmp_path.iterdir() if p.is_dir()]
    assert len(run_dirs) == 1
    run_dir = run_dirs[0]
    manifest = (run_dir / "run_manifest.json").read_text(encoding="utf-8")
    assert STRATEGY_ID in manifest
    assert SOURCE_COMMIT_PIN in manifest
    assert "wave_fade_no_be50_v1" in manifest
    assert '"be50_outcome_active": false' in manifest
    assert '"clickhouse_canary": false' in manifest
    assert '"writes_to_clickhouse": false' in manifest
    import json

    data = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    assert data["selected_symbols"] == ["1000PEPEUSDT"]
    assert data["selected_symbol"] == "1000PEPEUSDT"
    assert data["default_canary_symbol"] == "1000PEPEUSDT"
    assert data["default_canary_symbol_is_not_run_symbol"] is True
    assert "canary_symbol" not in data
    assert data["universe_count"] == 51
    assert data["symbol_allowlisted"] is True
    assert data["side_effect_flags"]["writes_to_clickhouse"] is False
    assert (run_dir / "coverage.json").is_file()
    assert (run_dir / "signals.jsonl").is_file()
    assert (run_dir / "summary.json").is_file()


def test_synthetic_bars_evaluate_without_writers() -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc)
    rows = []
    t0 = start - timedelta(days=1)
    px = 100.0
    n = 60 * 27
    for i in range(n):
        ot = t0 + timedelta(minutes=i)
        px = 100.0 + (i % 40) * 0.2
        rows.append(
            {
                "open_time": ot,
                "open": px,
                "high": px + 0.3,
                "low": px - 0.3,
                "close": px + 0.1,
                "volume": 10.0,
            }
        )
    src = MemoryCandleSource({CANARY_SYMBOL: pd.DataFrame(rows)})
    out = evaluate_symbol(
        symbol=CANARY_SYMBOL,
        candle_source=src,
        signal_start=start,
        signal_end_exclusive=end,
    )
    assert out["status"] in {"EVALUATED_WITH_SIGNALS", "EVALUATED_NO_SIGNAL"}
    assert out["side_effect_flags"]["writes_to_signals"] is False
    for sig in out["signals"]:
        assert sig["strategy_version"] == STRATEGY_ID
        assert sig["generator_version"] == "stoch_fade_research_runner_v1"
