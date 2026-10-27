import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from d80_window_v1 import EXPECTED_CLOSE_COUNT, iter_window_closes_inclusive, plan_calendar_day_chunks


def test_three_day_close_count() -> None:
    closes = iter_window_closes_inclusive()
    assert len(closes) == EXPECTED_CLOSE_COUNT == 864
    chunks = plan_calendar_day_chunks(closes)
    assert len(chunks) == 3
    assert sum(len(c) for c in chunks) == 864
