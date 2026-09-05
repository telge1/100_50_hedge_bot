"""Offline tests for EDC multicoin export (P2D3; no ClickHouse)."""

from __future__ import annotations

import csv
import importlib
import importlib.util
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from orderbook_analyse.strategy_lab import edc_multicoin_export_v2 as ex
from orderbook_analyse.strategy_lab.catalogs.v2.models import CATALOG_CONTRACT_VERSION
from orderbook_analyse.strategy_lab.compiler_v2 import compile_strategy_v2
from orderbook_analyse.strategy_lab.decoder_v2 import load_strategy_v2_yaml_file
from orderbook_analyse.strategy_lab.edc_multicoin_v2 import (
    EdcMulticoinRunV2,
    SymbolRunFailureV2,
)
from orderbook_analyse.strategy_lab.models.contracts_v2.enums import (
    ResearchConfirmationPolicyV2,
)
from orderbook_analyse.strategy_lab.models.enums import SideName
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


def _trade(
    *,
    symbol: str,
    eid: str,
    decision: datetime,
    net: Decimal | None,
    exit_reason: TradeExitReasonV2,
    side: SideName = SideName.LONG,
) -> StrategyTradeV2:
    resolved = exit_reason in {
        TradeExitReasonV2.TP_EXIT,
        TradeExitReasonV2.SL_EXIT,
        TradeExitReasonV2.TIME_EXIT,
    }
    if resolved:
        assert net is not None
        return StrategyTradeV2(
            source_event_id=SourceEventIdV2(value=eid),
            symbol=symbol,
            side=side,
            decision_time=decision,
            entry_time=decision,
            entry_price=Decimal("1"),
            exit_time=decision.replace(hour=2),
            exit_price=Decimal("1.01"),
            exit_reason=exit_reason,
            gross_return_pct=net + Decimal("1.1"),
            roundtrip_cost_pct=Decimal("0.11"),
            net_return_pct=net,
            gross_pnl_usdt=net + Decimal("1.1"),
            costs_usdt=Decimal("1.1"),
            net_pnl_usdt=net,
            mode_id=StableIdentifier(value="m0_strict_sync"),
            confirmation_policy=ResearchConfirmationPolicyV2.CORE_RESEARCH_SUPPORTIVE,
        )
    return StrategyTradeV2(
        source_event_id=SourceEventIdV2(value=eid),
        symbol=symbol,
        side=side,
        decision_time=decision,
        entry_time=decision,
        entry_price=Decimal("1"),
        exit_time=None,
        exit_price=None,
        exit_reason=exit_reason,
        gross_return_pct=None,
        roundtrip_cost_pct=Decimal("0.11"),
        net_return_pct=None,
        gross_pnl_usdt=None,
        costs_usdt=None,
        net_pnl_usdt=None,
        mode_id=StableIdentifier(value="m0_strict_sync"),
        confirmation_policy=ResearchConfirmationPolicyV2.CORE_RESEARCH_SUPPORTIVE,
    )


def _run_result(spec, compiled, symbol: str, trades: tuple[StrategyTradeV2, ...]):
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
        candidate_count=len(trades),
        trades=trades,
    )


def _sample_run(edc_spec, edc_compiled) -> EdcMulticoinRunV2:
    xrp = _run_result(
        edc_spec,
        edc_compiled,
        "XRPUSDT",
        (
            _trade(
                symbol="XRPUSDT",
                eid="edc:xrp000000000001",
                decision=datetime(2026, 7, 25, 1, 0, tzinfo=UTC),
                net=Decimal("6.4"),
                exit_reason=TradeExitReasonV2.TP_EXIT,
            ),
            _trade(
                symbol="XRPUSDT",
                eid="edc:xrp000000000002",
                decision=datetime(2026, 7, 25, 0, 0, tzinfo=UTC),
                net=Decimal("-2.1"),
                exit_reason=TradeExitReasonV2.SL_EXIT,
            ),
            _trade(
                symbol="XRPUSDT",
                eid="edc:xrp000000000003",
                decision=datetime(2026, 7, 25, 3, 0, tzinfo=UTC),
                net=None,
                exit_reason=TradeExitReasonV2.COVERAGE_MISSING,
            ),
        ),
    )
    lit = _run_result(edc_spec, edc_compiled, "LITUSDT", ())
    return EdcMulticoinRunV2(
        strategy_hash=edc_compiled.strategy_hash,
        universe=edc_spec.universe,
        start=START,
        end=END,
        requested_symbols=("XRPUSDT", "NEARUSDT", "LITUSDT"),
        completed_runs=(xrp, lit),
        failures=(
            SymbolRunFailureV2(
                symbol="NEARUSDT",
                error_type="StrategyMarketDataError",
                message="load failed for NEARUSDT",
            ),
        ),
    )


def test_export_artifacts_complete(edc_spec, edc_compiled, tmp_path):
    run = _sample_run(edc_spec, edc_compiled)
    out = tmp_path / "out"
    ex.export_edc_multicoin_artifacts_v2(run, edc_spec, output_dir=out)

    manifest = json.loads((out / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["export_format_version"] == "edc_multicoin_export/v1"
    assert manifest["strategy_hash"] == HASH_EDC
    assert manifest["plugin_id"] == "edc_m0_strict_sync"
    assert manifest["requested_symbols"] == ["XRPUSDT", "NEARUSDT", "LITUSDT"]
    assert manifest["execution_order"] == ["XRPUSDT", "NEARUSDT", "LITUSDT"]
    assert manifest["completed_symbols"] == ["XRPUSDT", "LITUSDT"]
    assert manifest["failed_symbols"] == ["NEARUSDT"]
    assert manifest["trade_count"] == 3
    assert manifest["candidate_count"] == 3
    assert manifest["gross_pnl_usdt"] == str(run.gross_pnl_usdt)
    assert isinstance(manifest["gross_pnl_usdt"], str)
    assert "host" not in manifest
    assert "created" not in manifest

    failures = json.loads((out / "failures.json").read_text(encoding="utf-8"))
    assert failures == [
        {
            "error_message": "load failed for NEARUSDT",
            "error_type": "StrategyMarketDataError",
            "symbol": "NEARUSDT",
        }
    ]

    coin_rows = list(csv.DictReader((out / "coin_summary.csv").open(encoding="utf-8")))
    assert [r["symbol"] for r in coin_rows] == ["XRPUSDT", "NEARUSDT", "LITUSDT"]
    xrp = coin_rows[0]
    assert xrp["status"] == "complete"
    assert xrp["winning_trades"] == "1"
    assert xrp["losing_trades"] == "1"
    assert xrp["unresolved_trades"] == "1"
    # win_rate = winning / (winning + losing); unresolved/zero-PnL excluded
    assert xrp["win_rate"] == str(Decimal("1") / Decimal("2"))
    assert xrp["avg_net_pnl_usdt"] == str(
        (Decimal("6.4") + Decimal("-2.1")) / Decimal("2")
    )
    near = coin_rows[1]
    assert near["status"] == "failed"
    assert near["error_type"] == "StrategyMarketDataError"
    assert near["trade_count"] == ""
    assert near["win_rate"] == ""
    lit = coin_rows[2]
    assert lit["status"] == "complete"
    assert lit["trade_count"] == "0"
    assert lit["win_rate"] == ""
    assert lit["avg_net_pnl_usdt"] == ""

    trade_rows = list(csv.DictReader((out / "trades.csv").open(encoding="utf-8")))
    assert len(trade_rows) == 3
    # Sorted by symbol, decision_time, source_event_id
    assert [r["source_event_id"] for r in trade_rows] == [
        "edc:xrp000000000002",
        "edc:xrp000000000001",
        "edc:xrp000000000003",
    ]
    unresolved = trade_rows[2]
    assert unresolved["exit_time"] == ""
    assert unresolved["exit_price"] == ""
    assert unresolved["gross_pnl_usdt"] == ""
    assert unresolved["exit_reason"] == "coverage_missing"
    assert "6.4" in trade_rows[1]["net_pnl_usdt"]


def test_export_identical_bytes(edc_spec, edc_compiled, tmp_path):
    run = _sample_run(edc_spec, edc_compiled)
    a = tmp_path / "a"
    b = tmp_path / "b"
    ex.export_edc_multicoin_artifacts_v2(run, edc_spec, output_dir=a)
    ex.export_edc_multicoin_artifacts_v2(run, edc_spec, output_dir=b)
    for name in (
        "run_manifest.json",
        "coin_summary.csv",
        "trades.csv",
        "failures.json",
    ):
        assert (a / name).read_bytes() == (b / name).read_bytes()


def test_atomic_overwrite_preserves_old_on_failure(
    edc_spec, edc_compiled, tmp_path, monkeypatch
):
    run = _sample_run(edc_spec, edc_compiled)
    out = tmp_path / "out"
    ex.export_edc_multicoin_artifacts_v2(run, edc_spec, output_dir=out)
    path = out / "failures.json"
    before = path.read_bytes()

    def boom_replace(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(ex.os, "replace", boom_replace)
    with pytest.raises(OSError, match="replace failed"):
        ex.export_edc_multicoin_artifacts_v2(run, edc_spec, output_dir=out)
    assert path.read_bytes() == before
    assert not list(out.glob("*.tmp"))


def test_no_mutation(edc_spec, edc_compiled, tmp_path):
    run = _sample_run(edc_spec, edc_compiled)
    before = edc_compiled.canonical_bytes
    ex.export_edc_multicoin_artifacts_v2(run, edc_spec, output_dir=tmp_path)
    assert edc_compiled.canonical_bytes is before
    assert run.requested_symbols == ("XRPUSDT", "NEARUSDT", "LITUSDT")


def test_empty_failures_is_empty_array(edc_spec, edc_compiled, tmp_path):
    run = EdcMulticoinRunV2(
        strategy_hash=edc_compiled.strategy_hash,
        universe=edc_spec.universe,
        start=START,
        end=END,
        requested_symbols=("LITUSDT",),
        completed_runs=(
            _run_result(edc_spec, edc_compiled, "LITUSDT", ()),
        ),
        failures=(),
    )
    ex.export_edc_multicoin_artifacts_v2(run, edc_spec, output_dir=tmp_path)
    assert (tmp_path / "failures.json").read_text(encoding="utf-8") == "[]\n"


def test_decimal_not_float_in_json(edc_spec, edc_compiled, tmp_path):
    exotic = Decimal("-12.345678901234567890")
    trade = _trade(
        symbol="XRPUSDT",
        eid="edc:xrp000000000099",
        decision=datetime(2026, 7, 25, 1, 0, tzinfo=UTC),
        net=exotic,
        exit_reason=TradeExitReasonV2.TP_EXIT,
    )
    # overwrite pnl fields with exotic strings via reconstruction already using Decimal
    run = EdcMulticoinRunV2(
        strategy_hash=edc_compiled.strategy_hash,
        universe=edc_spec.universe,
        start=START,
        end=END,
        requested_symbols=("XRPUSDT",),
        completed_runs=(_run_result(edc_spec, edc_compiled, "XRPUSDT", (trade,)),),
        failures=(),
    )
    ex.export_edc_multicoin_artifacts_v2(run, edc_spec, output_dir=tmp_path)
    raw = (tmp_path / "run_manifest.json").read_text(encoding="utf-8")
    assert f'"{exotic}"' in (tmp_path / "trades.csv").read_text(encoding="utf-8") or str(
        exotic
    ) in (tmp_path / "trades.csv").read_text(encoding="utf-8")
    data = json.loads(raw)
    assert isinstance(data["net_pnl_usdt"], str)


def test_cli_args_and_passthrough(edc_spec, monkeypatch, tmp_path):
    calls: list[dict] = []

    class _Client:
        def query(self, sql, parameters=None, settings=None):
            raise AssertionError("should not query")

    def fake_run_and_export(**kwargs):
        calls.append(kwargs)
        return _sample_run(edc_spec, compile_strategy_v2(edc_spec, production_catalog_bundle_v2()))

    monkeypatch.setattr(ex, "run_and_export_edc_m0_multicoin_v2", fake_run_and_export)
    monkeypatch.setattr(
        "orderbook_analyse.orderbook_v2.ch_client.get_clickhouse_client",
        lambda: _Client(),
    )
    rc = ex.main(
        [
            "--strategy",
            str(EDC_YAML),
            "--universe",
            str(UNIVERSE),
            "--start",
            "2026-07-24T00:00:00Z",
            "--end",
            "2026-08-23T00:00:00Z",
            "--output-dir",
            str(tmp_path),
            "--symbol",
            "XRPUSDT",
            "--symbol",
            "LITUSDT",
            "--resume",
            "--retry-failures",
        ]
    )
    assert rc == 0
    assert len(calls) == 1
    assert calls[0]["resume"] is True
    assert calls[0]["retry_failures"] is True
    assert calls[0]["symbols"] == ("XRPUSDT", "LITUSDT")
    assert calls[0]["checkpoint_dir"] is None


def test_run_and_export_calls_p2d2_once(edc_spec, edc_compiled, monkeypatch, tmp_path):
    calls: list[object] = []

    def fake_runner(*args, **kwargs):
        calls.append(kwargs)
        return _sample_run(edc_spec, edc_compiled)

    monkeypatch.setattr(ex, "run_edc_m0_multicoin_v2", fake_runner)

    class _Client:
        def query(self, sql, parameters=None, settings=None):
            return None

    out = ex.run_and_export_edc_m0_multicoin_v2(
        strategy_path=EDC_YAML,
        universe_path=UNIVERSE,
        start=START,
        end=END,
        output_dir=tmp_path,
        client=_Client(),
        symbols=("XRPUSDT", "LITUSDT", "NEARUSDT"),
        resume=True,
        retry_failures=False,
    )
    assert len(calls) == 1
    assert calls[0]["resume"] is True
    assert calls[0]["retry_failures"] is False
    assert calls[0]["checkpoint_dir"] == tmp_path / "checkpoints"
    assert (tmp_path / "run_manifest.json").is_file()
    assert out.trade_count == 3


def test_module_import_does_not_open_clickhouse(monkeypatch):
    opened: list[str] = []

    def boom(*a, **k):
        opened.append("opened")
        raise AssertionError("must not open")

    monkeypatch.setattr(
        "orderbook_analyse.orderbook_v2.ch_client.get_clickhouse_client", boom
    )
    importlib.import_module("orderbook_analyse.strategy_lab.edc_multicoin_export_v2")
    _ = ex.build_arg_parser()
    assert opened == []


def test_script_import_does_not_open_clickhouse(monkeypatch):
    opened: list[str] = []

    def boom(*a, **k):
        opened.append("opened")
        raise AssertionError("must not open")

    monkeypatch.setattr(
        "orderbook_analyse.orderbook_v2.ch_client.get_clickhouse_client", boom
    )
    path = REPO / "scripts" / "run_strategy_lab_edc_multicoin.py"
    spec = importlib.util.spec_from_file_location("run_strategy_lab_edc_multicoin", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert opened == []


def test_invalid_window_cli():
    with pytest.raises(SystemExit):
        ex.build_arg_parser().parse_args([])


def test_fsync_used(edc_spec, edc_compiled, tmp_path, monkeypatch):
    calls: list[str] = []
    real = os.fsync

    def track(fd):
        calls.append("fsync")
        return real(fd)

    monkeypatch.setattr(ex.os, "fsync", track)
    ex.export_edc_multicoin_artifacts_v2(
        _sample_run(edc_spec, edc_compiled), edc_spec, output_dir=tmp_path
    )
    assert calls.count("fsync") == 4


def test_win_rate_excludes_breakeven_and_unresolved(edc_spec, edc_compiled, tmp_path):
    trades = (
        _trade(
            symbol="XRPUSDT",
            eid="edc:xrp000000000010",
            decision=datetime(2026, 7, 25, 1, 0, tzinfo=UTC),
            net=Decimal("5"),
            exit_reason=TradeExitReasonV2.TP_EXIT,
        ),
        _trade(
            symbol="XRPUSDT",
            eid="edc:xrp000000000011",
            decision=datetime(2026, 7, 25, 2, 0, tzinfo=UTC),
            net=Decimal("0"),
            exit_reason=TradeExitReasonV2.TIME_EXIT,
        ),
        _trade(
            symbol="XRPUSDT",
            eid="edc:xrp000000000012",
            decision=datetime(2026, 7, 25, 3, 0, tzinfo=UTC),
            net=None,
            exit_reason=TradeExitReasonV2.COVERAGE_MISSING,
        ),
    )
    run = EdcMulticoinRunV2(
        strategy_hash=edc_compiled.strategy_hash,
        universe=edc_spec.universe,
        start=START,
        end=END,
        requested_symbols=("XRPUSDT",),
        completed_runs=(_run_result(edc_spec, edc_compiled, "XRPUSDT", trades),),
        failures=(),
    )
    ex.export_edc_multicoin_artifacts_v2(run, edc_spec, output_dir=tmp_path)
    row = next(csv.DictReader((tmp_path / "coin_summary.csv").open(encoding="utf-8")))
    assert row["winning_trades"] == "1"
    assert row["losing_trades"] == "0"
    assert row["unresolved_trades"] == "1"
    assert row["win_rate"] == "1"  # 1 / (1+0); zero-PnL excluded from denominator
    assert row["avg_net_pnl_usdt"] == str((Decimal("5") + Decimal("0")) / Decimal("2"))


def test_cross_artifact_invariants(edc_spec, edc_compiled, tmp_path):
    run = _sample_run(edc_spec, edc_compiled)
    ex.export_edc_multicoin_artifacts_v2(run, edc_spec, output_dir=tmp_path)
    manifest = json.loads((tmp_path / "run_manifest.json").read_text(encoding="utf-8"))
    coins = list(csv.DictReader((tmp_path / "coin_summary.csv").open(encoding="utf-8")))
    trades = list(csv.DictReader((tmp_path / "trades.csv").open(encoding="utf-8")))
    failures = json.loads((tmp_path / "failures.json").read_text(encoding="utf-8"))

    assert manifest["trade_count"] == len(trades)
    assert manifest["completed_symbols"] == [
        c["symbol"] for c in coins if c["status"] == "complete"
    ]
    assert manifest["failed_symbols"] == [f["symbol"] for f in failures]
    assert {c["symbol"] for c in coins} == set(manifest["requested_symbols"])
    assert set(manifest["completed_symbols"]).isdisjoint(set(manifest["failed_symbols"]))

    success_coins = [c for c in coins if c["status"] == "complete"]
    gross = sum((Decimal(c["gross_pnl_usdt"]) for c in success_coins), Decimal("0"))
    costs = sum((Decimal(c["costs_usdt"]) for c in success_coins), Decimal("0"))
    net = sum((Decimal(c["net_pnl_usdt"]) for c in success_coins), Decimal("0"))
    assert manifest["gross_pnl_usdt"] == str(gross)
    assert manifest["costs_usdt"] == str(costs)
    assert manifest["net_pnl_usdt"] == str(net)

    for coin in success_coins:
        coin_trades = [t for t in trades if t["symbol"] == coin["symbol"]]
        assert len(coin_trades) == int(coin["trade_count"])
        t_net = sum(
            (Decimal(t["net_pnl_usdt"]) for t in coin_trades if t["net_pnl_usdt"] != ""),
            Decimal("0"),
        )
        assert coin["net_pnl_usdt"] == str(t_net)
        assert int(coin["candidate_count"]) >= int(coin["trade_count"])

    for t in trades:
        assert t["symbol"] in {c["symbol"] for c in coins}
        assert "None" not in t.values()


def test_cli_rejects_bad_window_without_clickhouse(monkeypatch):
    opened: list[str] = []

    def boom(*a, **k):
        opened.append("opened")
        raise AssertionError("must not open")

    monkeypatch.setattr(
        "orderbook_analyse.orderbook_v2.ch_client.get_clickhouse_client", boom
    )
    rc = ex.main(
        [
            "--strategy",
            str(EDC_YAML),
            "--universe",
            str(UNIVERSE),
            "--start",
            "2026-08-23T00:00:00Z",
            "--end",
            "2026-07-24T00:00:00Z",
            "--output-dir",
            "/tmp/unused_p2d3",
        ]
    )
    assert rc == 2
    assert opened == []


def test_cli_rejects_missing_strategy_without_clickhouse(monkeypatch, tmp_path):
    opened: list[str] = []

    def boom(*a, **k):
        opened.append("opened")
        raise AssertionError("must not open")

    monkeypatch.setattr(
        "orderbook_analyse.orderbook_v2.ch_client.get_clickhouse_client", boom
    )
    rc = ex.main(
        [
            "--strategy",
            str(tmp_path / "missing.yaml"),
            "--universe",
            str(UNIVERSE),
            "--start",
            "2026-07-24T00:00:00Z",
            "--end",
            "2026-08-23T00:00:00Z",
            "--output-dir",
            str(tmp_path / "out"),
        ]
    )
    assert rc == 2
    assert opened == []


def test_run_and_export_rejects_end_before_start(edc_spec, tmp_path):
    class _Client:
        def query(self, sql, parameters=None, settings=None):
            raise AssertionError("no query")

    with pytest.raises(ex.StrategyMulticoinExportError, match="end must be"):
        ex.run_and_export_edc_m0_multicoin_v2(
            strategy_path=EDC_YAML,
            universe_path=UNIVERSE,
            start=END,
            end=START,
            output_dir=tmp_path,
            client=_Client(),
        )
