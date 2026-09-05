"""EDC M0 adapter parity: portable engine checks + gated local XRP integration."""

from __future__ import annotations

import csv
import hashlib
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.tpsl_pnl_engine import (
    apply_costs,
    simulate_tpsl_trade,
)
from orderbook_analyse.strategy_lab.adapters.edc_m0 import (
    EdcM0MarketDataV2,
    execute_edc_m0_strict_sync_v2,
)
from orderbook_analyse.strategy_lab.compiler_v2 import compile_strategy_v2
from orderbook_analyse.strategy_lab.decoder_v2 import load_strategy_v2_yaml_file
from orderbook_analyse.strategy_lab.models.enums import SideName
from orderbook_analyse.strategy_lab.results_v2 import TradeExitReasonV2
from orderbook_analyse.strategy_lab.validation.catalogs import production_catalog_bundle_v2

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
HASH_EDC = "4aced6b481d19eadd5505afc535e6fb4976f231fd2894b11f7d79acebc53598f"
WINDOW_START = datetime(2026, 7, 24, 0, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 8, 23, 0, 0, tzinfo=UTC)


def test_portable_apply_costs_011_not_015() -> None:
    paid_011 = apply_costs({"gross_return_pct": 0.75}, 0.11)
    paid_015 = apply_costs({"gross_return_pct": 0.75}, 0.15)
    assert paid_011["roundtrip_cost_pct"] == 0.11
    assert paid_011["net_return_pct"] == pytest.approx(0.64)
    assert paid_011["net_pnl_usdt"] == pytest.approx(6.4)
    assert paid_015["net_pnl_usdt"] == pytest.approx(6.0)
    assert paid_011["net_pnl_usdt"] != paid_015["net_pnl_usdt"]


def test_portable_simulate_tpsl_plus_apply_costs_011() -> None:
    entry = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    rows = []
    for i in range(480):
        rows.append(
            {
                "open_time": entry + timedelta(minutes=i),
                "open": 100.0,
                "high": 101.0 if i == 0 else 100.2,
                "low": 99.9,
                "close": 100.0,
                "volume": 1.0,
            }
        )
    candles = pd.DataFrame(rows)
    sim = simulate_tpsl_trade(
        candles,
        direction="BULLISH",
        entry_at=entry,
        entry_price=100.0,
        tp_pct=0.75,
        sl_pct=0.50,
        horizon_min=480,
        require_full_horizon=False,
        incomplete_if_truncated_path=True,
    )
    assert sim["exit_reason"] == "TP_EXIT"
    paid = apply_costs(sim, 0.11)
    assert paid["roundtrip_cost_pct"] == 0.11
    assert paid["costs_usdt"] == pytest.approx(1.1)
    assert paid["net_pnl_usdt"] == pytest.approx(6.4)


def _require_parity_env() -> None:
    if os.environ.get("STRATEGY_LAB_EDC_PARITY") != "1":
        pytest.skip(
            "local XRP EDC parity disabled; export STRATEGY_LAB_EDC_PARITY=1 to run "
            "(ClickHouse + gitignored results/). Not a silent pass."
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


def _load_local_xrp_market() -> EdcM0MarketDataV2:
    from orderbook_analyse.cluster_sweep_research.clickhouse_source import (
        fetch_candles_1m,
        fetch_liquidations,
        fetch_ob_1m,
        fetch_oi_1m,
        fetch_trades_1m,
    )
    from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.shared_strategy.semantics import (
        OUTCOME_PAD_HOURS,
        SOURCE_PAD_HOURS,
        WARMUP_PAD_DAYS,
    )
    from orderbook_analyse.orderbook_v2.ch_client import get_clickhouse_client

    client = get_clickhouse_client()
    warm = timedelta(days=WARMUP_PAD_DAYS)
    out_pad = timedelta(hours=OUTCOME_PAD_HOURS)
    src_pad = timedelta(hours=SOURCE_PAD_HOURS)
    return EdcM0MarketDataV2(
        candles_1m=fetch_candles_1m(
            client, "XRPUSDT", WINDOW_START - warm, WINDOW_END + out_pad
        ),
        trades_1m=fetch_trades_1m(
            client, "XRPUSDT", WINDOW_START - src_pad, WINDOW_END + src_pad
        ),
        orderbook_1m=fetch_ob_1m(
            client, "XRPUSDT", WINDOW_START - src_pad, WINDOW_END + src_pad
        ),
        open_interest_1m=fetch_oi_1m(
            client, "XRPUSDT", WINDOW_START - src_pad, WINDOW_END + src_pad
        ),
        liquidations=fetch_liquidations(
            client, "XRPUSDT", WINDOW_START - src_pad, WINDOW_END + src_pad
        ),
    )


def test_xrp_reference_cell_shape_local() -> None:
    _require_parity_env()
    cands = _ref_supportive_candidates()
    trades = _ref_cell_trades()
    assert len(cands) == 15
    assert len(trades) == 15
    assert {r["candidate_id"] for r in cands} == {r["candidate_id"] for r in trades}
    assert {r["roundtrip_cost_pct"] for r in trades} == {"0.15"}
    gross = sum(Decimal(str(r["gross_pnl_usdt"])) for r in trades)
    net_015 = sum(Decimal(str(r["net_pnl_usdt"])) for r in trades)
    assert gross == Decimal("50.0")
    assert net_015 == Decimal("27.5")
    net_011 = sum(Decimal(str(r["gross_pnl_usdt"])) - Decimal("1.1") for r in trades)
    assert net_011 == Decimal("33.5")
    print("cand_sha256", hashlib.sha256(CAND_CSV.read_bytes()).hexdigest())
    print("trade_sha256", hashlib.sha256(TRADE_CSV.read_bytes()).hexdigest())


def test_xrp_adapter_full_parity_local() -> None:
    _require_parity_env()
    ref_cands = _ref_supportive_candidates()
    ref_trades = {r["candidate_id"]: r for r in _ref_cell_trades()}
    try:
        market = _load_local_xrp_market()
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"ClickHouse market load failed: {exc!r}")

    catalogs = production_catalog_bundle_v2()
    spec = load_strategy_v2_yaml_file(EDC_YAML)
    compiled = compile_strategy_v2(spec, catalogs)
    assert compiled.strategy_hash == HASH_EDC

    result = execute_edc_m0_strict_sync_v2(
        spec,
        compiled,
        catalogs,
        symbol="XRPUSDT",
        start=WINDOW_START,
        end=WINDOW_END,
        market_data=market,
    )

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

    # Derive control PnL from this run (do not hardcode 33.5).
    ref_gross = sum(Decimal(str(r["gross_pnl_usdt"])) for r in ref_trades.values())
    run_gross = result.gross_pnl_usdt
    run_costs = result.costs_usdt
    run_net = result.net_pnl_usdt
    assert run_gross == ref_gross
    assert run_costs == Decimal("1.1") * Decimal(result.trade_count)
    assert run_net == run_gross - run_costs
    assert all(t.costs_usdt == Decimal("1.1") for t in result.trades)
