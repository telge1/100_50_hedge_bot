import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from d85_window_v1 import (
    EXPECTED_CLOSE_COUNT,
    REQUIRED_VISIBLE_TO_COVERAGE_UTC,
    chunk_boundary_starts,
    iter_window_closes_inclusive,
    interior_sample_closes,
    plan_calendar_day_chunks,
)


def test_july_close_count() -> None:
    closes = iter_window_closes_inclusive()
    assert len(closes) == EXPECTED_CLOSE_COUNT == 8928
    chunks = plan_calendar_day_chunks(closes)
    assert len(chunks) == 31
    assert sum(len(c) for c in chunks) == 8928
    assert len(chunk_boundary_starts()) == 30
    assert len(interior_sample_closes()) == 31


def test_last_close_visible_to() -> None:
    import sys as _sys
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[3]
    _sys.path.insert(0, str(root / "tools/phase_bt_d3_canonical_depth2_provider_v1"))
    from window_policy_v1 import causal_window_bounds

    closes = iter_window_closes_inclusive()
    _vf, vis_to = causal_window_bounds(closes[-1])
    assert vis_to == REQUIRED_VISIBLE_TO_COVERAGE_UTC
