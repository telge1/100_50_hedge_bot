from types import SimpleNamespace

from contracts_v1 import RegimeContextV1
from entry_gate_v1 import evaluate_entry_gate
from short_pause_state_v1 import ShortPauseControllerV1


def test_short_paused_blocks_even_with_fake_ready_regime_path():
    pause = ShortPauseControllerV1()
    fake_sig = SimpleNamespace(pool_lower=0.1, pool_upper=0.11, pool_key="0.1|0.11", origin_ids="a|b")
    decision = evaluate_entry_gate(
        context=RegimeContextV1(
            decision_close_utc="2026-09-09T16:30:00Z",
            ohlc_to_close={"o": 0.1, "h": 0.1, "l": 0.09, "c": 0.095},
        ),
        pause=pause,
        baseline_signal=fake_sig,
        snapshot={"upper_bands": [], "lower_bands": []},
    )
    assert decision.allow_new_short_intent is False
    assert any(r.startswith("REGIME_") for r in decision.reasons)
    assert any(r.startswith("PAUSE_") for r in decision.reasons)
