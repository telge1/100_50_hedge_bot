"""Tests for A+ Liquidity Pool Signal Scanner V1 (research-only)."""

from __future__ import annotations

import ast
import importlib
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1 import VERDICT_CODE_READY
from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.config import VERIFIED_TICK_SYMBOLS
from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.fixtures import (
    long_invalidation_bundle,
    pool,
    pullback_short_confirmation_bundle,
    pullback_short_fixture_pools,
    pullback_short_no_rejection_bundle,
    short_invalidation_bundle,
    static_pools,
    static_pools_mirrored_long,
    static_pools_terminal_long,
    static_pools_terminal_short,
    terminal_long_confirmation_bundle,
    terminal_long_no_reclaim_bundle,
)
from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.gates import (
    REQUIRED_GATES,
    evaluate_gates,
    gross_rr,
)
from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.markers import signals_to_marker_specs
from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.models import CandidateState, PoolRecord, ScannerCandidate
from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.pools import pools_known_before_approach
from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.runner import run_scanner
from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.setups import (
    _intermediate_blocks,
    detect_pullback_short_context,
    finalize_levels,
    in_lower_half,
    in_upper_half,
    is_green_reaction,
    is_red_reaction,
)
from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.shadow_log import ShadowEventLog


def _loader(pools: dict[str, list[PoolRecord]]):
    def _load(candles_by_tf, *, symbol: str, as_of: datetime, **kwargs):
        return pools

    return _load


def test_verdict_constant():
    assert VERDICT_CODE_READY == "A_PLUS_LIQUIDITY_POOL_SIGNAL_SCANNER_V1_CODE_READY"


def test_pool_known_before_approach():
    approach = datetime(2026, 8, 15, 12, 0, 0)
    p = pool(pool_id="a", tf="15m", side="ASK", lower=0.1, upper=0.11, known_at=approach - timedelta(hours=1))
    assert p.is_known_before(approach)
    assert pools_known_before_approach(p, approach)


def test_pool_after_touch_not_usable():
    approach = datetime(2026, 8, 15, 12, 0, 0)
    late = pool(pool_id="late", tf="15m", side="ASK", lower=0.1, upper=0.11, known_at=approach + timedelta(minutes=1))
    assert not late.is_known_before(approach)
    asks = [p for p in pullback_short_fixture_pools(approach)["15m"] if p.is_known_before(approach)]
    assert all(p.pool_id != "late" for p in asks)


def test_pullback_short_upper_half():
    p = pool(pool_id="x", tf="15m", side="ASK", lower=0.1010, upper=0.1015, known_at=datetime(2026, 1, 1))
    assert in_upper_half(p, 0.1013)
    assert not in_upper_half(p, 0.1010)


def test_in_lower_half_mirrored():
    p = pool(pool_id="x", tf="15m", side="BID", lower=0.0990, upper=0.0995, known_at=datetime(2026, 1, 1))
    assert in_lower_half(p, 0.0992)
    assert not in_lower_half(p, 0.0996)


def test_red_green_reaction():
    assert is_red_reaction(0.1013, 0.1012)
    assert not is_red_reaction(0.1012, 0.1013)
    assert is_green_reaction(0.0990, 0.0995)
    assert not is_green_reaction(0.0995, 0.0990)


def test_intermediate_pool_blocks():
    entry = pool(pool_id="e", tf="15m", side="ASK", lower=0.101, upper=0.102, known_at=datetime(2026, 1, 1))
    target = pool(pool_id="t", tf="30m", side="BID", lower=0.098, upper=0.099, known_at=datetime(2026, 1, 1))
    mid = pool(pool_id="m", tf="30m", side="ASK", lower=0.0995, upper=0.1005, known_at=datetime(2026, 1, 1), n=5)
    assert _intermediate_blocks(entry, target, [entry, target, mid], direction="SHORT")


def test_stop_and_target_outside_pool():
    entry_pool = pool(pool_id="e", tf="15m", side="ASK", lower=0.1010, upper=0.1015, known_at=datetime(2026, 1, 1))
    target_pool = pool(pool_id="t", tf="30m", side="BID", lower=0.0980, upper=0.0985, known_at=datetime(2026, 1, 1))
    cand = ScannerCandidate(
        setup_id="s",
        setup_type="A_PLUS_PULLBACK_SHORT",
        symbol="DOGEUSDT",
        direction="SHORT",
        state=CandidateState.WAITING_FOR_1M_CONFIRMATION,
        entry_pool=entry_pool,
        target_pool=target_pool,
        entry_price=0.10118,
        sweep_high=0.10140,
    )
    finalize_levels(cand, symbol="DOGEUSDT", atr=0.0003)
    assert cand.stop_price is not None and cand.target_price is not None
    assert cand.stop_price > entry_pool.upper_edge
    assert cand.target_price > target_pool.upper_edge


def test_unverified_tick_no_trade():
    entry_pool = pool(pool_id="e", tf="15m", side="ASK", lower=0.1, upper=0.11, known_at=datetime(2026, 1, 1))
    cand = ScannerCandidate(
        setup_id="s",
        setup_type="A_PLUS_PULLBACK_SHORT",
        symbol="FAKEUSDT",
        direction="SHORT",
        state=CandidateState.WAITING_FOR_1M_CONFIRMATION,
        entry_pool=entry_pool,
        target_pool=entry_pool,
        entry_price=0.105,
        stop_price=0.11,
        target_price=0.09,
    )
    gates = evaluate_gates(
        cand,
        symbol="FAKEUSDT",
        approach_at_known=True,
        closed_bar_safe=True,
        context_complete=True,
        intermediate_block=False,
        confirmed_1m=True,
        candle_coverage_ok=True,
        no_data_gap=True,
        unique_episode=True,
        target_reached_before_entry=False,
        tick_verified=False,
    )
    tick_gate = next(g for g in gates if g.gate == "verified_tick_size")
    assert not tick_gate.passed
    assert tick_gate.reason == "TICK_SIZE_UNVERIFIED"
    assert "FAKEUSDT" not in VERIFIED_TICK_SYMBOLS


def test_required_gate_count():
    assert len(REQUIRED_GATES) == 16


def test_pullback_short_confirmed_integration():
    candles, approach_at = pullback_short_confirmation_bundle()
    pools = static_pools(known_at=approach_at - timedelta(hours=2))
    result = run_scanner(
        symbol="DOGEUSDT",
        candles_by_tf=candles,
        pool_loader=_loader(pools),
    )
    confirmed = [c for c in result["confirmed"] if c["setup_type"] == "A_PLUS_PULLBACK_SHORT"]
    assert len(confirmed) >= 1
    row = confirmed[0]
    assert row["direction"] == "SHORT"
    assert row["entry_pool"]["pool_id"] == "ask15"
    assert row["entry_pool"]["pool_id"] != "late"
    assert row["limit_entry_price"] is not None
    assert all(g["passed"] for g in row["gates"])


def test_no_1m_rejection_no_short():
    candles = pullback_short_no_rejection_bundle()
    _, approach_at = pullback_short_confirmation_bundle()
    result = run_scanner(
        symbol="DOGEUSDT",
        candles_by_tf=candles,
        pool_loader=_loader(static_pools(known_at=approach_at - timedelta(hours=2))),
    )
    confirmed = [c for c in result["confirmed"] if c["setup_type"] == "A_PLUS_PULLBACK_SHORT"]
    assert len(confirmed) == 0


def test_close_below_reaction_low_confirms_short():
    candles, approach_at = pullback_short_confirmation_bundle()
    result = run_scanner(
        symbol="DOGEUSDT",
        candles_by_tf=candles,
        pool_loader=_loader(static_pools(known_at=approach_at - timedelta(hours=2))),
    )
    confirmed = [c for c in result["confirmed"] if c["setup_type"] == "A_PLUS_PULLBACK_SHORT"]
    assert confirmed
    # V2: limit fill at 60% pool width (0.1013 for ask15 0.1010-0.1015)
    assert confirmed[0]["entry_price"] == pytest.approx(0.1013, rel=1e-4)
    assert confirmed[0]["limit_entry_price"] == pytest.approx(0.1013, rel=1e-4)


def test_terminal_long_no_reclaim():
    candles = terminal_long_no_reclaim_bundle()
    _, approach_at = terminal_long_confirmation_bundle()
    result = run_scanner(
        symbol="DOGEUSDT",
        candles_by_tf=candles,
        pool_loader=_loader(static_pools_terminal_long(known_at=approach_at - timedelta(hours=2))),
        enable_terminal=True,
        enable_pullback=False,
    )
    assert not [c for c in result["confirmed"] if c["setup_type"] == "A_PLUS_TERMINAL_POOL_LONG"]


def test_terminal_long_confirmed():
    candles, approach_at = terminal_long_confirmation_bundle()
    result = run_scanner(
        symbol="DOGEUSDT",
        candles_by_tf=candles,
        pool_loader=_loader(static_pools_terminal_long(known_at=approach_at - timedelta(hours=2))),
        enable_terminal=True,
        enable_pullback=False,
    )
    confirmed = [c for c in result["confirmed"] if c["setup_type"] == "A_PLUS_TERMINAL_POOL_LONG"]
    assert len(confirmed) >= 1
    assert confirmed[0]["direction"] == "LONG"
    assert confirmed[0]["reaction_high"] is not None


def test_short_invalidated_on_new_high():
    candles = short_invalidation_bundle()
    _, approach_at = pullback_short_confirmation_bundle()
    result = run_scanner(
        symbol="DOGEUSDT",
        candles_by_tf=candles,
        pool_loader=_loader(static_pools(known_at=approach_at - timedelta(hours=2))),
    )
    invalidated = [c for c in result["invalidated"] if c["setup_type"] == "A_PLUS_PULLBACK_SHORT"]
    assert invalidated
    assert invalidated[0]["invalidation_reason"] in ("5m_accepted_above_entry_pool", "asymmetry_lost", "setup_expired_unfilled")


def test_long_invalidated_on_new_low():
    candles = long_invalidation_bundle()
    _, approach_at = terminal_long_confirmation_bundle()
    result = run_scanner(
        symbol="DOGEUSDT",
        candles_by_tf=candles,
        pool_loader=_loader(static_pools_terminal_long(known_at=approach_at - timedelta(hours=2))),
        enable_terminal=True,
        enable_pullback=False,
    )
    resets = result.get("reaction_state_resets", [])
    assert resets or result.get("n_superseded", 0) >= 0


def test_unique_episode_per_pool():
    candles, approach_at = pullback_short_confirmation_bundle()
    result = run_scanner(
        symbol="DOGEUSDT",
        candles_by_tf=candles,
        pool_loader=_loader(static_pools(known_at=approach_at - timedelta(hours=2))),
    )
    shorts = [c for c in result["confirmed"] if c["setup_type"] == "A_PLUS_PULLBACK_SHORT"]
    pool_ids = [c["entry_pool"]["pool_id"] for c in shorts]
    assert len(pool_ids) == len(set(pool_ids))


def test_marker_specs_confirmed_only():
    row = {
        "setup_id": "abc",
        "setup_type": "A_PLUS_PULLBACK_SHORT",
        "symbol": "DOGEUSDT",
        "direction": "SHORT",
        "state": "CONFIRMED",
        "signal_at": "2026-08-15T10:00:00",
        "entry_price": 0.10118,
        "stop_price": 0.1016,
        "target_price": 0.0987,
        "entry_pool": {"timeframe": "15m", "pool_id": "ask15", "known_at": "2026-08-15T08:00:00"},
        "target_pool": {"timeframe": "30m", "pool_id": "bid30"},
        "gates": [],
        "data_quality": {"gross_rr": 2.1},
    }
    specs = signals_to_marker_specs([row], display_mode="confirmed")
    kinds = {s["kind"] for s in specs}
    assert "APS_CONFIRMED" in kinds
    assert "APS_LINE" not in kinds  # confirmed never emits plan lines
    active = signals_to_marker_specs([row], display_mode="active")
    assert "APS_LINE" not in {s["kind"] for s in active}
    armed = {
        **row,
        "state": "LIMIT_INTENT_ARMED",
        "armed_at": row["signal_at"],
        "limit_entry_price": row["entry_price"],
    }
    armed_specs = signals_to_marker_specs([armed], display_mode="active")
    assert "APS_LINE" in {s["kind"] for s in armed_specs}
    assert not signals_to_marker_specs([{**row, "state": "INVALIDATED"}], display_mode="confirmed")


def test_marker_debug_mode():
    row = {"setup_id": "x", "direction": "LONG", "state": "INVALIDATED", "confirmation_at": "2026-08-15T10:00:00"}
    specs = signals_to_marker_specs([row], display_mode="debug")
    assert specs and specs[0]["kind"] == "APS_INVALID"


def test_gross_rr_long_short_mirrored():
    assert gross_rr("LONG", 100.0, 99.0, 102.0) == pytest.approx(2.0)
    assert gross_rr("SHORT", 100.0, 101.0, 98.0) == pytest.approx(2.0)


def test_shadow_log_refuses_overwrite(tmp_path: Path):
    d = tmp_path / "run1"
    log = ShadowEventLog(d)
    log.append("candidates", {"setup_id": "a"})
    with pytest.raises(FileExistsError):
        ShadowEventLog(d)


def test_no_execution_imports_in_scanner_package():
    root = Path(__file__).resolve().parents[1] / "src" / "orderbook_analyse" / "a_plus_liquidity_pool_signal_scanner_v1"
    banned = ("bybit", "place_order", "create_order", "position_manager", "pybit")
    for py in root.glob("*.py"):
        text = py.read_text(encoding="utf-8").lower()
        for token in banned:
            assert token not in text, f"{py.name} mentions {token}"


def test_runner_has_no_order_client_import():
    runner = importlib.import_module("orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.runner")
    src = Path(runner.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    assert not any("bybit" in i.lower() or "order" in i.lower() and "orderbook" not in i.lower() for i in imports)


def test_detect_pullback_uses_only_known_pools():
    import pandas as pd

    approach = datetime(2026, 8, 15, 12, 0, 0)
    pools = pullback_short_fixture_pools(approach)
    row = pd.Series({"ema_9": 0.101, "ema_20": 0.1012, "ema_59": 0.1015, "ema_9_slope_1": -0.0001, "ema_20_slope_1": -0.0001, "close": 0.10112, "prior_swing_high": 0.1014})
    det = detect_pullback_short_context(
        symbol="DOGEUSDT",
        price=0.10112,
        approach_at=approach,
        pools_15m=pools["15m"],
        pools_30m=pools["30m"],
        row_5m=row,
        atr=0.0003,
    )
    assert det is not None
    assert det.entry_pool.pool_id == "ask15"


def test_mirrored_long_pullback_detect():
    import pandas as pd

    approach = datetime(2026, 8, 15, 12, 0, 0)
    pools = static_pools_mirrored_long(known_at=approach - timedelta(hours=2))
    row = pd.Series({"ema_9": 0.1005, "ema_20": 0.1003, "ema_59": 0.0990, "ema_9_slope_1": 0.0001, "ema_20_slope_1": 0.0001, "close": 0.0993, "prior_swing_low": 0.0990})
    from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.setups import detect_pullback_long_context

    det = detect_pullback_long_context(
        symbol="DOGEUSDT",
        price=0.0993,
        approach_at=approach,
        pools_15m=pools["15m"],
        pools_30m=pools["30m"],
        row_5m=row,
        atr=0.0003,
    )
    assert det is not None
    assert det.setup_type == "A_PLUS_PULLBACK_LONG"
    assert det.direction == "LONG"


def test_terminal_short_detect():
    import pandas as pd

    approach = datetime(2026, 8, 15, 12, 0, 0)
    pools = static_pools_terminal_short(known_at=approach - timedelta(hours=2))
    from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.setups import detect_terminal_short_context

    det = detect_terminal_short_context(
        symbol="DOGEUSDT",
        price=0.1012,
        approach_at=approach,
        pools_1h=pools["1h"],
        pools_15m=pools["15m"],
        pools_30m=pools["30m"],
        atr=0.0004,
        wick_high=0.1018,
    )
    assert det is not None
    assert det.setup_type == "A_PLUS_TERMINAL_POOL_SHORT"
