"""Test/research helper around planner plan_orders. Production batch does not use this.

The productive path is run_pools_once (single-pass 5m) + plan_from_snapshot.
This wrapper still calls plan_orders (which re-runs the engine) and therefore
must reject 1m frames.
"""

from __future__ import annotations

import sys
from typing import Any

import pandas as pd

from .candles import FutureBarInFrame, ensure_utc
from .config import (
    LOOKBACK,
    REPLAY,
    allow_dirty_planner,
    expected_planner_commit,
    planner_root,
)
from .pin import PinMismatch, inspect_repo, planner_relevant_files
from .pool_snapshot import assert_five_minute_frame


class PlannerPinError(PinMismatch):
    pass


def load_plan_orders():
    root = planner_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from research.liquidity.order_planner import plan_orders  # noqa: WPS433

    return plan_orders


def inspect_planner(*, environ: dict | None = None) -> dict[str, Any]:
    root = planner_root(environ)
    info = inspect_repo(root, planner_relevant_files(root))
    info["expected_commit"] = expected_planner_commit(environ)
    info["lookback"] = LOOKBACK
    info["replay"] = REPLAY
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from research.liquidity import order_planner as op  # noqa: WPS433

    info["constants"] = {
        "LOOKBACK": int(LOOKBACK),
        "TP1_BUFFER": float(op.TP1_BUFFER),
        "TP2_BUFFER": float(op.TP2_BUFFER),
        "SL_BUFFER": float(op.SL_BUFFER),
        "EMPTY_GAP_MAX_PCT": float(op.EMPTY_GAP_MAX_PCT),
        "SL_MAX_ABS_PCT": float(op.SL_MAX_ABS_PCT),
    }
    # DEFAULT_LOOKBACK lives on bigbeluga
    from research.liquidity.bigbeluga_pools import DEFAULT_LOOKBACK

    info["constants"]["DEFAULT_LOOKBACK"] = int(DEFAULT_LOOKBACK)
    return info


def assert_planner_pin(*, environ: dict | None = None) -> dict[str, Any]:
    info = inspect_planner(environ=environ)
    expected = expected_planner_commit(environ)
    actual = str(info.get("commit") or "")
    ok_commit = actual == expected or actual.startswith(expected) or expected.startswith(actual[:7])
    # require full match when expected is full sha
    if len(expected) >= 40:
        ok_commit = actual == expected
    dirty = bool(info.get("dirty"))
    override = allow_dirty_planner(environ)
    if (not ok_commit or dirty) and not override:
        raise PlannerPinError(
            f"planner pin mismatch commit={actual} expected={expected} dirty={dirty}"
        )
    info["pin_ok"] = bool(ok_commit) and (not dirty or override)
    info["pin_override"] = override and (dirty or not ok_commit)
    return info


def call_plan_orders(
    candles: pd.DataFrame,
    *,
    symbol: str,
    entry_time,
    entry_price: float,
    direction: str,
    test_fixture_only: bool = False,
) -> dict[str, Any]:
    if candles is None or candles.empty:
        raise ValueError("empty causal frame")
    et = ensure_utc(entry_time)
    frame = candles.copy()
    if "timestamp" not in frame.columns:
        raise ValueError("causal frame needs timestamp")
    guarded = assert_five_minute_frame(frame, max_close=et)
    plan_orders = load_plan_orders()
    plan = plan_orders(
        guarded[["timestamp", "open", "high", "low", "close", "volume"]],
        timestamp=et,
        entry_price=float(entry_price),
        direction=str(direction).upper(),
        lookback=LOOKBACK,
        replay=REPLAY,
    )
    plan["symbol"] = str(symbol).strip().upper()
    if test_fixture_only:
        plan["pool_candle_source"] = "TEST_FIXTURE_ONLY"
        plan["test_fixture_only"] = True
    return plan
