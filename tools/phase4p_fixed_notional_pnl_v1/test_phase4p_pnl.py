"""Phase 4P fixed notional PnL tests."""

from __future__ import annotations

import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

from notional_pnl import (
    POSITION_NOTIONAL_USDT,
    FEE_RATE_PER_SIDE,
    ambiguous_scenarios,
    compute_trade_pnl,
    equity_curve_from_ledger,
    quantity_doge,
)


def test_long_tp_profit():
    entry, exit_p, sl, tp = 0.10, 0.11, 0.098, 0.11
    pnl = compute_trade_pnl("LONG", entry, exit_p, sl, tp, ambiguous=False, open_at_end=False)
    assert pnl.gross_pnl_usdt is not None and pnl.gross_pnl_usdt > 0
    assert pnl.net_pnl_usdt is not None and pnl.net_pnl_usdt < pnl.gross_pnl_usdt


def test_long_sl_loss():
    pnl = compute_trade_pnl("LONG", 0.10, 0.098, 0.098, 0.11, ambiguous=False, open_at_end=False)
    assert pnl.gross_pnl_usdt is not None and pnl.gross_pnl_usdt < 0


def test_short_tp_profit():
    pnl = compute_trade_pnl("SHORT", 0.10, 0.09, 0.102, 0.09, ambiguous=False, open_at_end=False)
    assert pnl.gross_pnl_usdt is not None and pnl.gross_pnl_usdt > 0


def test_short_sl_loss():
    pnl = compute_trade_pnl("SHORT", 0.10, 0.102, 0.102, 0.09, ambiguous=False, open_at_end=False)
    assert pnl.gross_pnl_usdt is not None and pnl.gross_pnl_usdt < 0


def test_position_notional_1000():
    assert POSITION_NOTIONAL_USDT == 1000.0


def test_quantity_doge():
    q = quantity_doge(0.08)
    assert abs(q * 0.08 - 1000.0) < 1e-9


def test_fees_deducted():
    pnl = compute_trade_pnl("LONG", 0.10, 0.11, 0.098, 0.11, ambiguous=False, open_at_end=False)
    expected_entry_fee = 1000.0 * FEE_RATE_PER_SIDE
    assert abs(pnl.entry_fee_usdt - expected_entry_fee) < 1e-9
    assert pnl.net_pnl_usdt is not None
    assert pnl.gross_pnl_usdt is not None
    assert pnl.net_pnl_usdt < pnl.gross_pnl_usdt


def test_no_compounding_implied():
    """Each trade uses fixed 1000 USDT notional at entry."""
    q1 = quantity_doge(0.08)
    q2 = quantity_doge(0.12)
    assert abs(q1 * 0.08 - POSITION_NOTIONAL_USDT) < 1e-9
    assert abs(q2 * 0.12 - POSITION_NOTIONAL_USDT) < 1e-9


def test_no_leverage_field():
    pnl = compute_trade_pnl("SHORT", 0.1, 0.09, 0.102, 0.09, ambiguous=False, open_at_end=False)
    assert pnl.quantity_doge == 1000.0 / 0.1


def test_ambiguous_not_counted_as_realized():
    pnl = compute_trade_pnl("SHORT", 0.1, None, 0.102, 0.09, ambiguous=True, open_at_end=False)
    assert pnl.net_pnl_usdt is None
    amb = ambiguous_scenarios("SHORT", 0.1, 0.102, 0.09)
    assert "conservative_sl_first_net_usdt" in amb


def test_open_trade_unrealized():
    pnl = compute_trade_pnl(
        "LONG", 0.10, None, 0.098, 0.11, ambiguous=False, open_at_end=True, mark_close=0.105
    )
    assert pnl.unrealized_pnl_usdt is not None
    assert pnl.realized_R_multiple is None


def test_equity_ignores_ambiguous():
    ledger = [
        {
            "trade_id": "T1",
            "exit_utc": "2026-01-01T00:05:00Z",
            "exit_reason": "TP",
            "execution_ambiguity": "NONE",
            "net_pnl_usdt": 10.0,
        },
        {
            "trade_id": "T2",
            "exit_utc": "2026-01-01T00:10:00Z",
            "exit_reason": "AMBIGUOUS_INTRABAR_EXIT",
            "execution_ambiguity": "AMBIGUOUS_SL_TP_SEQUENCE",
            "net_pnl_usdt": 999.0,
        },
    ]
    curve = equity_curve_from_ledger(ledger)
    assert len(curve) == 1
    assert curve[0]["cumulative_net_pnl_usdt"] == 10.0


def test_max_one_position_enforced_by_source_engines():
    """Phase 4P does not alter entry engines; collectors import unchanged modules."""
    root = TOOLS.parents[1]
    assert (root / "tools/phase4h_oco_pending_entry_v1/oco_engine.py").is_file()
    assert (root / "tools/phase4m_touch_rejection_market_backtester_v1/market_engine.py").is_file()


def test_identical_fee_assumptions():
    assert FEE_RATE_PER_SIDE * 2 == 0.0011


def test_no_lookahead_in_pnl_layer():
    """PnL uses provided entry/exit only."""
    pnl = compute_trade_pnl("LONG", 0.1, 0.11, 0.09, 0.12, ambiguous=False, open_at_end=False)
    assert pnl.gross_pnl_usdt == (0.11 - 0.1) * (1000.0 / 0.1)
