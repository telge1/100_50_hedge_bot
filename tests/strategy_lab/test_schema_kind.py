"""P2 tests: `_schema_kind` ClassVar must not alter P1 model semantics."""

from __future__ import annotations

from dataclasses import fields
from typing import ClassVar, get_args, get_origin

from orderbook_analyse.strategy_lab.models.strategy import (
    BoolParam,
    DecimalParam,
    DurationParam,
    IdentifierParam,
    IntParam,
    RateParam,
    StringParam,
    TimeframeParam,
    _PARAM_VALUE_TYPES,
)
from orderbook_analyse.strategy_lab.schema import generate_strategy_spec_schema


def test_schema_kind_is_classvar_not_dataclass_field() -> None:
    for cls in _PARAM_VALUE_TYPES:
        field_names = {f.name for f in fields(cls)}
        assert "_schema_kind" not in field_names
        assert "value" in field_names
        annotation = cls.__annotations__["_schema_kind"]
        # Under from __future__ import annotations this is a string.
        assert annotation in {"ClassVar[str]", ClassVar[str]} or (
            get_origin(annotation) is ClassVar
            and get_args(annotation) == (str,)
        )
        assert isinstance(cls._schema_kind, str)


def test_schema_kind_not_in_constructor_signature() -> None:
    # kw_only value-only construction remains valid.
    assert BoolParam(value=True)._schema_kind == "boolean"
    assert StringParam(value="9")._schema_kind == "string"
    assert IntParam(value=9)._schema_kind == "integer"


def test_schema_kind_does_not_affect_equality_or_hash() -> None:
    a = BoolParam(value=True)
    b = BoolParam(value=True)
    assert a == b
    assert hash(a) == hash(b)
    assert a != BoolParam(value=False)


def test_param_value_kinds_are_unique() -> None:
    kinds = [cls._schema_kind for cls in _PARAM_VALUE_TYPES]
    assert len(kinds) == len(set(kinds))
    assert set(kinds) == {
        "string",
        "boolean",
        "integer",
        "decimal",
        "rate",
        "duration",
        "timeframe",
        "identifier",
    }


def test_schema_discriminator_separates_string_and_identifier() -> None:
    schema = generate_strategy_spec_schema()
    string_kind = schema["$defs"]["StringParam"]["properties"]["kind"]["const"]
    ident_kind = schema["$defs"]["IdentifierParam"]["properties"]["kind"]["const"]
    assert string_kind == "string"
    assert ident_kind == "identifier"
    assert string_kind != ident_kind
    # oneOf + const kind => only one variant can match a given kind.
    for cls in _PARAM_VALUE_TYPES:
        defn = schema["$defs"][cls.__name__]
        assert defn["properties"]["kind"]["const"] == cls._schema_kind
        assert "kind" in defn["required"]
        assert defn["additionalProperties"] is False


def test_all_param_variants_exported_in_closed_oneof() -> None:
    schema = generate_strategy_spec_schema()
    refs = [item["$ref"] for item in schema["$defs"]["ParamValue"]["oneOf"]]
    expected = [f"#/$defs/{cls.__name__}" for cls in sorted(
        _PARAM_VALUE_TYPES, key=lambda c: c.__name__
    )]
    assert refs == expected
