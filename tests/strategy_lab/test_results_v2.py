"""P2A Strategy Lab result contract tests."""

from __future__ import annotations

from dataclasses import fields, is_dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, get_args, get_origin

import pytest

from orderbook_analyse.strategy_lab.models.contracts_v2.enums import (
    ResearchConfirmationPolicyV2,
)
from orderbook_analyse.strategy_lab.models.contracts_v2.phase1_contracts import (
    VersionedUniverseRefV2,
)
from orderbook_analyse.strategy_lab.models.enums import (
    ModelingStatus,
    RateUnit,
    SideName,
    TimeframeUnit,
)
from orderbook_analyse.strategy_lab.models.identifiers import ContractVersion, StableIdentifier
from orderbook_analyse.strategy_lab.models.strategy import RateValue, TimeframeValue
from orderbook_analyse.strategy_lab.results_v2 import (
    SourceEventIdV2,
    StrategyRunResultV2,
    StrategyRunStatusV2,
    StrategyTradeV2,
    TradeExitReasonV2,
)

UTC = timezone.utc
HASH_A = "4aced6b481d19eadd5505afc535e6fb4976f231fd2894b11f7d79acebc53598f"
HASH_B = "c70a16d5e22c9ccd1060e2800bf429c7eafdd7c8299f7e91e68903cad980cc4f"


def _dt(hour: int = 12, minute: int = 0, day: int = 1) -> datetime:
    return datetime(2026, 7, day, hour, minute, tzinfo=UTC)


def _universe() -> VersionedUniverseRefV2:
    return VersionedUniverseRefV2(
        universe_id=StableIdentifier(value="universe_tradeable_51"),
        version="v1",
        content_hash="sha256:" + ("ab" * 32),
    )


def _valid_trade(
    *,
    symbol: str = "XRPUSDT",
    side: SideName = SideName.LONG,
    exit_reason: TradeExitReasonV2 = TradeExitReasonV2.TP_EXIT,
    source_event_id: str = "edc:1ef21699f01b9131ebee",
    decision_time: datetime | None = None,
    entry_time: datetime | None = None,
    exit_time: datetime | None = None,
    unresolved: bool = False,
) -> StrategyTradeV2:
    decision = decision_time or _dt(2, 30)
    entry = entry_time or _dt(2, 35)
    if unresolved:
        return StrategyTradeV2(
            source_event_id=SourceEventIdV2(value=source_event_id),
            symbol=symbol,
            side=side,
            decision_time=decision,
            entry_time=entry,
            entry_price=Decimal("0.5"),
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
    return StrategyTradeV2(
        source_event_id=SourceEventIdV2(value=source_event_id),
        symbol=symbol,
        side=side,
        decision_time=decision,
        entry_time=entry,
        entry_price=Decimal("0.5"),
        exit_time=exit_time or _dt(3, 27),
        exit_price=Decimal("0.50375"),
        exit_reason=exit_reason,
        gross_return_pct=Decimal("0.75"),
        roundtrip_cost_pct=Decimal("0.11"),
        net_return_pct=Decimal("0.64"),
        gross_pnl_usdt=Decimal("7.5"),
        costs_usdt=Decimal("1.1"),
        net_pnl_usdt=Decimal("6.4"),
        mode_id=StableIdentifier(value="m0_strict_sync"),
        confirmation_policy=ResearchConfirmationPolicyV2.CORE_RESEARCH_SUPPORTIVE,
    )


def _valid_run(
    *,
    trades: tuple[StrategyTradeV2, ...] | None = None,
    symbols: tuple[str, ...] = ("XRPUSDT",),
    candidate_count: int | None = None,
    status: StrategyRunStatusV2 = StrategyRunStatusV2.COMPLETE,
    start: datetime | None = None,
    end: datetime | None = None,
    strategy_hash: str = HASH_A,
) -> StrategyRunResultV2:
    trade_tuple = trades if trades is not None else (_valid_trade(),)
    return StrategyRunResultV2(
        strategy_hash=strategy_hash,
        plugin_id=StableIdentifier(value="edc_m0_strict_sync"),
        plugin_contract_version=ContractVersion(value="catalog/v2"),
        universe=_universe(),
        start=start or datetime(2026, 7, 1, 0, 0, tzinfo=UTC),
        end=end or datetime(2026, 7, 31, 0, 0, tzinfo=UTC),
        symbols=symbols,
        signal_timeframe=TimeframeValue(value=5, unit=TimeframeUnit.MINUTES),
        execution_timeframe=TimeframeValue(value=1, unit=TimeframeUnit.MINUTES),
        roundtrip_cost=RateValue(value=Decimal("0.11"), unit=RateUnit.PERCENT),
        slippage_status=ModelingStatus.NOT_MODELED,
        funding_status=ModelingStatus.NOT_MODELED,
        status=status,
        candidate_count=candidate_count if candidate_count is not None else len(trade_tuple),
        trades=trade_tuple,
    )


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_valid_trade_constructs() -> None:
    trade = _valid_trade()
    assert trade.side is SideName.LONG
    assert trade.exit_reason is TradeExitReasonV2.TP_EXIT


def test_valid_run_with_trade() -> None:
    run = _valid_run()
    assert run.trade_count == 1
    assert run.candidate_count == 1


def test_valid_run_without_trades() -> None:
    run = _valid_run(trades=(), candidate_count=0)
    assert run.trade_count == 0
    assert run.gross_pnl_usdt == Decimal("0")
    assert run.costs_usdt == Decimal("0")
    assert run.net_pnl_usdt == Decimal("0")


@pytest.mark.parametrize("side", [SideName.LONG, SideName.SHORT])
def test_both_sides(side: SideName) -> None:
    trade = _valid_trade(side=side)
    assert trade.side is side


@pytest.mark.parametrize(
    "reason",
    [
        TradeExitReasonV2.TP_EXIT,
        TradeExitReasonV2.SL_EXIT,
        TradeExitReasonV2.TIME_EXIT,
    ],
)
def test_resolved_exit_reasons(reason: TradeExitReasonV2) -> None:
    trade = _valid_trade(exit_reason=reason)
    assert trade.exit_reason is reason


@pytest.mark.parametrize(
    "reason",
    [
        TradeExitReasonV2.COVERAGE_MISSING,
        TradeExitReasonV2.INCOMPLETE_OUTCOME_HORIZON,
    ],
)
def test_unresolved_exit_reasons(reason: TradeExitReasonV2) -> None:
    trade = _valid_trade(exit_reason=reason, unresolved=True)
    assert trade.exit_time is None
    assert trade.net_pnl_usdt is None


@pytest.mark.parametrize("status", list(StrategyRunStatusV2))
def test_all_run_statuses(status: StrategyRunStatusV2) -> None:
    run = _valid_run(status=status, trades=(), candidate_count=0)
    assert run.status is status


def test_cluster_style_optional_mode_and_policy() -> None:
    trade = StrategyTradeV2(
        source_event_id=SourceEventIdV2(value="csw:500521a95c4b4ffb"),
        symbol="BTCUSDT",
        side=SideName.LONG,
        decision_time=datetime(2026, 8, 19, 0, 15, tzinfo=UTC),
        entry_time=datetime(2026, 8, 19, 0, 45, tzinfo=UTC),
        entry_price=Decimal("60000"),
        exit_time=datetime(2026, 8, 19, 8, 45, tzinfo=UTC),
        exit_price=Decimal("60450"),
        exit_reason=TradeExitReasonV2.TP_EXIT,
        gross_return_pct=Decimal("0.75"),
        roundtrip_cost_pct=Decimal("0.11"),
        net_return_pct=Decimal("0.64"),
        gross_pnl_usdt=Decimal("7.5"),
        costs_usdt=Decimal("1.1"),
        net_pnl_usdt=Decimal("6.4"),
        mode_id=None,
        confirmation_policy=None,
    )
    run = _valid_run(
        trades=(trade,),
        symbols=("BTCUSDT",),
        strategy_hash=HASH_B,
        start=datetime(2026, 8, 19, 0, 0, tzinfo=UTC),
        end=datetime(2026, 8, 20, 0, 0, tzinfo=UTC),
    )
    assert run.trades[0].mode_id is None
    assert run.trades[0].confirmation_policy is None


# ---------------------------------------------------------------------------
# Type / invariant guards
# ---------------------------------------------------------------------------


def test_trade_and_run_are_frozen_slots_kw_only() -> None:
    for cls in (StrategyTradeV2, StrategyRunResultV2, SourceEventIdV2):
        assert is_dataclass(cls)
        assert cls.__dataclass_params__.frozen is True  # type: ignore[attr-defined]
        assert cls.__slots__  # type: ignore[attr-defined]
        assert cls.__dataclass_params__.kw_only is True  # type: ignore[attr-defined]


def test_no_dict_list_any_callable_fields() -> None:
    forbidden_origins = {dict, list, type(lambda: None)}
    for cls in (StrategyTradeV2, StrategyRunResultV2, SourceEventIdV2):
        for field in fields(cls):
            anno = field.type
            origin = get_origin(anno)
            if origin in forbidden_origins or anno in forbidden_origins:
                pytest.fail(f"{cls.__name__}.{field.name} has forbidden type {anno!r}")
            if anno is Any:
                pytest.fail(f"{cls.__name__}.{field.name} uses Any")
            for arg in get_args(anno) or ():
                if arg is Any or arg in forbidden_origins:
                    pytest.fail(
                        f"{cls.__name__}.{field.name} has forbidden type arg {arg!r}"
                    )


def test_reject_float_int_bool_for_decimal_fields() -> None:
    base = _valid_trade()
    with pytest.raises(TypeError, match="Decimal"):
        replace(base, entry_price=0.5)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="Decimal"):
        replace(base, entry_price=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="Decimal"):
        replace(base, entry_price=True)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="Decimal"):
        replace(base, roundtrip_cost_pct=0)  # type: ignore[arg-type]


def test_reject_bool_candidate_count() -> None:
    with pytest.raises(TypeError, match="candidate_count"):
        _valid_run(candidate_count=True)  # type: ignore[arg-type]


def test_reject_naive_datetime() -> None:
    naive = datetime(2026, 7, 1, 12, 0, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        _valid_trade(decision_time=naive)  # type: ignore[arg-type]


def test_reject_non_utc_datetime() -> None:
    berlin = timezone(timedelta(hours=2))
    local = datetime(2026, 7, 1, 12, 0, tzinfo=berlin)
    with pytest.raises(ValueError, match="UTC"):
        _valid_trade(decision_time=local)


def test_reject_entry_before_decision() -> None:
    with pytest.raises(ValueError, match="entry_time"):
        _valid_trade(decision_time=_dt(3, 0), entry_time=_dt(2, 0))


def test_reject_exit_before_entry() -> None:
    with pytest.raises(ValueError, match="exit_time"):
        _valid_trade(entry_time=_dt(3, 0), exit_time=_dt(2, 0))


def test_reject_non_positive_price() -> None:
    base = _valid_trade()
    with pytest.raises(ValueError, match="entry_price"):
        replace(base, entry_price=Decimal("0"))
    with pytest.raises(ValueError, match="exit_price"):
        replace(base, exit_price=Decimal("-1"))


def test_reject_negative_costs() -> None:
    base = _valid_trade()
    with pytest.raises(ValueError, match="roundtrip_cost_pct"):
        replace(base, roundtrip_cost_pct=Decimal("-0.01"))


def test_reject_invalid_hash() -> None:
    with pytest.raises(ValueError, match="strategy_hash"):
        _valid_run(strategy_hash="ABC")
    with pytest.raises(ValueError, match="strategy_hash"):
        _valid_run(strategy_hash="A" * 64)


def test_reject_duplicate_symbols() -> None:
    with pytest.raises(ValueError, match="duplicate symbol"):
        _valid_run(symbols=("XRPUSDT", "XRPUSDT"), trades=(), candidate_count=0)


def test_reject_trade_symbol_outside_run() -> None:
    trade = _valid_trade(symbol="BTCUSDT")
    with pytest.raises(ValueError, match="not in run symbols"):
        _valid_run(trades=(trade,), symbols=("XRPUSDT",))


def test_reject_mutable_trades_list() -> None:
    with pytest.raises(TypeError, match="trades must be a tuple"):
        StrategyRunResultV2(
            strategy_hash=HASH_A,
            plugin_id=StableIdentifier(value="edc_m0_strict_sync"),
            plugin_contract_version=ContractVersion(value="catalog/v2"),
            universe=_universe(),
            start=datetime(2026, 7, 1, 0, 0, tzinfo=UTC),
            end=datetime(2026, 7, 31, 0, 0, tzinfo=UTC),
            symbols=("XRPUSDT",),
            signal_timeframe=TimeframeValue(value=5, unit=TimeframeUnit.MINUTES),
            execution_timeframe=TimeframeValue(value=1, unit=TimeframeUnit.MINUTES),
            roundtrip_cost=RateValue(value=Decimal("0.11"), unit=RateUnit.PERCENT),
            slippage_status=ModelingStatus.NOT_MODELED,
            funding_status=ModelingStatus.NOT_MODELED,
            status=StrategyRunStatusV2.COMPLETE,
            candidate_count=0,
            trades=[],  # type: ignore[arg-type]
        )


def test_reject_candidate_count_below_trade_count() -> None:
    with pytest.raises(ValueError, match="candidate_count"):
        _valid_run(candidate_count=0)


def test_exit_after_run_end_is_allowed() -> None:
    """Outcome padding: exit may land after the detection window end."""
    trade = _valid_trade(
        decision_time=datetime(2026, 7, 30, 23, 0, tzinfo=UTC),
        entry_time=datetime(2026, 7, 30, 23, 5, tzinfo=UTC),
        exit_time=datetime(2026, 7, 31, 7, 5, tzinfo=UTC),
    )
    run = _valid_run(
        trades=(trade,),
        start=datetime(2026, 7, 1, 0, 0, tzinfo=UTC),
        end=datetime(2026, 7, 31, 0, 0, tzinfo=UTC),
    )
    assert run.trades[0].exit_time > run.end


def test_decision_before_run_start_rejected() -> None:
    trade = _valid_trade(decision_time=datetime(2026, 6, 30, 12, 0, tzinfo=UTC))
    with pytest.raises(ValueError, match="decision_time"):
        _valid_run(trades=(trade,))


def test_roundtrip_cost_must_be_percent() -> None:
    with pytest.raises(ValueError, match="PERCENT"):
        StrategyRunResultV2(
            strategy_hash=HASH_A,
            plugin_id=StableIdentifier(value="edc_m0_strict_sync"),
            plugin_contract_version=ContractVersion(value="catalog/v2"),
            universe=_universe(),
            start=datetime(2026, 7, 1, 0, 0, tzinfo=UTC),
            end=datetime(2026, 7, 31, 0, 0, tzinfo=UTC),
            symbols=("XRPUSDT",),
            signal_timeframe=TimeframeValue(value=5, unit=TimeframeUnit.MINUTES),
            execution_timeframe=TimeframeValue(value=1, unit=TimeframeUnit.MINUTES),
            roundtrip_cost=RateValue(
                value=Decimal("11"), unit=RateUnit.BASIS_POINTS
            ),
            slippage_status=ModelingStatus.NOT_MODELED,
            funding_status=ModelingStatus.NOT_MODELED,
            status=StrategyRunStatusV2.COMPLETE,
            candidate_count=0,
            trades=(),
        )


def test_source_event_id_preserves_legacy_form() -> None:
    eid = SourceEventIdV2(value="edc:1ef21699f01b9131ebee")
    assert eid.value == "edc:1ef21699f01b9131ebee"
    with pytest.raises(ValueError):
        SourceEventIdV2(value="  padded  ")


def test_unresolved_exit_rejects_populated_outcome_fields() -> None:
    with pytest.raises(ValueError, match="unresolved"):
        _valid_trade(exit_reason=TradeExitReasonV2.COVERAGE_MISSING)


@pytest.mark.parametrize(
    "field_name",
    [
        "exit_time",
        "exit_price",
        "gross_return_pct",
        "net_return_pct",
        "gross_pnl_usdt",
        "costs_usdt",
        "net_pnl_usdt",
    ],
)
def test_resolved_exit_rejects_none_outcome_fields(field_name: str) -> None:
    base = _valid_trade(exit_reason=TradeExitReasonV2.TP_EXIT)
    with pytest.raises(ValueError, match="resolved exits require"):
        replace(base, **{field_name: None})


@pytest.mark.parametrize(
    "field_name",
    [
        "exit_time",
        "exit_price",
        "gross_return_pct",
        "net_return_pct",
        "gross_pnl_usdt",
        "costs_usdt",
        "net_pnl_usdt",
    ],
)
def test_unresolved_exit_rejects_any_populated_outcome_field(field_name: str) -> None:
    base = _valid_trade(
        exit_reason=TradeExitReasonV2.INCOMPLETE_OUTCOME_HORIZON,
        unresolved=True,
    )
    if field_name == "exit_time":
        value: object = _dt(6, 0)
    elif field_name == "exit_price":
        value = Decimal("0.5")
    else:
        value = Decimal("1")
    with pytest.raises(ValueError, match="unresolved exits require"):
        replace(base, **{field_name: value})


def test_decision_time_equal_to_run_end_allowed() -> None:
    """Bar close may land exactly on exclusive window end."""
    end = datetime(2026, 7, 31, 0, 0, tzinfo=UTC)
    trade = _valid_trade(
        decision_time=end,
        entry_time=end,
        exit_time=datetime(2026, 7, 31, 8, 0, tzinfo=UTC),
    )
    run = _valid_run(
        trades=(trade,),
        start=datetime(2026, 7, 1, 0, 0, tzinfo=UTC),
        end=end,
    )
    assert run.trades[0].decision_time == run.end


def test_decision_time_after_run_end_allowed_when_bar_straddles() -> None:
    """candidate open in [start, end) can yield decision_time > end."""
    end = datetime(2026, 7, 31, 0, 2, tzinfo=UTC)
    trade = _valid_trade(
        decision_time=datetime(2026, 7, 31, 0, 5, tzinfo=UTC),
        entry_time=datetime(2026, 7, 31, 0, 5, tzinfo=UTC),
        exit_time=datetime(2026, 7, 31, 8, 5, tzinfo=UTC),
    )
    run = _valid_run(
        trades=(trade,),
        start=datetime(2026, 7, 1, 0, 0, tzinfo=UTC),
        end=end,
    )
    assert run.trades[0].decision_time > run.end


@pytest.mark.parametrize(
    "status",
    [StrategyRunStatusV2.FAILED, StrategyRunStatusV2.FAILED_PARITY],
)
def test_failed_runs_with_candidates_and_empty_trades(status: StrategyRunStatusV2) -> None:
    run = _valid_run(status=status, trades=(), candidate_count=15)
    assert run.trade_count == 0
    assert run.candidate_count == 15
    assert run.net_pnl_usdt == Decimal("0")


def test_source_event_id_accepts_edc_and_cluster_forms() -> None:
    edc = SourceEventIdV2(value="edc:1ef21699f01b9131ebee")
    csw = SourceEventIdV2(value="csw:500521a95c4b4ffb")
    assert edc.value.startswith("edc:")
    assert csw.value.startswith("csw:")
    with pytest.raises(ValueError):
        SourceEventIdV2(value="")
    with pytest.raises(TypeError):
        SourceEventIdV2(value=None)  # type: ignore[arg-type]


def test_zero_offset_fixed_timezone_accepted_without_normalization() -> None:
    fixed = timezone(timedelta(0))
    decision = datetime(2026, 7, 1, 2, 30, tzinfo=fixed)
    entry = datetime(2026, 7, 1, 2, 35, tzinfo=fixed)
    trade = _valid_trade(decision_time=decision, entry_time=entry)
    assert trade.decision_time.tzinfo is fixed


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


def test_run_summary_properties() -> None:
    t1 = _valid_trade(source_event_id="edc:aaaaaaaaaaaaaaaaaaaa")
    t2 = replace(
        _valid_trade(
            symbol="BTCUSDT",
            source_event_id="edc:bbbbbbbbbbbbbbbbbbbb",
            decision_time=_dt(4, 0),
            entry_time=_dt(4, 5),
            exit_time=_dt(5, 0),
        ),
        gross_pnl_usdt=Decimal("3.0"),
        costs_usdt=Decimal("1.1"),
        net_pnl_usdt=Decimal("1.9"),
    )
    run = _valid_run(
        trades=(t1, t2),
        symbols=("XRPUSDT", "BTCUSDT"),
        candidate_count=2,
    )
    assert run.trade_count == 2
    assert run.gross_pnl_usdt == Decimal("10.5")
    assert run.costs_usdt == Decimal("2.2")
    assert run.net_pnl_usdt == Decimal("8.3")
    assert run.symbols_with_trades == ("BTCUSDT", "XRPUSDT")
    field_names = {f.name for f in fields(StrategyRunResultV2)}
    assert "trade_count" not in field_names
    assert "gross_pnl_usdt" not in field_names
    assert "costs_usdt" not in field_names
    assert "net_pnl_usdt" not in field_names
    assert "symbols_with_trades" not in field_names


def test_summary_properties_skip_unresolved_pnl() -> None:
    resolved = _valid_trade()
    unresolved = _valid_trade(
        exit_reason=TradeExitReasonV2.INCOMPLETE_OUTCOME_HORIZON,
        unresolved=True,
        source_event_id="edc:cccccccccccccccccccc",
        decision_time=_dt(5, 0),
        entry_time=_dt(5, 5),
    )
    run = _valid_run(trades=(resolved, unresolved), candidate_count=2)
    assert run.trade_count == 2
    assert run.net_pnl_usdt == Decimal("6.4")


# ---------------------------------------------------------------------------
# Determinism / immutability
# ---------------------------------------------------------------------------


def test_equality_and_hashability() -> None:
    a = _valid_run()
    b = _valid_run()
    assert a == b
    assert hash(a) == hash(b)
    assert {a, b} == {a}


def test_frozen_rejects_mutation() -> None:
    trade = _valid_trade()
    run = _valid_run()
    with pytest.raises(Exception):
        trade.symbol = "BTCUSDT"  # type: ignore[misc]
    with pytest.raises(Exception):
        run.status = StrategyRunStatusV2.FAILED  # type: ignore[misc]


def test_closed_enums_match_legacy_evidence() -> None:
    assert {e.value for e in TradeExitReasonV2} == {
        "tp_exit",
        "sl_exit",
        "time_exit",
        "coverage_missing",
        "incomplete_outcome_horizon",
    }
    assert {e.value for e in StrategyRunStatusV2} == {
        "complete",
        "failed",
        "failed_parity",
    }
