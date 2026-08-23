"""Typed ConfigEntry / ParamValue contract tests."""

from __future__ import annotations

from decimal import Decimal

import pytest

from orderbook_analyse.strategy_lab.models import (
    BoolParam,
    ConfigEntry,
    DecimalParam,
    DurationParam,
    DurationUnit,
    DurationValue,
    IdentifierParam,
    IntParam,
    PluginKind,
    PluginRef,
    RateParam,
    RateUnit,
    RateValue,
    StringParam,
    TimeframeParam,
    TimeframeUnit,
    TimeframeValue,
)


def test_01_bool_true_is_not_integer() -> None:
    entry = ConfigEntry(key="enable_sync_cross", value=BoolParam(value=True))
    assert isinstance(entry.value, BoolParam)
    assert entry.value.value is True
    assert type(entry.value.value) is bool
    assert not isinstance(entry.value, IntParam)
    with pytest.raises(TypeError, match="exact int"):
        IntParam(value=True)  # type: ignore[arg-type]


def test_02_integer_nine_remains_integer() -> None:
    entry = ConfigEntry(key="ema_fast", value=IntParam(value=9))
    assert isinstance(entry.value, IntParam)
    assert entry.value.value == 9
    assert type(entry.value.value) is int


def test_03_decimal_remains_decimal() -> None:
    entry = ConfigEntry(
        key="band_compression_pct",
        value=DecimalParam(value=Decimal("0.15")),
    )
    assert isinstance(entry.value, DecimalParam)
    assert entry.value.value == Decimal("0.15")
    assert type(entry.value.value) is Decimal


def test_04_string_nine_distinct_from_int_nine() -> None:
    as_str = ConfigEntry(key="ema_fast", value=StringParam(value="9"))
    as_int = ConfigEntry(key="ema_fast", value=IntParam(value=9))
    assert as_str.value != as_int.value
    assert isinstance(as_str.value, StringParam)
    assert isinstance(as_int.value, IntParam)
    assert as_str.value.value == "9"
    assert as_int.value.value == 9


def test_05_rate_value_as_config() -> None:
    rate = RateValue(value=Decimal("4"), unit=RateUnit.BASIS_POINTS)
    entry = ConfigEntry(key="approach_bps", value=RateParam(value=rate))
    assert isinstance(entry.value, RateParam)
    assert entry.value.value.unit is RateUnit.BASIS_POINTS
    assert entry.value.value.value == Decimal("4")


def test_06_duration_value_as_config() -> None:
    dur = DurationValue(value=Decimal("8"), unit=DurationUnit.HOURS)
    entry = ConfigEntry(key="horizon", value=DurationParam(value=dur))
    assert isinstance(entry.value, DurationParam)
    assert entry.value.value.unit is DurationUnit.HOURS


def test_07_timeframe_value_as_config() -> None:
    tf = TimeframeValue(value=15, unit=TimeframeUnit.MINUTES)
    entry = ConfigEntry(key="signal_tf", value=TimeframeParam(value=tf))
    assert isinstance(entry.value, TimeframeParam)
    assert entry.value.value.value == 15


def test_08_float_not_accepted_as_decimal_param() -> None:
    with pytest.raises(TypeError, match="Decimal"):
        DecimalParam(value=0.15)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="ParamValue"):
        ConfigEntry(key="x", value=0.15)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="Decimal"):
        RateValue(value=0.15, unit=RateUnit.PERCENT)  # type: ignore[arg-type]


def test_09_config_sequences_remain_tuples() -> None:
    plugin = PluginRef(
        id="signal.test",
        version="1.0.0",
        kind=PluginKind.SIGNAL,
        config=(
            ConfigEntry(key="enable_sync_cross", value=BoolParam(value=True)),
            ConfigEntry(key="ema_fast", value=IntParam(value=9)),
        ),
    )
    assert isinstance(plugin.config, tuple)
    assert type(plugin.config) is tuple
    with pytest.raises(AttributeError):
        plugin.config.append(plugin.config[0])  # type: ignore[attr-defined]


def test_10_edc_shaped_typed_plugin_config() -> None:
    plugin = PluginRef(
        id="edc.detect_cross_events",
        version="1.0.0",
        kind=PluginKind.SIGNAL,
        config=(
            ConfigEntry(key="enable_sync_cross", value=BoolParam(value=True)),
            ConfigEntry(key="ema_fast", value=IntParam(value=9)),
            ConfigEntry(
                key="band_compression_pct",
                value=DecimalParam(value=Decimal("0.15")),
            ),
            ConfigEntry(
                key="mode_id",
                value=IdentifierParam(value="M0_STRICT_SYNC"),
            ),
        ),
    )
    by_key = {e.key: e.value for e in plugin.config}
    assert by_key["enable_sync_cross"] == BoolParam(value=True)
    assert by_key["ema_fast"] == IntParam(value=9)
    assert by_key["band_compression_pct"] == DecimalParam(value=Decimal("0.15"))
    assert isinstance(by_key["mode_id"], IdentifierParam)


def test_11_cluster_shaped_typed_plugin_config() -> None:
    plugin = PluginRef(
        id="cluster.sweep.signal",
        version="1.0.0",
        kind=PluginKind.SIGNAL,
        config=(
            ConfigEntry(key="minimum_cluster_pools", value=IntParam(value=3)),
            ConfigEntry(
                key="approach_bps",
                value=RateParam(
                    value=RateValue(
                        value=Decimal("4"),
                        unit=RateUnit.BASIS_POINTS,
                    )
                ),
            ),
        ),
    )
    by_key = {e.key: e.value for e in plugin.config}
    assert by_key["minimum_cluster_pools"] == IntParam(value=3)
    approach = by_key["approach_bps"]
    assert isinstance(approach, RateParam)
    assert approach.value.unit is RateUnit.BASIS_POINTS
    assert approach.value.value == Decimal("4")
