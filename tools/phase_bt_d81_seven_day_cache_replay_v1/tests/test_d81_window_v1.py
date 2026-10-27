import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from d81_window_v1 import (
    EXPECTED_CLOSE_COUNT,
    chunk_boundary_starts,
    iter_window_closes_inclusive,
    interior_sample_closes,
    plan_calendar_day_chunks,
)


def test_seven_day_close_count() -> None:
    closes = iter_window_closes_inclusive()
    assert len(closes) == EXPECTED_CLOSE_COUNT == 2016
    chunks = plan_calendar_day_chunks(closes)
    assert len(chunks) == 7
    assert sum(len(c) for c in chunks) == 2016
    assert len(chunk_boundary_starts()) == 6
    assert len(interior_sample_closes()) == 7
