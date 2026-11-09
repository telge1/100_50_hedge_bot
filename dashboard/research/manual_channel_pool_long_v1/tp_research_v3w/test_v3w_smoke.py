#!/usr/bin/env python3
"""Smoke tests for V3W control trade reproduction."""

from __future__ import annotations

import csv
import sys
from pathlib import Path

V3W = Path(__file__).resolve().parent
THRESH = 0.05


def test_aug26_tp4_is_loss():
    p = V3W / "v3w_identical_entries_results.csv"
    rows = [r for r in csv.DictReader(p.open()) if r["entry_utc"] == "2026-08-26 15:10:00 UTC" and r["variant"] == "A"]
    tp4 = next(r for r in rows if r["tp_model"] == "TP4_far_d2_max")
    assert abs(float(tp4["gross_pnl_pct"]) + 1.2) < THRESH


def test_aug26_tp1_near_d2():
    rows = list(csv.DictReader((V3W / "v3w_identical_entries_results.csv").open()))
    r = next(
        x
        for x in rows
        if x["entry_utc"] == "2026-08-26 15:10:00 UTC"
        and x["variant"] == "A"
        and x["tp_model"] == "TP1_nearest_d2"
    )
    assert abs(float(r["gross_pnl_pct"]) - 1.8948) < 0.02


if __name__ == "__main__":
    test_aug26_tp4_is_loss()
    test_aug26_tp1_near_d2()
    print("ok")
