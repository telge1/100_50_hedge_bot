"""EDC M0 Strict Sync Strategy Lab adapter (P2B).

Composes existing shared-strategy detection and the TP/SL engine.
Does not reimplement EMA crosses, confirmation, or exit logic.

Legacy engines load lazily on first ``execute_edc_m0_strict_sync_v2`` call via
fixed importlib module paths (not Strategy-File controllable). This keeps
``import orderbook_analyse.strategy_lab.adapters`` free of Legacy side effects
while satisfying Phase-1 static import isolation for non-adapter modules.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Mapping

import pandas as pd

from orderbook_analyse.strategy_lab.catalogs.v2.models import CATALOG_CONTRACT_VERSION
from orderbook_analyse.strategy_lab.compiler_v2 import (
    CompiledStrategyV2,
    compile_strategy_v2,
)
from orderbook_analyse.strategy_lab.models.contracts_v2.data_requirement import EntrySpecV2
from orderbook_analyse.strategy_lab.models.contracts_v2.enums import (
    AvailabilityTimingV2,
    EntryPriceReferenceV2,
    EntryReferenceRuleV2,
    EntryTimingAnchorV2,
    PortfolioEvaluationModeV2,
    ResearchConfirmationPolicyV2,
)
from orderbook_analyse.strategy_lab.models.enums import (
    DurationUnit,
    RateUnit,
    SameBarPriority,
    SideName,
    TimeframeUnit,
)
from orderbook_analyse.strategy_lab.models.identifiers import ContractVersion, StableIdentifier
from orderbook_analyse.strategy_lab.models.signals import PluginSignalSpec
from orderbook_analyse.strategy_lab.models.strategy import IntParam
from orderbook_analyse.strategy_lab.models.strategy_v2 import StrategySpecV2
from orderbook_analyse.strategy_lab.results_v2 import (
    SourceEventIdV2,
    StrategyRunResultV2,
    StrategyRunStatusV2,
    StrategyTradeV2,
    TradeExitReasonV2,
)
from orderbook_analyse.strategy_lab.validation.catalogs import CatalogBundleV2
from orderbook_analyse.strategy_lab.validation.models import ValidationFailedError
from orderbook_analyse.strategy_lab.validation.p4c import require_valid_strategy_v2_p4c

_PLUGIN_ID = "edc_m0_strict_sync"
_MODE_ID = "m0_strict_sync"
_LEGACY_MODE_ID = "M0_STRICT_SYNC"
_SUPPORTIVE = "CORE_RESEARCH_SUPPORTIVE"
_SIGNAL_TF = "5m"
_ZERO = timedelta(0)
_CANDLE_COLUMNS = ("open_time", "open", "high", "low", "close", "volume")

# Populated lazily by ``_ensure_legacy``. Unit tests may monkeypatch symbols after load.
_legacy_loaded = False
_EmaDualCrossConfig: Any = None
attach_atr: Any = None
aggregate_timeframe: Any = None
attach_emas: Any = None
detect_strict_sync_baseline: Any = None
evaluate_candidates_canonical: Any = None
NOTIONAL_USDT: Any = None
simulate_tpsl_trade: Any = None
apply_costs: Any = None

_EXIT_REASON_MAP: Mapping[str, TradeExitReasonV2] = {
    "TP_EXIT": TradeExitReasonV2.TP_EXIT,
    "SL_EXIT": TradeExitReasonV2.SL_EXIT,
    "TIME_EXIT": TradeExitReasonV2.TIME_EXIT,
    "COVERAGE_MISSING": TradeExitReasonV2.COVERAGE_MISSING,
    "INCOMPLETE_OUTCOME_HORIZON": TradeExitReasonV2.INCOMPLETE_OUTCOME_HORIZON,
}

_SIDE_MAP: Mapping[str, SideName] = {
    "BULLISH": SideName.LONG,
    "BEARISH": SideName.SHORT,
}


class StrategyAdapterError(ValueError):
    """Deterministic adapter rejection (invalid Spec/Compiled/params)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class EdcM0MarketDataV2:
    """Public in-memory market frames for one EDC M0 adapter run.

    ``load_strategy_market_data`` returns an untyped dict; this named type is the
    public adapter surface for the same frames without ``dict``/``Any``.
    """

    candles_1m: pd.DataFrame
    trades_1m: pd.DataFrame
    orderbook_1m: pd.DataFrame
    open_interest_1m: pd.DataFrame
    liquidations: pd.DataFrame

    def __post_init__(self) -> None:
        for name in (
            "candles_1m",
            "trades_1m",
            "orderbook_1m",
            "open_interest_1m",
            "liquidations",
        ):
            value = getattr(self, name)
            if not isinstance(value, pd.DataFrame):
                raise TypeError(f"{name} must be a pandas DataFrame")


@dataclass(frozen=True, slots=True, kw_only=True)
class _EdcM0BoundParams:
    ema_fast: int
    ema_medium: int
    ema_slow: int
    atr_period: int
    signal_minutes: int
    execution_minutes: int
    tp_pct: Decimal
    sl_pct: Decimal
    horizon_min: int
    roundtrip_cost_pct: Decimal
    fixed_notional: Decimal
    mode_id: StableIdentifier
    confirmation_policy: ResearchConfirmationPolicyV2
    warmup_bars: int


def _ensure_legacy() -> None:
    """Load fixed Legacy modules once on first adapter execute."""
    global _legacy_loaded
    global _EmaDualCrossConfig, attach_atr, aggregate_timeframe, attach_emas
    global detect_strict_sync_baseline, evaluate_candidates_canonical
    global NOTIONAL_USDT, simulate_tpsl_trade, apply_costs

    if _legacy_loaded:
        return
    try:
        _EmaDualCrossConfig = importlib.import_module(
            "orderbook_analyse.ema_dual_cross_multisource.config"
        ).EmaDualCrossConfig
        attach_atr = importlib.import_module(
            "orderbook_analyse.ema_dual_cross_multisource.ema_candidate"
        ).attach_atr
        aggregate_timeframe = importlib.import_module(
            "orderbook_analyse.cluster_sweep_research.clickhouse_source"
        ).aggregate_timeframe
        attach_emas = importlib.import_module(
            "orderbook_analyse.cluster_sweep_research.ema_features"
        ).attach_emas
        detect_strict_sync_baseline = importlib.import_module(
            "orderbook_analyse.ema_dual_cross_multisource.tolerance_research.detect_bar_gap"
        ).detect_strict_sync_baseline
        evaluate_candidates_canonical = importlib.import_module(
            "orderbook_analyse.ema_dual_cross_multisource.tolerance_research"
            ".shared_strategy.candidates"
        ).evaluate_candidates_canonical
        tpsl = importlib.import_module(
            "orderbook_analyse.ema_dual_cross_multisource.tolerance_research.tpsl_pnl_engine"
        )
        NOTIONAL_USDT = tpsl.NOTIONAL_USDT
        simulate_tpsl_trade = tpsl.simulate_tpsl_trade
        apply_costs = tpsl.apply_costs
    except ImportError as exc:
        raise StrategyAdapterError(
            f"failed to load EDC legacy engine modules: {exc}"
        ) from exc
    _legacy_loaded = True


def execute_edc_m0_strict_sync_v2(
    spec: StrategySpecV2,
    compiled: CompiledStrategyV2,
    catalogs: CatalogBundleV2,
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    market_data: EdcM0MarketDataV2,
) -> StrategyRunResultV2:
    """Run EDC M0 Strict Sync for one symbol on preloaded in-memory market data."""
    if type(symbol) is not str or not symbol or symbol != symbol.strip():
        raise StrategyAdapterError("symbol must be a non-empty str without padding")
    start_u = _require_utc(start, field_name="start")
    end_u = _require_utc(end, field_name="end")
    if end_u <= start_u:
        raise StrategyAdapterError("end must be > start")
    if type(market_data) is not EdcM0MarketDataV2:
        raise StrategyAdapterError("market_data must be EdcM0MarketDataV2")

    _assert_compiled_matches_spec(spec, compiled, catalogs)
    params = _bind_edc_m0_params(spec)
    _ensure_legacy()
    if float(params.fixed_notional) != float(NOTIONAL_USDT):
        raise StrategyAdapterError(
            "fixed_notional must equal legacy NOTIONAL_USDT "
            f"({NOTIONAL_USDT}); apply_costs has no notional argument"
        )
    _require_candle_columns(market_data.candles_1m)

    cfg = _EmaDualCrossConfig(
        ema_fast=params.ema_fast,
        ema_medium=params.ema_medium,
        ema_slow=params.ema_slow,
        atr_period=params.atr_period,
        # Remaining gates are frozen M0 research constants (not Spec fields).
        # Passed explicitly so dataclass defaults are not silent outcome drivers.
        band_compression_pct=0.15,
        band_compression_atr=0.35,
        max_band_lookback=5,
        max_total_band_atr=0.55,
        flat_slope_atr=0.02,
        rebound_body_atr_min=0.45,
        rebound_range_atr_min=0.55,
        rebound_ema_dist_atr_max=0.40,
        enable_sync_cross=True,
        enable_compressed_rebound=False,
        require_ob_for_allow=True,
        require_trades_for_allow=True,
        require_candles=True,
        require_oi_for_allow=True,
        require_liq_for_allow=True,
        ob_stale_minutes=30,
        episode_reset_bars=48,
    )
    signal_df = _prepare_signal_frame(
        market_data.candles_1m,
        timeframe=_SIGNAL_TF,
        ema_fast=params.ema_fast,
        ema_medium=params.ema_medium,
        ema_slow=params.ema_slow,
        atr_period=params.atr_period,
    )
    raw = detect_strict_sync_baseline(
        signal_df,
        symbol=symbol,
        timeframe=_SIGNAL_TF,
        cfg=cfg,
    )
    candidates = evaluate_candidates_canonical(
        raw,
        df=signal_df,
        symbol=symbol,
        timeframe=_SIGNAL_TF,
        window_start=start_u,
        window_end=end_u,
        trades_1m=market_data.trades_1m,
        ob_1m=market_data.orderbook_1m,
        oi_1m=market_data.open_interest_1m,
        liq=market_data.liquidations,
        window_report=None,
        mode_id=_LEGACY_MODE_ID,
    )
    supportive = [
        c
        for c in candidates
        if c.get("core_research_verdict") == _SUPPORTIVE
    ]
    supportive.sort(
        key=lambda c: (
            str(c.get("symbol") or ""),
            str(c.get("decision_at") or ""),
            str(c.get("candidate_id") or ""),
        )
    )

    cost_pct = float(params.roundtrip_cost_pct)
    tp_pct = float(params.tp_pct)
    sl_pct = float(params.sl_pct)
    trades: list[StrategyTradeV2] = []
    for cand in supportive:
        paid = _simulate_outcome(
            market_data.candles_1m,
            candidate=cand,
            tp_pct=tp_pct,
            sl_pct=sl_pct,
            horizon_min=params.horizon_min,
            roundtrip_cost_pct=cost_pct,
        )
        trades.append(
            _map_trade(
                candidate=cand,
                paid=paid,
                mode_id=params.mode_id,
                confirmation_policy=params.confirmation_policy,
                roundtrip_cost_pct=params.roundtrip_cost_pct,
            )
        )

    return StrategyRunResultV2(
        strategy_hash=compiled.strategy_hash,
        plugin_id=StableIdentifier(value=_PLUGIN_ID),
        plugin_contract_version=ContractVersion(value=CATALOG_CONTRACT_VERSION),
        universe=spec.universe,
        start=start_u,
        end=end_u,
        symbols=(symbol,),
        signal_timeframe=spec.timeframes.signal,
        execution_timeframe=spec.timeframes.execution,
        roundtrip_cost=spec.costs.roundtrip_cost,
        slippage_status=spec.costs.slippage,
        funding_status=spec.costs.funding,
        status=StrategyRunStatusV2.COMPLETE,
        candidate_count=len(supportive),
        trades=tuple(trades),
    )


def _require_candle_columns(candles_1m: pd.DataFrame) -> None:
    missing = [c for c in _CANDLE_COLUMNS if c not in candles_1m.columns]
    if missing:
        raise StrategyAdapterError(
            f"candles_1m missing required columns: {', '.join(missing)}"
        )


def _assert_compiled_matches_spec(
    spec: object,
    compiled: object,
    catalogs: CatalogBundleV2,
) -> None:
    if type(spec) is not StrategySpecV2:
        raise StrategyAdapterError("spec must be StrategySpecV2")
    if type(compiled) is not CompiledStrategyV2:
        raise StrategyAdapterError("compiled must be CompiledStrategyV2")
    if type(spec.signal) is not PluginSignalSpec:
        raise StrategyAdapterError("spec.signal must be PluginSignalSpec")
    signal = spec.signal
    if signal.plugin.plugin_id.value != _PLUGIN_ID:
        raise StrategyAdapterError(
            f"plugin_id must be {_PLUGIN_ID!r}, got {signal.plugin.plugin_id.value!r}"
        )
    if signal.plugin.contract_version.value != CATALOG_CONTRACT_VERSION:
        raise StrategyAdapterError(
            "plugin contract_version must be "
            f"{CATALOG_CONTRACT_VERSION!r}, got {signal.plugin.contract_version.value!r}"
        )
    if signal.mode_id is None or signal.mode_id.value != _MODE_ID:
        raise StrategyAdapterError(
            f"mode_id must be {_MODE_ID!r}, got "
            f"{None if signal.mode_id is None else signal.mode_id.value!r}"
        )
    if signal.confirmation_policy is not ResearchConfirmationPolicyV2.CORE_RESEARCH_SUPPORTIVE:
        raise StrategyAdapterError(
            "confirmation_policy must be core_research_supportive"
        )
    try:
        require_valid_strategy_v2_p4c(spec, catalogs)
    except ValidationFailedError as exc:
        raise StrategyAdapterError("StrategySpecV2 failed P4C validation") from exc
    recomputed = compile_strategy_v2(spec, catalogs)
    if recomputed.strategy_hash != compiled.strategy_hash:
        raise StrategyAdapterError("compiled.strategy_hash does not match Spec")
    if recomputed.canonical_bytes != compiled.canonical_bytes:
        raise StrategyAdapterError("compiled.canonical_bytes do not match Spec")


def _bind_edc_m0_params(spec: StrategySpecV2) -> _EdcM0BoundParams:
    signal = spec.signal
    assert type(signal) is PluginSignalSpec
    assert signal.mode_id is not None
    assert signal.confirmation_policy is not None

    if spec.timeframes.signal.unit is not TimeframeUnit.MINUTES:
        raise StrategyAdapterError("signal timeframe unit must be minutes")
    if spec.timeframes.signal.value != 5:
        raise StrategyAdapterError("signal timeframe must be 5 minutes")
    if spec.timeframes.execution.unit is not TimeframeUnit.MINUTES:
        raise StrategyAdapterError("execution timeframe unit must be minutes")
    if spec.timeframes.execution.value != 1:
        raise StrategyAdapterError("execution timeframe must be 1 minute")

    ema_fast = _feature_period(spec, alias="ema_fast", feature_id="ema")
    ema_medium = _feature_period(spec, alias="ema_medium", feature_id="ema")
    ema_slow = _feature_period(spec, alias="ema_slow", feature_id="ema")
    atr_period = _feature_period(spec, alias="atr", feature_id="atr_wilder")
    if (ema_fast, ema_medium, ema_slow, atr_period) != (9, 20, 59, 14):
        raise StrategyAdapterError(
            "EDC M0 requires feature periods ema 9/20/59 and atr 14 from Spec"
        )

    if spec.warmup.signal_engine.minimum_bars != 79:
        raise StrategyAdapterError("signal warmup minimum_bars must be 79")
    if spec.warmup.signal_engine.bar_timeframe.value != 5:
        raise StrategyAdapterError("signal warmup bar_timeframe must be 5 minutes")

    _require_entry_contract(spec.entry)

    if spec.intrabar_policy.same_bar_priority is not SameBarPriority.SL_FIRST:
        raise StrategyAdapterError("intrabar same_bar_priority must be sl_first")

    tp = spec.exit.take_profit
    sl = spec.exit.stop_loss
    if tp.unit is not RateUnit.PERCENT or sl.unit is not RateUnit.PERCENT:
        raise StrategyAdapterError("take_profit/stop_loss unit must be percent")
    horizon = spec.exit.horizon
    if horizon.unit is not DurationUnit.HOURS:
        raise StrategyAdapterError("exit horizon unit must be hours")
    if horizon.value <= 0:
        raise StrategyAdapterError("exit horizon must be > 0")
    horizon_min = int(horizon.value * Decimal(60))
    if Decimal(horizon_min) != horizon.value * Decimal(60):
        raise StrategyAdapterError("exit horizon must convert to a whole number of minutes")

    cost = spec.costs.roundtrip_cost
    if cost.unit is not RateUnit.PERCENT:
        raise StrategyAdapterError("roundtrip_cost unit must be percent")
    if cost.value < 0:
        raise StrategyAdapterError("roundtrip_cost must be >= 0")

    notional = spec.execution_assumptions.fixed_notional
    if type(notional) is not Decimal:
        raise StrategyAdapterError("fixed_notional must be Decimal")
    # Engine notional is verified after lazy load in execute; bind stores Spec value.
    if notional != Decimal("1000"):
        raise StrategyAdapterError(
            "fixed_notional must be 1000 USDT (legacy apply_costs NOTIONAL_USDT)"
        )
    if spec.execution_assumptions.execution_timeframe.value != 1:
        raise StrategyAdapterError("execution_assumptions timeframe must be 1m")

    if spec.portfolio_assumptions.compounding is not False:
        raise StrategyAdapterError("compounding must be False")
    if (
        spec.portfolio_assumptions.evaluation_mode
        is not PortfolioEvaluationModeV2.PER_TRADE_INDEPENDENT
    ):
        raise StrategyAdapterError(
            "portfolio evaluation_mode must be per_trade_independent"
        )

    return _EdcM0BoundParams(
        ema_fast=ema_fast,
        ema_medium=ema_medium,
        ema_slow=ema_slow,
        atr_period=atr_period,
        signal_minutes=5,
        execution_minutes=1,
        tp_pct=tp.value,
        sl_pct=sl.value,
        horizon_min=horizon_min,
        roundtrip_cost_pct=cost.value,
        fixed_notional=notional,
        mode_id=signal.mode_id,
        confirmation_policy=signal.confirmation_policy,
        warmup_bars=79,
    )


def _require_entry_contract(entry: EntrySpecV2) -> None:
    if entry.signal_decision_timing is not AvailabilityTimingV2.SIGNAL_BAR_CLOSE:
        raise StrategyAdapterError("entry signal_decision_timing must be signal_bar_close")
    if (
        entry.entry_reference_rule
        is not EntryReferenceRuleV2.SIGNAL_TF_NEXT_OPEN_AFTER_SIGNAL_BAR
    ):
        raise StrategyAdapterError(
            "entry_reference_rule must be signal_tf_next_open_after_signal_bar"
        )
    if entry.entry_timing_anchor is not EntryTimingAnchorV2.SIGNAL_TIMEFRAME_BAR_OPEN:
        raise StrategyAdapterError(
            "entry_timing_anchor must be signal_timeframe_bar_open"
        )
    if entry.entry_price_reference is not EntryPriceReferenceV2.BAR_OPEN:
        raise StrategyAdapterError("entry_price_reference must be bar_open")


def _feature_period(spec: StrategySpecV2, *, alias: str, feature_id: str) -> int:
    matches = [
        f
        for f in spec.features
        if f.alias.value == alias and f.catalog_feature_id.value == feature_id
    ]
    if len(matches) != 1:
        raise StrategyAdapterError(
            f"expected exactly one feature binding alias={alias!r} feature_id={feature_id!r}"
        )
    feature = matches[0]
    period_bindings = [b for b in feature.bindings if b.name.value == "period"]
    if len(period_bindings) != 1:
        raise StrategyAdapterError(f"feature {alias!r} requires a single period binding")
    value = period_bindings[0].value
    if type(value) is not IntParam:
        raise StrategyAdapterError(f"feature {alias!r} period must be IntParam")
    return value.value


def _prepare_signal_frame(
    candles_1m: pd.DataFrame,
    *,
    timeframe: str,
    ema_fast: int,
    ema_medium: int,
    ema_slow: int,
    atr_period: int,
) -> pd.DataFrame:
    df = aggregate_timeframe(candles_1m, timeframe)
    df = attach_emas(df, fast=ema_fast, medium=ema_medium, slow=ema_slow)
    df = attach_atr(df, atr_period)
    return df


def _simulate_outcome(
    candles_1m: pd.DataFrame,
    *,
    candidate: Mapping[str, object],
    tp_pct: float,
    sl_pct: float,
    horizon_min: int,
    roundtrip_cost_pct: float,
) -> dict[str, object]:
    direction = str(candidate["direction"])
    entry_at = candidate["entry_at"]
    entry_price = float(candidate["entry_price"])
    # require_full_horizon=False + incomplete_if_truncated_path=True matches the
    # frozen shared outcome path without using simulate_canonical_trade (0.15%).
    sim = simulate_tpsl_trade(
        candles_1m,
        direction=direction,
        entry_at=entry_at,
        entry_price=entry_price,
        tp_pct=tp_pct,
        sl_pct=sl_pct,
        horizon_min=horizon_min,
        require_full_horizon=False,
        incomplete_if_truncated_path=True,
    )
    return apply_costs(sim, roundtrip_cost_pct, funding_pnl_usdt=0.0)


def _map_trade(
    *,
    candidate: Mapping[str, object],
    paid: Mapping[str, object],
    mode_id: StableIdentifier,
    confirmation_policy: ResearchConfirmationPolicyV2,
    roundtrip_cost_pct: Decimal,
) -> StrategyTradeV2:
    direction = str(candidate["direction"])
    if direction not in _SIDE_MAP:
        raise StrategyAdapterError(f"unsupported legacy direction: {direction!r}")
    exit_reason_raw = str(paid.get("exit_reason"))
    if exit_reason_raw not in _EXIT_REASON_MAP:
        raise StrategyAdapterError(f"unsupported exit_reason: {exit_reason_raw!r}")
    exit_reason = _EXIT_REASON_MAP[exit_reason_raw]

    return StrategyTradeV2(
        source_event_id=SourceEventIdV2(value=str(candidate["candidate_id"])),
        symbol=str(candidate["symbol"]),
        side=_SIDE_MAP[direction],
        decision_time=_parse_utc(candidate["decision_at"], field_name="decision_at"),
        entry_time=_parse_utc(
            paid.get("entry_at") or candidate["entry_at"], field_name="entry_at"
        ),
        entry_price=_decimal_from_legacy(
            paid.get("entry_price", candidate["entry_price"])
        ),
        exit_time=_optional_utc(paid.get("exit_at"), field_name="exit_at"),
        exit_price=_optional_decimal(paid.get("exit_price")),
        exit_reason=exit_reason,
        gross_return_pct=_optional_decimal(paid.get("gross_return_pct")),
        roundtrip_cost_pct=roundtrip_cost_pct,
        net_return_pct=_optional_decimal(paid.get("net_return_pct")),
        gross_pnl_usdt=_optional_decimal(paid.get("gross_pnl_usdt")),
        costs_usdt=_optional_decimal(paid.get("costs_usdt")),
        net_pnl_usdt=_optional_decimal(paid.get("net_pnl_usdt")),
        mode_id=mode_id,
        confirmation_policy=confirmation_policy,
    )


def _require_utc(value: datetime, *, field_name: str) -> datetime:
    if type(value) is not datetime:
        raise StrategyAdapterError(f"{field_name} must be datetime")
    if value.tzinfo is None:
        raise StrategyAdapterError(f"{field_name} must be timezone-aware UTC")
    offset = value.utcoffset()
    if offset is None or offset != _ZERO:
        raise StrategyAdapterError(f"{field_name} must be UTC (zero offset)")
    return value


def _parse_utc(value: object, *, field_name: str) -> datetime:
    if isinstance(value, datetime):
        return _require_utc(value, field_name=field_name)
    if type(value) is not str:
        raise StrategyAdapterError(f"{field_name} must be datetime or ISO str")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _require_utc(parsed, field_name=field_name)


def _optional_utc(value: object, *, field_name: str) -> datetime | None:
    if value is None:
        return None
    return _parse_utc(value, field_name=field_name)


def _decimal_from_legacy(value: object) -> Decimal:
    if type(value) is Decimal:
        return value
    if value is None:
        raise StrategyAdapterError("Decimal field must not be None")
    return Decimal(str(value))


def _optional_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    return _decimal_from_legacy(value)
