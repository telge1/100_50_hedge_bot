"""Bucket policy formula + INJUSDT pilot lock."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

DASHBOARD_DIR = Path(__file__).resolve().parents[2]
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))

from footprint_candles.bucket_policy import (  # noqa: E402
    PILOT_STEPS,
    bucket_step_from_price,
    bucket_step_from_range,
    derive_bucket_step,
    display_steps_for_raw,
    nice_multiple,
    resolve_bucket_step,
)
from footprint_candles.contracts import bucket_bounds, bucket_index_for_price  # noqa: E402
from footprint_candles.service import validate_request  # noqa: E402


def test_nice_multiple_grid():
    assert nice_multiple(Decimal("0.0004"), Decimal("0.001")) == Decimal("0.001")
    assert nice_multiple(Decimal("0.00105"), Decimal("0.001")) == Decimal("0.002")
    assert nice_multiple(Decimal("3"), Decimal("0.1")) == Decimal("5.0")
    assert nice_multiple(Decimal("5"), Decimal("0.1")) == Decimal("5.0")


def test_inj_formula_matches_pilot_lock():
    tick = Decimal("0.001")
    # Live sample ~6.2 mid / ~0.021 median 5m range
    price_step = bucket_step_from_price(Decimal("6.2"), tick)
    range_step = bucket_step_from_range(Decimal("0.021"), tick)
    combined = derive_bucket_step(price=Decimal("6.2"), median_5m_range=Decimal("0.021"), tick=tick)
    assert price_step == Decimal("0.001")
    # 0.021/20 = 0.00105 → nice up to 0.002; max(0.001, 0.002)=0.002
    assert range_step == Decimal("0.002")
    assert combined == Decimal("0.002")
    # Pilot lock prefers tick-aligned 0.001 (denser) after visual tuning intent:
    # we intentionally lock INJ to 0.001 (= price-relative result, 1 tick).
    assert resolve_bucket_step("INJUSDT") == Decimal("0.001")
    assert PILOT_STEPS["INJUSDT"] == Decimal("0.001")


def test_btc_formula_stays_at_five():
    assert bucket_step_from_price(Decimal("77000"), Decimal("0.1")) == Decimal("5")
    assert resolve_bucket_step("BTCUSDT") == Decimal("5")
    assert display_steps_for_raw(5) == [5.0, 10.0, 15.0, 20.0, 25.0, 50.0]


def test_inj_bucket_math():
    step = Decimal("0.001")
    assert bucket_index_for_price("6.204", step) == 6204
    lo, hi = bucket_bounds(6204, step)
    assert lo == 6.204
    assert abs(hi - 6.205) < 1e-12


def test_validate_allows_inj_pilot_step():
    req = validate_request(
        symbol="INJUSDT",
        timeframe="5m",
        mode="DISPLAY",
        bucket_step=0.001,
        start=1_000,
        end=2_000,
    )
    assert req["symbol"] == "INJUSDT"
    assert req["bucket_step"] == 0.001
