"""JSON Schema generation for StrategySpec V1 from normative dataclasses.

Authoring rule for Decimal fields:
- JSON Schema uses ``type: number`` (YAML/JSON numeric scalars).
- The P2 YAML loader materializes those scalars as ``decimal.Decimal``,
  never as binary ``float``.
- No string-form Decimal and no float/Decimal ``oneOf`` mix in P2.
"""

from __future__ import annotations

import json
from dataclasses import MISSING, fields, is_dataclass
from decimal import Decimal
from enum import Enum
from pathlib import Path
from types import UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

from orderbook_analyse.strategy_lab.models import (
    STRATEGY_SPEC_SCHEMA_VERSION,
    ParamValue,
    StrategySpec,
)
from orderbook_analyse.strategy_lab.models.strategy import _PARAM_VALUE_TYPES

JSON_SCHEMA_DRAFT = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_ID = (
    "https://orderbook-analyse.local/strategy_lab/"
    "strategy_spec_v1.schema.json"
)
SCHEMA_TITLE = "StrategySpec V1"

COMMITTED_SCHEMA_PATH = Path(__file__).with_name("strategy_spec_v1.schema.json")

_DECIMAL_SCHEMA: dict[str, object] = {
    "type": "number",
    "description": (
        "Decimal authoring value. Schema type is number; the YAML loader "
        "preserves numeric scalars as decimal.Decimal (never float)."
    ),
}


class SchemaGenerationError(TypeError):
    """Raised when a model type cannot be mapped to JSON Schema."""


def generate_strategy_spec_schema() -> dict[str, object]:
    """Build a deterministic JSON Schema document for StrategySpec V1."""
    defs: dict[str, dict[str, object]] = {}
    _ensure_dataclass_def(StrategySpec, defs, path="StrategySpec")
    _ensure_param_value_def(defs)

    ordered_defs = {name: defs[name] for name in sorted(defs)}
    return {
        "$schema": JSON_SCHEMA_DRAFT,
        "$id": SCHEMA_ID,
        "title": SCHEMA_TITLE,
        "type": "object",
        "$ref": _ref_name(StrategySpec.__name__),
        "$defs": ordered_defs,
        "additionalProperties": False,
        "x-strategy-spec-schema-version": STRATEGY_SPEC_SCHEMA_VERSION,
        "x-decimal-authoring": "number_in_schema__decimal_in_loader",
    }


def render_strategy_spec_schema_json() -> str:
    """Deterministic JSON text for the committed schema file."""
    return (
        json.dumps(
            generate_strategy_spec_schema(),
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n"
    )


def write_committed_strategy_spec_schema(
    path: Path | None = None,
) -> Path:
    """Write the generated schema to disk (helper for regeneration)."""
    target = path if path is not None else COMMITTED_SCHEMA_PATH
    target.write_text(render_strategy_spec_schema_json(), encoding="utf-8")
    return target


def _ref_name(name: str) -> str:
    return f"#/$defs/{name}"


def _ensure_param_value_def(defs: dict[str, dict[str, object]]) -> None:
    if "ParamValue" in defs:
        return
    variants: list[dict[str, object]] = []
    for cls in sorted(_PARAM_VALUE_TYPES, key=lambda c: c.__name__):
        _ensure_dataclass_def(cls, defs, path=f"ParamValue.{cls.__name__}")
        variants.append({"$ref": _ref_name(cls.__name__)})
    defs["ParamValue"] = {
        "title": "ParamValue",
        "oneOf": variants,
        "description": (
            "Closed ParamValue union. Each variant object includes a "
            "const ``kind`` discriminator derived from ``_schema_kind``."
        ),
    }


def _ensure_dataclass_def(
    cls: type,
    defs: dict[str, dict[str, object]],
    *,
    path: str,
) -> None:
    name = cls.__name__
    if name in defs:
        return
    if not is_dataclass(cls):
        raise SchemaGenerationError(
            f"unsupported non-dataclass type at {path}: {cls!r}"
        )

    # Placeholder prevents recursive cycles while building nested refs.
    defs[name] = {}

    hints = get_type_hints(cls, include_extras=True)
    properties: dict[str, dict[str, object]] = {}
    required: list[str] = []

    schema_kind = getattr(cls, "_schema_kind", None)
    if isinstance(schema_kind, str):
        properties["kind"] = {"type": "string", "const": schema_kind}
        required.append("kind")

    for field in fields(cls):
        field_path = f"{path}.{field.name}"
        annotation = hints.get(field.name, field.type)
        properties[field.name] = _schema_for_annotation(
            annotation,
            defs,
            path=field_path,
        )
        has_default = (
            field.default is not MISSING or field.default_factory is not MISSING
        )
        if not has_default:
            required.append(field.name)
        else:
            default_schema = _json_default_for_field(field)
            if default_schema is not None:
                properties[field.name] = {
                    **properties[field.name],
                    "default": default_schema,
                }

    defs[name] = {
        "type": "object",
        "title": name,
        "properties": {k: properties[k] for k in sorted(properties)},
        "required": sorted(set(required)),
        "additionalProperties": False,
    }


def _ensure_enum_def(
    cls: type[Enum],
    defs: dict[str, dict[str, object]],
) -> None:
    name = cls.__name__
    if name in defs:
        return
    values = [member.value for member in cls]
    defs[name] = {
        "type": "string",
        "title": name,
        "enum": values,
    }


def _schema_for_annotation(
    annotation: Any,
    defs: dict[str, dict[str, object]],
    *,
    path: str,
) -> dict[str, object]:
    if annotation is ParamValue:
        _ensure_param_value_def(defs)
        return {"$ref": _ref_name("ParamValue")}

    origin = get_origin(annotation)
    args = get_args(annotation)

    if origin is tuple:
        if len(args) == 2 and args[1] is Ellipsis:
            item_schema = _schema_for_annotation(
                args[0], defs, path=f"{path}[]"
            )
            return {"type": "array", "items": item_schema}
        raise SchemaGenerationError(
            f"unsupported tuple annotation at {path}: {annotation!r}"
        )

    if origin in (Union, UnionType) or isinstance(annotation, UnionType):
        union_args = args if args else get_args(annotation)
        non_none = [a for a in union_args if a is not type(None)]
        has_none = len(non_none) != len(union_args)
        if has_none and len(non_none) == 1:
            inner = _schema_for_annotation(non_none[0], defs, path=path)
            return {"oneOf": [inner, {"type": "null"}]}
        if has_none:
            raise SchemaGenerationError(
                f"unsupported Optional union with multiple non-None "
                f"arms at {path}: {annotation!r}"
            )
        variants = [
            _schema_for_annotation(
                a,
                defs,
                path=f"{path}|{getattr(a, '__name__', a)}",
            )
            for a in non_none
        ]
        return {"oneOf": variants}

    if annotation is str:
        return {"type": "string"}
    if annotation is bool:
        return {"type": "boolean"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        raise SchemaGenerationError(
            f"float is not allowed in StrategySpec schema at {path}"
        )
    if annotation is Decimal:
        return dict(_DECIMAL_SCHEMA)

    if isinstance(annotation, type) and issubclass(annotation, Enum):
        _ensure_enum_def(annotation, defs)
        return {"$ref": _ref_name(annotation.__name__)}

    if isinstance(annotation, type) and is_dataclass(annotation):
        _ensure_dataclass_def(annotation, defs, path=path)
        return {"$ref": _ref_name(annotation.__name__)}

    if isinstance(annotation, str):
        raise SchemaGenerationError(
            f"unevaluated string annotation at {path}: {annotation!r}"
        )

    raise SchemaGenerationError(
        f"unsupported type at {path}: {annotation!r}"
    )


def _json_default_for_field(field: Any) -> object | None:
    if field.default_factory is not MISSING:
        try:
            value = field.default_factory()
        except Exception:
            return None
        return _json_literal(value)
    if field.default is not MISSING:
        return _json_literal(field.default)
    return None


def _json_literal(value: object) -> object | None:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        as_int = int(value)
        if Decimal(as_int) == value:
            return as_int
        # Schema default metadata only; avoid binary float in committed
        # schema by emitting a decimal string marker is forbidden — skip.
        return None
    if isinstance(value, tuple) and value == ():
        return []
    return None


# ---------------------------------------------------------------------------
# StrategySpec V2 schema generation (V1 functions above remain unchanged)
# ---------------------------------------------------------------------------

from orderbook_analyse.strategy_lab.models.rules import (  # noqa: E402
    BooleanAndExpression,
    BooleanExpression,
    BooleanNotExpression,
    BooleanOrExpression,
    ComparisonExpression,
    ComponentReference,
    FeatureOutputReference,
    LiteralOperand,
    Operand,
)
from orderbook_analyse.strategy_lab.models.signals import (  # noqa: E402
    PluginSignalSpec,
    RuleBasedSignalSpec,
    SignalDefinition,
    StateMachineSignalSpec,
)
from orderbook_analyse.strategy_lab.models.strategy_v2 import (  # noqa: E402
    STRATEGY_SPEC_V2_SCHEMA_VERSION,
    StrategySpecV2,
)

SCHEMA_V2_ID = (
    "https://orderbook-analyse.local/strategy_lab/"
    "strategy_spec_v2.schema.json"
)
SCHEMA_V2_TITLE = "StrategySpec V2"

COMMITTED_SCHEMA_V2_PATH = Path(__file__).with_name(
    "strategy_spec_v2.schema.json"
)

_OPERAND_TYPES = (FeatureOutputReference, LiteralOperand)
_BOOLEAN_EXPRESSION_TYPES = (
    ComparisonExpression,
    BooleanAndExpression,
    BooleanOrExpression,
    BooleanNotExpression,
    ComponentReference,
)
_SIGNAL_DEFINITION_TYPES = (
    PluginSignalSpec,
    RuleBasedSignalSpec,
    StateMachineSignalSpec,
)

_STABLE_ID_PATTERN = r"^[a-z][a-z0-9_]*$"
_CONTRACT_VERSION_PATTERN = r"^[a-z][a-z0-9_]+/v[0-9]+$"


def generate_strategy_spec_v2_schema() -> dict[str, object]:
    """Build a deterministic JSON Schema document for StrategySpec V2."""
    defs: dict[str, dict[str, object]] = {}
    _ensure_dataclass_def_v2(StrategySpecV2, defs, path="StrategySpecV2")
    _ensure_param_value_def_v2(defs)
    _ensure_operand_def(defs)
    _ensure_boolean_expression_def(defs)
    _ensure_signal_definition_def(defs)
    _apply_v2_schema_patches(defs)

    ordered_defs = {name: defs[name] for name in sorted(defs)}
    return {
        "$schema": JSON_SCHEMA_DRAFT,
        "$id": SCHEMA_V2_ID,
        "title": SCHEMA_V2_TITLE,
        "type": "object",
        "$ref": _ref_name(StrategySpecV2.__name__),
        "$defs": ordered_defs,
        "additionalProperties": False,
        "x-strategy-spec-schema-version": STRATEGY_SPEC_V2_SCHEMA_VERSION,
        "x-decimal-authoring": "number_in_schema__decimal_in_loader",
    }


def render_strategy_spec_v2_schema_json() -> str:
    """Deterministic JSON text for the committed V2 schema file."""
    return (
        json.dumps(
            generate_strategy_spec_v2_schema(),
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n"
    )


def write_committed_strategy_spec_v2_schema(
    path: Path | None = None,
) -> Path:
    """Write the generated V2 schema to disk (helper for regeneration)."""
    target = path if path is not None else COMMITTED_SCHEMA_V2_PATH
    target.write_text(render_strategy_spec_v2_schema_json(), encoding="utf-8")
    return target


def _ensure_param_value_def_v2(defs: dict[str, dict[str, object]]) -> None:
    if "ParamValue" in defs:
        return
    variants: list[dict[str, object]] = []
    for cls in sorted(_PARAM_VALUE_TYPES, key=lambda c: c.__name__):
        _ensure_dataclass_def_v2(cls, defs, path=f"ParamValue.{cls.__name__}")
        variants.append({"$ref": _ref_name(cls.__name__)})
    defs["ParamValue"] = {
        "title": "ParamValue",
        "oneOf": variants,
        "description": (
            "Closed ParamValue union. Each variant object includes a "
            "const ``kind`` discriminator derived from ``_schema_kind``."
        ),
    }


def _ensure_operand_def(defs: dict[str, dict[str, object]]) -> None:
    if "Operand" in defs:
        return
    variants: list[dict[str, object]] = []
    for cls in sorted(_OPERAND_TYPES, key=lambda c: c.__name__):
        _ensure_dataclass_def_v2(cls, defs, path=f"Operand.{cls.__name__}")
        variants.append({"$ref": _ref_name(cls.__name__)})
    defs["Operand"] = {
        "title": "Operand",
        "oneOf": variants,
        "description": (
            "Closed Operand union. Each variant object includes a "
            "const ``kind`` discriminator derived from ``_schema_kind``."
        ),
    }


def _ensure_boolean_expression_def(defs: dict[str, dict[str, object]]) -> None:
    if "BooleanExpression" in defs:
        return
    variants: list[dict[str, object]] = []
    for cls in sorted(_BOOLEAN_EXPRESSION_TYPES, key=lambda c: c.__name__):
        _ensure_dataclass_def_v2(
            cls,
            defs,
            path=f"BooleanExpression.{cls.__name__}",
        )
        variants.append({"$ref": _ref_name(cls.__name__)})
    defs["BooleanExpression"] = {
        "title": "BooleanExpression",
        "oneOf": variants,
        "description": (
            "Closed BooleanExpression union. Each variant object includes a "
            "const ``kind`` discriminator derived from ``_schema_kind``."
        ),
    }


def _ensure_signal_definition_def(defs: dict[str, dict[str, object]]) -> None:
    if "SignalDefinition" in defs:
        return
    variants: list[dict[str, object]] = []
    for cls in sorted(_SIGNAL_DEFINITION_TYPES, key=lambda c: c.__name__):
        _ensure_dataclass_def_v2(
            cls,
            defs,
            path=f"SignalDefinition.{cls.__name__}",
        )
        variants.append({"$ref": _ref_name(cls.__name__)})
    defs["SignalDefinition"] = {
        "title": "SignalDefinition",
        "oneOf": variants,
        "description": (
            "Closed SignalDefinition union. Each variant object includes a "
            "const ``kind`` discriminator derived from ``_schema_kind``."
        ),
    }


def _ensure_dataclass_def_v2(
    cls: type,
    defs: dict[str, dict[str, object]],
    *,
    path: str,
) -> None:
    name = cls.__name__
    if name in defs:
        return
    if not is_dataclass(cls):
        raise SchemaGenerationError(
            f"unsupported non-dataclass type at {path}: {cls!r}"
        )

    defs[name] = {}

    hints = get_type_hints(cls, include_extras=True)
    properties: dict[str, dict[str, object]] = {}
    required: list[str] = []

    schema_kind = getattr(cls, "_schema_kind", None)
    if isinstance(schema_kind, str):
        properties["kind"] = {"type": "string", "const": schema_kind}
        required.append("kind")

    for field_info in fields(cls):
        field_path = f"{path}.{field_info.name}"
        annotation = hints.get(field_info.name, field_info.type)
        properties[field_info.name] = _schema_for_v2_annotation(
            annotation,
            defs,
            path=field_path,
        )
        has_default = (
            field_info.default is not MISSING
            or field_info.default_factory is not MISSING
        )
        if not has_default:
            required.append(field_info.name)
        else:
            default_schema = _json_default_for_field(field_info)
            if default_schema is not None:
                properties[field_info.name] = {
                    **properties[field_info.name],
                    "default": default_schema,
                }

    defs[name] = {
        "type": "object",
        "title": name,
        "properties": {k: properties[k] for k in sorted(properties)},
        "required": sorted(set(required)),
        "additionalProperties": False,
    }


def _schema_for_v2_annotation(
    annotation: Any,
    defs: dict[str, dict[str, object]],
    *,
    path: str,
) -> dict[str, object]:
    if annotation is ParamValue:
        _ensure_param_value_def_v2(defs)
        return {"$ref": _ref_name("ParamValue")}
    if annotation is Operand:
        _ensure_operand_def(defs)
        return {"$ref": _ref_name("Operand")}
    if annotation is BooleanExpression:
        _ensure_boolean_expression_def(defs)
        return {"$ref": _ref_name("BooleanExpression")}
    if annotation is SignalDefinition:
        _ensure_signal_definition_def(defs)
        return {"$ref": _ref_name("SignalDefinition")}

    origin = get_origin(annotation)
    args = get_args(annotation)

    if origin in (Union, UnionType) or isinstance(annotation, UnionType):
        union_args = args if args else get_args(annotation)
        non_none = [a for a in union_args if a is not type(None)]
        has_none = len(non_none) != len(union_args)
        non_none_set = set(non_none)
        if non_none_set and non_none_set <= set(_BOOLEAN_EXPRESSION_TYPES):
            _ensure_boolean_expression_def(defs)
            inner: dict[str, object] = {"$ref": _ref_name("BooleanExpression")}
            if has_none:
                return {"oneOf": [inner, {"type": "null"}]}
            return inner
        if non_none_set and non_none_set <= set(_OPERAND_TYPES):
            _ensure_operand_def(defs)
            inner = {"$ref": _ref_name("Operand")}
            if has_none:
                return {"oneOf": [inner, {"type": "null"}]}
            return inner
        if has_none and len(non_none) == 1:
            inner = _schema_for_v2_annotation(non_none[0], defs, path=path)
            return {"oneOf": [inner, {"type": "null"}]}
        if has_none:
            raise SchemaGenerationError(
                f"unsupported Optional union with multiple non-None "
                f"arms at {path}: {annotation!r}"
            )
        variants = [
            _schema_for_v2_annotation(
                a,
                defs,
                path=f"{path}|{getattr(a, '__name__', a)}",
            )
            for a in non_none
        ]
        return {"oneOf": variants}

    if origin is tuple:
        if len(args) == 2 and args[1] is Ellipsis:
            item_schema = _schema_for_v2_annotation(
                args[0], defs, path=f"{path}[]"
            )
            return {"type": "array", "items": item_schema}
        raise SchemaGenerationError(
            f"unsupported tuple annotation at {path}: {annotation!r}"
        )

    if annotation is str:
        return {"type": "string"}
    if annotation is bool:
        return {"type": "boolean"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        raise SchemaGenerationError(
            f"float is not allowed in StrategySpec schema at {path}"
        )
    if annotation is Decimal:
        return dict(_DECIMAL_SCHEMA)

    if isinstance(annotation, type) and issubclass(annotation, Enum):
        _ensure_enum_def(annotation, defs)
        return {"$ref": _ref_name(annotation.__name__)}

    if isinstance(annotation, type) and is_dataclass(annotation):
        _ensure_dataclass_def_v2(annotation, defs, path=path)
        return {"$ref": _ref_name(annotation.__name__)}

    if isinstance(annotation, str):
        raise SchemaGenerationError(
            f"unevaluated string annotation at {path}: {annotation!r}"
        )

    raise SchemaGenerationError(
        f"unsupported type at {path}: {annotation!r}"
    )


def _apply_v2_schema_patches(defs: dict[str, dict[str, object]]) -> None:
    metadata = defs.get("Metadata")
    if metadata is not None:
        schema_version = metadata["properties"]["schema_version"]
        schema_version["const"] = STRATEGY_SPEC_V2_SCHEMA_VERSION

    stable_id = defs.get("StableIdentifier")
    if stable_id is not None:
        stable_id["properties"]["value"]["pattern"] = _STABLE_ID_PATTERN

    contract_version = defs.get("ContractVersion")
    if contract_version is not None:
        contract_version["properties"]["value"]["pattern"] = (
            _CONTRACT_VERSION_PATTERN
        )

    for name in ("BooleanAndExpression", "BooleanOrExpression"):
        expr = defs.get(name)
        if expr is not None:
            expr["properties"]["operands"]["minItems"] = 2

    sm = defs.get("StateMachineSignalSpec")
    if sm is not None:
        sm["properties"]["states"]["minItems"] = 1

    transition = defs.get("TransitionSpec")
    if transition is not None:
        transition["properties"]["priority"]["minimum"] = 1

    timeout = defs.get("TimeoutTransitionSpec")
    if timeout is not None:
        timeout["properties"]["after_bars"]["minimum"] = 1
        timeout["properties"]["priority"]["minimum"] = 1

    plugin_signal = defs.get("PluginSignalSpec")
    if plugin_signal is not None:
        plugin_signal["properties"]["rules_embedded_in_yaml"] = {
            "type": "boolean",
            "const": False,
        }
