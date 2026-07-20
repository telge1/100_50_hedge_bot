"""Tests for C3.5D exit-levels Pine generator."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pandas as pd

from research.regime_scanner.pullback_entry_c3_5d_exit_levels_pine import (
    build_exit_levels_pine,
)
from research.regime_scanner.trend_pine_export import validate_pine_script

C35_PATH = Path("research/regime_scanner/pullback_entry_c3_5.py")
C34B_PATH = Path("research/regime_scanner/market_structure_c3_4b.py")
C35_HASH = "d61714ffb980013ac241c2053a6258f0a58957cec57bbbd56a7ad512a207e268"
C34B_HASH = "083c58d6b10d4432bf95aafb49bb7a69985b44ca5174946ffe9c5e3cbf68f210"


def test_parent_hashes_unchanged() -> None:
    assert hashlib.sha256(C35_PATH.read_bytes()).hexdigest() == C35_HASH
    assert hashlib.sha256(C34B_PATH.read_bytes()).hexdigest() == C34B_HASH


def test_build_exit_levels_pine_valid_and_separates_reclaim() -> None:
    df = pd.DataFrame(
        [
            {
                "setup_id": 1,
                "direction": "long",
                "side": 1,
                "fill_timestamp": "2026-02-01 12:00:00+00:00",
                "trigger_timestamp": "2026-02-01 11:45:00+00:00",
                "entry_price": 100.0,
                "frozen_breakout_level": 101.0,
                "frozen_pullback_high": 102.0,
                "frozen_pullback_low": 98.0,
                "setup_protected_level": 97.0,
                "entry_protected_level": 95.0,
                "signed_return_pct_at_break": -2.5,
                "protected_break_timestamp": "2026-02-01 15:00:00+00:00",
                "h24__add_adverse_pct": -0.8,
                "full__add_adverse_pct": -3.0,
                "h24__prot_wick_retest": True,
                "h24__prot_close_reclaim": True,
                "h24__bars_to_prot_wick_retest": 1,
                "h24__bars_to_prot_close_reclaim": 2,
                "h24__entry_close_recovery": False,
                "h48__entry_close_recovery": False,
                "h96__entry_close_recovery": True,
                "full__entry_close_recovery": True,
                "full__bars_to_prot_close_reclaim": 2,
                "full__prot_wick_retest": True,
                "full__prot_close_reclaim": True,
                "full__bars_to_prot_wick_retest": 1,
                "post24__prot_close_reclaim": True,
                "post24__bars_to_prot_close_reclaim": 2,
                "post24__entry_close_recovery": False,
            }
        ]
    )
    pine = build_exit_levels_pine(df)
    validate_pine_script(pine)
    assert "≠ entry recovery" in pine or "NOT entry recovery" in pine
    assert "entryRecH24" in pine
    assert "entryRecFull" in pine
    assert "PROT close reclaim" in pine
    assert "ENTRY recovery flags" in pine
    assert "Protected line keeps running after break" in pine
    assert "showEntryMarkers" in pine
    assert "LONG ENTRY" in pine
    assert "SHORT ENTRY" in pine
    assert "entryMarkers" in pine
    assert "Exact fill-bar entry marker" in pine
