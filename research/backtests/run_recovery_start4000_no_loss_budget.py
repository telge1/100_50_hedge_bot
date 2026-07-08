from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from research.backtests.backtest_report import BacktestResult
from research.backtests.candle_loader import DEFAULT_DATA_DIR, load_candles_for_symbol
from research.backtests.fill_models import resolve_fill_model_config
from research.backtests.historical_backtest import run_historical_backtest
from research.backtests.recovery_bot.config import (
    RecoveryBotConfig,
    config_from_dict as recovery_config_from_dict,
)
from research.backtests.recovery_bot_start4000_diagnostics import (
    _build_events,
    _write_event_log_csv,
    _write_markdown_summary,
    _write_timeline_csv,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
RESULT_BASELINE = (
    REPO_ROOT / "research" / "backtests" / "results" / "recovery_bot_current_three_audit"
)
RESULT_NO_BUDGET = (
    REPO_ROOT
    / "research"
    / "backtests"
    / "results"
    / "recovery_bot_start4000_no_loss_budget"
)


def _load_baseline_full() -> dict[str, Any]:
    path = RESULT_BASELINE / "APTUSDT_start4000_full.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _load_candles_for_window(meta: dict[str, Any]) -> list[dict[str, Any]]:
    start_ts = str(meta.get("source_candle_timestamp") or "")
    candles_processed = int(meta.get("candles_processed") or 0)
    rows = load_candles_for_symbol(
        "APTUSDT",
        timeframe="5m",
        data_dir=DEFAULT_DATA_DIR,
        limit=60000,
    )
    start_idx = None
    for idx, row in enumerate(rows):
        ts = row.get("timestamp")
        if ts is None:
            continue
        if hasattr(ts, "isoformat"):
            ts_str = ts.isoformat()
        else:
            ts_str = str(ts)
        if ts_str == start_ts:
            start_idx = idx
            break
    if start_idx is None:
        raise ValueError(
            f"could not find candle with timestamp={start_ts!r} in loaded APTUSDT candles"
        )
    end_idx = start_idx + candles_processed
    return rows[start_idx:end_idx]


def _build_recovery_config_disabled(baseline_meta: dict[str, Any]) -> RecoveryBotConfig:
    """Rekonstruiere eine RecoveryBotConfig und deaktiviere das Loss-Budget."""
    # Ausgangspunkt: eine gültige, aktivierte Config mit denselben Defaults wie im
    # ursprünglichen Lauf; nur das Loss-Budget wird auf 'disabled' gesetzt.
    base = RecoveryBotConfig(enabled=True)
    payload = {**base.__dict__}
    payload["enabled"] = True
    payload["loss_budget_mode"] = "disabled"
    # Belasse fixed_loss_budget_usdt unverändert, damit Diagnosen vergleichbar sind.
    return recovery_config_from_dict(payload)


def _write_full_result(path: Path, result: BacktestResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = result.to_dict()
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def main() -> int:
    baseline = _load_baseline_full()
    window = _load_candles_for_window(baseline)

    cfg_diag = dict(baseline.get("config_diagnostics") or {})
    config_source = str(cfg_diag.get("config_source") or "live")
    long_config_path = cfg_diag.get("config_path") or ""

    from research.backtests.backtest_config_loader import DEFAULT_SHORT_CONFIG_PATH

    fill_model = str(baseline.get("fill_model") or "conservative")
    fill_cfg = resolve_fill_model_config(fill_model=fill_model, max_fills_per_candle=None)

    recovery_bot_config = _build_recovery_config_disabled(baseline)

    result = run_historical_backtest(
        "APTUSDT",
        "long",
        window,
        max_candles=max(0, len(window) - 1),
        fill_model=fill_cfg.fill_model,
        max_fills_per_candle=fill_cfg.max_fills_per_candle,
        config_source=config_source,
        long_config_path=long_config_path,
        short_config_path=DEFAULT_SHORT_CONFIG_PATH,
        file_config_path=None,
        recovery_bot_config=recovery_bot_config,
    )
    result.start_index = int(baseline.get("requested_start_index") or 4000)
    result.window_candles = len(window)

    # Vollständiges Result speichern.
    full_path = RESULT_NO_BUDGET / "APTUSDT_start4000_full_no_loss_budget.json"
    _write_full_result(full_path, result)

    # Diagnostik auf Basis des neuen Results erneut aufbauen.
    events = _build_events(result.to_dict())
    event_log_path = RESULT_NO_BUDGET / "APTUSDT_start4000_event_log.csv"
    _write_event_log_csv(event_log_path, events)

    timeline_path = RESULT_NO_BUDGET / "APTUSDT_start4000_timeline.csv"
    _write_timeline_csv(timeline_path, events)

    diag_md_path = RESULT_NO_BUDGET / "APTUSDT_start4000_diagnostics.md"
    _write_markdown_summary(diag_md_path, result.to_dict(), events)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

