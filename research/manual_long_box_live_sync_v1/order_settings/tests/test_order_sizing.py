"""Fixed 10 USDT sizing — no exchange writes."""

from __future__ import annotations

from decimal import Decimal

import pytest

from research.manual_long_box_live_sync_v1.order_settings.order_sizing import (
    TEST_ORDER_NOTIONAL_USDT,
    amend_notional_ok,
    compute_fixed_notional_qty,
    floor_qty_to_step,
)


@pytest.mark.parametrize(
    "entry,qty_step,min_qty,expected_qty,expected_notional",
    [
        (0.06731, Decimal("1"), Decimal("1"), Decimal("148"), Decimal("9.96188")),
        (0.1, Decimal("1"), Decimal("1"), Decimal("100"), Decimal("10.0")),
    ],
)
def test_compute_fixed_notional_qty(entry, qty_step, min_qty, expected_qty, expected_notional):
    qty, meta = compute_fixed_notional_qty(
        entry,
        qty_step=qty_step,
        min_qty=min_qty,
        notional_cap=TEST_ORDER_NOTIONAL_USDT,
    )
    assert qty == expected_qty
    assert Decimal(meta["notional_usdt"]) == expected_notional
    assert qty * Decimal(str(entry)) <= TEST_ORDER_NOTIONAL_USDT


def test_floor_never_rounds_up():
    raw = Decimal("148.7")
    assert floor_qty_to_step(raw, Decimal("1")) == Decimal("148")


def test_below_min_qty_blocked():
    entry = Decimal("0.50")
    qty, meta = compute_fixed_notional_qty(
        float(entry),
        qty_step=Decimal("1"),
        min_qty=Decimal("25"),
        notional_cap=TEST_ORDER_NOTIONAL_USDT,
    )
    assert qty is None
    assert meta.get("error") == "below_min_qty"


def test_amend_cap_blocks_higher_notional():
    ok, err = amend_notional_ok(148, 0.068)
    assert not ok
    assert err and "amend_notional_exceeds_cap" in err


def test_amend_cap_allows_lower_entry():
    ok, err = amend_notional_ok(148, 0.067)
    assert ok
    assert err is None
