"""P2 schema generation and committed-schema drift tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orderbook_analyse.strategy_lab.schema import (
    COMMITTED_SCHEMA_PATH,
    SCHEMA_ID,
    SchemaGenerationError,
    generate_strategy_spec_schema,
    render_strategy_spec_schema_json,
)
from orderbook_analyse.strategy_lab.schema.generator import (
    _schema_for_annotation,
)


def test_generate_schema_has_required_root_keys() -> None:
    schema = generate_strategy_spec_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == SCHEMA_ID
    assert schema["title"] == "StrategySpec V1"
    assert schema["type"] == "object"
    assert schema["$ref"] == "#/$defs/StrategySpec"
    assert schema["additionalProperties"] is False
    assert "StrategySpec" in schema["$defs"]
    assert "ParamValue" in schema["$defs"]


def test_schema_generation_is_deterministic() -> None:
    a = render_strategy_spec_schema_json()
    b = render_strategy_spec_schema_json()
    assert a == b
    assert a.endswith("\n")


def test_committed_schema_matches_generator() -> None:
    assert COMMITTED_SCHEMA_PATH.is_file()
    committed = COMMITTED_SCHEMA_PATH.read_text(encoding="utf-8")
    generated = render_strategy_spec_schema_json()
    assert committed == generated
    # Also compare parsed dicts for clarity on failures.
    assert json.loads(committed) == generate_strategy_spec_schema()


def test_param_value_is_closed_oneof() -> None:
    schema = generate_strategy_spec_schema()
    param = schema["$defs"]["ParamValue"]
    assert "oneOf" in param
    refs = [item["$ref"] for item in param["oneOf"]]
    assert len(refs) == 8
    assert refs == sorted(refs)
    bool_def = schema["$defs"]["BoolParam"]
    assert bool_def["properties"]["kind"]["const"] == "boolean"
    assert "kind" in bool_def["required"]
    assert bool_def["additionalProperties"] is False


def test_enums_are_closed() -> None:
    schema = generate_strategy_spec_schema()
    rate_unit = schema["$defs"]["RateUnit"]
    assert rate_unit["type"] == "string"
    assert set(rate_unit["enum"]) == {"percent", "fraction", "basis_points"}


def test_decimal_fields_use_number_authoring() -> None:
    schema = generate_strategy_spec_schema()
    rate_value = schema["$defs"]["RateValue"]
    assert rate_value["properties"]["value"]["type"] == "number"
    assert "decimal.Decimal" in rate_value["properties"]["value"]["description"] or (
        "Decimal" in rate_value["properties"]["value"]["description"]
    )


def test_strategy_spec_required_fields_exclude_defaults_only() -> None:
    schema = generate_strategy_spec_schema()
    required = set(schema["$defs"]["StrategySpec"]["required"])
    # Provenance and long/short are mandatory root sections.
    assert {"provenance", "long", "short", "fees", "slippage", "funding"} <= required
    # No silent omission of metadata.
    assert "metadata" in required


def test_dataclass_defs_forbid_additional_properties() -> None:
    schema = generate_strategy_spec_schema()
    for name, defn in schema["$defs"].items():
        if defn.get("type") == "object" and "properties" in defn:
            assert defn.get("additionalProperties") is False, name


def test_unknown_type_raises_explicit_error() -> None:
    with pytest.raises(SchemaGenerationError, match="unsupported type"):
        _schema_for_annotation(complex, {}, path="test.field")


def test_committed_schema_path_location() -> None:
    expected = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "orderbook_analyse"
        / "strategy_lab"
        / "schema"
        / "strategy_spec_v1.schema.json"
    )
    assert COMMITTED_SCHEMA_PATH.resolve() == expected
