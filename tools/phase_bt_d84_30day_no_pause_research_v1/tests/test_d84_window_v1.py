import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "phase_downtrend_isolated_upper_short_v1"))

from d84_window_v1 import (
    EXPECTED_CLOSE_COUNT,
    chunk_boundary_starts,
    iter_window_closes_inclusive,
    interior_sample_closes,
    plan_calendar_day_chunks,
)
from research_pause_v1 import ResearchPauseControllerV1


def test_thirty_day_close_count() -> None:
    closes = iter_window_closes_inclusive()
    assert len(closes) == EXPECTED_CLOSE_COUNT == 8640
    chunks = plan_calendar_day_chunks(closes)
    assert len(chunks) == 30
    assert sum(len(c) for c in chunks) == 8640
    assert len(chunk_boundary_starts()) == 29
    assert len(interior_sample_closes()) == 30


def test_post_sl_pause_opt_out() -> None:
    ctl = ResearchPauseControllerV1(post_sl_pause_enabled=False)
    ctl.set_ready_for_research_only()
    ctl.on_modeled_stop_loss(exit_bar_utc="2026-09-01T00:00:00Z", position_id="p1")
    assert ctl.pause_state == "READY"
    assert len(ctl.events) == 1

    ctl2 = ResearchPauseControllerV1(post_sl_pause_enabled=True)
    ctl2.set_ready_for_research_only()
    ctl2.on_modeled_stop_loss(exit_bar_utc="2026-09-01T00:00:00Z", position_id="p2")
    assert ctl2.pause_state == "SHORT_PAUSED"


def test_density_tail_violations() -> None:
    from density_validation_v1 import validate_full_window_density

    closes = iter_window_closes_inclusive()
    gate = validate_full_window_density(closes, expected_count=EXPECTED_CLOSE_COUNT)
    assert gate["density_scope_violation_count"] == 3
    assert gate["density_scope_violations"][-1] == "2026-09-30T23:55:00Z"
