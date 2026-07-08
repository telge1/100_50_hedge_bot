from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from research.backtests.candle_loader import DEFAULT_DATA_DIR, load_candles_for_symbol
from research.backtests.fill_models import resolve_fill_model_config
from research.backtests.historical_backtest import run_historical_backtest
from research.backtests.backtest_config_loader import DEFAULT_LONG_CONFIG_PATH, DEFAULT_SHORT_CONFIG_PATH


REPO_ROOT = Path(__file__).resolve().parents[2]


def _strict_no_refill_config_path() -> Path:
    cfg_path = REPO_ROOT / DEFAULT_LONG_CONFIG_PATH
    payload: Dict[str, Any] = json.loads(cfg_path.read_text(encoding="utf-8"))
    payload["time_distance_refill_trigger_minutes"] = 0
    payload["disable_cycle_refill"] = True
    payload["disable_recovery_refill"] = True
    out = REPO_ROOT / "research" / "backtests" / "results" / "start4000_strict_no_refill_no_recovery" / "long_config_strict_no_refill.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out


def _load_start4000_window() -> List[Dict[str, Any]]:
    """Lade das gleiche Candle-Fenster wie im bestehenden Audit-Lauf."""
    base_path = REPO_ROOT / "research" / "backtests" / "results" / "recovery_bot_current_three_audit" / "APTUSDT_start4000_full.json"
    meta = json.loads(base_path.read_text(encoding="utf-8"))
    source_ts = str(meta.get("source_candle_timestamp") or "")
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
        ts_str = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
        if ts_str == source_ts:
            start_idx = idx
            break
    if start_idx is None:
        raise ValueError(f"could not find candle with timestamp={source_ts!r}")
    return rows[start_idx : start_idx + candles_processed]


@pytest.mark.slow
def test_strict_no_refill_short_window_has_no_refill_purposes() -> None:
    """Kurzlauf bis nach Candle 55 darf keinerlei REFILL*-Purposes enthalten."""
    cfg_path = _strict_no_refill_config_path()
    window = _load_start4000_window()
    # Bis etwas nach Candle 55 laufen lassen.
    max_candles = 60

    fill_cfg = resolve_fill_model_config(fill_model="conservative", max_fills_per_candle=None)

    result = run_historical_backtest(
        "APTUSDT",
        "long",
        window,
        max_candles=max_candles,
        fill_model=fill_cfg.fill_model,
        max_fills_per_candle=fill_cfg.max_fills_per_candle,
        config_source="file",
        long_config_path=cfg_path,
        short_config_path=DEFAULT_SHORT_CONFIG_PATH,
        file_config_path=cfg_path,
        recovery_bot_config=None,
    )

    def _has_refill_purpose(entries: List[Dict[str, Any]], key: str = "purpose") -> bool:
        for ev in entries:
            val = str(ev.get(key) or "")
            if val.startswith("REFILL_") or val.startswith("RECOVERY_REFILL_") or val.startswith("RECOVERY_RELOAD_"):
                return True
        return False

    fill_log = list(result.fill_log or [])
    order_log = list(result.order_log or [])
    intent_log = list(result.intent_log or [])

    assert not _has_refill_purpose(fill_log), "refill purposes present in fill_log"
    assert not _has_refill_purpose(order_log), "refill purposes present in order_log"
    assert not _has_refill_purpose(intent_log), "refill purposes present in intent_log"
    assert not result.recovery_trace, "recovery_trace must be empty when recovery is disabled"


