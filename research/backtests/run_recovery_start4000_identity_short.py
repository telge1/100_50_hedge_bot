from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable, Tuple

from research.backtests.backtest_report import BacktestResult
from research.backtests.candle_loader import DEFAULT_DATA_DIR, load_candles_for_symbol
from research.backtests.fill_models import resolve_fill_model_config
from research.backtests.historical_backtest import run_historical_backtest
from research.backtests.recovery_bot.config import (
    RecoveryBotConfig,
    config_from_dict as recovery_config_from_dict,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
RESULT_BASELINE_DIR = (
    REPO_ROOT / "research" / "backtests" / "results" / "recovery_bot_current_three_audit"
)
BASELINE_PATH = RESULT_BASELINE_DIR / "APTUSDT_start4000_full.json"


def _load_baseline() -> dict[str, Any]:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def _load_window_candles(meta: dict[str, Any], *, max_candles: int) -> list[dict[str, Any]]:
    """Rekonstruiere exakt dasselbe Candle-Fenster wie im Baseline-Result."""
    source_ts = str(meta.get("source_candle_timestamp") or "")
    all_rows = load_candles_for_symbol(
        "APTUSDT",
        timeframe="5m",
        data_dir=DEFAULT_DATA_DIR,
        limit=60000,
    )
    start_idx = None
    for idx, row in enumerate(all_rows):
        ts = row.get("timestamp")
        if ts is None:
            continue
        ts_str = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
        if ts_str == source_ts:
            start_idx = idx
            break
    if start_idx is None:
        raise ValueError(f"could not find candle with timestamp={source_ts!r}")
    window = all_rows[start_idx : start_idx + max_candles + 1]
    if len(window) < max_candles + 1:
        raise ValueError(f"window too short: expected >= {max_candles+1}, got {len(window)}")
    return window


def _build_reconstructed_payload() -> dict[str, Any]:
    """Rekonstruierte Recovery-Konfiguration für den Audit-Lauf (mit Budget).

    Diese Funktion basiert auf:
    - den Defaults von RecoveryBotConfig,
    - den beobachteten Trigger- und Neutralisierungs-Ereignissen im Baseline-Trace.
    """
    base = RecoveryBotConfig(enabled=True)
    payload = asdict(base)

    # Trigger-Konfiguration: CYCLE_2_SHORT_REDUCE bei Candle 47, sofortige Aktivierung.
    payload["enabled"] = True
    payload["trigger_order"] = "CYCLE_2_SHORT_REDUCE"
    payload["trigger_wait_candles"] = 0
    # Preis-Down-Move-Anforderung: Drop von ca. 1.31 % reicht aus.
    payload["trigger_price_drop_pct"] = 0.0

    # Neutralisierung: konstante Schrittgröße 5.444 über 3 Schritte -> 5 Zielschritte.
    payload["neutralize_reduce_mode"] = "fixed_steps"
    payload["neutralize_target_steps"] = 5

    # Loss-Budget: laut Summary 1.5 USDT, als fixer Betrag modelliert.
    payload["loss_budget_mode"] = "fixed"
    payload["fixed_loss_budget_usdt"] = 1.5

    # Reload-spezifische Einstellungen sind für die Candles bis 60 nicht relevant;
    # wir belassen sie bei den Defaults.
    return payload


def _extract_identity_markers(trace: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Extrahiere die Identity-Merkmale aus einem Recovery-Trace."""
    markers: dict[str, Any] = {
        "trigger_candle": None,
        "trigger_reason": None,
        "neutralization_filled_candles": [],
        "first_block_candle": None,
        "qty_after_candle_54": None,
        "loss_budget_used": None,
    }
    for entry in trace:
        action = str(entry.get("action") or "")
        candle = entry.get("candle_index")
        if candle is None:
            continue
        candle_i = int(candle)

        if action == "RECOVERY_TRIGGER_OBSERVED" and markers["trigger_candle"] is None:
            markers["trigger_candle"] = candle_i
            markers["trigger_reason"] = str(entry.get("reason") or "")
        elif action == "NEUTRALIZATION_FILLED":
            markers["neutralization_filled_candles"].append(candle_i)
            if candle_i == 54:
                markers["qty_after_candle_54"] = (
                    float(entry.get("long_qty") or 0.0),
                    float(entry.get("short_qty") or 0.0),
                )
        elif action == "NEUTRALIZATION_BLOCKED" and markers["first_block_candle"] is None:
            markers["first_block_candle"] = candle_i
            markers["loss_budget_used"] = float(entry.get("loss_budget_used_usdt") or 0.0)

    return markers


def _compare_markers(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
) -> Tuple[bool, str]:
    """Vergleiche die Identity-Merkmale; gib bei Abweichung eine kurze Begründung zurück."""
    if baseline["trigger_candle"] != candidate["trigger_candle"]:
        return False, f"trigger_candle differs: baseline={baseline['trigger_candle']} candidate={candidate['trigger_candle']}"
    if baseline["trigger_reason"] != candidate["trigger_reason"]:
        return False, f"trigger_reason differs: baseline={baseline['trigger_reason']!r} candidate={candidate['trigger_reason']!r}"

    if baseline["neutralization_filled_candles"] != candidate["neutralization_filled_candles"]:
        return (
            False,
            f"neutralization_filled_candles differs: baseline={baseline['neutralization_filled_candles']} "
            f"candidate={candidate['neutralization_filled_candles']}",
        )

    if baseline["first_block_candle"] != candidate["first_block_candle"]:
        return False, f"first_block_candle differs: baseline={baseline['first_block_candle']} candidate={candidate['first_block_candle']}"

    b_qty = baseline["qty_after_candle_54"]
    c_qty = candidate["qty_after_candle_54"]
    if b_qty != c_qty:
        return False, f"qty_after_candle_54 differs: baseline={b_qty} candidate={c_qty}"

    b_budget = baseline["loss_budget_used"]
    c_budget = candidate["loss_budget_used"]
    if b_budget is None or c_budget is None:
        return False, f"loss_budget_used missing: baseline={b_budget} candidate={c_budget}"
    if abs(b_budget - c_budget) > 1e-9:
        return False, f"loss_budget_used differs: baseline={b_budget} candidate={c_budget}"

    return True, "identity markers match"


def _run_short_backtest(
    *,
    recovery_payload: dict[str, Any],
    max_candles: int,
) -> BacktestResult:
    baseline = _load_baseline()
    window = _load_window_candles(baseline, max_candles=max_candles)

    cfg_diag = dict(baseline.get("config_diagnostics") or {})
    config_source = str(cfg_diag.get("config_source") or "live")
    long_config_path = cfg_diag.get("config_path") or ""

    from research.backtests.backtest_config_loader import DEFAULT_SHORT_CONFIG_PATH

    fill_model_name = str(baseline.get("fill_model") or "conservative")
    fill_cfg = resolve_fill_model_config(fill_model=fill_model_name, max_fills_per_candle=None)

    recovery_config = recovery_config_from_dict(recovery_payload)

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
    return result


def main() -> int:
    baseline_meta = _load_baseline()
    baseline_trace = list(baseline_meta.get("recovery_trace") or [])
    baseline_markers = _extract_identity_markers(baseline_trace)

    # 1. Budget-Identity-Test mit rekonstruierter Config.
    budget_payload = _build_reconstructed_payload()
    budget_result = _run_short_backtest(recovery_payload=budget_payload, max_candles=60)
    budget_markers = _extract_identity_markers(budget_result.recovery_trace or [])
    ok, reason = _compare_markers(baseline_markers, budget_markers)
    print("budget_identity_test:", "OK" if ok else "FAIL", "-", reason)
    if not ok:
        return 1

    # 2. No-Budget-Kurztest: identische Config, nur loss_budget_mode='disabled'.
    no_budget_payload = dict(budget_payload)
    no_budget_payload["loss_budget_mode"] = "disabled"
    no_budget_result = _run_short_backtest(recovery_payload=no_budget_payload, max_candles=60)
    no_budget_markers = _extract_identity_markers(no_budget_result.recovery_trace or [])

    # Für den No-Budget-Test vergleichen wir die gleichen Marker, erwarten aber
    # insbesondere, dass ab Candle 55 weitere Neutralisierungen möglich wären
    # (d.h. die Blockierung sich ändert). Wir geben nur die erste Abweichung aus.
    ok2, reason2 = _compare_markers(baseline_markers, no_budget_markers)
    print("no_budget_short_test:", "OK" if ok2 else "DIFF", "-", reason2)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

