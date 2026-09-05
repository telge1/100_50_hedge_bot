"""Unit tests for EDC M0 Strategy Lab adapter (no ClickHouse)."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from orderbook_analyse.strategy_lab.adapters.edc_m0 import (
    EdcM0MarketDataV2,
    StrategyAdapterError,
    execute_edc_m0_strict_sync_v2,
)
from orderbook_analyse.strategy_lab.compiler_v2 import compile_strategy_v2
from orderbook_analyse.strategy_lab.decoder_v2 import load_strategy_v2_yaml_file
from orderbook_analyse.strategy_lab.models.contracts_v2.enums import (
    ResearchConfirmationPolicyV2,
)
from orderbook_analyse.strategy_lab.models.enums import SideName
from orderbook_analyse.strategy_lab.models.identifiers import StableIdentifier
from orderbook_analyse.strategy_lab.results_v2 import (
    StrategyRunStatusV2,
    TradeExitReasonV2,
)
from orderbook_analyse.strategy_lab.validation.catalogs import production_catalog_bundle_v2

UTC = timezone.utc
REPO = Path(__file__).resolve().parents[2]
EDC_YAML = REPO / "strategies" / "strategy_lab" / "edc_m0_strict_sync_v2.yaml"
HASH_EDC = "4aced6b481d19eadd5505afc535e6fb4976f231fd2894b11f7d79acebc53598f"


@pytest.fixture(scope="module")
def catalogs():
    return production_catalog_bundle_v2()


@pytest.fixture(scope="module")
def edc_spec(catalogs):
    spec = load_strategy_v2_yaml_file(EDC_YAML)
    compile_strategy_v2(spec, catalogs)  # ensure loadable
    return spec


@pytest.fixture(scope="module")
def edc_compiled(edc_spec, catalogs):
    compiled = compile_strategy_v2(edc_spec, catalogs)
    assert compiled.strategy_hash == HASH_EDC
    return compiled


def _empty_frames() -> EdcM0MarketDataV2:
    cols = ["open_time", "open", "high", "low", "close", "volume"]
    empty = pd.DataFrame(columns=cols)
    return EdcM0MarketDataV2(
        candles_1m=empty,
        trades_1m=pd.DataFrame(),
        orderbook_1m=pd.DataFrame(),
        open_interest_1m=pd.DataFrame(),
        liquidations=pd.DataFrame(),
    )


def _candles_1m(start: datetime, n: int, *, price: float = 100.0) -> pd.DataFrame:
    rows = []
    px = price
    for i in range(n):
        rows.append(
            {
                "open_time": start + timedelta(minutes=i),
                "open": px,
                "high": px + 0.5,
                "low": px - 0.5,
                "close": px + 0.01,
                "volume": 1.0,
            }
        )
        px += 0.01
    return pd.DataFrame(rows)


def _market(candles: pd.DataFrame) -> EdcM0MarketDataV2:
    return EdcM0MarketDataV2(
        candles_1m=candles,
        trades_1m=pd.DataFrame(),
        orderbook_1m=pd.DataFrame(),
        open_interest_1m=pd.DataFrame(),
        liquidations=pd.DataFrame(),
    )


def _supportive_candidate(
    *,
    candidate_id: str = "edc:aaaaaaaaaaaaaaaaaaaa",
    direction: str = "BULLISH",
    decision_at: datetime,
    entry_at: datetime,
    entry_price: float = 100.0,
    symbol: str = "XRPUSDT",
) -> dict:
    return {
        "mode_id": "M0_STRICT_SYNC",
        "candidate_id": candidate_id,
        "cross_episode_id": "ep1",
        "symbol": symbol,
        "timeframe": "5m",
        "direction": direction,
        "candidate_at": (decision_at - timedelta(minutes=5)).isoformat(),
        "decision_at": decision_at.isoformat(),
        "entry_at": entry_at.isoformat(),
        "entry_price": entry_price,
        "core_research_verdict": "CORE_RESEARCH_SUPPORTIVE",
    }


@pytest.fixture(autouse=True)
def _load_legacy_engines() -> None:
    """Load legacy symbols once so monkeypatches replace real callables."""
    from orderbook_analyse.strategy_lab.adapters import edc_m0 as m

    m._ensure_legacy()


def test_valid_adapter_run_with_stubbed_detection(
    monkeypatch, edc_spec, edc_compiled, catalogs
) -> None:
    start = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    end = datetime(2026, 8, 2, 0, 0, tzinfo=UTC)
    entry = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    decision = entry
    # Force TP on first bar: high clears +0.75%
    candles = _candles_1m(entry, 500, price=100.0)
    candles.loc[0, "high"] = 101.0
    candles.loc[0, "low"] = 99.9
    market = _market(candles)
    cand = _supportive_candidate(decision_at=decision, entry_at=entry)

    monkeypatch.setattr(
        "orderbook_analyse.strategy_lab.adapters.edc_m0.detect_strict_sync_baseline",
        lambda *a, **k: [{"candidate_id": cand["candidate_id"], "bar_index": 0}],
    )
    monkeypatch.setattr(
        "orderbook_analyse.strategy_lab.adapters.edc_m0.evaluate_candidates_canonical",
        lambda *a, **k: [cand],
    )

    result = execute_edc_m0_strict_sync_v2(
        edc_spec,
        edc_compiled,
        catalogs,
        symbol="XRPUSDT",
        start=start,
        end=end,
        market_data=market,
    )
    assert result.strategy_hash == HASH_EDC
    assert result.status is StrategyRunStatusV2.COMPLETE
    assert result.candidate_count == 1
    assert result.trade_count == 1
    trade = result.trades[0]
    assert trade.side is SideName.LONG
    assert trade.exit_reason is TradeExitReasonV2.TP_EXIT
    assert trade.roundtrip_cost_pct == Decimal("0.11")
    assert trade.costs_usdt == Decimal("1.1")
    assert trade.net_pnl_usdt == Decimal("6.4")
    assert trade.mode_id == StableIdentifier(value="m0_strict_sync")
    assert (
        trade.confirmation_policy
        is ResearchConfirmationPolicyV2.CORE_RESEARCH_SUPPORTIVE
    )
    assert result.roundtrip_cost.value == Decimal("0.11")
    assert result.symbols == ("XRPUSDT",)


def test_repeated_run_identical(monkeypatch, edc_spec, edc_compiled, catalogs) -> None:
    start = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    end = datetime(2026, 8, 2, 0, 0, tzinfo=UTC)
    entry = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    candles = _candles_1m(entry, 500)
    candles.loc[0, "high"] = 101.0
    market = _market(candles)
    cand = _supportive_candidate(decision_at=entry, entry_at=entry)
    monkeypatch.setattr(
        "orderbook_analyse.strategy_lab.adapters.edc_m0.detect_strict_sync_baseline",
        lambda *a, **k: [{"x": 1}],
    )
    monkeypatch.setattr(
        "orderbook_analyse.strategy_lab.adapters.edc_m0.evaluate_candidates_canonical",
        lambda *a, **k: [cand],
    )
    a = execute_edc_m0_strict_sync_v2(
        edc_spec, edc_compiled, catalogs, symbol="XRPUSDT", start=start, end=end, market_data=market
    )
    b = execute_edc_m0_strict_sync_v2(
        edc_spec, edc_compiled, catalogs, symbol="XRPUSDT", start=start, end=end, market_data=market
    )
    assert a == b


def test_inputs_not_mutated(monkeypatch, edc_spec, edc_compiled, catalogs) -> None:
    start = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    end = datetime(2026, 8, 2, 0, 0, tzinfo=UTC)
    entry = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    candles = _candles_1m(entry, 500)
    candles.loc[0, "high"] = 101.0
    market = _market(candles)
    before = candles.copy(deep=True)
    cand = _supportive_candidate(decision_at=entry, entry_at=entry)
    monkeypatch.setattr(
        "orderbook_analyse.strategy_lab.adapters.edc_m0.detect_strict_sync_baseline",
        lambda *a, **k: [{"x": 1}],
    )
    monkeypatch.setattr(
        "orderbook_analyse.strategy_lab.adapters.edc_m0.evaluate_candidates_canonical",
        lambda *a, **k: [cand],
    )
    execute_edc_m0_strict_sync_v2(
        edc_spec, edc_compiled, catalogs, symbol="XRPUSDT", start=start, end=end, market_data=market
    )
    pd.testing.assert_frame_equal(market.candles_1m, before)


def test_reject_wrong_hash(edc_spec, catalogs) -> None:
    compiled = compile_strategy_v2(edc_spec, catalogs)
    bad = replace(compiled, strategy_hash="0" * 64)
    with pytest.raises(StrategyAdapterError, match="strategy_hash"):
        execute_edc_m0_strict_sync_v2(
            edc_spec,
            bad,
            catalogs,
            symbol="XRPUSDT",
            start=datetime(2026, 8, 1, tzinfo=UTC),
            end=datetime(2026, 8, 2, tzinfo=UTC),
            market_data=_empty_frames(),
        )


def test_reject_wrong_canonical_bytes(edc_spec, catalogs) -> None:
    compiled = compile_strategy_v2(edc_spec, catalogs)
    bad = replace(compiled, canonical_bytes=b'{"not":"matching"}')
    # hash still matches old → bytes check fails after recompile compare
    # replace only bytes keeps old hash; recompute will differ on both
    with pytest.raises(StrategyAdapterError):
        execute_edc_m0_strict_sync_v2(
            edc_spec,
            bad,
            catalogs,
            symbol="XRPUSDT",
            start=datetime(2026, 8, 1, tzinfo=UTC),
            end=datetime(2026, 8, 2, tzinfo=UTC),
            market_data=_empty_frames(),
        )


def test_reject_wrong_plugin_id(edc_spec, edc_compiled, catalogs) -> None:
    signal = edc_spec.signal
    bad_plugin = replace(
        signal.plugin, plugin_id=StableIdentifier(value="cluster_sweep")
    )
    bad_signal = replace(signal, plugin=bad_plugin)
    bad_spec = replace(edc_spec, signal=bad_signal)
    with pytest.raises(StrategyAdapterError, match="plugin_id"):
        execute_edc_m0_strict_sync_v2(
            bad_spec,
            edc_compiled,
            catalogs,
            symbol="XRPUSDT",
            start=datetime(2026, 8, 1, tzinfo=UTC),
            end=datetime(2026, 8, 2, tzinfo=UTC),
            market_data=_empty_frames(),
        )


def test_reject_wrong_mode(edc_spec, edc_compiled, catalogs) -> None:
    bad_signal = replace(
        edc_spec.signal, mode_id=StableIdentifier(value="other_mode")
    )
    bad_spec = replace(edc_spec, signal=bad_signal)
    with pytest.raises(StrategyAdapterError, match="mode_id"):
        execute_edc_m0_strict_sync_v2(
            bad_spec,
            edc_compiled,
            catalogs,
            symbol="XRPUSDT",
            start=datetime(2026, 8, 1, tzinfo=UTC),
            end=datetime(2026, 8, 2, tzinfo=UTC),
            market_data=_empty_frames(),
        )


def test_reject_wrong_policy(edc_spec, edc_compiled, catalogs) -> None:
    bad_signal = replace(edc_spec.signal, confirmation_policy=None)
    bad_spec = replace(edc_spec, signal=bad_signal)
    with pytest.raises(StrategyAdapterError, match="confirmation_policy"):
        execute_edc_m0_strict_sync_v2(
            bad_spec,
            edc_compiled,
            catalogs,
            symbol="XRPUSDT",
            start=datetime(2026, 8, 1, tzinfo=UTC),
            end=datetime(2026, 8, 2, tzinfo=UTC),
            market_data=_empty_frames(),
        )


def test_reject_wrong_timeframes(edc_spec, edc_compiled, catalogs) -> None:
    from orderbook_analyse.strategy_lab.models.enums import TimeframeUnit
    from orderbook_analyse.strategy_lab.models.strategy import TimeframeValue, Timeframes

    bad_tf = Timeframes(
        signal=TimeframeValue(value=15, unit=TimeframeUnit.MINUTES),
        execution=edc_spec.timeframes.execution,
    )
    bad_spec = replace(edc_spec, timeframes=bad_tf)
    with pytest.raises(StrategyAdapterError):
        execute_edc_m0_strict_sync_v2(
            bad_spec,
            edc_compiled,
            catalogs,
            symbol="XRPUSDT",
            start=datetime(2026, 8, 1, tzinfo=UTC),
            end=datetime(2026, 8, 2, tzinfo=UTC),
            market_data=_empty_frames(),
        )


def test_reject_missing_ema_binding(edc_spec, edc_compiled, catalogs) -> None:
    features = tuple(f for f in edc_spec.features if f.alias.value != "ema_slow")
    bad_spec = replace(edc_spec, features=features)
    with pytest.raises(StrategyAdapterError):
        execute_edc_m0_strict_sync_v2(
            bad_spec,
            edc_compiled,
            catalogs,
            symbol="XRPUSDT",
            start=datetime(2026, 8, 1, tzinfo=UTC),
            end=datetime(2026, 8, 2, tzinfo=UTC),
            market_data=_empty_frames(),
        )


def test_reject_missing_candle_columns(edc_spec, edc_compiled, catalogs) -> None:
    market = EdcM0MarketDataV2(
        candles_1m=pd.DataFrame({"open_time": [], "open": []}),
        trades_1m=pd.DataFrame(),
        orderbook_1m=pd.DataFrame(),
        open_interest_1m=pd.DataFrame(),
        liquidations=pd.DataFrame(),
    )
    with pytest.raises(StrategyAdapterError, match="candles_1m missing"):
        execute_edc_m0_strict_sync_v2(
            edc_spec,
            edc_compiled,
            catalogs,
            symbol="XRPUSDT",
            start=datetime(2026, 8, 1, tzinfo=UTC),
            end=datetime(2026, 8, 2, tzinfo=UTC),
            market_data=market,
        )


def test_cost_is_spec_011_not_legacy_015(
    monkeypatch, edc_spec, edc_compiled, catalogs
) -> None:
    entry = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    candles = _candles_1m(entry, 500)
    candles.loc[0, "high"] = 101.0
    market = _market(candles)
    cand = _supportive_candidate(decision_at=entry, entry_at=entry)
    monkeypatch.setattr(
        "orderbook_analyse.strategy_lab.adapters.edc_m0.detect_strict_sync_baseline",
        lambda *a, **k: [{"x": 1}],
    )
    monkeypatch.setattr(
        "orderbook_analyse.strategy_lab.adapters.edc_m0.evaluate_candidates_canonical",
        lambda *a, **k: [cand],
    )
    result = execute_edc_m0_strict_sync_v2(
        edc_spec,
        edc_compiled,
        catalogs,
        symbol="XRPUSDT",
        start=datetime(2026, 8, 1, tzinfo=UTC),
        end=datetime(2026, 8, 2, tzinfo=UTC),
        market_data=market,
    )
    trade = result.trades[0]
    assert trade.roundtrip_cost_pct == Decimal("0.11")
    assert trade.roundtrip_cost_pct != Decimal("0.15")
    assert trade.costs_usdt == Decimal("1.1")


def test_bearish_side_and_sl_exit(monkeypatch, edc_spec, edc_compiled, catalogs) -> None:
    entry = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    candles = _candles_1m(entry, 500, price=100.0)
    candles.loc[0, "high"] = 100.2
    candles.loc[0, "low"] = 99.0  # SL for short at +0.5% from 100 → 100.5? short SL is above
    # BEARISH SL: high >= entry * (1 + sl/100) = 100.5
    candles.loc[0, "high"] = 100.6
    candles.loc[0, "low"] = 99.5
    market = _market(candles)
    cand = _supportive_candidate(
        decision_at=entry, entry_at=entry, direction="BEARISH"
    )
    monkeypatch.setattr(
        "orderbook_analyse.strategy_lab.adapters.edc_m0.detect_strict_sync_baseline",
        lambda *a, **k: [{"x": 1}],
    )
    monkeypatch.setattr(
        "orderbook_analyse.strategy_lab.adapters.edc_m0.evaluate_candidates_canonical",
        lambda *a, **k: [cand],
    )
    result = execute_edc_m0_strict_sync_v2(
        edc_spec,
        edc_compiled,
        catalogs,
        symbol="XRPUSDT",
        start=datetime(2026, 8, 1, tzinfo=UTC),
        end=datetime(2026, 8, 2, tzinfo=UTC),
        market_data=market,
    )
    trade = result.trades[0]
    assert trade.side is SideName.SHORT
    assert trade.exit_reason is TradeExitReasonV2.SL_EXIT


def test_notional_bound_to_engine_constant(edc_spec, edc_compiled, catalogs) -> None:
    from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.tpsl_pnl_engine import (
        NOTIONAL_USDT,
    )

    assert edc_spec.execution_assumptions.fixed_notional == Decimal(str(NOTIONAL_USDT))
    assert edc_spec.execution_assumptions.fixed_notional == Decimal("1000")
