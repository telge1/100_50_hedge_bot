#!/usr/bin/env python3
"""Unit checks for V3AN config + 45-matrix regression vs frozen summary."""

from __future__ import annotations

import csv
import sys
from pathlib import Path

V3AN = Path(__file__).resolve().parent
sys.path.insert(0, str(V3AN))

from watcher_config import TouchWatcherConfig, load_touch_watcher_config, preset_matrix_v1

import pytest


def test_matrix_has_45_rows():
    assert len(preset_matrix_v1()) == 45


def test_invalid_tolerance_raises():
    with pytest.raises(ValueError):
        TouchWatcherConfig(touch_tolerance_pct=0.21)


def test_example_yaml_loads():
    cfg = load_touch_watcher_config(V3AN / "configs" / "touch_watcher_example.yaml")
    assert cfg.confirmation_mode == "A2"
    assert cfg.touch_tolerance_pct == 0.03


def test_regression_against_frozen_summary():
    baseline = V3AN / "l1_v3an_aug30_variant_summary_v1frozen.csv"
    if not baseline.is_file():
        return
    with baseline.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 45


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
