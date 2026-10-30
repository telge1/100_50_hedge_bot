"""Phase 2M ledger reconstruction tests."""

from __future__ import annotations

import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG))

from long_phase2m_ledger_integrity_v1 import reconstruct_trade_lifecycle  # noqa: E402


def test_reconstruct_resolves_replace_then_fill_chain():
    ledger = [
        {
            "phase": "entry",
            "action": "PLACE",
            "bar_utc": "2026-09-17T15:30:00Z",
            "limit": 0.08113,
            "sl": 0.07988,
            "order_id": "old-order",
        },
        {
            "phase": "entry",
            "action": "REPLACE",
            "bar_utc": "2026-09-17T16:15:00Z",
            "new_limit": 0.08168,
            "new_sl": 0.08064,
        },
        {
            "phase": "entry",
            "action": "FILL",
            "bar_utc": "2026-09-17T16:30:00Z",
            "entry": 0.08168,
            "sl": 0.08064,
            "order_id": "new-order",
        },
        {
            "phase": "exit",
            "action": "TP_LIMIT_FILL",
            "bar_utc": "2026-09-17T17:00:00Z",
            "detail": {"price": 0.082106},
        },
    ]
    r = reconstruct_trade_lifecycle(
        ledger=ledger,
        exit_ledger=ledger,
        fill_utc="2026-09-17T16:30:00Z",
        entry_price=0.08168,
        sl_price=0.08064,
    )
    assert r["status"] == "RESOLVED"
    assert r["initial_place"]["order_id"] == "old-order"
    assert r["effective_place"]["kind"] == "REPLACE_EFFECTIVE_PLACE"
    assert r["parent_child"]["replaced_without_explicit_cancel"] is True
