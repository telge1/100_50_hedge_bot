"""Tests for fight research-db eligibility gates and loaders."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from research.btc_ob_fight.eligibility_contract import (
    CONTEXT_PARTIAL,
    DATA_COMPLETE,
    DATA_CONTRACT_ERROR,
    DATA_NOT_AVAILABLE,
    DATA_PARTIAL_FACTS_ONLY,
    evaluate_eligibility,
    exit_code_for,
)
from research.btc_ob_fight.coverage_gate import evaluate_ob200_coverage, evaluate_oi_coverage, evaluate_trades_coverage
from research.btc_ob_fight.research_db_loader import FORBIDDEN_WRITE, _assert_read_only_sql


def test_data_complete_flags():
    gate = evaluate_eligibility(
        mandatory_statuses={"OB200": "COMPLETE", "PUBLIC_TRADES": "COMPLETE", "PROFILE_TRADES": "COMPLETE"},
        context_statuses={"OPEN_INTEREST": "COMPLETE", "LIQUIDATIONS": "COMPLETE", "CANDLES_1M": "COMPLETE"},
        profile_causality_passed=True,
    )
    assert gate["eligibility_status"] == DATA_COMPLETE
    assert gate["facts_computation_allowed"] is True
    assert gate["interpretation_allowed"] is False
    assert gate["trade_decision_eligible"] is False
    assert gate["rules_frozen"] is False
    assert gate["trade_verdict_evaluated"] is False
    assert gate["direction"] is None


def test_ob200_absent_is_data_not_available_even_if_trades_exist():
    gate = evaluate_eligibility(
        mandatory_statuses={"OB200": "NOT_AVAILABLE", "PUBLIC_TRADES": "COMPLETE", "PROFILE_TRADES": "COMPLETE"},
        context_statuses={"OPEN_INTEREST": "COMPLETE", "LIQUIDATIONS": "COMPLETE", "CANDLES_1M": "COMPLETE"},
        profile_causality_passed=False,
    )
    assert gate["eligibility_status"] == DATA_NOT_AVAILABLE
    assert gate["decision_blocked_reason"] == "OB200_HISTORY_ABSENT"


def test_context_partial_oi():
    gate = evaluate_eligibility(
        mandatory_statuses={"OB200": "COMPLETE", "PUBLIC_TRADES": "COMPLETE", "PROFILE_TRADES": "COMPLETE"},
        context_statuses={"OPEN_INTEREST": "PARTIAL", "LIQUIDATIONS": "COMPLETE", "CANDLES_1M": "COMPLETE"},
        profile_causality_passed=True,
    )
    assert gate["eligibility_status"] == CONTEXT_PARTIAL
    assert gate["facts_computation_allowed"] is True
    assert gate["mandatory_data_complete"] is True
    assert gate["context_data_complete"] is False


def test_partial_ob_mid_hour_gap():
    gate = evaluate_eligibility(
        mandatory_statuses={"OB200": "PARTIAL", "PUBLIC_TRADES": "COMPLETE", "PROFILE_TRADES": "COMPLETE"},
        context_statuses={"OPEN_INTEREST": "COMPLETE", "LIQUIDATIONS": "COMPLETE", "CANDLES_1M": "COMPLETE"},
        profile_causality_passed=True,
    )
    assert gate["eligibility_status"] == DATA_PARTIAL_FACTS_ONLY
    assert gate["trade_decision_eligible"] is False
    assert gate["decision_blocked_reason"] == "MANDATORY_DATA_GAP"


def test_data_not_available():
    gate = evaluate_eligibility(
        mandatory_statuses={
            "OB200": "NOT_AVAILABLE",
            "PUBLIC_TRADES": "NOT_AVAILABLE",
            "PROFILE_TRADES": "NOT_AVAILABLE",
        },
        context_statuses={"OPEN_INTEREST": "NOT_AVAILABLE", "LIQUIDATIONS": "COMPLETE", "CANDLES_1M": "NOT_AVAILABLE"},
        profile_causality_passed=False,
    )
    assert gate["eligibility_status"] == DATA_NOT_AVAILABLE


def test_contract_error():
    gate = evaluate_eligibility(
        mandatory_statuses={"OB200": "COMPLETE", "PUBLIC_TRADES": "COMPLETE", "PROFILE_TRADES": "COMPLETE"},
        context_statuses={"OPEN_INTEREST": "COMPLETE", "LIQUIDATIONS": "COMPLETE", "CANDLES_1M": "COMPLETE"},
        profile_causality_passed=True,
        contract_error="LINEAGE_MISMATCH",
    )
    assert gate["eligibility_status"] == DATA_CONTRACT_ERROR


def test_require_complete_exit_codes():
    assert exit_code_for(DATA_COMPLETE, require_complete=True) == 0
    assert exit_code_for(CONTEXT_PARTIAL, require_complete=True) == 4
    assert exit_code_for(DATA_PARTIAL_FACTS_ONLY, require_complete=True) == 4
    assert exit_code_for(DATA_NOT_AVAILABLE, require_complete=False) == 3
    assert exit_code_for(DATA_CONTRACT_ERROR, require_complete=False) == 5


def test_ob200_unique_seconds_and_gap():
    start = datetime(2026, 8, 27, 6, 0, tzinfo=timezone.utc)
    snaps = []
    for i in range(3600):
        if i == 2543:  # 06:42:23
            continue
        ts = start + timedelta(seconds=i)
        snaps.append(
            {
                "ts": ts,
                "ok": True,
                "genuine_200": True,
                "coverage_status": "PARTIAL",
                "build_id": "x",
                "source_fingerprint": "f",
            }
        )
    cov = evaluate_ob200_coverage(snaps, symbol="BTCUSDT", start=start, end=start + timedelta(hours=1), inclusive_end=False)
    # window [06:00, 07:00) => 3600 expected
    assert cov["expected_units"] == 3600
    assert cov["observed_units"] == 3599
    assert cov["missing_count"] == 1
    assert cov["effective_coverage_status"] == "PARTIAL"
    assert "2026-08-27T06:42:23Z" in (cov["missing_seconds"] or [])


def test_oi_partial_is_context_not_mandatory():
    start = datetime(2026, 8, 18, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    oi_rows = [{"ts": start, "coverage_status": "PARTIAL"}]
    cov = evaluate_oi_coverage(oi_rows, {"expected_samples": 17280, "min_ts": None, "max_ts": None, "coverage_statuses": ["PARTIAL"]}, symbol="BTCUSDT", start=start, end=end)
    assert cov["effective_coverage_status"] == "PARTIAL"
    assert cov["mandatory_for_facts"] is False


def test_null_liquidations_not_gap():
    from research.btc_ob_fight.coverage_gate import evaluate_liq_coverage
    from research.btc_ob_fight.research_db_loader import TimedQuery

    with patch("research.btc_ob_fight.coverage_gate.terminal_batch_status", return_value="READY"):
        cov = evaluate_liq_coverage(
            [],
            {"min_ts": None, "max_ts": None},
            symbol="BTCUSDT",
            start=datetime(2026, 8, 31, tzinfo=timezone.utc),
            end=datetime(2026, 9, 1, tzinfo=timezone.utc),
            client=MagicMock(),
            timer=TimedQuery(),
        )
    assert cov["effective_coverage_status"] == "COMPLETE"
    assert cov["observed_units"] == 0
    assert cov["null_events_are_valid"] is True


def test_trade_dedup_coverage_complete():
    start = datetime(2026, 8, 31, 18, 30, tzinfo=timezone.utc)
    end = datetime(2026, 8, 31, 19, 30, tzinfo=timezone.utc)
    trades = [
        {"ts": start, "trade_id": "1"},
        {"ts": end - timedelta(seconds=1), "trade_id": "2"},
    ]
    cov = evaluate_trades_coverage(
        trades,
        {"min_ts": "2026-08-31T18:30:00Z", "max_ts": "2026-08-31T19:29:59Z", "source_mode": "X"},
        symbol="BTCUSDT",
        start=start,
        end=end,
    )
    assert cov["effective_coverage_status"] == "COMPLETE"


def test_forbid_write_sql():
    with pytest.raises(Exception):
        _assert_read_only_sql("ALTER TABLE x DELETE WHERE 1")
    _assert_read_only_sql("SELECT count() FROM btc_doge_research.research_ob200_snapshots_1s")


def test_no_raw_fallback_in_research_loader_module():
    import research.btc_ob_fight.research_db_loader as m
    src = open(m.__file__, encoding="utf-8").read()
    assert "import zstandard" not in src
    assert "ob200_v3" not in src
    assert "iter_ndjson" not in src
    assert "replay_hour_at_cutoffs" not in src
    assert FORBIDDEN_WRITE


def test_cli_defaults_research_db():
    from research.btc_ob_fight.cli import build_parser

    p = build_parser()
    args = p.parse_args(["--timestamp", "2026-08-31T19:00:00Z"])
    assert args.data_source == "research-db"
    assert args.symbol == "BTCUSDT"


def test_public_trades_absent_is_research_trade_events_missing():
    gate = evaluate_eligibility(
        mandatory_statuses={"OB200": "COMPLETE", "PUBLIC_TRADES": "NOT_AVAILABLE", "PROFILE_TRADES": "NOT_AVAILABLE"},
        context_statuses={"OPEN_INTEREST": "COMPLETE", "LIQUIDATIONS": "COMPLETE", "CANDLES_1M": "COMPLETE"},
        profile_causality_passed=True,
    )
    assert gate["eligibility_status"] == DATA_NOT_AVAILABLE
    assert gate["decision_blocked_reason"] == "RESEARCH_TRADE_EVENTS_MISSING"


def test_instrument_contract_btc_doge_ticks():
    from research.btc_ob_fight.instrument_contract import instrument_for, price_to_tick, tick_to_price

    btc = instrument_for("BTCUSDT")
    doge = instrument_for("DOGEUSDT")
    assert float(btc.tick_size) == 0.1
    assert float(doge.tick_size) == 0.00001
    assert price_to_tick(78545.0, "BTCUSDT") == 785450
    assert abs(tick_to_price(price_to_tick(0.08267, "DOGEUSDT"), "DOGEUSDT") - 0.08267) < 1e-8


def test_active_symbol_switches_edge_ticks():
    from research.btc_ob_fight.profile_edge_state import price_to_tick, set_active_symbol

    set_active_symbol("BTCUSDT")
    assert price_to_tick(100.0) == 1000
    set_active_symbol("DOGEUSDT")
    assert price_to_tick(0.1) == 10000


def test_loader_default_has_no_companion_string_path_when_events_missing():
    import inspect
    from research.btc_ob_fight.research_db_loader import load_public_trades

    src = inspect.getsource(load_public_trades)
    assert "allow_legacy_trade_companion" in src
    assert "RESEARCH_TRADE_EVENTS_MISSING" in src


def test_cli_has_purity_and_benchmark_flags():
    from research.btc_ob_fight.cli import build_parser

    p = build_parser()
    args = p.parse_args(
        [
            "--timestamp",
            "2026-08-31T19:00:00Z",
            "--coverage-only",
            "--benchmark",
        ]
    )
    assert args.coverage_only is True
    assert args.benchmark is True
    assert args.allow_legacy_trade_companion is False
    assert args.heavy_detail_csv is False


def test_cli_heavy_detail_csv_flag_defaults_false_and_opt_in():
    from research.btc_ob_fight.cli import build_parser, validate_args

    p = build_parser()
    lean = p.parse_args(["--timestamp", "2026-08-31T19:00:00Z"])
    assert lean.heavy_detail_csv is False
    cfg_lean = validate_args(lean)
    assert cfg_lean.heavy_detail_csv is False

    heavy = p.parse_args(["--timestamp", "2026-08-31T19:00:00Z", "--heavy-detail-csv"])
    assert heavy.heavy_detail_csv is True
    cfg_heavy = validate_args(heavy)
    assert cfg_heavy.heavy_detail_csv is True
