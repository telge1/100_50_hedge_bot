from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from density_bridge_v1 import FixtureDensitySessionV1
from sell_limit_fill_v1 import evaluate_short_sell_limit_fill
from short_research_engine_v1 import run_short_research_backtest
from config_validate_v1 import load_and_validate_config

TOOLS = Path(__file__).resolve().parents[1]
FIXTURE = TOOLS / "fixtures/synthetic_integration_v1.json"
CFG = TOOLS / "configs/synthetic_fixture_v1.json"


def test_legacy_bracket_touch():
    d = evaluate_short_sell_limit_fill(
        fill_model="legacy_v1",
        limit_price=0.1,
        bar_utc="2026-01-01T12:10:00Z",
        order_active_from_utc="2026-01-01T12:10:00Z",
        ohlc={"o": 0.099, "h": 0.1005, "l": 0.098, "c": 0.099},
    )
    assert d.filled and d.fill_price == 0.1
    assert d.rule_id == "LEGACY_V1_BRACKET_TOUCH"


def test_legacy_no_fill_when_marketable_gap():
    d = evaluate_short_sell_limit_fill(
        fill_model="legacy_v1",
        limit_price=0.1,
        bar_utc="2026-01-01T12:10:00Z",
        order_active_from_utc="2026-01-01T12:10:00Z",
        ohlc={"o": 0.102, "h": 0.103, "l": 0.101, "c": 0.102},
    )
    assert not d.filled


def test_causal_marketable_at_open():
    d = evaluate_short_sell_limit_fill(
        fill_model="causal_sell_limit_v2",
        limit_price=0.1,
        bar_utc="2026-01-01T12:10:00Z",
        order_active_from_utc="2026-01-01T12:10:00Z",
        ohlc={"o": 0.102, "h": 0.103, "l": 0.101, "c": 0.102},
    )
    assert d.filled and d.fill_price == 0.102
    assert d.rule_id == "CAUSAL_V2_MARKETABLE_AT_OPEN"


def test_causal_open_exactly_on_limit():
    d = evaluate_short_sell_limit_fill(
        fill_model="causal_sell_limit_v2",
        limit_price=0.1,
        bar_utc="2026-01-01T12:10:00Z",
        order_active_from_utc="2026-01-01T12:10:00Z",
        ohlc={"o": 0.1, "h": 0.1003, "l": 0.099, "c": 0.0992},
    )
    assert d.filled and d.fill_price == 0.1


def test_causal_intrabar_touch():
    d = evaluate_short_sell_limit_fill(
        fill_model="causal_sell_limit_v2",
        limit_price=0.1,
        bar_utc="2026-01-01T12:10:00Z",
        order_active_from_utc="2026-01-01T12:10:00Z",
        ohlc={"o": 0.099, "h": 0.1003, "l": 0.098, "c": 0.0992},
    )
    assert d.filled and d.fill_price == 0.1
    assert d.rule_id == "CAUSAL_V2_LIMIT_TOUCH_INTRABAR"


def test_causal_high_below_limit():
    d = evaluate_short_sell_limit_fill(
        fill_model="causal_sell_limit_v2",
        limit_price=0.1,
        bar_utc="2026-01-01T12:10:00Z",
        order_active_from_utc="2026-01-01T12:10:00Z",
        ohlc={"o": 0.098, "h": 0.099, "l": 0.097, "c": 0.0985},
    )
    assert not d.filled


def test_not_active_before_activation():
    d = evaluate_short_sell_limit_fill(
        fill_model="causal_sell_limit_v2",
        limit_price=0.1,
        bar_utc="2026-01-01T12:05:00Z",
        order_active_from_utc="2026-01-01T12:10:00Z",
        ohlc={"o": 0.102, "h": 0.103, "l": 0.101, "c": 0.102},
    )
    assert not d.filled
    assert d.rule_id == "ORDER_NOT_ACTIVE"


def test_engine_legacy_regression_marketable_no_trade():
    fix = json.loads(FIXTURE.read_text(encoding="utf-8"))
    prices = dict(fix["prices"])
    prices["2026-01-01T12:10:00Z"] = {"o": 0.102, "h": 0.103, "l": 0.101, "c": 0.102}
    cfg = load_and_validate_config(CFG)
    cfg["short_entry"]["pending_limit_fill_model"] = "legacy_v1"
    session = FixtureDensitySessionV1(fix["snapshots"])
    result = run_short_research_backtest(cfg, session, prices)
    assert result["signals"]
    assert not result["trades"]


def test_engine_causal_marketable_fills():
    fix = json.loads(FIXTURE.read_text(encoding="utf-8"))
    prices = dict(fix["prices"])
    prices["2026-01-01T12:10:00Z"] = {"o": 0.102, "h": 0.103, "l": 0.101, "c": 0.102}
    cfg = load_and_validate_config(CFG)
    cfg["short_entry"]["pending_limit_fill_model"] = "causal_sell_limit_v2"
    session = FixtureDensitySessionV1(fix["snapshots"])
    result = run_short_research_backtest(cfg, session, prices)
    assert result["trades"]
    assert any(o.get("status") == "FILLED" for o in result["orders"])


def test_fill_before_pool_cancel_same_bar():
    """Touch on activation bar even if pool absent at density close snapshot."""
    fix = json.loads(FIXTURE.read_text(encoding="utf-8"))
    snaps = copy.deepcopy(fix["snapshots"])
    # At 12:10 pool band removed at close but price still trades through entry 0.0995
    snaps["2026-01-01T12:10:00Z"] = {
        "upper_bands": [
            {"side": "upper", "lower_price": "0.1010", "upper_price": "0.1015", "unique_origin_count": 2, "origin_ids": "e|f"}
        ],
        "lower_bands": snaps["2026-01-01T12:10:00Z"]["lower_bands"],
    }
    prices = dict(fix["prices"])
    prices["2026-01-01T12:10:00Z"] = {"o": 0.102, "h": 0.103, "l": 0.101, "c": 0.102}
    cfg = load_and_validate_config(CFG)
    cfg["short_entry"]["pending_limit_fill_model"] = "causal_sell_limit_v2"
    session = FixtureDensitySessionV1(snaps)
    legacy = run_short_research_backtest(
        {**cfg, "short_entry": {**cfg["short_entry"], "pending_limit_fill_model": "legacy_v1"}},
        session,
        prices,
    )
    causal = run_short_research_backtest(cfg, session, prices)
    assert not legacy["trades"]
    assert causal["trades"]
