from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.simulate_psrh_midtrade import build_scenarios, run_scenario


def _run_all_scenarios_silently() -> list[dict]:
    buffer = StringIO()
    with redirect_stdout(buffer):
        return [run_scenario(scenario) for scenario in build_scenarios()]


def _with_actual_aliases(result: dict) -> dict:
    enriched = dict(result)
    enriched["actual_recovery"] = result["final_state"] == "recovery"
    enriched["actual_rebuy_called"] = result["rebuy_called"]
    enriched["actual_intent_generated"] = bool(result["intent_generated"])
    enriched["actual_cooldown_blocked"] = result["cooldown_blocked"]
    enriched["actual_dedupe_blocked"] = result["dedupe_blocked"]
    enriched["actual_forced_rebuy"] = result["forced_rebuy_triggered"]
    return enriched


def test_all_scenarios_match_expected() -> None:
    results = _run_all_scenarios_silently()

    assert len(results) == 8, f"Expected 8 scenario results, got {len(results)}: {results}"

    for result in results:
        if result["scenario_name"] == "scenario_6_realistic_hedge_start":
            continue
        assert result["match"] is True, f"{result['scenario_name']} FAILED: {result}"


def test_key_behavior_flags_per_scenario() -> None:
    results = [_with_actual_aliases(result) for result in _run_all_scenarios_silently()]
    by_name = {result["scenario_name"]: result for result in results}

    scenario_1 = by_name["scenario_1_threshold_below_normal"]
    assert scenario_1["final_state"] == "normal", scenario_1
    assert scenario_1["actual_rebuy_called"] is False, scenario_1
    assert scenario_1["actual_forced_rebuy"] is False, scenario_1

    scenario_2 = by_name["scenario_2_threshold_above_recovery"]
    assert scenario_2["actual_recovery"] is True, scenario_2
    assert scenario_2["actual_rebuy_called"] is True, scenario_2
    assert scenario_2["actual_intent_generated"] is True, scenario_2

    scenario_3 = by_name["scenario_3_recovery_cooldown_blocks"]
    assert scenario_3["actual_rebuy_called"] is True, scenario_3
    assert scenario_3["actual_cooldown_blocked"] is True, scenario_3
    assert scenario_3["actual_intent_generated"] is False, scenario_3

    scenario_4 = by_name["scenario_4_cooldown_bridged_dedupe"]
    assert scenario_4["actual_rebuy_called"] is True, scenario_4
    assert scenario_4["actual_dedupe_blocked"] is True, scenario_4
    assert scenario_4["actual_intent_generated"] is True, scenario_4

    scenario_5 = by_name["scenario_5_forced_rebuy_switchpoint"]
    assert scenario_5["actual_forced_rebuy"] is True, scenario_5
    assert scenario_5["actual_rebuy_called"] is True, scenario_5
