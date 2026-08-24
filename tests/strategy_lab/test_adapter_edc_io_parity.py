"""Gated local XRP end-to-end: load_edc_m0_market_data_v2 → P2B adapter."""

from __future__ import annotations

import csv
import hashlib
import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.tpsl_pnl_engine import (
    apply_costs,
)
from orderbook_analyse.strategy_lab.adapters.edc_io import load_edc_m0_market_data_v2
from orderbook_analyse.strategy_lab.adapters.edc_m0 import execute_edc_m0_strict_sync_v2
from orderbook_analyse.strategy_lab.compiler_v2 import compile_strategy_v2
from orderbook_analyse.strategy_lab.decoder_v2 import load_strategy_v2_yaml_file
from orderbook_analyse.strategy_lab.models.enums import SideName
from orderbook_analyse.strategy_lab.results_v2 import TradeExitReasonV2
from orderbook_analyse.strategy_lab.validation.catalogs import production_catalog_bundle_v2
from orderbook_analyse.strategy_lab.validation.p4c import require_valid_strategy_v2_p4c

UTC = timezone.utc
REPO = Path(__file__).resolve().parents[2]
EDC_YAML = REPO / "strategies" / "strategy_lab" / "edc_m0_strict_sync_v2.yaml"
CAND_CSV = (
    REPO
    / "results"
    / "edc_sync_tolerance"
    / "xrp_30d_core_sources_comparison"
    / "candidates_with_sources.csv"
)
TRADE_CSV = (
    REPO
    / "results"
    / "edc_sync_tolerance"
    / "xrp_30d_horizon_tp_sl_matrix"
    / "trades_matrix.csv"
)
HASH_EDC = "fb0ebc45827c68ab60d3a920c2d5d68651857080cf950fc020994044935f81ea"
WINDOW_START = datetime(2026, 7, 24, 0, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 8, 23, 0, 0, tzinfo=UTC)


def _require_io_parity_env() -> None:
    if os.environ.get("STRATEGY_LAB_EDC_IO_PARITY") != "1":
        pytest.skip(
            "local XRP EDC IO parity disabled; export STRATEGY_LAB_EDC_IO_PARITY=1 "
            "to run (ClickHouse + gitignored results/). Not a silent pass."
        )


def _ref_supportive_candidates() -> list[dict[str, str]]:
    if not CAND_CSV.is_file():
        pytest.fail(f"XRP candidate reference missing: {CAND_CSV}")
    return [
        r
        for r in csv.DictReader(CAND_CSV.open())
        if r.get("timeframe") == "5m"
        and r.get("mode_id") == "M0_STRICT_SYNC"
        and r.get("core_research_verdict") == "CORE_RESEARCH_SUPPORTIVE"
    ]


def _ref_cell_trades() -> list[dict[str, str]]:
    if not TRADE_CSV.is_file():
        pytest.fail(f"XRP trade reference missing: {TRADE_CSV}")
    return [
        r
        for r in csv.DictReader(TRADE_CSV.open())
        if r.get("signal_timeframe") == "5m"
        and r.get("mode_id") == "M0_STRICT_SYNC"
        and r.get("group") == "CORE_RESEARCH_SUPPORTIVE"
        and r.get("tp_pct") == "0.75"
        and r.get("sl_pct") == "0.5"
        and r.get("horizon") == "8h"
    ]


class _CountingClient:
    """Wraps a real ClickHouse client and counts ``query`` calls."""

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.query_count = 0
        self.sql_snippets: list[str] = []

    def query(
        self,
        sql: str,
        parameters: object = None,
        settings: object = None,
    ) -> object:
        self.query_count += 1
        self.sql_snippets.append(" ".join(sql.split())[:120])
        return self._inner.query(sql, parameters=parameters, settings=settings)


def test_xrp_io_wrapper_full_parity_local() -> None:
    _require_io_parity_env()
    ref_cands = _ref_supportive_candidates()
    ref_trades = {r["candidate_id"]: r for r in _ref_cell_trades()}
    assert len(ref_cands) == 15
    assert len(ref_trades) == 15

    cand_sha = hashlib.sha256(CAND_CSV.read_bytes()).hexdigest()
    trade_sha = hashlib.sha256(TRADE_CSV.read_bytes()).hexdigest()
    print("cand_sha256", cand_sha)
    print("trade_sha256", trade_sha)

    catalogs = production_catalog_bundle_v2()
    spec = load_strategy_v2_yaml_file(EDC_YAML)
    require_valid_strategy_v2_p4c(spec, catalogs)
    compiled = compile_strategy_v2(spec, catalogs)
    assert compiled.strategy_hash == HASH_EDC

    from orderbook_analyse.orderbook_v2.ch_client import get_clickhouse_client

    t0 = time.perf_counter()
    try:
        raw_client = get_clickhouse_client()
        client = _CountingClient(raw_client)
        market = load_edc_m0_market_data_v2(
            spec,
            catalogs,
            client=client,
            symbol="XRPUSDT",
            start=WINDOW_START,
            end=WINDOW_END,
        )
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"ClickHouse market load via edc_io failed: {exc!r}")

    assert client.query_count == 5, (
        f"expected exactly 5 legacy loader queries, got {client.query_count}: "
        f"{client.sql_snippets}"
    )
    assert not market.candles_1m.empty

    result = execute_edc_m0_strict_sync_v2(
        spec,
        compiled,
        catalogs,
        symbol="XRPUSDT",
        start=WINDOW_START,
        end=WINDOW_END,
        market_data=market,
    )
    elapsed = time.perf_counter() - t0
    print("elapsed_sec", round(elapsed, 3))
    print("query_count", client.query_count)

    assert result.candidate_count == 15
    assert result.trade_count == 15
    assert result.strategy_hash == HASH_EDC

    by_id = {t.source_event_id.value: t for t in result.trades}
    assert set(by_id) == {c["candidate_id"] for c in ref_cands}
    assert set(by_id) == set(ref_trades)

    side_map = {"BULLISH": SideName.LONG, "BEARISH": SideName.SHORT}
    exit_map = {
        "TP_EXIT": TradeExitReasonV2.TP_EXIT,
        "SL_EXIT": TradeExitReasonV2.SL_EXIT,
        "TIME_EXIT": TradeExitReasonV2.TIME_EXIT,
        "COVERAGE_MISSING": TradeExitReasonV2.COVERAGE_MISSING,
        "INCOMPLETE_OUTCOME_HORIZON": TradeExitReasonV2.INCOMPLETE_OUTCOME_HORIZON,
    }

    for cid, ref in ref_trades.items():
        trade = by_id[cid]
        rc = next(c for c in ref_cands if c["candidate_id"] == cid)
        assert trade.side is side_map[rc["direction"]]
        assert trade.decision_time == datetime.fromisoformat(
            rc["decision_at"].replace("Z", "+00:00")
        )
        assert trade.entry_time == datetime.fromisoformat(
            ref["entry_at"].replace("Z", "+00:00")
        )
        assert trade.entry_price == Decimal(str(ref["entry_price"]))
        if ref["exit_at"]:
            assert trade.exit_time == datetime.fromisoformat(
                ref["exit_at"].replace("Z", "+00:00")
            )
            assert trade.exit_price == Decimal(str(ref["exit_price"]))
        else:
            assert trade.exit_time is None
        assert trade.exit_reason is exit_map[ref["exit_reason"]]
        assert trade.gross_return_pct == Decimal(str(ref["gross_return_pct"]))
        assert trade.gross_pnl_usdt == Decimal(str(ref["gross_pnl_usdt"]))
        assert trade.roundtrip_cost_pct == Decimal("0.11")
        paid = apply_costs({"gross_return_pct": float(ref["gross_return_pct"])}, 0.11)
        assert trade.net_return_pct == Decimal(str(paid["net_return_pct"]))
        assert trade.net_pnl_usdt == Decimal(str(paid["net_pnl_usdt"]))
        assert trade.costs_usdt == Decimal(str(paid["costs_usdt"]))

    ref_gross = sum(Decimal(str(r["gross_pnl_usdt"])) for r in ref_trades.values())
    run_gross = result.gross_pnl_usdt
    run_costs = result.costs_usdt
    run_net = result.net_pnl_usdt
    assert run_gross == ref_gross
    assert run_costs == Decimal("1.1") * Decimal(result.trade_count)
    assert run_net == run_gross - run_costs
    assert all(t.costs_usdt == Decimal("1.1") for t in result.trades)
    print("gross", run_gross, "costs", run_costs, "net", run_net)
