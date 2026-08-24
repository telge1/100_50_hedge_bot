"""Offline unit tests for EDC profitability diagnosis (P2E1).

Synthetic fixtures only — no ClickHouse, no network, no full result runs.
"""

from __future__ import annotations

import csv
import hashlib
import importlib
import io
import json
from decimal import Decimal
from pathlib import Path

import pytest

from orderbook_analyse.strategy_lab.analysis.edc_profitability_v2 import (
    StrategyProfitabilityError,
    analyze_edc_profitability_v2,
    build_arg_parser,
    sample_size_bucket,
    scenario_net_usdt,
    wilson_interval,
)


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _trade(
    *,
    symbol: str,
    eid: str,
    side: str,
    decision_time: str,
    net: str,
    gross: str | None = None,
) -> dict[str, str]:
    net_d = Decimal(net)
    if gross is None:
        # simple accounting: costs 1.1 on 1000@0.11%
        gross_d = net_d + Decimal("1.1")
    else:
        gross_d = Decimal(gross)
    costs = gross_d - net_d
    return {
        "strategy_hash": "abc",
        "plugin_id": "edc_m0",
        "symbol": symbol,
        "source_event_id": eid,
        "side": side,
        "decision_time": decision_time,
        "entry_time": decision_time,
        "entry_price": "1",
        "exit_time": decision_time,
        "exit_price": "1",
        "exit_reason": "tp",
        "gross_return_pct": "0",
        "roundtrip_cost_pct": "0.11",
        "net_return_pct": "0",
        "gross_pnl_usdt": str(gross_d),
        "costs_usdt": str(costs),
        "net_pnl_usdt": str(net_d),
        "mode_id": "m0",
        "confirmation_policy": "strict",
    }


def _enr(
    *,
    symbol: str,
    eid: str,
    direction: str,
    decision_at: str,
    atr: str = "1.0",
    causal_atr: str = "True",
    cov_atr: str = "OK",
    unresolved_val: str = "9",
    unresolved_causal: str = "False",
    label_net: str = "1",
) -> dict[str, str]:
    return {
        "candidate_id": eid,
        "symbol": symbol,
        "setup_id": "s1",
        "feature__symbol": symbol,
        "feature__symbol__causal": "True",
        "feature__symbol__coverage_status": "OK",
        "feature__symbol__missing_reason": "",
        "feature__symbol__feature_asof": decision_at,
        "feature__symbol__source_table": "identity",
        "feature__candidate_id": eid,
        "feature__candidate_id__causal": "True",
        "feature__candidate_id__coverage_status": "OK",
        "feature__candidate_id__missing_reason": "",
        "feature__candidate_id__feature_asof": decision_at,
        "feature__candidate_id__source_table": "identity",
        "feature__direction": direction,
        "feature__direction__causal": "True",
        "feature__direction__coverage_status": "OK",
        "feature__direction__missing_reason": "",
        "feature__direction__feature_asof": decision_at,
        "feature__direction__source_table": "identity",
        "feature__decision_at": decision_at,
        "feature__decision_at__causal": "True",
        "feature__decision_at__coverage_status": "OK",
        "feature__decision_at__missing_reason": "",
        "feature__decision_at__feature_asof": decision_at,
        "feature__decision_at__source_table": "identity",
        "feature__entry_at": decision_at,
        "feature__entry_at__causal": "True",
        "feature__entry_at__coverage_status": "OK",
        "feature__entry_at__missing_reason": "",
        "feature__entry_at__feature_asof": decision_at,
        "feature__entry_at__source_table": "identity",
        "feature__atr14_pct": atr,
        "feature__atr14_pct__causal": causal_atr,
        "feature__atr14_pct__coverage_status": cov_atr,
        "feature__atr14_pct__missing_reason": "",
        "feature__atr14_pct__feature_asof": decision_at,
        "feature__atr14_pct__source_table": "ohlcv",
        "feature__lld_score": unresolved_val,
        "feature__lld_score__causal": unresolved_causal,
        "feature__lld_score__coverage_status": "CAUSALITY_UNPROVEN",
        "feature__lld_score__missing_reason": "",
        "feature__lld_score__feature_asof": "",
        "feature__lld_score__source_table": "lld",
        "feature__tp_pct": "1.5",
        "feature__tp_pct__causal": "True",
        "feature__tp_pct__coverage_status": "OK",
        "feature__tp_pct__missing_reason": "",
        "feature__tp_pct__feature_asof": decision_at,
        "feature__tp_pct__source_table": "strategy",
        "label__net_pnl_usdt": label_net,
        "label__mfe_pct": "2",
        "label__exit_reason": "tp",
    }


def _coin(symbol: str, trades: list[dict[str, str]]) -> dict[str, str]:
    gross = sum(Decimal(t["gross_pnl_usdt"]) for t in trades)
    costs = sum(Decimal(t["costs_usdt"]) for t in trades)
    net = sum(Decimal(t["net_pnl_usdt"]) for t in trades)
    wins = sum(1 for t in trades if Decimal(t["net_pnl_usdt"]) > 0)
    losses = sum(1 for t in trades if Decimal(t["net_pnl_usdt"]) < 0)
    return {
        "symbol": symbol,
        "status": "complete",
        "candidate_count": str(len(trades)),
        "trade_count": str(len(trades)),
        "winning_trades": str(wins),
        "losing_trades": str(losses),
        "zero_trades": "0",
        "gross_pnl_usdt": str(gross),
        "costs_usdt": str(costs),
        "net_pnl_usdt": str(net),
    }


def _bundle(
    tmp_path: Path,
    trades: list[dict[str, str]],
    enrichment: list[dict[str, str]],
    coins: list[dict[str, str]] | None = None,
) -> tuple[Path, Path, Path, Path]:
    trades_path = tmp_path / "trades.csv"
    coins_path = tmp_path / "coin_summary.csv"
    enr_dir = tmp_path / "enrichment"
    out_dir = tmp_path / "analysis_out"
    if coins is None:
        by: dict[str, list[dict[str, str]]] = {}
        for t in trades:
            by.setdefault(t["symbol"], []).append(t)
        coins = [_coin(sym, rows) for sym, rows in sorted(by.items())]
    _write_csv(trades_path, list(trades[0].keys()), trades)
    _write_csv(coins_path, list(coins[0].keys()), coins)
    _write_csv(enr_dir / "enriched_trades.csv", list(enrichment[0].keys()), enrichment)
    return trades_path, coins_path, enr_dir, out_dir


def test_exact_join_success(tmp_path: Path) -> None:
    trades = [
        _trade(symbol="AAA", eid="edc:1", side="long", decision_time="2026-07-24T01:00:00+00:00", net="5"),
        _trade(symbol="BBB", eid="edc:2", side="short", decision_time="2026-07-24T02:00:00+00:00", net="-3"),
    ]
    enrichment = [
        _enr(symbol="AAA", eid="edc:1", direction="BULLISH", decision_at="2026-07-24T01:00:00+00:00", atr="2"),
        _enr(symbol="BBB", eid="edc:2", direction="BEARISH", decision_at="2026-07-24T02:00:00+00:00", atr="1"),
    ]
    tp, cp, ep, op = _bundle(tmp_path, trades, enrichment)
    result = analyze_edc_profitability_v2(
        trades_path=tp, coin_summary_path=cp, enrichment_path=ep, output_dir=op
    )
    assert result.trade_count == 2
    assert result.verdict == "P2E1_EDC_PROFITABILITY_DIAGNOSIS_COMPLETE"
    assert (op / "analysis_manifest.json").is_file()


def test_missing_id_blocks(tmp_path: Path) -> None:
    trades = [
        _trade(symbol="AAA", eid="edc:1", side="long", decision_time="2026-07-24T01:00:00+00:00", net="5"),
        _trade(symbol="BBB", eid="edc:2", side="short", decision_time="2026-07-24T02:00:00+00:00", net="-3"),
    ]
    enrichment = [
        _enr(symbol="AAA", eid="edc:1", direction="BULLISH", decision_at="2026-07-24T01:00:00+00:00"),
    ]
    tp, cp, ep, op = _bundle(tmp_path, trades, enrichment)
    with pytest.raises(StrategyProfitabilityError, match="parity"):
        analyze_edc_profitability_v2(
            trades_path=tp, coin_summary_path=cp, enrichment_path=ep, output_dir=op
        )


def test_extra_id_blocks(tmp_path: Path) -> None:
    trades = [
        _trade(symbol="AAA", eid="edc:1", side="long", decision_time="2026-07-24T01:00:00+00:00", net="5"),
    ]
    enrichment = [
        _enr(symbol="AAA", eid="edc:1", direction="BULLISH", decision_at="2026-07-24T01:00:00+00:00"),
        _enr(symbol="BBB", eid="edc:2", direction="BEARISH", decision_at="2026-07-24T02:00:00+00:00"),
    ]
    tp, cp, ep, op = _bundle(tmp_path, trades, enrichment)
    with pytest.raises(StrategyProfitabilityError, match="parity"):
        analyze_edc_profitability_v2(
            trades_path=tp, coin_summary_path=cp, enrichment_path=ep, output_dir=op
        )


def test_duplicate_id_blocks(tmp_path: Path) -> None:
    trades = [
        _trade(symbol="AAA", eid="edc:1", side="long", decision_time="2026-07-24T01:00:00+00:00", net="5"),
        _trade(symbol="AAA", eid="edc:1", side="long", decision_time="2026-07-24T01:00:00+00:00", net="4"),
    ]
    enrichment = [
        _enr(symbol="AAA", eid="edc:1", direction="BULLISH", decision_at="2026-07-24T01:00:00+00:00"),
        _enr(symbol="BBB", eid="edc:2", direction="BEARISH", decision_at="2026-07-24T02:00:00+00:00"),
    ]
    # force duplicate on enrichment side with matching symbols carefully
    enrichment = [
        _enr(symbol="AAA", eid="edc:1", direction="BULLISH", decision_at="2026-07-24T01:00:00+00:00"),
        _enr(symbol="AAA", eid="edc:1", direction="BULLISH", decision_at="2026-07-24T01:00:00+00:00"),
    ]
    tp, cp, ep, op = _bundle(tmp_path, trades, enrichment)
    with pytest.raises(StrategyProfitabilityError, match="duplicate"):
        analyze_edc_profitability_v2(
            trades_path=tp, coin_summary_path=cp, enrichment_path=ep, output_dir=op
        )


def test_side_mismatch_blocks(tmp_path: Path) -> None:
    trades = [
        _trade(symbol="AAA", eid="edc:1", side="long", decision_time="2026-07-24T01:00:00+00:00", net="5"),
    ]
    enrichment = [
        _enr(symbol="AAA", eid="edc:1", direction="BEARISH", decision_at="2026-07-24T01:00:00+00:00"),
    ]
    tp, cp, ep, op = _bundle(tmp_path, trades, enrichment)
    with pytest.raises(StrategyProfitabilityError, match="side mismatch"):
        analyze_edc_profitability_v2(
            trades_path=tp, coin_summary_path=cp, enrichment_path=ep, output_dir=op
        )


def test_decision_time_mismatch_blocks(tmp_path: Path) -> None:
    trades = [
        _trade(symbol="AAA", eid="edc:1", side="long", decision_time="2026-07-24T01:00:00+00:00", net="5"),
    ]
    enrichment = [
        _enr(symbol="AAA", eid="edc:1", direction="BULLISH", decision_at="2026-07-24T01:00:01+00:00"),
    ]
    tp, cp, ep, op = _bundle(tmp_path, trades, enrichment)
    with pytest.raises(StrategyProfitabilityError, match="decision_time mismatch"):
        analyze_edc_profitability_v2(
            trades_path=tp, coin_summary_path=cp, enrichment_path=ep, output_dir=op
        )


def test_future_and_unresolved_excluded(tmp_path: Path) -> None:
    trades = [
        _trade(symbol="AAA", eid="edc:1", side="long", decision_time="2026-07-24T01:00:00+00:00", net="5"),
        _trade(symbol="BBB", eid="edc:2", side="short", decision_time="2026-07-24T02:00:00+00:00", net="-2"),
    ]
    enrichment = [
        _enr(symbol="AAA", eid="edc:1", direction="BULLISH", decision_at="2026-07-24T01:00:00+00:00", atr="3"),
        _enr(symbol="BBB", eid="edc:2", direction="BEARISH", decision_at="2026-07-24T02:00:00+00:00", atr="1"),
    ]
    tp, cp, ep, op = _bundle(tmp_path, trades, enrichment)
    analyze_edc_profitability_v2(
        trades_path=tp, coin_summary_path=cp, enrichment_path=ep, output_dir=op
    )
    avail = list(csv.DictReader((op / "feature_availability.csv").open()))
    by_col = {r["column"]: r for r in avail}
    assert by_col["label__net_pnl_usdt"]["group"] == "OUTCOME_FUTURE"
    assert by_col["feature__lld_score"]["group"] == "UNRESOLVED_AVAILABILITY"
    assert by_col["feature__atr14_pct"]["group"] == "PREDICTOR_CAUSAL"
    assert by_col["feature__direction"]["group"] == "IDENTITY_CONTEXT"
    cmp_rows = list(csv.DictReader((op / "trade_feature_comparison.csv").open()))
    feats = {r["feature"] for r in cmp_rows}
    assert "feature__atr14_pct" in feats
    assert "label__net_pnl_usdt" not in feats
    assert "feature__lld_score" not in feats


def test_winner_loser_zero_and_long_short(tmp_path: Path) -> None:
    trades = [
        _trade(symbol="AAA", eid="edc:1", side="long", decision_time="2026-07-24T01:00:00+00:00", net="5"),
        _trade(symbol="AAA", eid="edc:2", side="long", decision_time="2026-07-24T02:00:00+00:00", net="0"),
        _trade(symbol="BBB", eid="edc:3", side="short", decision_time="2026-07-24T03:00:00+00:00", net="-4"),
    ]
    enrichment = [
        _enr(symbol="AAA", eid="edc:1", direction="BULLISH", decision_at="2026-07-24T01:00:00+00:00", atr="4"),
        _enr(symbol="AAA", eid="edc:2", direction="BULLISH", decision_at="2026-07-24T02:00:00+00:00", atr="2"),
        _enr(symbol="BBB", eid="edc:3", direction="BEARISH", decision_at="2026-07-24T03:00:00+00:00", atr="1"),
    ]
    tp, cp, ep, op = _bundle(tmp_path, trades, enrichment)
    analyze_edc_profitability_v2(
        trades_path=tp, coin_summary_path=cp, enrichment_path=ep, output_dir=op
    )
    findings = json.loads((op / "findings.json").read_text())
    obs = findings[0]["observation"]
    assert "winners=1" in obs and "losers=1" in obs and "zero=1" in obs
    cmp_rows = list(csv.DictReader((op / "trade_feature_comparison.csv").open()))
    scopes = {(r["feature"], r["scope"]) for r in cmp_rows}
    assert ("feature__atr14_pct", "pooled") in scopes
    assert ("feature__atr14_pct", "long") in scopes
    assert ("feature__atr14_pct", "short") in scopes


def test_coin_aggregation_and_sample_buckets(tmp_path: Path) -> None:
    trades = []
    enrichment = []
    # AAA: 3 trades -> VERY_SMALL
    for i in range(3):
        eid = f"edc:a{i}"
        net = "2" if i == 0 else "-1"
        trades.append(
            _trade(
                symbol="AAA",
                eid=eid,
                side="long",
                decision_time=f"2026-07-24T0{i}:00:00+00:00",
                net=net,
            )
        )
        enrichment.append(
            _enr(
                symbol="AAA",
                eid=eid,
                direction="BULLISH",
                decision_at=f"2026-07-24T0{i}:00:00+00:00",
                atr=str(i + 1),
            )
        )
    # BBB: 12 trades -> MEDIUM
    for i in range(12):
        eid = f"edc:b{i}"
        trades.append(
            _trade(
                symbol="BBB",
                eid=eid,
                side="short" if i % 2 else "long",
                decision_time=f"2026-07-25T{i:02d}:00:00+00:00",
                net="1" if i % 3 else "-2",
            )
        )
        enrichment.append(
            _enr(
                symbol="BBB",
                eid=eid,
                direction="BEARISH" if i % 2 else "BULLISH",
                decision_at=f"2026-07-25T{i:02d}:00:00+00:00",
                atr=str(i),
            )
        )
    tp, cp, ep, op = _bundle(tmp_path, trades, enrichment)
    analyze_edc_profitability_v2(
        trades_path=tp, coin_summary_path=cp, enrichment_path=ep, output_dir=op
    )
    coin_rows = {r["symbol"]: r for r in csv.DictReader((op / "coin_analysis.csv").open())}
    assert coin_rows["AAA"]["sample_bucket"] == "VERY_SMALL"
    assert coin_rows["BBB"]["sample_bucket"] == "MEDIUM"
    assert int(coin_rows["AAA"]["trade_count"]) == 3
    assert int(coin_rows["BBB"]["trade_count"]) == 12


def test_wilson_interval_and_sample_size_helpers() -> None:
    low, high = wilson_interval(8, 10)
    assert low is not None and high is not None
    assert Decimal("0") <= low <= high <= Decimal("1")
    assert wilson_interval(0, 0) == (None, None)
    assert sample_size_bucket(0) == "VERY_SMALL"
    assert sample_size_bucket(5) == "SMALL"
    assert sample_size_bucket(10) == "MEDIUM"
    assert sample_size_bucket(20) == "LARGER"


def test_decimal_cost_formula_and_gross_pos_net_neg() -> None:
    gross = Decimal("50")
    # 10 trades * 1000 * 0.11/100 = 11 → net 39
    assert scenario_net_usdt(
        gross_pnl_usdt=gross,
        trade_count=10,
        notional_usdt=Decimal("1000"),
        cost_pct=Decimal("0.11"),
    ) == Decimal("39")
    # gross positive, costs larger → net negative
    net = scenario_net_usdt(
        gross_pnl_usdt=Decimal("5"),
        trade_count=10,
        notional_usdt=Decimal("1000"),
        cost_pct=Decimal("0.11"),
    )
    assert net == Decimal("-6")
    assert gross > 0 and net < 0


def test_quartiles_and_missing_rate(tmp_path: Path) -> None:
    trades = []
    enrichment = []
    # 24 trades with ascending atr so quartiles form; one missing atr
    for i in range(24):
        eid = f"edc:{i}"
        side = "long" if i % 2 == 0 else "short"
        direction = "BULLISH" if side == "long" else "BEARISH"
        trades.append(
            _trade(
                symbol="CCC" if i < 12 else "DDD",
                eid=eid,
                side=side,
                decision_time=f"2026-07-24T{i:02d}:00:00+00:00",
                net="2" if i % 2 == 0 else "-1",
            )
        )
        atr = "" if i == 0 else str(i)
        enrichment.append(
            _enr(
                symbol="CCC" if i < 12 else "DDD",
                eid=eid,
                direction=direction,
                decision_at=f"2026-07-24T{i:02d}:00:00+00:00",
                atr=atr,
            )
        )
    tp, cp, ep, op = _bundle(tmp_path, trades, enrichment)
    analyze_edc_profitability_v2(
        trades_path=tp, coin_summary_path=cp, enrichment_path=ep, output_dir=op
    )
    qrows = [
        r
        for r in csv.DictReader((op / "feature_quantiles.csv").open())
        if r["feature"] == "feature__atr14_pct"
    ]
    assert {r["quartile"] for r in qrows} == {"1", "2", "3", "4"}
    cmp_rows = [
        r
        for r in csv.DictReader((op / "trade_feature_comparison.csv").open())
        if r["feature"] == "feature__atr14_pct" and r["scope"] == "pooled"
    ]
    assert cmp_rows
    assert Decimal(cmp_rows[0]["missing_rate"]) > 0


def test_leave_one_coin_out_and_coin_mix(tmp_path: Path) -> None:
    """Pooled winner_higher driven by one large coin; others opposite → mix/single."""
    trades: list[dict[str, str]] = []
    enrichment: list[dict[str, str]] = []

    def add(sym: str, n: int, *, winner_high_atr: bool, day: int) -> None:
        for i in range(n):
            eid = f"edc:{sym}:{i}"
            is_win = i % 2 == 0
            # atr: winners high vs losers low (or reverse)
            if winner_high_atr:
                atr = "10" if is_win else "1"
            else:
                atr = "1" if is_win else "10"
            minute = i % 60
            hour = i // 60
            ts = f"2026-07-{day:02d}T{hour:02d}:{minute:02d}:00+00:00"
            trades.append(
                _trade(
                    symbol=sym,
                    eid=eid,
                    side="long",
                    decision_time=ts,
                    net="3" if is_win else "-2",
                )
            )
            enrichment.append(
                _enr(
                    symbol=sym,
                    eid=eid,
                    direction="BULLISH",
                    decision_at=ts,
                    atr=atr,
                )
            )

    # BIG coin dominates pooled with winner_higher
    add("BIG", 40, winner_high_atr=True, day=24)
    # Two medium coins with opposite pattern
    add("OPP1", 12, winner_high_atr=False, day=25)
    add("OPP2", 12, winner_high_atr=False, day=26)

    tp, cp, ep, op = _bundle(tmp_path, trades, enrichment)
    analyze_edc_profitability_v2(
        trades_path=tp, coin_summary_path=cp, enrichment_path=ep, output_dir=op
    )
    stab = {
        r["feature"]: r
        for r in csv.DictReader((op / "stability_analysis.csv").open())
    }
    assert "feature__atr14_pct" in stab
    assert stab["feature__atr14_pct"]["assessment"] in {
        "MIXED_DIRECTION",
        "POSSIBLE_COIN_MIX_CONFOUNDING",
        "SINGLE_COIN_DRIVEN",
    }


def test_deterministic_outputs_and_inputs_not_mutated(tmp_path: Path) -> None:
    trades = [
        _trade(symbol="AAA", eid="edc:1", side="long", decision_time="2026-07-24T01:00:00+00:00", net="5"),
        _trade(symbol="BBB", eid="edc:2", side="short", decision_time="2026-07-24T02:00:00+00:00", net="-3"),
    ]
    enrichment = [
        _enr(symbol="AAA", eid="edc:1", direction="BULLISH", decision_at="2026-07-24T01:00:00+00:00", atr="2"),
        _enr(symbol="BBB", eid="edc:2", direction="BEARISH", decision_at="2026-07-24T02:00:00+00:00", atr="1"),
    ]
    tp, cp, ep, op = _bundle(tmp_path, trades, enrichment)
    before = {
        "trades": hashlib.sha256(tp.read_bytes()).hexdigest(),
        "coins": hashlib.sha256(cp.read_bytes()).hexdigest(),
        "enr": hashlib.sha256((ep / "enriched_trades.csv").read_bytes()).hexdigest(),
    }
    analyze_edc_profitability_v2(
        trades_path=tp, coin_summary_path=cp, enrichment_path=ep, output_dir=op
    )
    snap1 = {p.name: p.read_bytes() for p in sorted(op.iterdir()) if p.is_file()}
    # second run into fresh dir
    op2 = tmp_path / "analysis_out2"
    analyze_edc_profitability_v2(
        trades_path=tp, coin_summary_path=cp, enrichment_path=ep, output_dir=op2
    )
    snap2 = {p.name: p.read_bytes() for p in sorted(op2.iterdir()) if p.is_file()}
    assert snap1 == snap2
    assert hashlib.sha256(tp.read_bytes()).hexdigest() == before["trades"]
    assert hashlib.sha256(cp.read_bytes()).hexdigest() == before["coins"]
    assert hashlib.sha256((ep / "enriched_trades.csv").read_bytes()).hexdigest() == before["enr"]


def test_cli_import_without_execution() -> None:
    mod = importlib.import_module(
        "orderbook_analyse.strategy_lab.analysis.edc_profitability_v2"
    )
    assert callable(mod.analyze_edc_profitability_v2)
    parser = build_arg_parser()
    args = parser.parse_args(
        [
            "--trades",
            "t.csv",
            "--coin-summary",
            "c.csv",
            "--enrichment",
            "e",
            "--output-dir",
            "o",
        ]
    )
    assert args.trades == Path("t.csv")


def test_no_db_or_network_imports() -> None:
    import ast

    tree = ast.parse(
        Path(
            "src/orderbook_analyse/strategy_lab/analysis/edc_profitability_v2.py"
        ).read_text(encoding="utf-8")
    )
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden = {
        "clickhouse_connect",
        "requests",
        "urllib",
        "socket",
        "http",
        "aiohttp",
    }
    assert imported.isdisjoint(forbidden)


def test_cli_parity_exit_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from orderbook_analyse.strategy_lab.analysis import edc_profitability_v2 as mod

    trades = [
        _trade(symbol="AAA", eid="edc:1", side="long", decision_time="2026-07-24T01:00:00+00:00", net="5"),
        _trade(symbol="BBB", eid="edc:2", side="short", decision_time="2026-07-24T02:00:00+00:00", net="-3"),
    ]
    enrichment = [
        _enr(symbol="AAA", eid="edc:1", direction="BULLISH", decision_at="2026-07-24T01:00:00+00:00"),
    ]
    tp, cp, ep, op = _bundle(tmp_path, trades, enrichment)
    code = mod.main(
        [
            "--trades",
            str(tp),
            "--coin-summary",
            str(cp),
            "--enrichment",
            str(ep),
            "--output-dir",
            str(op),
        ]
    )
    assert code == 2


def test_cost_scenarios_csv(tmp_path: Path) -> None:
    trades = [
        _trade(
            symbol="AAA",
            eid="edc:1",
            side="long",
            decision_time="2026-07-24T01:00:00+00:00",
            net="8.9",
            gross="10",
        ),
    ]
    enrichment = [
        _enr(symbol="AAA", eid="edc:1", direction="BULLISH", decision_at="2026-07-24T01:00:00+00:00"),
    ]
    tp, cp, ep, op = _bundle(tmp_path, trades, enrichment)
    analyze_edc_profitability_v2(
        trades_path=tp, coin_summary_path=cp, enrichment_path=ep, output_dir=op
    )
    rows = list(csv.DictReader((op / "cost_scenarios.csv").open()))
    port = [r for r in rows if r["scope"] == "portfolio"]
    assert {r["cost_pct"] for r in port} == {"0", "0.055", "0.11", "0.15", "0.20"}
    row_011 = next(r for r in port if r["cost_pct"] == "0.11")
    assert Decimal(row_011["scenario_net_usdt"]) == Decimal("10") - Decimal("1.1")


def _large_mixed_bundle(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """Five coins × 12 trades with several numeric causal features."""
    trades: list[dict[str, str]] = []
    enrichment: list[dict[str, str]] = []
    extra_feats = [
        "feature__signal_a",
        "feature__signal_b",
        "feature__signal_c",
        "feature__signal_d",
        "feature__signal_e",
    ]
    for c_i, sym in enumerate(["C0", "C1", "C2", "C3", "C4"]):
        for i in range(12):
            eid = f"edc:{sym}:{i}"
            is_win = i % 2 == 0
            winner_high = c_i < 3
            if winner_high:
                atr = "10" if is_win else "1"
            else:
                atr = "1" if is_win else "10"
            ts = f"2026-07-{24 + c_i:02d}T{i:02d}:00:00+00:00"
            trades.append(
                _trade(
                    symbol=sym,
                    eid=eid,
                    side="long",
                    decision_time=ts,
                    net="3" if is_win else "-2",
                )
            )
            row = _enr(
                symbol=sym,
                eid=eid,
                direction="BULLISH",
                decision_at=ts,
                atr=atr,
            )
            for j, feat in enumerate(extra_feats):
                # Distinct effect magnitudes for deterministic ranking
                hi = str(100 * (j + 1)) if is_win else str(j + 1)
                lo = str(j + 1) if is_win else str(100 * (j + 1))
                val = hi if winner_high else lo
                # Slightly perturb by feature index so strengths differ
                if is_win and winner_high:
                    val = str(100 * (j + 1) + i)
                elif (not is_win) and winner_high:
                    val = str(j + 1)
                elif is_win and not winner_high:
                    val = str(j + 1)
                else:
                    val = str(100 * (j + 1) + i)
                row[feat] = val
                row[f"{feat}__causal"] = "True"
                row[f"{feat}__coverage_status"] = "OK"
                row[f"{feat}__missing_reason"] = ""
                row[f"{feat}__feature_asof"] = ts
                row[f"{feat}__source_table"] = "synthetic"
            enrichment.append(row)
    return _bundle(tmp_path, trades, enrichment)


def test_census_names_not_allowed(tmp_path: Path) -> None:
    tp, cp, ep, op = _large_mixed_bundle(tmp_path)
    result = analyze_edc_profitability_v2(
        trades_path=tp, coin_summary_path=cp, enrichment_path=ep, output_dir=op
    )
    man = json.loads((op / "analysis_manifest.json").read_text())
    assert "allowed_features" not in man
    census = man["feature_census"]
    assert "predictor_causal_total" in census
    assert "predictor_causal_analyzable" in census
    assert "predictor_causal_numeric_analyzable" in census
    assert census["predictor_causal_total"] >= census["predictor_causal_analyzable"]
    assert (
        census["predictor_causal_analyzable"]
        == census["predictor_causal_numeric_analyzable"]
        + census["predictor_causal_categorical_analyzable"]
    )
    assert (
        census["predictor_causal_analyzable"]
        + census["predictor_causal_excluded_missing"]
        + census["predictor_causal_excluded_constant"]
        == census["predictor_causal_total"]
    )
    assert result.predictor_causal_total == census["predictor_causal_total"]
    report = (op / "report.md").read_text()
    assert "PREDICTOR_CAUSAL (total)" in report
    assert "allowed=" not in report


def test_constant_predictor_excluded(tmp_path: Path) -> None:
    trades = [
        _trade(symbol="AAA", eid="edc:1", side="long", decision_time="2026-07-24T01:00:00+00:00", net="5"),
        _trade(symbol="BBB", eid="edc:2", side="short", decision_time="2026-07-24T02:00:00+00:00", net="-2"),
    ]
    enrichment = [
        _enr(symbol="AAA", eid="edc:1", direction="BULLISH", decision_at="2026-07-24T01:00:00+00:00", atr="2"),
        _enr(symbol="BBB", eid="edc:2", direction="BEARISH", decision_at="2026-07-24T02:00:00+00:00", atr="2"),
    ]
    tp, cp, ep, op = _bundle(tmp_path, trades, enrichment)
    analyze_edc_profitability_v2(
        trades_path=tp, coin_summary_path=cp, enrichment_path=ep, output_dir=op
    )
    by_col = {r["column"]: r for r in csv.DictReader((op / "feature_availability.csv").open())}
    assert by_col["feature__atr14_pct"]["value_kind"] == "constant"
    assert by_col["feature__atr14_pct"]["usable"] == "no"
    man = json.loads((op / "analysis_manifest.json").read_text())
    assert man["feature_census"]["predictor_causal_excluded_constant"] >= 1


def test_bool_string_not_numeric(tmp_path: Path) -> None:
    trades = [
        _trade(symbol="AAA", eid="edc:1", side="long", decision_time="2026-07-24T01:00:00+00:00", net="5"),
        _trade(symbol="BBB", eid="edc:2", side="short", decision_time="2026-07-24T02:00:00+00:00", net="-2"),
    ]
    enrichment = [
        _enr(symbol="AAA", eid="edc:1", direction="BULLISH", decision_at="2026-07-24T01:00:00+00:00", atr="3"),
        _enr(symbol="BBB", eid="edc:2", direction="BEARISH", decision_at="2026-07-24T02:00:00+00:00", atr="1"),
    ]
    for row in enrichment:
        row["feature__flag_like"] = "True" if row["candidate_id"] == "edc:1" else "False"
        for suf, val in (
            ("__causal", "True"),
            ("__coverage_status", "OK"),
            ("__missing_reason", ""),
            ("__feature_asof", row["feature__decision_at"]),
            ("__source_table", "test"),
        ):
            row[f"feature__flag_like{suf}"] = val
    tp, cp, ep, op = _bundle(tmp_path, trades, enrichment)
    analyze_edc_profitability_v2(
        trades_path=tp, coin_summary_path=cp, enrichment_path=ep, output_dir=op
    )
    by_col = {r["column"]: r for r in csv.DictReader((op / "feature_availability.csv").open())}
    assert by_col["feature__flag_like"]["value_kind"] == "categorical"
    assert by_col["feature__flag_like"]["group"] == "PREDICTOR_CAUSAL"


def test_hypotheses_filled_and_aligned(tmp_path: Path) -> None:
    tp, cp, ep, op = _large_mixed_bundle(tmp_path)
    analyze_edc_profitability_v2(
        trades_path=tp, coin_summary_path=cp, enrichment_path=ep, output_dir=op
    )
    findings = json.loads((op / "findings.json").read_text())
    hyps = [f for f in findings if str(f["finding_id"]).startswith("F_HYP_")]
    assert len(hyps) == 5
    report = (op / "report.md").read_text()
    assert "## Nächste Hypothesen (unbestätigt)" in report
    for h in hyps:
        assert h["feature"]
        assert h["feature"] not in ("", None)
        assert not str(h["feature"]).startswith("label__")
        assert "observed_direction" in h
        assert "winner_loser_diff" in h
        assert int(h["n_trades"]) >= 50
        assert int(h["n_coins"]) >= 5
        assert "stability_status" in h
        assert "limitations" in h and h["limitations"]
        assert "next_test" in h and h["next_test"]
        assert h["feature"] in report
        if h["stability_status"] == "MIXED_DIRECTION":
            assert "UNSTABLE" in h["observation"] or "MIXED" in h["limitations"]
    # No future/unresolved features in hypotheses
    avail = {r["column"]: r for r in csv.DictReader((op / "feature_availability.csv").open())}
    for h in hyps:
        assert avail[h["feature"]]["group"] == "PREDICTOR_CAUSAL"
        assert avail[h["feature"]]["usable"] == "yes"


def test_stability_insufficient_not_mixed_for_sparse_coin(tmp_path: Path) -> None:
    """Coins with <2 winners or <2 losers must not count as opposite direction."""
    from orderbook_analyse.strategy_lab.analysis.edc_profitability_v2 import (
        _coin_feature_direction,
    )

    rows = []
    # 10 trades, only 1 winner → insufficient
    for i in range(10):
        label = "winner" if i == 0 else "loser"
        rows.append(
            {
                "symbol": "AAA",
                "label": label,
                "enrichment": {"feature__atr14_pct": str(i + 1)},
            }
        )
    assert _coin_feature_direction(rows, "feature__atr14_pct") == "INSUFFICIENT_DATA"
