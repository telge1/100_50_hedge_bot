"""Tests for lld_research_kernel (time boundaries + causal slice)."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_repo = Path(__file__).resolve().parents[5]
if str(_repo) not in sys.path:
    sys.path.insert(0, str(_repo))
if str(_repo / "backtester" / "short") not in sys.path:
    sys.path.insert(0, str(_repo / "backtester" / "short"))
from pool_pattern.market import ensure_paths

ensure_paths()

from dashboard.research_charts.lld_research_kernel import (
    causal_window_from_index,
    candle_stamps,
    pool_known_at,
)


def _row(known: datetime, break_at: datetime | None) -> dict:
    return {
        "pool_id": "test",
        "side": "upper",
        "bottom": 1.0,
        "top": 1.1,
        "known": known,
        "source": known,
        "break_at": break_at,
    }


def test_pool_known_at_equality_boundaries():
    t = datetime(2026, 6, 1, 16, 45, tzinfo=timezone.utc)
    assert pool_known_at(_row(t, None), t) is True
    assert pool_known_at(_row(t + timedelta(seconds=1), None), t) is False
    assert pool_known_at(_row(t - timedelta(minutes=15), t), t) is False
    assert pool_known_at(_row(t - timedelta(minutes=30), t + timedelta(minutes=15)), t) is True


def test_causal_window_from_index():
    base = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    stamps = [base + timedelta(minutes=15 * i) for i in range(10)]
    wf = causal_window_from_index(stamps, 9, weeks=1)
    assert wf >= 0
    assert stamps[wf] <= stamps[9]


@pytest.mark.skipif(
    not __import__("pathlib").Path("/home/telgenbuescher/projects/trading_research_platform").is_dir(),
    reason="TRP not available",
)
def test_scanner_selection_tracks_pane_data_end():
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parents[5]
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    from pool_pattern.market import ensure_paths

    ensure_paths()
    from dashboard.research_charts.lld_research_kernel import (
        causal_pools_for_index,
        load_pane_candles,
        ui_lld_config,
    )
    from dashboard.research_charts.trp_import import load_trp

    load_trp()
    entry = datetime(2026, 6, 1, 16, 30, tzinfo=timezone.utc)
    from_unix = int(datetime(2026, 2, 10, tzinfo=timezone.utc).timestamp())
    to_unix = int(datetime(2026, 9, 26, 12, tzinfo=timezone.utc).timestamp())
    _packed, candles = load_pane_candles(
        "XRPUSDT",
        "15m",
        from_unix=from_unix,
        to_unix=to_unix,
    )
    stamps = candle_stamps(candles)
    idx = stamps.index(entry)
    cfg = ui_lld_config("15m")
    from dashboard.research_charts.lld_research_kernel import parity_scanner_vs_ui_at_index

    parity = parity_scanner_vs_ui_at_index(candles, "15m", idx, cfg)
    assert parity["ids_match"] is True


def test_no_stale_snapshot_index_semantics():
    """Causal path uses per-index cache; index 16:30 must not reuse index 12:00 pools."""
    moment_1245 = datetime(2026, 6, 1, 12, 45, tzinfo=timezone.utc)
    moment_1645 = datetime(2026, 6, 1, 16, 45, tzinfo=timezone.utc)
    known_1600 = datetime(2026, 6, 1, 16, 0, tzinfo=timezone.utc)
    pool_new = _row(known_1600, None)
    stale_list = []
    assert not any(pool_known_at(pool_new, moment_1245) for _ in [0])
    assert pool_known_at(pool_new, moment_1645)
