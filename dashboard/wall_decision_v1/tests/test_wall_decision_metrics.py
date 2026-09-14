"""Unit tests for wall-decision adapter semantics (no live CH required for pure helpers)."""

from __future__ import annotations

import sys
from pathlib import Path

DASHBOARD = Path(__file__).resolve().parents[1]
if str(DASHBOARD) not in sys.path:
    sys.path.insert(0, str(DASHBOARD))

from wall_decision_v1.metrics import (  # noqa: E402
    _match_wall,
    _zone,
    book_side_range,
    sum_zone_qty,
)


def test_sum_zone_qty_exact_tick():
    levels = [
        {"price": 77573.8, "size": 1.5},
        {"price": 77573.7, "size": 9.0},
        {"price": 77573.9, "size": 2.0},
    ]
    assert sum_zone_qty(levels, 77573.8, 77573.8) == 1.5
    assert sum_zone_qty(levels, 77573.7, 77573.9) == 12.5


def test_sum_zone_qty_missing_level_is_zero():
    levels = [{"price": 77574.0, "size": 3.0}]
    assert sum_zone_qty(levels, 77573.8, 77573.8) == 0.0


def test_book_side_range():
    levels = [{"price": 100.0, "size": 1}, {"price": 90.0, "size": 2}]
    assert book_side_range(levels) == (90.0, 100.0)


def test_match_wall_by_side_and_price():
    bars = [
        {"side": "ASK", "price": 101100.0, "qty": 10},
        {"side": "BID", "price": 100900.0, "qty": 8},
    ]
    hit = _match_wall(bars, {"side": "ASK", "price": 101100.0}, tick=0.1)
    assert hit is not None
    assert hit["qty"] == 10
    assert _match_wall(bars, {"side": "ASK", "price": 999999.0}, tick=0.1) is None


def test_zone_falls_back_to_tick_band():
    lo, hi = _zone({"price": 100.0}, tick=0.1)
    assert lo < 100.0 < hi


def test_session_semantics_no_reduce_means_explained_zero(monkeypatch):
    """Reproduce wd1 session bug: wall grew → explained must be 0, not incomplete."""
    from wall_decision_v1 import metrics as m

    def fake_wall(**kwargs):
        return {
            "qty": 7.731,
            "adapter": "ok",
            "source": "test",
            "book_ready": True,
            "wall_absent": False,
            "stale": False,
            "event_time": "2026-09-14T09:26:55Z",
            "coverage": {},
            "error": None,
        }

    def fake_trades(symbol, start, end, lo, hi):
        return [{"side": "Buy", "price": lo, "size": 0.001, "notional": 77.57, "trade_ts": start, "trade_id": "1"}]

    monkeypatch.setattr(m, "resolve_wall_current_qty", fake_wall)
    monkeypatch.setattr(m, "_load_trades_in_zone", fake_trades)

    out = m.compute_live_metrics(
        symbol="BTCUSDT",
        breakpoint=77587.7,
        target_wall={"side": "BID", "price": 77573.8, "zone_lo": 77573.8, "zone_hi": 77573.8, "qty": 1.922},
        baseline_qty=1.922,
        baseline_notional=149096.8,
        triggered_at=None,
        trigger_price=77587.7,
        live_price=77550.0,
        min_qty_seen=1.922,
    )
    assert out["incomplete_trades"] is False
    assert out["wall_current_qty"] == 7.731
    assert out["wall_reduce_pct"] == 0
    assert out["trade_explained_pct"] == 0.0
    assert out["pull_pct"] == 0.0
    assert out["adapters"]["trade_explained_note"] == "no_wall_reduction"


def test_absent_level_on_ready_book_is_zero(monkeypatch):
    from wall_decision_v1 import metrics as m

    def fake_wall(**kwargs):
        return {
            "qty": 0.0,
            "adapter": "ok",
            "source": "ob1000_on_demand",
            "book_ready": True,
            "wall_absent": True,
            "stale": False,
            "event_time": "2026-09-14T09:26:55Z",
            "coverage": {},
            "error": None,
        }

    monkeypatch.setattr(m, "resolve_wall_current_qty", fake_wall)
    monkeypatch.setattr(m, "_load_trades_in_zone", lambda *a, **k: [])

    out = m.compute_live_metrics(
        symbol="BTCUSDT",
        breakpoint=77587.7,
        target_wall={"side": "BID", "price": 77573.8, "zone_lo": 77573.8, "zone_hi": 77573.8},
        baseline_qty=1.922,
        baseline_notional=None,
        triggered_at=None,
        trigger_price=77587.7,
        live_price=77550.0,
    )
    assert out["wall_current_qty"] == 0.0
    assert out["wall_lost"] is True
    assert out["wall_reduce_pct"] == 1.0
    assert out["adapters"]["wall_current"] == "ok"


def test_prefer_ob1000_positive_over_sparse_full_zero(monkeypatch):
    """Sparse full book (depth 0) must not zero a wall that OB1000 still shows."""
    from wall_decision_v1 import metrics as m

    sparse_full = {
        "_requested_depth": 0,
        "data_status": "current",
        "freshness_state": "delayed",
        "freshness_ms": 1000,
        "timestamp_utc": "2026-09-14T10:00:00Z",
        "source": "orderbook_v3_live_full_on_demand",
        "bids": [{"price": 77999.9, "size": 1.0}, {"price": 77816.2, "size": 1.0}],
        "asks": [{"price": 78000.0, "size": 1.0}],
    }
    dense_1000 = {
        "_requested_depth": 1000,
        "data_status": "current",
        "freshness_state": "delayed",
        "freshness_ms": 1000,
        "timestamp_utc": "2026-09-14T10:00:01Z",
        "source": "ob1000_on_demand",
        "bids": [{"price": 77937.2, "size": 6.001}, {"price": 77900.0, "size": 1.0}],
        "asks": [{"price": 78000.0, "size": 1.0}],
    }

    # Candidates are returned depth-1000-first by production loader.
    monkeypatch.setattr(
        m, "_load_live_book_candidates", lambda symbol, **kwargs: [dense_1000, sparse_full]
    )

    out = m.resolve_wall_current_qty(
        symbol="BTCUSDT",
        target_wall={"side": "BID", "price": 77937.2, "zone_lo": 77937.2, "zone_hi": 77937.2},
        tick=0.1,
    )
    assert out["adapter"] == "ok"
    assert out["qty"] == 6.001
    assert out["wall_absent"] is False
    assert out["coverage"]["live_book"]["depth"] == 1000


def test_confirmed_zero_on_covering_ob1000(monkeypatch):
    from wall_decision_v1 import metrics as m

    book = {
        "_requested_depth": 1000,
        "data_status": "current",
        "freshness_state": "current",
        "freshness_ms": 100,
        "timestamp_utc": "2026-09-14T10:00:00Z",
        "source": "ob1000_on_demand",
        "bids": [{"price": 78000.0, "size": 2.0}, {"price": 77900.0, "size": 2.0}],
        "asks": [{"price": 78001.0, "size": 1.0}],
    }
    monkeypatch.setattr(m, "_load_live_book_candidates", lambda symbol, **kwargs: [book])
    out = m.resolve_wall_current_qty(
        symbol="BTCUSDT",
        target_wall={"side": "BID", "price": 77950.0, "zone_lo": 77950.0, "zone_hi": 77950.0},
        tick=0.1,
    )
    assert out["qty"] == 0.0
    assert out["adapter"] == "ok"
    assert out["wall_absent"] is True
