"""V2 schema generation and V1 compatibility tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from orderbook_analyse.strategy_lab.models import (
    STRATEGY_SPEC_SCHEMA_VERSION,
    STRATEGY_SPEC_V2_SCHEMA_VERSION,
    StrategySpec,
    StrategySpecV1,
    StrategySpecV2,
)
from orderbook_analyse.strategy_lab.schema import (
    COMMITTED_SCHEMA_PATH,
    COMMITTED_SCHEMA_V2_PATH,
    SCHEMA_ID,
    SCHEMA_V2_ID,
    generate_strategy_spec_schema,
    generate_strategy_spec_v2_schema,
    render_strategy_spec_schema_json,
    render_strategy_spec_v2_schema_json,
)

V1_HEAD_SHA256 = (
    "eeb954c276b7cfca567a7d36bb2f41dbc69e665808ea602f0f0c236929f56ea5"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_strategy_spec_remains_v1() -> None:
    assert StrategySpec is StrategySpecV1


def test_v1_schema_byte_identical_to_head() -> None:
    assert _sha256(COMMITTED_SCHEMA_PATH) == V1_HEAD_SHA256


def test_v1_generator_output_unchanged() -> None:
    committed = COMMITTED_SCHEMA_PATH.read_text(encoding="utf-8")
    generated = render_strategy_spec_schema_json()
    assert committed == generated


def test_v2_schema_exists_separately() -> None:
    assert COMMITTED_SCHEMA_V2_PATH.is_file()
    assert COMMITTED_SCHEMA_V2_PATH != COMMITTED_SCHEMA_PATH


def test_v2_schema_root_metadata() -> None:
    schema = generate_strategy_spec_v2_schema()
    assert schema["$id"] == SCHEMA_V2_ID
    assert schema["title"] == "StrategySpec V2"
    assert schema["$ref"] == "#/$defs/StrategySpecV2"
    assert schema["x-strategy-spec-schema-version"] == STRATEGY_SPEC_V2_SCHEMA_VERSION


def test_v2_metadata_schema_version_const() -> None:
    schema = generate_strategy_spec_v2_schema()
    metadata = schema["$defs"]["Metadata"]
    assert metadata["properties"]["schema_version"]["const"] == "strategy_spec/v2"


def test_v2_three_signal_variants_with_kind() -> None:
    schema = generate_strategy_spec_v2_schema()
    signal_def = schema["$defs"]["SignalDefinition"]
    refs = [item["$ref"].split("/")[-1] for item in signal_def["oneOf"]]
    assert refs == ["PluginSignalSpec", "RuleBasedSignalSpec", "StateMachineSignalSpec"]
    for name in refs:
        assert schema["$defs"][name]["properties"]["kind"]["const"] in {
            "plugin",
            "rule_based",
            "state_machine",
        }


def test_v2_rule_and_operand_discriminators() -> None:
    schema = generate_strategy_spec_v2_schema()
    assert "oneOf" in schema["$defs"]["BooleanExpression"]
    assert "oneOf" in schema["$defs"]["Operand"]
    assert "oneOf" in schema["$defs"]["ParamValue"]


def test_v2_recursive_refs_present() -> None:
    schema = generate_strategy_spec_v2_schema()
    not_expr = schema["$defs"]["BooleanNotExpression"]
    assert not_expr["properties"]["operand"]["$ref"] == "#/$defs/BooleanExpression"
    and_expr = schema["$defs"]["BooleanAndExpression"]
    assert and_expr["properties"]["operands"]["items"]["$ref"] == "#/$defs/BooleanExpression"


def test_v2_additional_properties_false() -> None:
    schema = generate_strategy_spec_v2_schema()
    for name, defn in schema["$defs"].items():
        if defn.get("type") == "object" and "properties" in defn:
            assert defn.get("additionalProperties") is False, name


def test_v2_generation_deterministic() -> None:
    a = render_strategy_spec_v2_schema_json()
    b = render_strategy_spec_v2_schema_json()
    assert a == b


def test_v2_committed_matches_generator() -> None:
    committed = COMMITTED_SCHEMA_V2_PATH.read_text(encoding="utf-8")
    generated = render_strategy_spec_v2_schema_json()
    assert committed == generated
    assert json.loads(committed) == generate_strategy_spec_v2_schema()


def test_v1_schema_id_unchanged() -> None:
    schema = generate_strategy_spec_schema()
    assert schema["$id"] == SCHEMA_ID
    assert schema["x-strategy-spec-schema-version"] == STRATEGY_SPEC_SCHEMA_VERSION


def test_strategy_spec_v2_rejects_v1_schema_version() -> None:
    from tests.strategy_lab.v2_fixtures import minimal_strategy_spec_v2
    from orderbook_analyse.strategy_lab.models import Metadata

    spec = minimal_strategy_spec_v2()
    bad_metadata = Metadata(
        schema_version=STRATEGY_SPEC_SCHEMA_VERSION,
        strategy_id=spec.metadata.strategy_id,
        strategy_version=spec.metadata.strategy_version,
        family=spec.metadata.family,
        variant=spec.metadata.variant,
        title=spec.metadata.title,
    )
    with pytest.raises(ValueError, match="strategy_spec/v2"):
        StrategySpecV2(
            **{
                f.name: getattr(spec, f.name)
                for f in __import__("dataclasses").fields(StrategySpecV2)
                if f.name != "metadata"
            },
            metadata=bad_metadata,
        )


def test_v2_schema_structural_constraints_match_python() -> None:
    schema = generate_strategy_spec_v2_schema()
    defs = schema["$defs"]
    assert defs["BooleanAndExpression"]["properties"]["operands"]["minItems"] == 2
    assert defs["BooleanOrExpression"]["properties"]["operands"]["minItems"] == 2
    assert defs["StateMachineSignalSpec"]["properties"]["states"]["minItems"] == 1
    assert defs["TransitionSpec"]["properties"]["priority"]["minimum"] == 1
    assert defs["TimeoutTransitionSpec"]["properties"]["after_bars"]["minimum"] == 1
    assert defs["TimeoutTransitionSpec"]["properties"]["priority"]["minimum"] == 1
    rey = defs["PluginSignalSpec"]["properties"]["rules_embedded_in_yaml"]
    assert rey["const"] is False
    assert rey["type"] == "boolean"
    assert defs["StableIdentifier"]["properties"]["value"]["pattern"] == (
        r"^[a-z][a-z0-9_]*$"
    )
    assert defs["ContractVersion"]["properties"]["value"]["pattern"] == (
        r"^[a-z][a-z0-9_]+/v[0-9]+$"
    )
    fout = defs["FeatureOutputReference"]
    assert "output_id" in fout["required"]
    assert "output_id" in fout["properties"]
    root_props = set(defs["StrategySpecV2"]["properties"])
    assert not {"setup", "trigger", "confirmation", "invalidation", "long", "short"} & root_props


def _v2_subschema(schema: dict[str, object], name: str) -> dict[str, object]:
    return {"$defs": schema["$defs"], "$ref": f"#/$defs/{name}"}


def _comparison_leaf() -> dict[str, object]:
    return {
        "kind": "comparison",
        "operator_id": {"value": "gt"},
        "left": {
            "kind": "feature_output",
            "feature_alias": {"value": "ema"},
            "output_id": {"value": "value"},
        },
        "right": {"kind": "literal", "value": {"kind": "integer", "value": 0}},
    }


def test_v2_schema_validation_recursion_and_constraints() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    from jsonschema import ValidationError, validate

    schema = generate_strategy_spec_v2_schema()
    leaf = _comparison_leaf()
    nested = {
        "kind": "boolean_not",
        "operand": {
            "kind": "boolean_or",
            "operands": [
                {
                    "kind": "boolean_and",
                    "operands": [
                        {"kind": "boolean_not", "operand": leaf},
                        leaf,
                    ],
                },
                leaf,
            ],
        },
    }
    validate(nested, _v2_subschema(schema, "BooleanExpression"))

    with pytest.raises(ValidationError):
        validate(
            {"kind": "boolean_and", "operands": [leaf]},
            _v2_subschema(schema, "BooleanAndExpression"),
        )

    with pytest.raises(ValidationError):
        validate(
            {"kind": "bogus", "operand": leaf},
            _v2_subschema(schema, "BooleanNotExpression"),
        )

    extra = dict(leaf)
    extra["extra"] = 1
    with pytest.raises(ValidationError):
        validate(extra, _v2_subschema(schema, "ComparisonExpression"))

    with pytest.raises(ValidationError):
        validate(
            {
                "after_bars": True,
                "priority": 1,
                "in_state": {"value": "idle"},
                "timeout_id": {"value": "t1"},
                "to_state": {"value": "armed"},
            },
            _v2_subschema(schema, "TimeoutTransitionSpec"),
        )

    with pytest.raises(ValidationError):
        validate(
            {
                "after_bars": 0,
                "priority": 1,
                "in_state": {"value": "idle"},
                "timeout_id": {"value": "t1"},
                "to_state": {"value": "armed"},
            },
            _v2_subschema(schema, "TimeoutTransitionSpec"),
        )

    with pytest.raises(ValidationError):
        validate(
            {
                "kind": "plugin",
                "plugin": {
                    "id": "p",
                    "version": "1",
                    "kind": "signal",
                },
                "mode_id": None,
                "directionality": "long",
                "rules_embedded_in_yaml": True,
                "confirmation_policy": None,
                "setup": {"description": "s"},
                "trigger": {"description": "t"},
                "confirmation": {"description": "c"},
                "invalidation": {"description": "i"},
            },
            _v2_subschema(schema, "PluginSignalSpec"),
        )


def test_v1_schema_has_no_v2_root() -> None:
    schema = generate_strategy_spec_schema()
    assert "StrategySpecV2" not in schema.get("$defs", {})
    assert schema["$ref"] == "#/$defs/StrategySpec"
