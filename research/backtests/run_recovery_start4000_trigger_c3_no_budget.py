from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from research.backtests.backtest_report import BacktestResult
from research.backtests.fill_models import resolve_fill_model_config
from research.backtests.historical_backtest import run_historical_backtest
from research.backtests.recovery_bot.config import (
    config_from_dict as recovery_config_from_dict,
)
from research.backtests.run_recovery_start4000_identity_short import (
    _build_reconstructed_payload,
    _load_baseline,
    _load_window_candles,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
RESULT_DIR = (
    REPO_ROOT
    / "research"
    / "backtests"
    / "results"
    / "recovery_bot_start4000_trigger_c3_no_budget"
)


def _write_full_result(path: Path, result: BacktestResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = result.to_dict()
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def main() -> int:
    baseline = _load_baseline()

    # Ausgangspunkt: validierte Recovery-Konfiguration für Start 4000.
    payload: dict[str, Any] = _build_reconstructed_payload()
    # Testkonfiguration laut Vorgabe:
    payload["trigger_order"] = "CYCLE_3_SHORT_REDUCE"
    payload["trigger_wait_candles"] = 0
    payload["trigger_price_drop_pct"] = 0.0
    payload["loss_budget_mode"] = "disabled"

    recovery_config = recovery_config_from_dict(payload)

    candles_processed = int(baseline.get("candles_processed") or 0)
    # Für die verlängerte Auswertung das vollständige Fenster verwenden.
    max_candles = candles_processed
    window = _load_window_candles(baseline, max_candles=max_candles)

    cfg_diag = dict(baseline.get("config_diagnostics") or {})
    config_source = str(cfg_diag.get("config_source") or "live")
    long_config_path = cfg_diag.get("config_path") or ""

    from research.backtests.backtest_config_loader import DEFAULT_SHORT_CONFIG_PATH

    fill_model_name = str(baseline.get("fill_model") or "conservative")
    fill_cfg = resolve_fill_model_config(
        fill_model=fill_model_name, max_fills_per_candle=None
    )

    result = run_historical_backtest(
        "APTUSDT",
        "long",
        window,
        max_candles=max_candles,
        fill_model=fill_cfg.fill_model,
        max_fills_per_candle=fill_cfg.max_fills_per_candle,
        config_source=config_source,
        long_config_path=long_config_path,
        short_config_path=DEFAULT_SHORT_CONFIG_PATH,
        file_config_path=None,
        recovery_bot_config=recovery_config,
    )

    full_path = RESULT_DIR / "APTUSDT_start4000_trigger_c3_no_budget_full.json"
    _write_full_result(full_path, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

