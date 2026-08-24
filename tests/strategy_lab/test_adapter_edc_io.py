"""Unit tests for EDC M0 market-data IO wrapper (no ClickHouse)."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from orderbook_analyse.strategy_lab.adapters import edc_io
from orderbook_analyse.strategy_lab.adapters.edc_io import (
    StrategyMarketDataError,
    load_edc_m0_market_data_v2,
)
from orderbook_analyse.strategy_lab.adapters.edc_m0 import EdcM0MarketDataV2
from orderbook_analyse.strategy_lab.decoder_v2 import load_strategy_v2_yaml_file
from orderbook_analyse.strategy_lab.models.contracts_v2.padding import (
    OutcomeEvaluationPaddingV2,
    SourceLoadingPaddingV2,
)
from orderbook_analyse.strategy_lab.models.enums import DurationUnit
from orderbook_analyse.strategy_lab.models.identifiers import StableIdentifier
from orderbook_analyse.strategy_lab.models.signals import PluginSignalSpec
from orderbook_analyse.strategy_lab.models.strategy import DurationValue
from orderbook_analyse.strategy_lab.models.warmup_v2 import WarmupSpecV2
from orderbook_analyse.strategy_lab.validation.catalogs import production_catalog_bundle_v2

UTC = timezone.utc
REPO = Path(__file__).resolve().parents[2]
EDC_YAML = REPO / "strategies" / "strategy_lab" / "edc_m0_strict_sync_v2.yaml"
_REQUIRED_KEYS = ("candles_1m", "trades", "ob", "oi", "liq", "pads")


class _FakeQueryResult:
    @property
    def result_rows(self) -> list[tuple[object, ...]]:
        return []


class _CountingClient:
    """Client that records query calls (must stay at 0 on Spec reject)."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    def query(
        self,
        sql: str,
        parameters: object = None,
        settings: object = None,
    ) -> _FakeQueryResult:
        self.queries.append(sql)
        return _FakeQueryResult()


@pytest.fixture(scope="module")
def catalogs():
    return production_catalog_bundle_v2()


@pytest.fixture(scope="module")
def edc_spec(catalogs):
    return load_strategy_v2_yaml_file(EDC_YAML)


def _empty_loader_payload() -> dict:
    cols = ["open_time", "open", "high", "low", "close", "volume"]
    empty = pd.DataFrame(columns=cols)
    return {
        "candles_1m": empty,
        "trades": pd.DataFrame(),
        "ob": pd.DataFrame(),
        "oi": pd.DataFrame(),
        "liq": pd.DataFrame(),
        "pads": {
            "warmup_pad_days": 5,
            "outcome_pad_hours": 12,
            "source_pad_hours": 2,
        },
    }


def _call(spec, catalogs, *, client=None, monkeypatch=None, loader=None):
    if loader is not None:
        assert monkeypatch is not None
        edc_io._ensure_legacy()
        monkeypatch.setattr(edc_io, "_load_strategy_market_data", loader)
    return load_edc_m0_market_data_v2(
        spec,
        catalogs,
        client=client or _CountingClient(),
        symbol="XRPUSDT",
        start=datetime(2026, 7, 24, tzinfo=UTC),
        end=datetime(2026, 8, 23, tzinfo=UTC),
    )


def test_maps_legacy_keys_to_edc_market_data(
    monkeypatch, edc_spec, catalogs
) -> None:
    payload = _empty_loader_payload()
    captured: dict[str, object] = {}

    def fake_load(client, symbol, start, end):
        captured["client"] = client
        captured["symbol"] = symbol
        captured["start"] = start
        captured["end"] = end
        return payload

    client = _CountingClient()
    market = _call(
        edc_spec, catalogs, client=client, monkeypatch=monkeypatch, loader=fake_load
    )
    assert type(market) is EdcM0MarketDataV2
    assert market.candles_1m is payload["candles_1m"]
    assert market.trades_1m is payload["trades"]
    assert market.orderbook_1m is payload["ob"]
    assert market.open_interest_1m is payload["oi"]
    assert market.liquidations is payload["liq"]
    assert captured["symbol"] == "XRPUSDT"
    assert captured["client"] is client
    # Stubbed loader: no client.query calls (legacy would issue exactly 5).
    assert client.queries == []


def test_rejects_wrong_plugin_zero_queries(edc_spec, catalogs) -> None:
    signal = edc_spec.signal
    assert type(signal) is PluginSignalSpec
    bad_plugin = replace(
        signal.plugin,
        plugin_id=StableIdentifier(value="not_edc"),
    )
    bad_spec = replace(edc_spec, signal=replace(signal, plugin=bad_plugin))
    client = _CountingClient()
    with pytest.raises(StrategyMarketDataError, match="plugin_id"):
        _call(bad_spec, catalogs, client=client)
    assert client.queries == []


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("candle_history", Decimal("96"), "candle_history"),
        ("auxiliary_source_history", Decimal("3"), "auxiliary_source_history"),
        ("post_window_duration", Decimal("8"), "post_window_duration"),
    ],
)
def test_rejects_spec_pad_mismatch_zero_queries(
    edc_spec, catalogs, field, value, match
) -> None:
    src = edc_spec.warmup.source_loading
    out = edc_spec.warmup.outcome_evaluation
    if field == "candle_history":
        bad_source = SourceLoadingPaddingV2(
            candle_history=DurationValue(value=value, unit=DurationUnit.HOURS),
            auxiliary_source_history=src.auxiliary_source_history,
        )
        bad_outcome = out
    elif field == "auxiliary_source_history":
        bad_source = SourceLoadingPaddingV2(
            candle_history=src.candle_history,
            auxiliary_source_history=DurationValue(
                value=value, unit=DurationUnit.HOURS
            ),
        )
        bad_outcome = out
    else:
        bad_source = src
        bad_outcome = OutcomeEvaluationPaddingV2(
            post_window_duration=DurationValue(value=value, unit=DurationUnit.HOURS)
        )
    bad_spec = replace(
        edc_spec,
        warmup=WarmupSpecV2(
            signal_engine=edc_spec.warmup.signal_engine,
            source_loading=bad_source,
            outcome_evaluation=bad_outcome,
        ),
    )
    client = _CountingClient()
    with pytest.raises(StrategyMarketDataError, match=match):
        _call(bad_spec, catalogs, client=client)
    assert client.queries == []


def test_rejects_loader_pad_mismatch(monkeypatch, edc_spec, catalogs) -> None:
    payload = _empty_loader_payload()
    payload["pads"] = {
        "warmup_pad_days": 5,
        "outcome_pad_hours": 12,
        "source_pad_hours": 99,
    }
    with pytest.raises(StrategyMarketDataError, match="source_pad_hours"):
        _call(
            edc_spec,
            catalogs,
            monkeypatch=monkeypatch,
            loader=lambda *a, **k: payload,
        )


def test_rejects_missing_pads_key(monkeypatch, edc_spec, catalogs) -> None:
    payload = _empty_loader_payload()
    del payload["pads"]
    with pytest.raises(StrategyMarketDataError, match="missing pads"):
        _call(
            edc_spec,
            catalogs,
            monkeypatch=monkeypatch,
            loader=lambda *a, **k: payload,
        )


@pytest.mark.parametrize("missing_key", _REQUIRED_KEYS)
def test_rejects_each_missing_required_key(
    monkeypatch, edc_spec, catalogs, missing_key
) -> None:
    payload = _empty_loader_payload()
    del payload[missing_key]
    with pytest.raises(StrategyMarketDataError, match="missing"):
        _call(
            edc_spec,
            catalogs,
            monkeypatch=monkeypatch,
            loader=lambda *a, **k: payload,
        )


def test_does_not_replace_missing_frame_with_empty(
    monkeypatch, edc_spec, catalogs
) -> None:
    payload = _empty_loader_payload()
    del payload["oi"]
    with pytest.raises(StrategyMarketDataError, match="missing keys: oi"):
        _call(
            edc_spec,
            catalogs,
            monkeypatch=monkeypatch,
            loader=lambda *a, **k: payload,
        )


def test_rejects_naive_datetimes_zero_queries(edc_spec, catalogs) -> None:
    client = _CountingClient()
    with pytest.raises(StrategyMarketDataError, match="timezone-aware"):
        load_edc_m0_market_data_v2(
            edc_spec,
            catalogs,
            client=client,
            symbol="XRPUSDT",
            start=datetime(2026, 7, 24),
            end=datetime(2026, 8, 23, tzinfo=UTC),
        )
    assert client.queries == []


def test_rejects_client_without_query(edc_spec, catalogs) -> None:
    with pytest.raises(StrategyMarketDataError, match="query"):
        load_edc_m0_market_data_v2(
            edc_spec,
            catalogs,
            client=object(),  # type: ignore[arg-type]
            symbol="XRPUSDT",
            start=datetime(2026, 7, 24, tzinfo=UTC),
            end=datetime(2026, 8, 23, tzinfo=UTC),
        )


def test_public_api_has_no_any_annotations() -> None:
    import ast
    from pathlib import Path

    tree = ast.parse(
        Path(edc_io.__file__).read_text(encoding="utf-8"),
        filename=edc_io.__file__,
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "load_edc_m0_market_data_v2":
            for ann in list(node.args.args) + list(node.args.kwonlyargs):
                if ann.annotation is None:
                    continue
                text = ast.unparse(ann.annotation)
                assert "Any" not in text, text
            if node.returns is not None:
                assert "Any" not in ast.unparse(node.returns)


def test_legacy_pads_match_edc_spec_constants(edc_spec) -> None:
    edc_io._ensure_legacy()
    assert edc_io._WARMUP_PAD_DAYS == 5
    assert edc_io._OUTCOME_PAD_HOURS == 12
    assert edc_io._SOURCE_PAD_HOURS == 2
    candle = edc_spec.warmup.source_loading.candle_history
    aux = edc_spec.warmup.source_loading.auxiliary_source_history
    out = edc_spec.warmup.outcome_evaluation.post_window_duration
    assert type(candle) is DurationValue and candle.value == Decimal("120")
    assert type(aux) is DurationValue and aux.value == Decimal("2")
    assert type(out) is DurationValue and out.value == Decimal("12")


def test_inputs_not_mutated(monkeypatch, edc_spec, catalogs) -> None:
    payload = _empty_loader_payload()
    before = deepcopy(payload)

    def fake_load(client, symbol, start, end):
        return payload

    market = _call(
        edc_spec, catalogs, monkeypatch=monkeypatch, loader=fake_load
    )
    assert market.candles_1m is payload["candles_1m"]
    assert payload.keys() == before.keys()
    for k in ("candles_1m", "trades", "ob", "oi", "liq"):
        pd.testing.assert_frame_equal(payload[k], before[k])
    assert payload["pads"] == before["pads"]
