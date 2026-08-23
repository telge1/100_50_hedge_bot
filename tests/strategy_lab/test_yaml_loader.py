"""P2 safe YAML loader tests (raw mapping only; no StrategySpec compile)."""

from __future__ import annotations

from decimal import Decimal
from io import StringIO
from pathlib import Path

import pytest

from orderbook_analyse.strategy_lab.loader import (
    StrategyYamlLoadError,
    is_raw_mapping,
    load_strategy_yaml,
    load_strategy_yaml_path,
)


def test_load_yaml_preserves_decimal_not_float() -> None:
    data = load_strategy_yaml("rate: 0.75\n")
    assert data["rate"] == Decimal("0.75")
    assert type(data["rate"]) is Decimal


def test_load_yaml_preserves_int_bool_null_string() -> None:
    data = load_strategy_yaml(
        "count: 9\nenabled: true\nflag: false\nname: alpha\nmissing: null\n"
    )
    assert data["count"] == 9
    assert type(data["count"]) is int
    assert data["enabled"] is True
    assert type(data["enabled"]) is bool
    assert data["flag"] is False
    assert type(data["flag"]) is bool
    assert data["name"] == "alpha"
    assert data["missing"] is None


def test_negative_and_scientific_decimal() -> None:
    data = load_strategy_yaml("neg: -2.25\nsci: 1.5e-3\n")
    assert type(data["neg"]) is Decimal
    assert data["neg"] == Decimal("-2.25")
    assert type(data["sci"]) is Decimal
    assert data["sci"] == Decimal("0.0015")


def test_env_placeholder_remains_literal_string() -> None:
    data = load_strategy_yaml("ref: ${GIT_HEAD}\n")
    assert data["ref"] == "${GIT_HEAD}"
    assert type(data["ref"]) is str


def test_utf8_content() -> None:
    data = load_strategy_yaml("title: Strategie α — Größe\n")
    assert "α" in data["title"]


def test_load_yaml_nested_mapping_and_list() -> None:
    text = """
metadata:
  strategy_id: demo
plugins:
  - id: a
    version: "1.0.0"
"""
    data = load_strategy_yaml(text)
    assert is_raw_mapping(data)
    assert data["metadata"]["strategy_id"] == "demo"
    assert data["plugins"][0]["id"] == "a"


@pytest.mark.parametrize(
    "text,match",
    [
        ("", "empty"),
        ("   \n", "empty"),
        ("- just\n- a\n", "mapping"),
        ("42\n", "mapping"),
        ("foo: [unterminated\n", "invalid YAML"),
        ("a: 1\na: 2\n", "duplicate key"),
        ("outer:\n  a: 1\n  a: 2\n", "duplicate key"),
        ("a: &x 1\nb: *x\n", "anchor"),
        ("a:\n  <<: {x: 1}\n  y: 2\n", "merge"),
        ("a: &base {x: 1}\nb:\n  <<: *base\n", "anchor"),
        ("flag: yes\n", "ambiguous boolean"),
        ("flag: no\n", "ambiguous boolean"),
        ("flag: on\n", "ambiguous boolean"),
        ("flag: off\n", "ambiguous boolean"),
        ("v: .nan\n", "non-finite"),
        ("v: .inf\n", "non-finite"),
        ("v: -.inf\n", "non-finite"),
        ("v: 1:20\n", "sexagesimal"),
        ("a: !!python/object/apply:os.system ['echo x']\n", "unsupported YAML tag"),
    ],
)
def test_load_yaml_rejects_unsafe_or_invalid(text: str, match: str) -> None:
    with pytest.raises(StrategyYamlLoadError, match=match):
        load_strategy_yaml(text)


def test_timestamp_not_auto_converted() -> None:
    data = load_strategy_yaml("t: 2024-01-01T00:00:00Z\n")
    assert type(data["t"]) is str
    assert data["t"] == "2024-01-01T00:00:00Z"


def test_load_yaml_from_path(tmp_path: Path) -> None:
    path = tmp_path / "sample.yaml"
    path.write_text("tp: 0.0075\nhorizon_hours: 8\n", encoding="utf-8")
    data = load_strategy_yaml_path(path)
    assert type(data["tp"]) is Decimal
    assert data["tp"] == Decimal("0.0075")
    assert type(data["horizon_hours"]) is int
    assert data["horizon_hours"] == 8


def test_load_yaml_from_stream() -> None:
    stream = StringIO("cost_bps: 4.5\n")
    data = load_strategy_yaml(stream, filename="mem.yaml")
    assert type(data["cost_bps"]) is Decimal
    assert data["cost_bps"] == Decimal("4.5")


def test_error_includes_line_information() -> None:
    with pytest.raises(StrategyYamlLoadError, match=r"line 2"):
        load_strategy_yaml("ok: 1\nbad: yes\n")


def test_edc_shaped_raw_fragment_loads() -> None:
    text = """
signal:
  plugin:
    id: edc.detect_cross_events
    version: "1.0.0"
    config:
      - key: enable_sync_cross
        value:
          kind: boolean
          value: true
      - key: ema_fast
        value:
          kind: integer
          value: 9
      - key: band_compression_pct
        value:
          kind: decimal
          value: 0.15
exit:
  take_profit:
    value: 0.75
    unit: percent
"""
    data = load_strategy_yaml(text)
    cfg = data["signal"]["plugin"]["config"]
    assert cfg[0]["value"]["value"] is True
    assert type(cfg[0]["value"]["value"]) is bool
    assert cfg[1]["value"]["value"] == 9
    assert type(cfg[1]["value"]["value"]) is int
    assert cfg[2]["value"]["value"] == Decimal("0.15")
    assert type(cfg[2]["value"]["value"]) is Decimal
    assert type(data["exit"]["take_profit"]["value"]) is Decimal


def test_cluster_shaped_raw_fragment_loads() -> None:
    text = """
signal:
  plugin:
    config:
      - key: minimum_cluster_pools
        value:
          kind: integer
          value: 3
      - key: approach_bps
        value:
          kind: rate
          value:
            value: 4
            unit: basis_points
"""
    data = load_strategy_yaml(text)
    by_key = {e["key"]: e["value"] for e in data["signal"]["plugin"]["config"]}
    assert by_key["minimum_cluster_pools"]["value"] == 3
    assert type(by_key["minimum_cluster_pools"]["value"]) is int
    assert by_key["approach_bps"]["value"]["unit"] == "basis_points"
    assert by_key["approach_bps"]["value"]["value"] == 4
