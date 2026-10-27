"""Frozen known-case checks against OHLC + D46 exit contract."""

from __future__ import annotations

import copy
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

CASE_SEP20 = {
    "label": "sep20_fill_sl",
    "signal_utc": "2026-09-20T13:55:00Z",
    "fill_utc": "2026-09-20T14:00:00Z",
    "entry": 0.085,
    "sl": 0.08518002000000001,
    "tp": 0.08428,
}

CASE_SEP23 = {
    "label": "sep23_fill_tp_touch_3day_pilot",
    "signal_utc": "2026-09-23T12:25:00Z",
    "fill_utc": "2026-09-23T12:30:00Z",
    "entry": 0.09969,
    "sl": 0.10009378800000002,
    "tp": 0.09875,
    "horizon_end": "2026-09-25T23:55:00Z",
}


def _load_prices(start: str, end: str) -> dict[str, dict[str, float]]:
    d60 = ROOT / "tools/phase_bt_d60_ema_history_validation_v1"
    if str(d60) not in sys.path:
        sys.path.insert(0, str(d60))
    from ohlc_loader_v1 import load_5m_ohlc_ch  # noqa: E402

    _, prices, _ = load_5m_ohlc_ch(start, end)
    return prices


def _isolated_config_and_replay(closes: list[str], bundle: Path) -> dict[str, Any]:
    iso = ROOT / "tools/phase_downtrend_isolated_upper_short_v1"
    d73 = ROOT / "tools/phase_bt_d73_selected_sep23_24_v1"
    d8 = ROOT / "tools/phase_bt_d8_canonical_density_cache_v1"
    d60 = ROOT / "tools/phase_bt_d60_ema_history_validation_v1"
    d7 = ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1"
    for p in (iso, d73, d8, d60, d7):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    from config_loader_v1 import load_isolated_strategy_bundle  # noqa: E402
    from density_bridge_v1 import create_density_session  # noqa: E402
    from isolated_chronological_runner_v1 import run_isolated_chronological_replay  # noqa: E402
    from ohlc_loader_v1 import _iso, _parse_utc, load_5m_ohlc_ch  # noqa: E402
    from pure_ema_confirmation_v1 import build_pure_ema_gate  # noqa: E402
    from short_pause_state_v1 import ShortPauseControllerV1  # noqa: E402
    from validation_config_v1 import REFERENCE_WARMUP_HOURS  # noqa: E402

    class _CacheSession:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def collect_at_close(self, decision_close_utc: str) -> dict[str, Any]:
            return dict(self._inner.collect_at_close(decision_close_utc))

    bundle_cfg = load_isolated_strategy_bundle()
    config = copy.deepcopy(bundle_cfg["baseline_config"])
    config["density"]["density_source"] = "validated_cache"
    config["density"]["validated_cache"] = {"bundle_path": str(bundle.resolve())}
    isolated = dict(bundle_cfg["isolated_strategy_v1"])
    isolated["pure_ema_confirmation"] = {**(isolated.get("pure_ema_confirmation") or {}), "enabled": True, "classifier_version": "v3"}
    isolated["isolated_upper_eligibility"] = {
        **(isolated.get("isolated_upper_eligibility") or {}),
        "research_pass_on_baseline_signal": True,
    }
    load_start = _parse_utc(closes[0]) - timedelta(hours=REFERENCE_WARMUP_HOURS + 48)
    load_end = _parse_utc(closes[-1])
    bar_keys, prices, _ = load_5m_ohlc_ch(_iso(load_start), _iso(load_end))
    gate = build_pure_ema_gate(bar_keys=bar_keys, prices=prices, isolated_strategy_config=isolated)
    pause = ShortPauseControllerV1()
    pause.set_ready_for_research_only()
    replay = run_isolated_chronological_replay(
        config=config,
        isolated_bundle=isolated,
        prices=prices,
        bars=closes,
        density_session=_CacheSession(create_density_session(config)),
        pause=pause,
        ema_gate=gate,
    )
    return {"replay": replay, "prices": prices, "config": config}


def run_known_case_regressions(
    *,
    d81_bundle: Path,
    d80_bundle: Path,
) -> list[dict[str, Any]]:
    from exit_semantics_audit_v1 import ohlc_touch_audit, research_exit_from_fill

    results: list[dict[str, Any]] = []

    # Case 1 — within seven-day executable path
    prices20 = _load_prices(CASE_SEP20["fill_utc"], "2026-09-20T14:30:00Z")
    d46_exit20 = research_exit_from_fill(
        _config(),
        entry_bar_utc=CASE_SEP20["fill_utc"],
        entry_price=CASE_SEP20["entry"],
        sl_price=CASE_SEP20["sl"],
        tp_price=CASE_SEP20["tp"],
        prices=prices20,
        end_utc="2026-09-20T14:30:00Z",
    )
    ohlc20 = ohlc_touch_audit(
        prices20,
        from_bar=CASE_SEP20["fill_utc"],
        to_bar="2026-09-20T14:30:00Z",
        sl_price=CASE_SEP20["sl"],
        tp_price=CASE_SEP20["tp"],
    )
    results.append(
        {
            "case": CASE_SEP20["label"],
            "executable_isolated_baseline": "SL at 2026-09-20T14:05:00Z (SL checked after fill on subsequent bars in isolated runner)",
            "d46_research_exit": d46_exit20,
            "ohlc_evidence": ohlc20,
            "ordering_note": (
                "D46 simulate_short_exit evaluates SL on the entry bar; isolated runner fills first "
                "then checks SL on later bars only — same-bar SL touch on fill bar yields D46@14:00 vs isolated@14:05."
            ),
            "pass": d46_exit20["exit_reason"] == "SL"
            and d46_exit20["exit_bar_utc"] in ("2026-09-20T14:00:00Z", "2026-09-20T14:05:00Z"),
        }
    )

    # Case 2 — three-day pilot window (no Sep-20 pause)
    d80_pkg = ROOT / "tools/phase_bt_d80_multichunk_cache_pilot_v1"
    if str(d80_pkg) not in sys.path:
        sys.path.insert(0, str(d80_pkg))
    from d80_window_v1 import iter_window_closes_inclusive  # noqa: E402

    closes3 = iter_window_closes_inclusive()
    sub = _isolated_config_and_replay(closes3, d80_bundle)
    fills = [p for p in sub["replay"]["modeled_positions"] if p.get("event") == "SIMULATED_FILL"]
    d46_exit23 = research_exit_from_fill(
        sub["config"],
        entry_bar_utc=CASE_SEP23["fill_utc"],
        entry_price=CASE_SEP23["entry"],
        sl_price=CASE_SEP23["sl"],
        tp_price=CASE_SEP23["tp"],
        prices=sub["prices"],
        end_utc=CASE_SEP23["horizon_end"],
    )
    ohlc23 = ohlc_touch_audit(
        sub["prices"],
        from_bar=CASE_SEP23["fill_utc"],
        to_bar=CASE_SEP23["horizon_end"],
        sl_price=CASE_SEP23["sl"],
        tp_price=CASE_SEP23["tp"],
    )
    baseline_open = len(fills) == 1 and not any(p.get("event") == "SIMULATED_SL" for p in sub["replay"]["modeled_positions"])
    results.append(
        {
            "case": CASE_SEP23["label"],
            "executable_isolated_baseline": {
                "fills": len(fills),
                "sl_events": sum(1 for p in sub["replay"]["modeled_positions"] if p.get("event") == "SIMULATED_SL"),
                "open_at_horizon": sub["replay"].get("position_open_at_end"),
            },
            "hypothetical_d46_research_exit": d46_exit23,
            "ohlc_evidence": ohlc23,
            "pass": d46_exit23["exit_reason"] == "TP" and d46_exit23["exit_bar_utc"] == "2026-09-23T13:05:00Z" and baseline_open,
        }
    )
    return results


def _config() -> dict[str, Any]:
    iso = ROOT / "tools/phase_downtrend_isolated_upper_short_v1"
    if str(iso) not in sys.path:
        sys.path.insert(0, str(iso))
    from config_loader_v1 import load_isolated_strategy_bundle  # noqa: E402

    return copy.deepcopy(load_isolated_strategy_bundle()["baseline_config"])
