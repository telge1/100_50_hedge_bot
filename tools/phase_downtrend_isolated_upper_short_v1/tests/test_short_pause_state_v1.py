from contracts_v1 import RegimeContextV1
from short_pause_state_v1 import ShortPauseControllerV1


def test_initial_short_paused():
    p = ShortPauseControllerV1()
    assert p.pause_state == "SHORT_PAUSED"
    assert p.blocks_new_short_intent() is True


def test_modeled_sl_sets_pause():
    p = ShortPauseControllerV1()
    p.set_ready_for_research_only()
    p.on_modeled_stop_loss(exit_bar_utc="2026-09-11T12:35:00Z", position_id="x")
    assert p.pause_state == "SHORT_PAUSED"
    assert len(p.events) == 1


def test_no_implicit_requalify_unlock():
    p = ShortPauseControllerV1()
    res = p.try_requalify(RegimeContextV1(decision_close_utc="2026-09-09T17:00:00Z"))
    assert res.unlocked is False
    assert p.pause_state == "SHORT_PAUSED"
