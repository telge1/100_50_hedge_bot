"""EDC M0 market-data IO (P2D1).

Thin wrapper: validate Spec pads → call existing ``load_strategy_market_data``
→ map dict keys → ``EdcM0MarketDataV2``.

Does not open a global ClickHouse client, invent pads, run detection, or
reimplement SQL. Legacy modules load lazily on the first call via fixed
importlib paths (Phase-1 AST isolation).
"""

from __future__ import annotations

import importlib
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Mapping, Protocol, Sequence, runtime_checkable

import pandas as pd

from orderbook_analyse.strategy_lab.adapters.edc_m0 import EdcM0MarketDataV2
from orderbook_analyse.strategy_lab.catalogs.v2.models import CATALOG_CONTRACT_VERSION
from orderbook_analyse.strategy_lab.models.contracts_v2.enums import (
    ResearchConfirmationPolicyV2,
)
from orderbook_analyse.strategy_lab.models.contracts_v2.padding import (
    PaddingNotApplicable,
)
from orderbook_analyse.strategy_lab.models.enums import DurationUnit, TimeframeUnit
from orderbook_analyse.strategy_lab.models.signals import PluginSignalSpec
from orderbook_analyse.strategy_lab.models.strategy import DurationValue
from orderbook_analyse.strategy_lab.models.strategy_v2 import StrategySpecV2
from orderbook_analyse.strategy_lab.validation.catalogs import CatalogBundleV2
from orderbook_analyse.strategy_lab.validation.models import ValidationFailedError
from orderbook_analyse.strategy_lab.validation.p4c import require_valid_strategy_v2_p4c

_PLUGIN_ID = "edc_m0_strict_sync"
_MODE_ID = "m0_strict_sync"
_ZERO = timedelta(0)

# Exact legacy return keys from load_strategy_market_data (verified in source).
_KEY_CANDLES = "candles_1m"
_KEY_TRADES = "trades"
_KEY_OB = "ob"
_KEY_OI = "oi"
_KEY_LIQ = "liq"
_KEY_PADS = "pads"
_REQUIRED_FRAME_KEYS = (_KEY_CANDLES, _KEY_TRADES, _KEY_OB, _KEY_OI, _KEY_LIQ)

_load_strategy_market_data = None
_WARMUP_PAD_DAYS: int | None = None
_OUTCOME_PAD_HOURS: int | None = None
_SOURCE_PAD_HOURS: int | None = None
_legacy_loaded = False


class StrategyMarketDataError(ValueError):
    """Deterministic market-data IO rejection."""


@runtime_checkable
class ClickHouseQueryResult(Protocol):
    """Minimal surface used by cluster_sweep_research.clickhouse_source._q."""

    @property
    def result_rows(self) -> Sequence[Sequence[object]]: ...


@runtime_checkable
class ClickHouseQueryClient(Protocol):
    """Duck-typed ClickHouse client: must expose ``query`` like clickhouse-connect.

    No connection is created here; the caller passes an already-open client.
    """

    def query(
        self,
        sql: str,
        parameters: Mapping[str, object] | None = None,
        settings: Mapping[str, object] | None = None,
    ) -> ClickHouseQueryResult: ...


def load_edc_m0_market_data_v2(
    spec: StrategySpecV2,
    catalogs: CatalogBundleV2,
    *,
    client: ClickHouseQueryClient,
    symbol: str,
    start: datetime,
    end: datetime,
) -> EdcM0MarketDataV2:
    """Load EDC M0 frames via legacy ``load_strategy_market_data`` and map to V2.

    Pads are not invented here: Spec warmup must match the frozen loader
    constants (5d candle warm / 2h source / 12h outcome). The loader then
    applies those same windows. Returns five typed DataFrames only.
    """
    if type(spec) is not StrategySpecV2:
        raise StrategyMarketDataError("spec must be StrategySpecV2")
    if type(catalogs) is not CatalogBundleV2:
        raise StrategyMarketDataError("catalogs must be CatalogBundleV2")
    if not isinstance(client, ClickHouseQueryClient):
        raise StrategyMarketDataError(
            "client must provide a query(...) method returning result_rows"
        )
    if type(symbol) is not str or not symbol or symbol != symbol.strip():
        raise StrategyMarketDataError("symbol must be a non-empty str without padding")
    start_u = _require_utc(start, field_name="start")
    end_u = _require_utc(end, field_name="end")
    if end_u <= start_u:
        raise StrategyMarketDataError("end must be > start")

    _assert_edc_m0_plugin(spec)
    _ensure_legacy()
    assert _WARMUP_PAD_DAYS is not None
    assert _OUTCOME_PAD_HOURS is not None
    assert _SOURCE_PAD_HOURS is not None
    assert _load_strategy_market_data is not None
    # Pads before P4C so mismatches are reported as explicit pad errors
    # (not only as opaque P4C failures).
    _assert_spec_pads_match_loader(
        spec,
        warmup_pad_days=_WARMUP_PAD_DAYS,
        outcome_pad_hours=_OUTCOME_PAD_HOURS,
        source_pad_hours=_SOURCE_PAD_HOURS,
    )
    _assert_edc_m0_p4c(spec, catalogs)

    raw = _load_strategy_market_data(client, symbol, start_u, end_u)
    return _map_loader_dict_to_market_data(
        raw,
        expected_pads={
            "warmup_pad_days": _WARMUP_PAD_DAYS,
            "outcome_pad_hours": _OUTCOME_PAD_HOURS,
            "source_pad_hours": _SOURCE_PAD_HOURS,
        },
    )


def _ensure_legacy() -> None:
    global _legacy_loaded, _load_strategy_market_data
    global _WARMUP_PAD_DAYS, _OUTCOME_PAD_HOURS, _SOURCE_PAD_HOURS
    if _legacy_loaded:
        return
    try:
        market_data = importlib.import_module(
            "orderbook_analyse.ema_dual_cross_multisource.tolerance_research"
            ".shared_strategy.market_data"
        )
        semantics = importlib.import_module(
            "orderbook_analyse.ema_dual_cross_multisource.tolerance_research"
            ".shared_strategy.semantics"
        )
        _load_strategy_market_data = market_data.load_strategy_market_data
        _WARMUP_PAD_DAYS = int(semantics.WARMUP_PAD_DAYS)
        _OUTCOME_PAD_HOURS = int(semantics.OUTCOME_PAD_HOURS)
        _SOURCE_PAD_HOURS = int(semantics.SOURCE_PAD_HOURS)
    except ImportError as exc:
        raise StrategyMarketDataError(
            f"failed to load EDC market-data legacy modules: {exc}"
        ) from exc
    _legacy_loaded = True


def _assert_edc_m0_plugin(spec: StrategySpecV2) -> None:
    if type(spec.signal) is not PluginSignalSpec:
        raise StrategyMarketDataError("spec.signal must be PluginSignalSpec")
    signal = spec.signal
    if signal.plugin.plugin_id.value != _PLUGIN_ID:
        raise StrategyMarketDataError(
            f"plugin_id must be {_PLUGIN_ID!r}, got {signal.plugin.plugin_id.value!r}"
        )
    if signal.plugin.contract_version.value != CATALOG_CONTRACT_VERSION:
        raise StrategyMarketDataError(
            "plugin contract_version must be "
            f"{CATALOG_CONTRACT_VERSION!r}, got {signal.plugin.contract_version.value!r}"
        )
    if signal.mode_id is None or signal.mode_id.value != _MODE_ID:
        raise StrategyMarketDataError(
            f"mode_id must be {_MODE_ID!r}, got "
            f"{None if signal.mode_id is None else signal.mode_id.value!r}"
        )
    if signal.confirmation_policy is not ResearchConfirmationPolicyV2.CORE_RESEARCH_SUPPORTIVE:
        raise StrategyMarketDataError(
            "confirmation_policy must be core_research_supportive"
        )


def _assert_edc_m0_p4c(spec: StrategySpecV2, catalogs: CatalogBundleV2) -> None:
    try:
        require_valid_strategy_v2_p4c(spec, catalogs)
    except ValidationFailedError as exc:
        raise StrategyMarketDataError("StrategySpecV2 failed P4C validation") from exc


def _assert_spec_pads_match_loader(
    spec: StrategySpecV2,
    *,
    warmup_pad_days: int,
    outcome_pad_hours: int,
    source_pad_hours: int,
) -> None:
    """Reject Spec pads that differ from the frozen loader constants."""
    candle = _require_duration_hours(
        spec.warmup.source_loading.candle_history,
        field_name="warmup.source_loading.candle_history",
    )
    aux = _require_duration_hours(
        spec.warmup.source_loading.auxiliary_source_history,
        field_name="warmup.source_loading.auxiliary_source_history",
    )
    outcome = _require_duration_hours(
        spec.warmup.outcome_evaluation.post_window_duration,
        field_name="warmup.outcome_evaluation.post_window_duration",
    )
    expected_candle_hours = Decimal(warmup_pad_days) * Decimal(24)
    if candle != expected_candle_hours:
        raise StrategyMarketDataError(
            "spec candle_history must equal loader WARMUP_PAD_DAYS "
            f"({warmup_pad_days}d = {expected_candle_hours}h), got {candle}h"
        )
    if aux != Decimal(source_pad_hours):
        raise StrategyMarketDataError(
            "spec auxiliary_source_history must equal loader SOURCE_PAD_HOURS "
            f"({source_pad_hours}h), got {aux}h"
        )
    if outcome != Decimal(outcome_pad_hours):
        raise StrategyMarketDataError(
            "spec post_window_duration must equal loader OUTCOME_PAD_HOURS "
            f"({outcome_pad_hours}h), got {outcome}h"
        )
    if spec.warmup.signal_engine.minimum_bars != 79:
        raise StrategyMarketDataError("signal warmup minimum_bars must be 79")
    if spec.warmup.signal_engine.bar_timeframe.unit is not TimeframeUnit.MINUTES:
        raise StrategyMarketDataError("signal warmup bar_timeframe unit must be minutes")
    if spec.warmup.signal_engine.bar_timeframe.value != 5:
        raise StrategyMarketDataError("signal warmup bar_timeframe must be 5 minutes")


def _require_duration_hours(value: object, *, field_name: str) -> Decimal:
    if type(value) is PaddingNotApplicable:
        raise StrategyMarketDataError(f"{field_name} must be a DurationValue, not N/A")
    if type(value) is not DurationValue:
        raise StrategyMarketDataError(f"{field_name} must be DurationValue")
    if value.unit is not DurationUnit.HOURS:
        raise StrategyMarketDataError(f"{field_name} unit must be hours")
    if value.value < 0:
        raise StrategyMarketDataError(f"{field_name} must be >= 0")
    return value.value


def _map_loader_dict_to_market_data(
    raw: object,
    *,
    expected_pads: Mapping[str, int],
) -> EdcM0MarketDataV2:
    if type(raw) is not dict:
        raise StrategyMarketDataError(
            "load_strategy_market_data must return a dict"
        )
    missing = [k for k in _REQUIRED_FRAME_KEYS if k not in raw]
    if missing:
        raise StrategyMarketDataError(
            "load_strategy_market_data missing keys: " + ", ".join(missing)
        )
    if _KEY_PADS not in raw:
        raise StrategyMarketDataError(
            "load_strategy_market_data missing pads metadata"
        )
    pads = raw[_KEY_PADS]
    if type(pads) is not dict:
        raise StrategyMarketDataError("pads metadata must be a dict")
    for key, expected in expected_pads.items():
        if pads.get(key) != expected:
            raise StrategyMarketDataError(
                f"loader pads[{key!r}] must be {expected!r}, got {pads.get(key)!r}"
            )
    for key in _REQUIRED_FRAME_KEYS:
        if not isinstance(raw[key], pd.DataFrame):
            raise StrategyMarketDataError(
                f"loader key {key!r} must be a pandas DataFrame"
            )
    return EdcM0MarketDataV2(
        candles_1m=raw[_KEY_CANDLES],
        trades_1m=raw[_KEY_TRADES],
        orderbook_1m=raw[_KEY_OB],
        open_interest_1m=raw[_KEY_OI],
        liquidations=raw[_KEY_LIQ],
    )


def _require_utc(value: datetime, *, field_name: str) -> datetime:
    if type(value) is not datetime:
        raise StrategyMarketDataError(f"{field_name} must be datetime")
    if value.tzinfo is None:
        raise StrategyMarketDataError(f"{field_name} must be timezone-aware UTC")
    offset = value.utcoffset()
    if offset is None or offset != _ZERO:
        raise StrategyMarketDataError(f"{field_name} must be UTC (zero offset)")
    return value
