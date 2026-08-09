"""Smoke tests for confluence windows / labels."""

from __future__ import annotations

from orderbook_analyse.fractal_signal_confluence_db.cluster import combo_label, confluence_class, pair_window


def test_pair_windows() -> None:
    assert pair_window("15m", "30m") == 30
    assert pair_window("30m", "1h") == 60
    assert pair_window("1h", "4h") == 240
    assert pair_window("15m", "4h") == 240


def test_labels() -> None:
    assert combo_label(["1h"]) == "1h_only"
    assert combo_label(["4h", "1h"]) == "1h+4h"
    assert confluence_class(1) == "SINGLE"
    assert confluence_class(4) == "QUAD"
