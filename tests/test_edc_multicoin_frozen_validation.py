"""Synthetic unit tests for multicoin frozen validation (no ClickHouse / no market run)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.aggregations import (
    equal_weight_per_coin,
    leave_one_coin_out,
)
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.checkpoint import (
    IncompatibleCheckpointError,
    is_complete_checkpoint,
    symbols_to_process,
    write_coin_checkpoint,
    write_coin_failure,
)
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.cli import (
    build_parser,
    main,
)
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.candidate_coverage import (
    classify_liq_feed,
    classify_oi_window,
    liq_status_at_decision,
    listing_audit,
    local_series_status,
    oi_status_at_decision,
    refine_coverage_dict,
)
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.constants import (
    DEFAULT_END,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_START,
    DEFAULT_SYMBOLS_FILE,
    ELIGIBILITY_MEANS_THRESHOLD_PASS_NOT_COMPLETE_COVERAGE,
    ELIGIBILITY_THRESHOLDS,
    ELIGIBLE_CORE_30D,
    ELIGIBLE_CORE_PARTIAL,
    ENTRY_RULE,
    INELIGIBLE_CORE,
    LISTING_LIMITED,
    PRIMARY_CELLS,
    PRIMARY_REFERENCE_CELL_ID,
)
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.coverage import (
    classify_coverage,
    select_eligible_for_main,
)
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.resources import (
    resource_snapshot,
)
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.entry import (
    assert_no_future_features,
    first_1m_open_at_or_after,
)
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.universe import (
    apply_limit_symbols,
    audit_universe,
    load_universe,
)
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.xrp_parity import (
    compare_xrp_candidates_to_export,
    frozen_cells_match_xrp_matrix_defs,
)
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.tpsl_pnl_engine import (
    apply_costs,
    simulate_tpsl_trade,
)
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.core_sources_research_policy import (
    apply_core_sources_research,
)


REPO = Path(__file__).resolve().parents[1]
UNIVERSE = REPO / "config" / "universe_tradeable_51.json"


def test_01_universe_exact_unique():
    uni = load_universe(UNIVERSE)
    audit = audit_universe(uni["symbols"], expected_n=51)
    assert audit["ok"]
    assert audit["unique"]
    assert audit["n_symbols"] == 51
    assert len(set(uni["symbols"])) == 51


def test_02_no_performance_based_filtering():
    rows = [
        {
            "symbol": "AAAUSDT",
            "coverage_class": ELIGIBLE_CORE_30D,
            "net_pnl_usdt": -999.0,  # must not affect eligibility
        },
        {
            "symbol": "BBBUSDT",
            "coverage_class": INELIGIBLE_CORE,
            "net_pnl_usdt": 999.0,
        },
        {
            "symbol": "CCCUSDT",
            "coverage_class": ELIGIBLE_CORE_PARTIAL,
            "net_pnl_usdt": 50.0,
        },
    ]
    eligible = select_eligible_for_main(rows)
    assert eligible == ["AAAUSDT"]
    assert "BBBUSDT" not in eligible  # positive PnL does not admit
    assert apply_limit_symbols(["A", "B", "C"], 2) == ["A", "B"]


def _listing_known(ts, *, limited=False):
    return {
        "listing_status": "KNOWN",
        "listing_first_ts": ts.isoformat() if hasattr(ts, "isoformat") else ts,
        "listing_limited": limited,
        "listing_limited_known": True,
        "listing_note": None,
    }


def _listing_unknown():
    return {
        "listing_status": "UNKNOWN",
        "listing_first_ts": None,
        "listing_limited": None,
        "listing_limited_known": False,
        "listing_note": "earliest_candle_unavailable",
    }


def _cc_kwargs(**overrides):
    start = datetime(2026, 7, 24, tzinfo=timezone.utc)
    end = datetime(2026, 8, 23, tzinfo=timezone.utc)
    exp = int((end - start).total_seconds() // 60)
    base = dict(
        symbol="XRPUSDT",
        start=start,
        end=end,
        candle_minutes=exp,
        trades_minutes=exp,
        ob_minutes=exp,
        outcome_minutes=exp,
        oi_minutes=0,
        oi_first_ts=None,
        oi_last_ts=None,
        liq_feed_first_ts=None,
        liq_feed_last_ts=None,
        liquidation_events=0,
        warmup_bars_available=100,
        warmup_bars_required=79,
        listing_audit_result=_listing_known(start - timedelta(days=100)),
    )
    base.update(overrides)
    return base


def test_03_coverage_classification():
    start = datetime(2026, 7, 24, tzinfo=timezone.utc)
    end = datetime(2026, 8, 23, tzinfo=timezone.utc)
    exp = int((end - start).total_seconds() // 60)

    full = classify_coverage(**_cc_kwargs(candle_minutes=int(exp * 0.99), trades_minutes=int(exp * 0.90), ob_minutes=int(exp * 0.90), outcome_minutes=int(exp * 0.99)))
    assert full["coverage_class"] == ELIGIBLE_CORE_30D
    assert full["oi_window_status"] == "MISSING"
    assert full["liq_feed_coverage_status"] == "MISSING"
    assert full["eligibility_means_threshold_pass_not_complete_coverage"] is True
    assert full["performance_used_for_eligibility"] is False

    partial = classify_coverage(
        **_cc_kwargs(
            symbol="AAAUSDT",
            candle_minutes=int(exp * 0.70),
            trades_minutes=1000,
            ob_minutes=1000,
            outcome_minutes=int(exp * 0.70),
            oi_minutes=10,
            oi_first_ts=start + timedelta(days=20),
            oi_last_ts=end - timedelta(hours=1),
            liq_feed_first_ts=start + timedelta(days=25),
            liq_feed_last_ts=end - timedelta(hours=1),
            liquidation_events=1,
            listing_audit_result=_listing_unknown(),
        )
    )
    assert partial["coverage_class"] == ELIGIBLE_CORE_PARTIAL
    assert partial["oi_window_status"] == "PARTIAL"

    listing = classify_coverage(
        **_cc_kwargs(
            symbol="NEWUSDT",
            candle_minutes=100,
            trades_minutes=100,
            ob_minutes=100,
            outcome_minutes=100,
            listing_audit_result=_listing_known(start + timedelta(days=10), limited=True),
        )
    )
    assert listing["coverage_class"] == LISTING_LIMITED

    bad = classify_coverage(
        **_cc_kwargs(
            symbol="BADUSDT",
            candle_minutes=0,
            trades_minutes=0,
            ob_minutes=0,
            outcome_minutes=0,
            warmup_bars_available=10,
            listing_audit_result=_listing_unknown(),
        )
    )
    assert bad["coverage_class"] == INELIGIBLE_CORE


def test_04_warmup_requirement():
    no_warm = classify_coverage(**_cc_kwargs(warmup_bars_available=10))
    assert no_warm["warmup_ok"] is False
    assert no_warm["coverage_class"] != ELIGIBLE_CORE_30D


def test_05_entry_at_or_after_decision():
    t0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    rows = []
    for i in range(5):
        rows.append(
            {
                "open_time": (t0 + timedelta(minutes=i)).replace(tzinfo=None),
                "open": 1.0 + i * 0.01,
                "high": 1.02,
                "low": 0.99,
                "close": 1.0,
                "volume": 1,
            }
        )
    df = pd.DataFrame(rows)
    decision = t0 + timedelta(minutes=1)  # 12:01
    entry_at, px = first_1m_open_at_or_after(df, decision)
    # exact minute at decision_at is chosen
    assert entry_at == decision
    assert px == pytest.approx(1.01)

    # never choose open before decision_at
    before_only = pd.DataFrame(
        [
            {
                "open_time": (decision - timedelta(minutes=1)).replace(tzinfo=None),
                "open": 9.99,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume": 1,
            }
        ]
    )
    assert first_1m_open_at_or_after(before_only, decision) == (None, None)

    # if exact minute missing, first later open
    later_only = pd.DataFrame(
        [
            {
                "open_time": (decision + timedelta(minutes=2)).replace(tzinfo=None),
                "open": 1.07,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume": 1,
            }
        ]
    )
    lat, lpx = first_1m_open_at_or_after(later_only, decision)
    assert lat == decision + timedelta(minutes=2)
    assert lpx == pytest.approx(1.07)

    # when exact minute present, do not skip to a later open
    with_exact_and_later = pd.DataFrame(
        [
            {
                "open_time": decision.replace(tzinfo=None),
                "open": 1.01,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume": 1,
            },
            {
                "open_time": (decision + timedelta(minutes=3)).replace(tzinfo=None),
                "open": 1.99,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume": 1,
            },
        ]
    )
    e2, p2 = first_1m_open_at_or_after(with_exact_and_later, decision)
    assert e2 == decision
    assert p2 == pytest.approx(1.01)


def test_05b_entry_long_short_identical_resolver():
    t0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    df = pd.DataFrame(
        [
            {
                "open_time": t0.replace(tzinfo=None),
                "open": 1.2345,
                "high": 1.3,
                "low": 1.1,
                "close": 1.2,
                "volume": 1,
            }
        ]
    )
    # resolver is direction-agnostic — same timestamps/prices for both sides
    a_ts, a_px = first_1m_open_at_or_after(df, t0)
    b_ts, b_px = first_1m_open_at_or_after(df, t0)
    assert a_ts == b_ts == t0
    assert a_px == b_px == pytest.approx(1.2345)


def test_06_no_future_data():
    dec = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    assert assert_no_future_features(dec, dec) is True
    assert assert_no_future_features(dec, dec - timedelta(minutes=1)) is True
    assert assert_no_future_features(dec, dec + timedelta(seconds=1)) is False


def test_07_long_short_symmetry():
    t0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    # path that hits +0.6% for long / mirrored for short
    df = pd.DataFrame(
        [
            {
                "open_time": t0.replace(tzinfo=None),
                "open": 1.0,
                "high": 1.006,
                "low": 0.994,
                "close": 1.0,
                "volume": 1,
            }
        ]
    )
    long = simulate_tpsl_trade(df, direction="BULLISH", entry_at=t0, entry_price=1.0, tp_pct=0.60, sl_pct=0.50, horizon_min=60)
    # mirrored short path: invert high/low around entry
    df_s = pd.DataFrame(
        [
            {
                "open_time": t0.replace(tzinfo=None),
                "open": 1.0,
                "high": 1.006,
                "low": 0.994,
                "close": 1.0,
                "volume": 1,
            }
        ]
    )
    short = simulate_tpsl_trade(df_s, direction="BEARISH", entry_at=t0, entry_price=1.0, tp_pct=0.60, sl_pct=0.50, horizon_min=60)
    # both TP and SL touched same bar -> SL_FIRST both sides
    assert long["exit_reason"] == "SL_EXIT"
    assert short["exit_reason"] == "SL_EXIT"
    assert long["gross_return_pct"] == pytest.approx(short["gross_return_pct"])


def test_08_sl_first():
    t0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    df = pd.DataFrame(
        [
            {
                "open_time": t0.replace(tzinfo=None),
                "open": 1.0,
                "high": 1.01,
                "low": 0.99,
                "close": 1.0,
                "volume": 1,
            }
        ]
    )
    r = simulate_tpsl_trade(df, direction="BULLISH", entry_at=t0, entry_price=1.0, tp_pct=0.60, sl_pct=0.50, horizon_min=60)
    assert r["exit_reason"] == "SL_EXIT"
    assert r["same_bar_conflict"] is True


def test_09_time_exit():
    t0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    rows = []
    for i in range(3):
        rows.append(
            {
                "open_time": (t0 + timedelta(minutes=i)).replace(tzinfo=None),
                "open": 1.0,
                "high": 1.0001,
                "low": 0.9999,
                "close": 1.001,
                "volume": 1,
            }
        )
    df = pd.DataFrame(rows)
    r = simulate_tpsl_trade(df, direction="BULLISH", entry_at=t0, entry_price=1.0, tp_pct=0.60, sl_pct=0.50, horizon_min=3)
    assert r["exit_reason"] == "TIME_EXIT"
    assert r["exit_price"] == pytest.approx(1.001)


def test_10_costs_once():
    trade = {"gross_return_pct": 1.0}
    paid = apply_costs(trade, 0.15)
    assert paid["net_return_pct"] == pytest.approx(0.85)
    assert paid["costs_usdt"] == pytest.approx(1.5)
    # applying costs formula once — no double subtract in engine
    assert paid["net_pnl_usdt"] == pytest.approx(paid["gross_pnl_usdt"] - paid["costs_usdt"])


def test_11_research_production_separated():
    # Missing core -> research INSUFFICIENT, never ALLOW
    cov = {
        "candles": {"status": "VALID"},
        "public_trades_cross": {"status": "MISSING"},
        "orderbook_ob200_v3": {"status": "VALID"},
        "liquidity_locations": {"status": "VALID"},
        "open_interest": {"status": "MISSING"},
        "liquidations": {"status": "MISSING"},
    }
    feats = {}
    core, _ = apply_core_sources_research(direction="BULLISH", features=feats, coverage=cov)
    assert core.startswith("CORE_RESEARCH_")
    assert core != "ALLOW"
    assert "ALLOW" not in core


def test_12_missing_stays_missing():
    row = classify_coverage(**_cc_kwargs())
    assert row["oi_window_status"] == "MISSING"
    assert row["oi_treated_as"] == "MISSING"
    assert row["liq_feed_coverage_status"] == "MISSING"
    assert row["liq_treated_as"] == "MISSING"
    assert row["oi_treated_as"] != "NEUTRAL"


def test_13_coin_checkpoint_atomic(tmp_path: Path):
    cp = tmp_path / "checkpoints"
    cp.mkdir()
    path = write_coin_checkpoint(cp, symbol="xrpusdt", status="COMPLETE", payload={"n_trades": 3})
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["status"] == "COMPLETE"
    assert data["symbol"] == "XRPUSDT"
    assert data["entry_rule"] == ENTRY_RULE
    assert is_complete_checkpoint(data)


def test_14_resume_skips_complete(tmp_path: Path):
    cp = tmp_path / "checkpoints"
    cp.mkdir()
    write_coin_checkpoint(cp, symbol="AAAUSDT", status="COMPLETE", payload={})
    write_coin_checkpoint(cp, symbol="BBBUSDT", status="FAILED", payload={"error": "x"})
    todo, skipped = symbols_to_process(["AAAUSDT", "BBBUSDT", "CCCUSDT"], cp, resume=True)
    assert skipped == ["AAAUSDT"]
    assert todo == ["BBBUSDT", "CCCUSDT"]
    todo2, skipped2 = symbols_to_process(["AAAUSDT", "BBBUSDT"], cp, resume=False)
    assert skipped2 == []
    assert todo2 == ["AAAUSDT", "BBBUSDT"]


def test_14b_resume_rejects_legacy_entry_checkpoint(tmp_path: Path):
    cp = tmp_path / "checkpoints"
    cp.mkdir()
    # Simulate legacy >-semantics checkpoint (no migration)
    legacy = {
        "schema_version": 1,
        "symbol": "AAAUSDT",
        "status": "COMPLETE",
        "entry_rule": "FIRST_1M_OPEN_STRICTLY_AFTER_DECISION_AT",
        "n_trades": 1,
    }
    (cp / "AAAUSDT.json").write_text(json.dumps(legacy) + "\n", encoding="utf-8")
    with pytest.raises(IncompatibleCheckpointError):
        symbols_to_process(["AAAUSDT"], cp, resume=True)
    # Missing entry_rule also incompatible
    bare = {"schema_version": 1, "symbol": "BBBUSDT", "status": "COMPLETE"}
    (cp / "BBBUSDT.json").write_text(json.dumps(bare) + "\n", encoding="utf-8")
    with pytest.raises(IncompatibleCheckpointError):
        symbols_to_process(["BBBUSDT"], cp, resume=True)


def test_15_failure_isolation(tmp_path: Path):
    fail = tmp_path / "failures"
    fail.mkdir()
    write_coin_failure(fail, symbol="BADUSDT", error="boom", detail={"x": 1})
    assert (fail / "BADUSDT.json").exists()
    # other coins unaffected: writing success checkpoint still works
    cp = tmp_path / "checkpoints"
    cp.mkdir()
    write_coin_checkpoint(cp, symbol="OKUSDT", status="COMPLETE", payload={})
    assert is_complete_checkpoint(json.loads((cp / "OKUSDT.json").read_text()))


def test_16_equal_weight_aggregation():
    coin_rows = [
        {"symbol": "A", "avg_net_pnl_usdt": 10.0, "net_pnl_usdt": 100.0, "profit_factor_net": 2.0},
        {"symbol": "B", "avg_net_pnl_usdt": -10.0, "net_pnl_usdt": -20.0, "profit_factor_net": 0.5},
    ]
    ew = equal_weight_per_coin(coin_rows)
    assert ew["mean_coin_expectancy_usdt"] == pytest.approx(0.0)
    assert ew["mean_coin_net_pnl_usdt"] == pytest.approx(40.0)
    assert ew["n_coins"] == 2


def test_17_leave_one_coin_out():
    coin_rows = [
        {"symbol": "A", "avg_net_pnl_usdt": 1.0, "net_pnl_usdt": 10.0},
        {"symbol": "B", "avg_net_pnl_usdt": 2.0, "net_pnl_usdt": 20.0},
        {"symbol": "C", "avg_net_pnl_usdt": 3.0, "net_pnl_usdt": 30.0},
    ]
    loo = leave_one_coin_out(coin_rows)
    assert len(loo) == 3
    left_a = next(r for r in loo if r["left_out_symbol"] == "A")
    assert left_a["remaining_pooled_net_pnl_usdt"] == pytest.approx(50.0)
    assert left_a["remaining_mean_expectancy_usdt"] == pytest.approx(2.5)


def test_18_xrp_parity_frozen_defs():
    parity = frozen_cells_match_xrp_matrix_defs()
    assert parity["cells_match"] is True
    assert parity["reference_is_tp075_sl050_8h"] is True
    assert PRIMARY_REFERENCE_CELL_ID == "M0_TP075_SL050_H8"
    assert len(PRIMARY_CELLS) == 4
    # Engine parity on export entry: recompute one SL_FIRST trade cost path
    t0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    df = pd.DataFrame(
        [
            {
                "open_time": t0.replace(tzinfo=None),
                "open": 1.0,
                "high": 1.01,
                "low": 0.99,
                "close": 1.0,
                "volume": 1,
            }
        ]
    )
    sim = simulate_tpsl_trade(df, direction="BULLISH", entry_at=t0, entry_price=1.0, tp_pct=0.75, sl_pct=0.50, horizon_min=480)
    paid = apply_costs(sim, 0.15)
    assert paid["exit_reason"] == "SL_EXIT"
    assert paid["net_pnl_usdt"] == pytest.approx(-6.5)  # -0.50% - 0.15% on 1000


def test_cli_requires_mode():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_cli_run_resume_exclusive_via_mutex():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--run", "--resume"])


def test_dry_run_no_clickhouse(tmp_path: Path, monkeypatch):
    # Ensure dry-run does not import/call default_client
    out = tmp_path / "out"
    rc = main(
        [
            "--dry-run",
            "--symbols-file",
            str(UNIVERSE),
            "--output-dir",
            str(out),
            "--limit-symbols",
            "3",
        ]
    )
    assert rc == 0
    plan = json.loads((out / "preflight" / "dry_run_plan.json").read_text())
    assert plan["clickhouse_queries"] is False
    assert plan["n_symbols_planned"] == 3
    assert plan["entry_rule"] == ENTRY_RULE
    assert ENTRY_RULE == "SIGNAL_TF_NEXT_OPEN_AFTER_SIGNAL_BAR"
    manifest = json.loads((out / "run_manifest.json").read_text())
    assert manifest["entry_rule"] == ENTRY_RULE
    assert (out / "run_manifest.json").exists()


def test_cli_defaults_match_spec():
    parser = build_parser()
    # defaults without mode would fail required group; inspect actions
    actions = {a.dest: a for a in parser._actions}
    assert actions["symbols_file"].default == "config/universe_tradeable_51.json"
    assert actions["symbols_file"].default == DEFAULT_SYMBOLS_FILE
    assert actions["start"].default == "2026-07-24T00:00:00Z"
    assert actions["end"].default == "2026-08-23T00:00:00Z"
    assert actions["output_dir"].default == "results/edc_sync_tolerance/multicoin_30d_frozen_validation"
    assert actions["output_dir"].default == DEFAULT_OUTPUT_DIR
    assert actions["max_workers"].default == 1
    assert actions["checkpoint_every"].default == 1
    assert DEFAULT_START.isoformat().replace("+00:00", "Z") == "2026-07-24T00:00:00Z"
    assert DEFAULT_END.isoformat().replace("+00:00", "Z") == "2026-08-23T00:00:00Z"


def test_xrp_candidate_parity_function_synthetic():
    export = [
        {
            "candidate_id": "edc:1",
            "symbol": "XRPUSDT",
            "timeframe": "5m",
            "mode_id": "M0_STRICT_SYNC",
            "direction": "BULLISH",
            "decision_at": "2026-07-24T02:35:00+00:00",
            "entry_at": "2026-07-24T02:35:00+00:00",
            "entry_price": "1.1056",
            "core_research_verdict": "CORE_RESEARCH_SUPPORTIVE",
        }
    ]
    produced_ok = [dict(export[0], entry_price=1.1056)]
    ok = compare_xrp_candidates_to_export(produced_ok, export)
    assert ok["ok"] is True
    produced_bad = [dict(export[0], entry_at="2026-07-24T02:36:00+00:00", entry_price=1.1056)]
    bad = compare_xrp_candidates_to_export(produced_bad, export)
    assert bad["ok"] is False
    assert bad["status"] == "FAILED_PARITY"


def test_static_audit_no_strict_after_entry_path():
    pkg = REPO / "src/orderbook_analyse/ema_dual_cross_multisource/tolerance_research/multicoin_frozen_validation"
    offenders = []
    for path in pkg.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "tcol > pd.Timestamp" in text or "open_time > decision_at" in text:
            offenders.append(str(path))
        if "first_1m_open_strictly_after" in text:
            offenders.append(f"{path}:legacy_fn")
    assert offenders == []
    assert ENTRY_RULE == "SIGNAL_TF_NEXT_OPEN_AFTER_SIGNAL_BAR"
    # productive entry module uses >=
    entry_src = (pkg / "entry.py").read_text(encoding="utf-8")
    assert "tcol >= pd.Timestamp" in entry_src
    assert "tcol > pd.Timestamp" not in entry_src


def test_coverage_oi_partial_not_full():
    start = datetime(2026, 7, 24, tzinfo=timezone.utc)
    end = datetime(2026, 8, 23, tzinfo=timezone.utc)
    exp = int((end - start).total_seconds() // 60)
    oi = classify_oi_window(
        oi_minutes=6240,
        expected_minutes=exp,
        oi_first_ts=start + timedelta(days=20),
        oi_last_ts=end - timedelta(hours=1),
        window_start=start,
        window_end=end,
    )
    assert oi["oi_window_status"] == "PARTIAL"
    assert oi["oi_window_status"] != "FULL"
    assert oi["oi_treated_as"] != "NEUTRAL"


def test_coverage_oi_at_decision_missing_before_start():
    oi_first = datetime(2026, 8, 18, tzinfo=timezone.utc)
    oi_last = datetime(2026, 8, 22, 23, tzinfo=timezone.utc)
    assert oi_status_at_decision(datetime(2026, 8, 1, tzinfo=timezone.utc), oi_first_ts=oi_first, oi_last_ts=oi_last) == "MISSING"
    assert oi_status_at_decision(datetime(2026, 8, 20, tzinfo=timezone.utc), oi_first_ts=oi_first, oi_last_ts=oi_last, has_rows_in_feature_window=True) == "VALID"


def test_coverage_liq_feed_missing_vs_valid_empty():
    start = datetime(2026, 7, 24, tzinfo=timezone.utc)
    end = datetime(2026, 8, 23, tzinfo=timezone.utc)
    missing = classify_liq_feed(
        liq_feed_first_ts=None,
        liq_feed_last_ts=None,
        liquidation_events=0,
        window_start=start,
        window_end=end,
    )
    assert missing["liq_feed_coverage_status"] == "MISSING"
    feed_first = datetime(2026, 8, 18, tzinfo=timezone.utc)
    feed_last = datetime(2026, 8, 22, tzinfo=timezone.utc)
    assert liq_status_at_decision(datetime(2026, 8, 1, tzinfo=timezone.utc), liq_feed_first_ts=feed_first, liq_feed_last_ts=feed_last, events_in_feature_window=0) == "MISSING"
    assert liq_status_at_decision(datetime(2026, 8, 20, tzinfo=timezone.utc), liq_feed_first_ts=feed_first, liq_feed_last_ts=feed_last, events_in_feature_window=0) == "VALID_EMPTY"
    assert liq_status_at_decision(datetime(2026, 8, 20, tzinfo=timezone.utc), liq_feed_first_ts=feed_first, liq_feed_last_ts=feed_last, events_in_feature_window=3) == "VALID_DATA"


def test_coverage_local_ob_gap_insufficient():
    dec = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    # global-looking series but gap before decision (last row stale / missing locally)
    early = pd.DataFrame(
        {
            "minute": [dec - timedelta(days=2) + timedelta(minutes=i) for i in range(10)],
            "imbalance_l50": [0.1] * 10,
        }
    )
    st = local_series_status(early, time_col="minute", decision_at=dec, min_points=5, stale_minutes=5)
    assert st in ("MISSING", "STALE", "INSUFFICIENT")
    cov = {"orderbook_ob200_v3": {"status": "VALID"}, "public_trades_cross": {"status": "VALID"}, "open_interest": {"status": "VALID"}, "liquidations": {"status": "VALID"}}
    refined = refine_coverage_dict(
        cov,
        decision_at=dec,
        feed_meta={"oi_first_ts": dec - timedelta(days=1), "oi_last_ts": dec + timedelta(days=1), "liq_feed_first_ts": dec - timedelta(days=1), "liq_feed_last_ts": dec + timedelta(days=1)},
        trades_1m=early.rename(columns={"imbalance_l50": "trade_count"}),
        ob_1m=early,
    )
    assert refined["core_local_insufficient"] is True
    assert refined["orderbook_ob200_v3"]["status"] in ("MISSING", "STALE")


def test_incomplete_8h_horizon_not_time_exit():
    t0 = datetime(2026, 8, 22, 20, 0, tzinfo=timezone.utc)
    # only 60 minutes of path for an 8h (=480m) horizon
    rows = []
    for i in range(60):
        rows.append(
            {
                "open_time": (t0 + timedelta(minutes=i)).replace(tzinfo=None),
                "open": 1.0,
                "high": 1.0001,
                "low": 0.9999,
                "close": 1.0,
                "volume": 1,
            }
        )
    df = pd.DataFrame(rows)
    r = simulate_tpsl_trade(
        df,
        direction="BULLISH",
        entry_at=t0,
        entry_price=1.0,
        tp_pct=0.75,
        sl_pct=0.50,
        horizon_min=480,
        require_full_horizon=True,
    )
    assert r["exit_reason"] == "INCOMPLETE_OUTCOME_HORIZON"
    assert r["gross_return_pct"] is None
    assert r["include_in_primary_pnl"] is False


def test_listing_unknown_not_false_limited():
    start = datetime(2026, 7, 24, tzinfo=timezone.utc)
    unk = listing_audit(earliest_candle_unbounded=start, window_start=start, window_bounded_first_ts=start)
    assert unk["listing_status"] == "UNKNOWN"
    assert unk["listing_limited"] is None
    assert unk["listing_limited_known"] is False
    known = listing_audit(
        earliest_candle_unbounded=start - timedelta(days=200),
        window_start=start,
        window_bounded_first_ts=start,
    )
    assert known["listing_status"] == "KNOWN"
    assert known["listing_limited"] is False


def test_eligibility_thresholds_in_manifest_dry_run(tmp_path: Path):
    out = tmp_path / "out"
    assert main(["--dry-run", "--symbols-file", str(UNIVERSE), "--output-dir", str(out), "--limit-symbols", "1"]) == 0
    plan = json.loads((out / "preflight" / "dry_run_plan.json").read_text())
    assert plan["eligibility_means_threshold_pass_not_complete_coverage"] is True
    assert plan["eligibility_thresholds"]["candles_coverage_ratio_min"] == ELIGIBILITY_THRESHOLDS["candles_coverage_ratio_min"]
    assert ELIGIBILITY_MEANS_THRESHOLD_PASS_NOT_COMPLETE_COVERAGE is True


def test_resource_snapshot_without_psutil(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "psutil" or name.startswith("psutil."):
            raise ModuleNotFoundError("No module named psutil")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    snap = resource_snapshot()
    assert snap.get("status") in ("OK_FALLBACK", "RESOURCE_METRICS_UNAVAILABLE") or snap.get("source") == "fallback_/proc_shutil"
    assert "error" not in snap or snap.get("status") != "crash"
