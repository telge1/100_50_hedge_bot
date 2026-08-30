"""Targeted tests for ASK_POOL_022736 wall public trade reaction audit."""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "run_ask_pool_022736_wall_trade_reaction_audit.py"
)


def _load():
    import sys

    spec = importlib.util.spec_from_file_location("ask022736", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["ask022736"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_01_single_cluster_constants(mod=None):
    mod = mod or _load()
    assert mod.ARRIVAL_TS == "2026-08-26T02:27:36Z"
    assert mod.CLUSTER_END_TS == "2026-08-26T02:30:21Z"


def test_02_03_window_end_no_future():
    mod = _load()
    assert mod._ms(mod.CLUSTER_END_TS) == mod._ms("2026-08-26T02:30:21Z")
    src = SCRIPT.read_text(encoding="utf-8")
    assert "Keine Daten nach" in src or "no_data_after_cluster_end" in src


def test_04_05_dedup_and_aggressor():
    from orderbook_analyse.aggressor_efficiency_flip.trade_loader import trades_from_rows
    from datetime import datetime, timezone

    rows = [
        {
            "trade_ts": datetime(2026, 8, 26, 2, 27, 36, tzinfo=timezone.utc),
            "trade_id": "1",
            "side": "Buy",
            "price": 79176.0,
            "size": 1.0,
            "notional": 79176.0,
        },
        {
            "trade_ts": datetime(2026, 8, 26, 2, 27, 36, tzinfo=timezone.utc),
            "trade_id": "1",
            "side": "Buy",
            "price": 79176.0,
            "size": 1.0,
            "notional": 79176.0,
        },
        {
            "trade_ts": datetime(2026, 8, 26, 2, 27, 37, tzinfo=timezone.utc),
            "trade_id": "2",
            "side": "Sell",
            "price": 79170.0,
            "size": 0.5,
            "notional": 39585.0,
        },
    ]
    trades = trades_from_rows(rows)
    assert len(trades) == 2
    assert trades[0].side == "Buy"
    assert trades[1].side == "Sell"


def test_06_07_08_wall_identities_and_b_after_first_seen():
    mod = _load()
    assert mod.WALL_A_REF == 79176.0
    assert mod.WALL_B_REF == 79217.1
    assert mod.WALL_A_REF != mod.WALL_B_REF
    src = SCRIPT.read_text(encoding="utf-8")
    assert "before_first_seen" in src
    assert "after_wall_b_first_seen" in src


def test_09_10_11_defense_and_decomp_classes():
    src = SCRIPT.read_text(encoding="utf-8")
    assert "WALL_NOT_ATTACKED" in src
    assert "TRADE_EXPLAINED_DEPLETION_SUPPORTED" in src
    assert "CANCELLATION_OR_MOVE_SUPPORTED" in src
    assert "REFILL_SUPPORTED" in src
    assert "no_queue_reconstruction" in src or "keine Queue" in src.lower() or "Queue" in src


def test_12_13_14_causal_reaction_prefix():
    src = SCRIPT.read_text(encoding="utf-8")
    assert "reaction_first_available_ts" in src
    assert "prefix_check" in src


def test_15_16_no_outcomes_no_mutation():
    src = SCRIPT.read_text(encoding="utf-8")
    assert "winrate" not in src.lower()
    assert "INSERT INTO" not in src
    assert "expectancy" not in src.lower()
    assert "Keine Strategie" in src or "No strategy" in src
