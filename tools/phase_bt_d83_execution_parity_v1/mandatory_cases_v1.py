"""BT-D83 mandatory execution regressions."""

from __future__ import annotations

import copy
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


def _paths() -> None:
    for p in (
        ROOT / "tools/phase_bt_d83_execution_parity_v1",
        ROOT / "tools/phase_bt_d82_tp_sl_pause_accounting_v1",
        ROOT / "tools/phase_bt_d81_seven_day_cache_replay_v1",
        ROOT / "tools/phase_bt_d80_multichunk_cache_pilot_v1",
        ROOT / "tools/phase_downtrend_isolated_upper_short_v1",
        ROOT / "tools/phase_bt_d73_selected_sep23_24_v1",
        ROOT / "tools/phase_bt_d8_canonical_density_cache_v1",
        ROOT / "tools/phase_bt_d60_ema_history_validation_v1",
        ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1",
    ):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))


def _session_setup(bundle: Path, closes: list[str]) -> dict[str, Any]:
    _paths()
    from config_loader_v1 import load_isolated_strategy_bundle  # noqa: E402
    from density_bridge_v1 import create_density_session  # noqa: E402
    from isolated_chronological_runner_v1 import run_isolated_chronological_replay  # noqa: E402
    from ohlc_loader_v1 import _iso, _parse_utc, load_5m_ohlc_ch  # noqa: E402
    from pure_ema_confirmation_v1 import build_pure_ema_gate  # noqa: E402
    from research_execution_runner_v1 import run_research_execution_replay  # noqa: E402
    from short_pause_state_v1 import ShortPauseControllerV1  # noqa: E402
    from validation_config_v1 import REFERENCE_WARMUP_HOURS  # noqa: E402

    class CacheOnlySession:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def collect_at_close(self, decision_close_utc: str) -> dict[str, Any]:
            return dict(self._inner.collect_at_close(decision_close_utc))

    bundle_cfg = load_isolated_strategy_bundle()
    config = copy.deepcopy(bundle_cfg["baseline_config"])
    config["density"]["density_source"] = "validated_cache"
    config["density"]["validated_cache"] = {"bundle_path": str(bundle.resolve())}
    isolated = dict(bundle_cfg["isolated_strategy_v1"])
    isolated["pure_ema_confirmation"] = {
        **(isolated.get("pure_ema_confirmation") or {}),
        "enabled": True,
        "classifier_version": "v3",
    }
    isolated["isolated_upper_eligibility"] = {
        **(isolated.get("isolated_upper_eligibility") or {}),
        "research_pass_on_baseline_signal": True,
    }
    load_start = _parse_utc(closes[0]) - timedelta(hours=REFERENCE_WARMUP_HOURS + 48)
    load_end = _parse_utc(closes[-1])
    bar_keys, prices, _ = load_5m_ohlc_ch(_iso(load_start), _iso(load_end))
    gate = build_pure_ema_gate(bar_keys=bar_keys, prices=prices, isolated_strategy_config=isolated)
    session = CacheOnlySession(create_density_session(config))
    pause_b = ShortPauseControllerV1()
    pause_r = ShortPauseControllerV1()
    pause_b.set_ready_for_research_only()
    pause_r.set_ready_for_research_only()
    baseline = run_isolated_chronological_replay(
        config=config,
        isolated_bundle=isolated,
        prices=prices,
        bars=closes,
        density_session=session,
        pause=pause_b,
        ema_gate=gate,
    )
    research = run_research_execution_replay(
        config=config,
        isolated_bundle=isolated,
        prices=prices,
        bars=closes,
        density_session=session,
        pause=pause_r,
        ema_gate=gate,
    )
    return {"config": config, "prices": prices, "baseline": baseline, "research": research, "closes": closes}


def run_mandatory_cases(d81_bundle: Path, d80_bundle: Path) -> list[dict[str, Any]]:
    _paths()
    from d81_window_v1 import iter_window_closes_inclusive  # noqa: E402
    from d80_window_v1 import iter_window_closes_inclusive as d80_closes  # noqa: E402
    from exit_semantics_audit_v1 import intrabar_exit_decision  # noqa: E402
    from research_execution_runner_v1 import summarize_trades  # noqa: E402

    out: list[dict[str, Any]] = []

    seven = _session_setup(d81_bundle, iter_window_closes_inclusive())
    b_sl = [p for p in seven["baseline"]["modeled_positions"] if p.get("event") == "SIMULATED_SL"]
    r_exits = [p for p in seven["research"]["modeled_positions"] if p.get("event") != "SIMULATED_FILL"]
    out.append(
        {
            "case": "sep20_fill_sl_timing",
            "baseline_sl_utc": b_sl[0]["decision_close_utc"] if b_sl else None,
            "research_exit": r_exits[0] if r_exits else None,
            "pass": b_sl and b_sl[0]["decision_close_utc"] == "2026-09-20T14:05:00Z"
            and r_exits
            and r_exits[0].get("event") in ("SIMULATED_SL", "SIMULATED_TP", "SIMULATED_EXIT_AMBIGUOUS"),
        }
    )

    three = _session_setup(d80_bundle, d80_closes())
    r_tp = [p for p in three["research"]["modeled_positions"] if p.get("event") == "SIMULATED_TP"]
    b_open = three["baseline"].get("position_open_at_end")
    out.append(
        {
            "case": "sep23_three_day_tp_research",
            "baseline_open_at_end": b_open,
            "research_tp": r_tp[0] if r_tp else None,
            "pass": len(r_tp) == 1 and r_tp[0].get("decision_close_utc") == "2026-09-23T13:05:00Z" and b_open,
        }
    )

    cfg = seven["config"]
    policy = cfg["pnl"]["intrabar_ambiguity_policy"]
    amb = intrabar_exit_decision(
        ohlc={"h": 1.0, "l": 0.9},
        sl_price=0.95,
        tp_price=0.92,
        policy=policy,
    )
    out.append(
        {
            "case": "both_hit_same_candle",
            "policy": policy,
            "result": {"reason": amb[0], "price": amb[1], "ambiguous": amb[2]},
            "pass": amb[0] == "AMBIGUOUS_INTRABAR" and amb[1] is None and policy == "no_trade_ambiguous",
        }
    )

    pause_after_sep20 = None
    for g in seven["baseline"]["gate_decisions"]:
        if g.get("decision_close_utc", "").startswith("2026-09-21"):
            pause_after_sep20 = g.get("pause_state")
            break
    out.append(
        {
            "case": "state_across_midnight",
            "pause_state_first_bar_sep21": pause_after_sep20,
            "pass": pause_after_sep20 == "SHORT_PAUSED",
        }
    )

    out.append(
        {
            "case": "seven_day_summary",
            "baseline": summarize_trades(seven["baseline"]),
            "research": summarize_trades(seven["research"]),
            "pass": True,
        }
    )
    return out
