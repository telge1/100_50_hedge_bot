"""Offline unit tests for EDC multicoin runner (P2D2; no ClickHouse)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from orderbook_analyse.strategy_lab import edc_multicoin_v2 as mc
from orderbook_analyse.strategy_lab.adapters.edc_io import StrategyMarketDataError
from orderbook_analyse.strategy_lab.adapters.edc_m0 import (
    EdcM0MarketDataV2,
    StrategyAdapterError,
)
from orderbook_analyse.strategy_lab.catalogs.v2.models import CATALOG_CONTRACT_VERSION
from orderbook_analyse.strategy_lab.compiler_v2 import compile_strategy_v2
from orderbook_analyse.strategy_lab.decoder_v2 import load_strategy_v2_yaml_file
from orderbook_analyse.strategy_lab.edc_multicoin_v2 import (
    EdcMulticoinRunV2,
    StrategyMulticoinError,
    SymbolRunFailureV2,
    run_edc_m0_multicoin_v2,
)
from orderbook_analyse.strategy_lab.models.contracts_v2.enums import (
    ResearchConfirmationPolicyV2,
)
from orderbook_analyse.strategy_lab.models.enums import ModelingStatus, SideName
from orderbook_analyse.strategy_lab.models.identifiers import ContractVersion, StableIdentifier
from orderbook_analyse.strategy_lab.results_v2 import (
    SourceEventIdV2,
    StrategyRunResultV2,
    StrategyRunStatusV2,
    StrategyTradeV2,
    TradeExitReasonV2,
)
from orderbook_analyse.strategy_lab.validation.catalogs import production_catalog_bundle_v2

UTC = timezone.utc
REPO = Path(__file__).resolve().parents[2]
EDC_YAML = REPO / "strategies" / "strategy_lab" / "edc_m0_strict_sync_v2.yaml"
UNIVERSE = REPO / "config" / "universe_tradeable_51.json"
HASH_EDC = "4aced6b481d19eadd5505afc535e6fb4976f231fd2894b11f7d79acebc53598f"
START = datetime(2026, 7, 24, tzinfo=UTC)
END = datetime(2026, 8, 23, tzinfo=UTC)


class _FakeClient:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def query(self, sql: str, parameters=None, settings=None):
        self.queries.append(sql)

        class _R:
            @property
            def result_rows(self):
                return []

        return _R()


@pytest.fixture(scope="module")
def catalogs():
    return production_catalog_bundle_v2()


@pytest.fixture(scope="module")
def edc_spec(catalogs):
    return load_strategy_v2_yaml_file(EDC_YAML)


@pytest.fixture(scope="module")
def edc_compiled(edc_spec, catalogs):
    compiled = compile_strategy_v2(edc_spec, catalogs)
    assert compiled.strategy_hash == HASH_EDC
    return compiled


def _empty_market() -> EdcM0MarketDataV2:
    import pandas as pd

    cols = ["open_time", "open", "high", "low", "close", "volume"]
    empty = pd.DataFrame(columns=cols)
    return EdcM0MarketDataV2(
        candles_1m=empty,
        trades_1m=pd.DataFrame(),
        orderbook_1m=pd.DataFrame(),
        open_interest_1m=pd.DataFrame(),
        liquidations=pd.DataFrame(),
    )


def _run_for_symbol(spec, compiled, symbol: str, *, n_trades: int = 1) -> StrategyRunResultV2:
    trades = []
    for i in range(n_trades):
        trades.append(
            StrategyTradeV2(
                source_event_id=SourceEventIdV2(
                    value=f"edc:{symbol.lower()[:8]}{i:012d}"
                ),
                symbol=symbol,
                side=SideName.LONG,
                decision_time=datetime(2026, 7, 25, 1, 0, tzinfo=UTC),
                entry_time=datetime(2026, 7, 25, 1, 0, tzinfo=UTC),
                entry_price=Decimal("1"),
                exit_time=datetime(2026, 7, 25, 2, 0, tzinfo=UTC),
                exit_price=Decimal("1.0075"),
                exit_reason=TradeExitReasonV2.TP_EXIT,
                gross_return_pct=Decimal("0.75"),
                roundtrip_cost_pct=Decimal("0.11"),
                net_return_pct=Decimal("0.64"),
                gross_pnl_usdt=Decimal("7.5"),
                costs_usdt=Decimal("1.1"),
                net_pnl_usdt=Decimal("6.4"),
                mode_id=StableIdentifier(value="m0_strict_sync"),
                confirmation_policy=ResearchConfirmationPolicyV2.CORE_RESEARCH_SUPPORTIVE,
            )
        )
    return StrategyRunResultV2(
        strategy_hash=compiled.strategy_hash,
        plugin_id=StableIdentifier(value="edc_m0_strict_sync"),
        plugin_contract_version=ContractVersion(value=CATALOG_CONTRACT_VERSION),
        universe=spec.universe,
        start=START,
        end=END,
        symbols=(symbol,),
        signal_timeframe=spec.timeframes.signal,
        execution_timeframe=spec.timeframes.execution,
        roundtrip_cost=spec.costs.roundtrip_cost,
        slippage_status=spec.costs.slippage,
        funding_status=spec.costs.funding,
        status=StrategyRunStatusV2.COMPLETE,
        candidate_count=n_trades,
        trades=tuple(trades),
    )


def _patch_success(monkeypatch, spec, compiled, *, fail_symbols: set[str] | None = None):
    fail_symbols = fail_symbols or set()
    loads: list[str] = []

    def fake_load(s, c, *, client, symbol, start, end):
        loads.append(symbol)
        if symbol in fail_symbols:
            raise StrategyMarketDataError(f"load failed for {symbol}")
        return _empty_market()

    def fake_exec(s, c, cats, *, symbol, start, end, market_data):
        if symbol in fail_symbols:
            raise StrategyAdapterError(f"exec failed for {symbol}")
        return _run_for_symbol(s, c, symbol)

    monkeypatch.setattr(mc, "load_edc_m0_market_data_v2", fake_load)
    monkeypatch.setattr(mc, "execute_edc_m0_strict_sync_v2", fake_exec)
    return loads


def test_two_symbols_success(monkeypatch, edc_spec, edc_compiled, catalogs, tmp_path):
    loads = _patch_success(monkeypatch, edc_spec, edc_compiled)
    out = run_edc_m0_multicoin_v2(
        edc_spec,
        edc_compiled,
        catalogs,
        client=_FakeClient(),
        universe_path=UNIVERSE,
        start=START,
        end=END,
        checkpoint_dir=tmp_path,
        symbols=("XRPUSDT", "LITUSDT"),
    )
    assert isinstance(out, EdcMulticoinRunV2)
    assert out.requested_symbols == ("XRPUSDT", "LITUSDT")  # universe file order
    assert out.completed_symbols == ("XRPUSDT", "LITUSDT")
    assert out.failed_symbols == ()
    assert out.trade_count == 2
    assert out.net_pnl_usdt == Decimal("12.8")
    assert loads == ["XRPUSDT", "LITUSDT"]
    assert (tmp_path / "symbols" / "LITUSDT.json").is_file()
    assert (tmp_path / "symbols" / "XRPUSDT.json").is_file()


def test_one_success_one_failure(monkeypatch, edc_spec, edc_compiled, catalogs, tmp_path):
    _patch_success(monkeypatch, edc_spec, edc_compiled, fail_symbols={"NEARUSDT"})
    out = run_edc_m0_multicoin_v2(
        edc_spec,
        edc_compiled,
        catalogs,
        client=_FakeClient(),
        universe_path=UNIVERSE,
        start=START,
        end=END,
        checkpoint_dir=tmp_path,
        symbols=("XRPUSDT", "NEARUSDT"),
    )
    assert out.completed_symbols == ("XRPUSDT",)
    assert out.failed_symbols == ("NEARUSDT",)
    assert out.failures[0].error_type == "StrategyMarketDataError"
    assert "0x" not in out.failures[0].message


def test_unknown_symbol_rejected(edc_spec, edc_compiled, catalogs, tmp_path):
    with pytest.raises(StrategyMulticoinError, match="not in the Strategy universe"):
        run_edc_m0_multicoin_v2(
            edc_spec,
            edc_compiled,
            catalogs,
            client=_FakeClient(),
            universe_path=UNIVERSE,
            start=START,
            end=END,
            checkpoint_dir=tmp_path,
            symbols=("NOTACOINUSDT",),
        )


def test_duplicate_symbols_rejected(edc_spec, edc_compiled, catalogs, tmp_path):
    with pytest.raises(StrategyMulticoinError, match="duplicate symbol"):
        run_edc_m0_multicoin_v2(
            edc_spec,
            edc_compiled,
            catalogs,
            client=_FakeClient(),
            universe_path=UNIVERSE,
            start=START,
            end=END,
            checkpoint_dir=tmp_path,
            symbols=("XRPUSDT", "XRPUSDT"),
        )


def test_empty_subset_rejected(edc_spec, edc_compiled, catalogs, tmp_path):
    with pytest.raises(StrategyMulticoinError, match="non-empty"):
        run_edc_m0_multicoin_v2(
            edc_spec,
            edc_compiled,
            catalogs,
            client=_FakeClient(),
            universe_path=UNIVERSE,
            start=START,
            end=END,
            checkpoint_dir=tmp_path,
            symbols=(),
        )


def test_wrong_universe_hash_rejected(
    edc_spec, edc_compiled, catalogs, tmp_path
):
    bad = tmp_path / "bad_universe.json"
    bad.write_text(
        json.dumps({"n": 51, "symbols": [f"S{i}USDT" for i in range(51)]}),
        encoding="utf-8",
    )
    with pytest.raises(StrategyMulticoinError, match="content_hash"):
        run_edc_m0_multicoin_v2(
            edc_spec,
            edc_compiled,
            catalogs,
            client=_FakeClient(),
            universe_path=bad,
            start=START,
            end=END,
            checkpoint_dir=tmp_path / "ck",
            symbols=("S0USDT",),
        )


def test_wrong_compiled_hash_rejected(edc_spec, catalogs, tmp_path):
    compiled = compile_strategy_v2(edc_spec, catalogs)
    bad = type(compiled)(
        strategy_hash="0" * 64,
        canonical_bytes=compiled.canonical_bytes,
    )
    with pytest.raises(StrategyMulticoinError, match="strategy_hash"):
        run_edc_m0_multicoin_v2(
            edc_spec,
            bad,
            catalogs,
            client=_FakeClient(),
            universe_path=UNIVERSE,
            start=START,
            end=END,
            checkpoint_dir=tmp_path,
            symbols=("XRPUSDT",),
        )


def test_invalid_window_rejected(edc_spec, edc_compiled, catalogs, tmp_path):
    with pytest.raises(StrategyMulticoinError, match="end must be"):
        run_edc_m0_multicoin_v2(
            edc_spec,
            edc_compiled,
            catalogs,
            client=_FakeClient(),
            universe_path=UNIVERSE,
            start=END,
            end=START,
            checkpoint_dir=tmp_path,
            symbols=("XRPUSDT",),
        )


def test_checkpoint_atomic_and_decimal_strings(
    monkeypatch, edc_spec, edc_compiled, catalogs, tmp_path
):
    _patch_success(monkeypatch, edc_spec, edc_compiled)
    run_edc_m0_multicoin_v2(
        edc_spec,
        edc_compiled,
        catalogs,
        client=_FakeClient(),
        universe_path=UNIVERSE,
        start=START,
        end=END,
        checkpoint_dir=tmp_path,
        symbols=("XRPUSDT",),
    )
    raw = (tmp_path / "symbols" / "XRPUSDT.json").read_text(encoding="utf-8")
    data = json.loads(raw)
    assert data["status"] == "complete"
    trade = data["result"]["trades"][0]
    assert trade["entry_price"] == "1"
    assert trade["net_pnl_usdt"] == "6.4"
    assert isinstance(trade["entry_price"], str)
    assert not (tmp_path / "symbols" / "XRPUSDT.json.tmp").exists()


def test_resume_skips_success_no_second_load(
    monkeypatch, edc_spec, edc_compiled, catalogs, tmp_path
):
    loads = _patch_success(monkeypatch, edc_spec, edc_compiled)
    args = dict(
        client=_FakeClient(),
        universe_path=UNIVERSE,
        start=START,
        end=END,
        checkpoint_dir=tmp_path,
        symbols=("XRPUSDT", "LITUSDT"),
    )
    a = run_edc_m0_multicoin_v2(edc_spec, edc_compiled, catalogs, **args)
    assert loads == ["XRPUSDT", "LITUSDT"]
    loads.clear()
    b = run_edc_m0_multicoin_v2(
        edc_spec, edc_compiled, catalogs, resume=True, **args
    )
    assert loads == []
    assert a == b


def test_failure_without_retry_loaded(
    monkeypatch, edc_spec, edc_compiled, catalogs, tmp_path
):
    _patch_success(monkeypatch, edc_spec, edc_compiled, fail_symbols={"NEARUSDT"})
    args = dict(
        client=_FakeClient(),
        universe_path=UNIVERSE,
        start=START,
        end=END,
        checkpoint_dir=tmp_path,
        symbols=("NEARUSDT",),
    )
    first = run_edc_m0_multicoin_v2(edc_spec, edc_compiled, catalogs, **args)
    assert first.failed_symbols == ("NEARUSDT",)

    loads = _patch_success(monkeypatch, edc_spec, edc_compiled)  # would succeed
    second = run_edc_m0_multicoin_v2(
        edc_spec,
        edc_compiled,
        catalogs,
        resume=True,
        retry_failures=False,
        **args,
    )
    assert loads == []
    assert second.failed_symbols == ("NEARUSDT",)
    assert second.completed_runs == ()


def test_failure_with_retry_reruns(
    monkeypatch, edc_spec, edc_compiled, catalogs, tmp_path
):
    _patch_success(monkeypatch, edc_spec, edc_compiled, fail_symbols={"NEARUSDT"})
    args = dict(
        client=_FakeClient(),
        universe_path=UNIVERSE,
        start=START,
        end=END,
        checkpoint_dir=tmp_path,
        symbols=("NEARUSDT",),
    )
    run_edc_m0_multicoin_v2(edc_spec, edc_compiled, catalogs, **args)
    loads = _patch_success(monkeypatch, edc_spec, edc_compiled)
    out = run_edc_m0_multicoin_v2(
        edc_spec,
        edc_compiled,
        catalogs,
        resume=True,
        retry_failures=True,
        **args,
    )
    assert loads == ["NEARUSDT"]
    assert out.completed_symbols == ("NEARUSDT",)
    assert out.failures == ()


def test_corrupt_checkpoint_rejected(
    monkeypatch, edc_spec, edc_compiled, catalogs, tmp_path
):
    _patch_success(monkeypatch, edc_spec, edc_compiled)
    run_edc_m0_multicoin_v2(
        edc_spec,
        edc_compiled,
        catalogs,
        client=_FakeClient(),
        universe_path=UNIVERSE,
        start=START,
        end=END,
        checkpoint_dir=tmp_path,
        symbols=("XRPUSDT",),
    )
    (tmp_path / "symbols" / "XRPUSDT.json").write_text("{not-json", encoding="utf-8")
    with pytest.raises(StrategyMulticoinError, match="corrupt checkpoint"):
        run_edc_m0_multicoin_v2(
            edc_spec,
            edc_compiled,
            catalogs,
            client=_FakeClient(),
            universe_path=UNIVERSE,
            start=START,
            end=END,
            checkpoint_dir=tmp_path,
            symbols=("XRPUSDT",),
            resume=True,
        )


def test_incompatible_hash_checkpoint_rejected(
    monkeypatch, edc_spec, edc_compiled, catalogs, tmp_path
):
    _patch_success(monkeypatch, edc_spec, edc_compiled)
    run_edc_m0_multicoin_v2(
        edc_spec,
        edc_compiled,
        catalogs,
        client=_FakeClient(),
        universe_path=UNIVERSE,
        start=START,
        end=END,
        checkpoint_dir=tmp_path,
        symbols=("XRPUSDT",),
    )
    path = tmp_path / "symbols" / "XRPUSDT.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["strategy_hash"] = "a" * 64
    path.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    with pytest.raises(StrategyMulticoinError, match="incompatible checkpoint"):
        run_edc_m0_multicoin_v2(
            edc_spec,
            edc_compiled,
            catalogs,
            client=_FakeClient(),
            universe_path=UNIVERSE,
            start=START,
            end=END,
            checkpoint_dir=tmp_path,
            symbols=("XRPUSDT",),
            resume=True,
        )


def test_incompatible_window_rejected(
    monkeypatch, edc_spec, edc_compiled, catalogs, tmp_path
):
    _patch_success(monkeypatch, edc_spec, edc_compiled)
    run_edc_m0_multicoin_v2(
        edc_spec,
        edc_compiled,
        catalogs,
        client=_FakeClient(),
        universe_path=UNIVERSE,
        start=START,
        end=END,
        checkpoint_dir=tmp_path,
        symbols=("XRPUSDT",),
    )
    with pytest.raises(StrategyMulticoinError, match="incompatible checkpoint"):
        run_edc_m0_multicoin_v2(
            edc_spec,
            edc_compiled,
            catalogs,
            client=_FakeClient(),
            universe_path=UNIVERSE,
            start=START,
            end=datetime(2026, 8, 22, tzinfo=UTC),
            checkpoint_dir=tmp_path,
            symbols=("XRPUSDT",),
            resume=True,
        )


def test_no_mutation(monkeypatch, edc_spec, edc_compiled, catalogs, tmp_path):
    _patch_success(monkeypatch, edc_spec, edc_compiled)
    before_hash = edc_compiled.strategy_hash
    before_bytes = edc_compiled.canonical_bytes
    run_edc_m0_multicoin_v2(
        edc_spec,
        edc_compiled,
        catalogs,
        client=_FakeClient(),
        universe_path=UNIVERSE,
        start=START,
        end=END,
        checkpoint_dir=tmp_path,
        symbols=("XRPUSDT",),
    )
    assert edc_compiled.strategy_hash == before_hash
    assert edc_compiled.canonical_bytes == before_bytes


def test_module_import_does_not_require_legacy():
    # Importing the module must not pull ema_dual/cluster via static imports.
    import ast
    from pathlib import Path

    tree = ast.parse(Path(mc.__file__).read_text(encoding="utf-8"))
    forbidden = (
        "orderbook_analyse.ema_dual_cross_multisource",
        "orderbook_analyse.cluster_sweep_research",
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                for f in forbidden:
                    assert not a.name.startswith(f)
        elif isinstance(node, ast.ImportFrom) and node.module:
            for f in forbidden:
                assert not node.module.startswith(f)


def test_full_universe_has_no_duplicates(edc_spec):
    symbols, _ = mc._load_and_verify_universe(UNIVERSE, expected=edc_spec.universe)
    assert len(symbols) == 51
    assert len(set(symbols)) == 51


def test_request_order_rewritten_to_universe_order(
    monkeypatch, edc_spec, edc_compiled, catalogs, tmp_path
):
    loads = _patch_success(monkeypatch, edc_spec, edc_compiled)
    out = run_edc_m0_multicoin_v2(
        edc_spec,
        edc_compiled,
        catalogs,
        client=_FakeClient(),
        universe_path=UNIVERSE,
        start=START,
        end=END,
        checkpoint_dir=tmp_path,
        symbols=("XRPUSDT", "LITUSDT", "NEARUSDT"),
    )
    assert out.requested_symbols == ("XRPUSDT", "NEARUSDT", "LITUSDT")
    assert loads == ["XRPUSDT", "NEARUSDT", "LITUSDT"]


def test_unexpected_typeerror_propagates_after_prior_success(
    monkeypatch, edc_spec, edc_compiled, catalogs, tmp_path
):
    def fake_load(s, c, *, client, symbol, start, end):
        return _empty_market()

    def fake_exec(s, c, cats, *, symbol, start, end, market_data):
        if symbol == "NEARUSDT":
            raise TypeError("simulated programming error")
        return _run_for_symbol(s, c, symbol)

    monkeypatch.setattr(mc, "load_edc_m0_market_data_v2", fake_load)
    monkeypatch.setattr(mc, "execute_edc_m0_strict_sync_v2", fake_exec)
    with pytest.raises(TypeError, match="simulated programming error"):
        run_edc_m0_multicoin_v2(
            edc_spec,
            edc_compiled,
            catalogs,
            client=_FakeClient(),
            universe_path=UNIVERSE,
            start=START,
            end=END,
            checkpoint_dir=tmp_path,
            symbols=("XRPUSDT", "NEARUSDT"),
        )
    assert (tmp_path / "symbols" / "XRPUSDT.json").is_file()
    assert not (tmp_path / "symbols" / "NEARUSDT.json").exists()


def test_checkpoint_result_roundtrip_exact(
    monkeypatch, edc_spec, edc_compiled, catalogs, tmp_path
):
    exotic = Decimal("-12.345678901234567890")

    def fake_load(s, c, *, client, symbol, start, end):
        return _empty_market()

    def fake_exec(s, c, cats, *, symbol, start, end, market_data):
        resolved = _run_for_symbol(s, c, symbol, n_trades=1)
        unresolved = StrategyTradeV2(
            source_event_id=SourceEventIdV2(value="edc:unresolved000001"),
            symbol=symbol,
            side=SideName.SHORT,
            decision_time=datetime(2026, 7, 25, 3, 0, tzinfo=UTC),
            entry_time=datetime(2026, 7, 25, 3, 0, tzinfo=UTC),
            entry_price=Decimal("2.5"),
            exit_time=None,
            exit_price=None,
            exit_reason=TradeExitReasonV2.COVERAGE_MISSING,
            gross_return_pct=None,
            roundtrip_cost_pct=Decimal("0.11"),
            net_return_pct=None,
            gross_pnl_usdt=None,
            costs_usdt=None,
            net_pnl_usdt=None,
            mode_id=StableIdentifier(value="m0_strict_sync"),
            confirmation_policy=ResearchConfirmationPolicyV2.CORE_RESEARCH_SUPPORTIVE,
        )
        # Rebuild run with exotic decimal on the resolved trade.
        t0 = resolved.trades[0]
        exotic_trade = StrategyTradeV2(
            source_event_id=t0.source_event_id,
            symbol=t0.symbol,
            side=t0.side,
            decision_time=t0.decision_time,
            entry_time=t0.entry_time,
            entry_price=t0.entry_price,
            exit_time=t0.exit_time,
            exit_price=t0.exit_price,
            exit_reason=t0.exit_reason,
            gross_return_pct=exotic,
            roundtrip_cost_pct=t0.roundtrip_cost_pct,
            net_return_pct=exotic,
            gross_pnl_usdt=exotic,
            costs_usdt=Decimal("1.1"),
            net_pnl_usdt=exotic,
            mode_id=t0.mode_id,
            confirmation_policy=t0.confirmation_policy,
        )
        return StrategyRunResultV2(
            strategy_hash=resolved.strategy_hash,
            plugin_id=resolved.plugin_id,
            plugin_contract_version=resolved.plugin_contract_version,
            universe=resolved.universe,
            start=resolved.start,
            end=resolved.end,
            symbols=resolved.symbols,
            signal_timeframe=resolved.signal_timeframe,
            execution_timeframe=resolved.execution_timeframe,
            roundtrip_cost=resolved.roundtrip_cost,
            slippage_status=resolved.slippage_status,
            funding_status=resolved.funding_status,
            status=resolved.status,
            candidate_count=2,
            trades=(exotic_trade, unresolved),
        )

    monkeypatch.setattr(mc, "load_edc_m0_market_data_v2", fake_load)
    monkeypatch.setattr(mc, "execute_edc_m0_strict_sync_v2", fake_exec)
    first = run_edc_m0_multicoin_v2(
        edc_spec,
        edc_compiled,
        catalogs,
        client=_FakeClient(),
        universe_path=UNIVERSE,
        start=START,
        end=END,
        checkpoint_dir=tmp_path,
        symbols=("XRPUSDT",),
    )
    raw = (tmp_path / "symbols" / "XRPUSDT.json").read_text(encoding="utf-8")
    assert '"gross_return_pct": "-12.345678901234567890"' in raw
    assert "float" not in raw
    assert first.completed_runs[0].trades[0].gross_return_pct == exotic
    assert first.completed_runs[0].trades[1].exit_reason is TradeExitReasonV2.COVERAGE_MISSING

    second = run_edc_m0_multicoin_v2(
        edc_spec,
        edc_compiled,
        catalogs,
        client=_FakeClient(),
        universe_path=UNIVERSE,
        start=START,
        end=END,
        checkpoint_dir=tmp_path,
        symbols=("XRPUSDT",),
        resume=True,
    )
    assert second.completed_runs[0] == first.completed_runs[0]


def test_resume_leaves_checkpoint_bytes_unchanged(
    monkeypatch, edc_spec, edc_compiled, catalogs, tmp_path
):
    _patch_success(monkeypatch, edc_spec, edc_compiled)
    args = dict(
        client=_FakeClient(),
        universe_path=UNIVERSE,
        start=START,
        end=END,
        checkpoint_dir=tmp_path,
        symbols=("XRPUSDT",),
    )
    run_edc_m0_multicoin_v2(edc_spec, edc_compiled, catalogs, **args)
    path = tmp_path / "symbols" / "XRPUSDT.json"
    before = path.read_bytes()
    run_edc_m0_multicoin_v2(edc_spec, edc_compiled, catalogs, resume=True, **args)
    assert path.read_bytes() == before


def test_fingerprint_rejects_missing_and_unknown_fields(
    monkeypatch, edc_spec, edc_compiled, catalogs, tmp_path
):
    _patch_success(monkeypatch, edc_spec, edc_compiled)
    run_edc_m0_multicoin_v2(
        edc_spec,
        edc_compiled,
        catalogs,
        client=_FakeClient(),
        universe_path=UNIVERSE,
        start=START,
        end=END,
        checkpoint_dir=tmp_path,
        symbols=("XRPUSDT",),
    )
    path = tmp_path / "symbols" / "XRPUSDT.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["extra_field"] = "nope"
    path.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    with pytest.raises(StrategyMulticoinError, match="unknown fields"):
        run_edc_m0_multicoin_v2(
            edc_spec,
            edc_compiled,
            catalogs,
            client=_FakeClient(),
            universe_path=UNIVERSE,
            start=START,
            end=END,
            checkpoint_dir=tmp_path,
            symbols=("XRPUSDT",),
            resume=True,
        )


def test_write_checkpoint_fsync_and_tmp_cleanup(tmp_path, monkeypatch):
    calls: list[str] = []
    real_fsync = os.fsync

    def tracking_fsync(fd):
        calls.append("fsync")
        return real_fsync(fd)

    monkeypatch.setattr(mc.os, "fsync", tracking_fsync)
    path = tmp_path / "XRPUSDT.json"
    fp = {
        "checkpoint_format_version": mc._CHECKPOINT_FORMAT_VERSION,
        "strategy_hash": "a" * 64,
    }
    failure = SymbolRunFailureV2(
        symbol="XRPUSDT", error_type="StrategyAdapterError", message="x"
    )
    mc._write_checkpoint(
        path, fingerprint=fp, symbol="XRPUSDT", status="failed", failure=failure
    )
    assert calls == ["fsync"]
    assert path.is_file()
    assert not path.with_suffix(".json.tmp").exists()

    def boom_replace(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(mc.os, "replace", boom_replace)
    with pytest.raises(OSError, match="replace failed"):
        mc._write_checkpoint(
            path, fingerprint=fp, symbol="XRPUSDT", status="failed", failure=failure
        )
    assert path.read_text(encoding="utf-8")  # prior checkpoint retained
    assert not list(tmp_path.glob("*.tmp"))


def test_repeated_offline_run_identical(
    monkeypatch, edc_spec, edc_compiled, catalogs, tmp_path
):
    _patch_success(monkeypatch, edc_spec, edc_compiled)
    args = dict(
        client=_FakeClient(),
        universe_path=UNIVERSE,
        start=START,
        end=END,
        checkpoint_dir=tmp_path / "a",
        symbols=("XRPUSDT", "LITUSDT"),
    )
    a = run_edc_m0_multicoin_v2(edc_spec, edc_compiled, catalogs, **args)
    b = run_edc_m0_multicoin_v2(
        edc_spec,
        edc_compiled,
        catalogs,
        client=_FakeClient(),
        universe_path=UNIVERSE,
        start=START,
        end=END,
        checkpoint_dir=tmp_path / "b",
        symbols=("XRPUSDT", "LITUSDT"),
    )
    assert a == b
