"""Decode raw StrategySpec V2 mappings into frozen ``StrategySpecV2`` (P6).

Uses the P2 safe YAML loader as the only YAML ingress, then reconstructs
typed dataclasses without semantic defaults, mutation, or V1 migration.
"""

from __future__ import annotations

from dataclasses import MISSING, fields, is_dataclass
from decimal import Decimal
from enum import Enum
from pathlib import Path
from types import UnionType
from typing import (
    Any,
    Mapping,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)

from orderbook_analyse.strategy_lab.compiler_v2 import (
    CompiledStrategyV2,
    compile_strategy_v2,
)
from orderbook_analyse.strategy_lab.loader import load_strategy_yaml, load_strategy_yaml_path
from orderbook_analyse.strategy_lab.models.strategy_v2 import (
    STRATEGY_SPEC_V2_SCHEMA_VERSION,
    StrategySpecV2,
)
from orderbook_analyse.strategy_lab.validation.catalogs import CatalogBundleV2


class StrategyDecodeError(Exception):
    """Raised when a raw mapping cannot be decoded into StrategySpecV2."""

    def __init__(
        self,
        message: str,
        *,
        path: str,
        expected: str,
        observed: str,
    ) -> None:
        self.path = path
        self.expected = expected
        self.observed = observed
        super().__init__(
            f"{message} (path={path}, expected={expected}, observed={observed})"
        )


def decode_strategy_v2(data: Mapping[str, object]) -> StrategySpecV2:
    """Decode a raw mapping into ``StrategySpecV2`` (no validation/compile)."""
    if not isinstance(data, Mapping):
        raise StrategyDecodeError(
            "strategy root must be a mapping",
            path="$",
            expected="Mapping[str, object]",
            observed=type(data).__name__,
        )
    if type(data) is not dict and not isinstance(data, Mapping):
        raise StrategyDecodeError(
            "strategy root must be a mapping",
            path="$",
            expected="Mapping[str, object]",
            observed=type(data).__name__,
        )

    metadata = data.get("metadata")
    if not isinstance(metadata, Mapping):
        raise StrategyDecodeError(
            "metadata must be a mapping",
            path="$.metadata",
            expected="mapping",
            observed=type(metadata).__name__,
        )
    schema_version = metadata.get("schema_version")
    if schema_version != STRATEGY_SPEC_V2_SCHEMA_VERSION:
        raise StrategyDecodeError(
            "unsupported schema_version (V1 migration is not allowed)",
            path="$.metadata.schema_version",
            expected=STRATEGY_SPEC_V2_SCHEMA_VERSION,
            observed=repr(schema_version),
        )

    decoded = _decode_value(data, StrategySpecV2, path="$")
    if not isinstance(decoded, StrategySpecV2):
        raise StrategyDecodeError(
            "decoded root is not StrategySpecV2",
            path="$",
            expected="StrategySpecV2",
            observed=type(decoded).__name__,
        )
    return decoded


def load_strategy_v2_yaml(text: str) -> StrategySpecV2:
    """Safely load YAML text and decode into ``StrategySpecV2``."""
    if type(text) is not str:
        raise TypeError("text must be exact str")
    return decode_strategy_v2(load_strategy_yaml(text))


def load_strategy_v2_yaml_file(path: str | Path) -> StrategySpecV2:
    """Safely load a YAML file and decode into ``StrategySpecV2``."""
    return decode_strategy_v2(load_strategy_yaml_path(path))


def load_compile_strategy_v2(
    path: str | Path,
    catalogs: CatalogBundleV2,
) -> CompiledStrategyV2:
    """Load YAML, decode, then compile (P4C runs inside compile_strategy_v2)."""
    return compile_strategy_v2(load_strategy_v2_yaml_file(path), catalogs)


def _decode_value(raw: object, expected: Any, *, path: str) -> object:
    expected = _resolve_hint(expected)
    origin = get_origin(expected)

    if _is_union(expected):
        return _decode_union(raw, expected, path=path)

    if origin is tuple:
        return _decode_tuple(raw, expected, path=path)

    if expected is Any:
        raise StrategyDecodeError(
            "Any is not a decodable target type",
            path=path,
            expected="concrete type",
            observed=type(raw).__name__,
        )

    if expected is type(None):
        if raw is None:
            return None
        raise StrategyDecodeError(
            "expected null",
            path=path,
            expected="None",
            observed=_observed(raw),
        )

    if expected is Decimal:
        if type(raw) is not Decimal:
            raise StrategyDecodeError(
                "Decimal required (float is not allowed)",
                path=path,
                expected="Decimal",
                observed=_observed(raw),
            )
        return raw

    if expected is int:
        if type(raw) is bool:
            raise StrategyDecodeError(
                "bool is not allowed where int is required",
                path=path,
                expected="int",
                observed=_observed(raw),
            )
        if type(raw) is not int:
            raise StrategyDecodeError(
                "int required",
                path=path,
                expected="int",
                observed=_observed(raw),
            )
        return raw

    if expected is bool:
        if type(raw) is not bool:
            raise StrategyDecodeError(
                "bool required",
                path=path,
                expected="bool",
                observed=_observed(raw),
            )
        return raw

    if expected is str:
        if type(raw) is not str:
            raise StrategyDecodeError(
                "str required",
                path=path,
                expected="str",
                observed=_observed(raw),
            )
        return raw

    if expected is float:
        raise StrategyDecodeError(
            "float target types are not allowed",
            path=path,
            expected="non-float type",
            observed=_observed(raw),
        )

    if isinstance(expected, type) and issubclass(expected, Enum):
        return _decode_enum(raw, expected, path=path)

    if isinstance(expected, type) and is_dataclass(expected):
        return _decode_dataclass(raw, expected, path=path)

    raise StrategyDecodeError(
        f"unsupported target type {expected!r}",
        path=path,
        expected=getattr(expected, "__name__", repr(expected)),
        observed=_observed(raw),
    )


def _decode_dataclass(raw: object, cls: type, *, path: str) -> object:
    if not isinstance(raw, Mapping):
        raise StrategyDecodeError(
            f"{cls.__name__} requires a mapping",
            path=path,
            expected="mapping",
            observed=_observed(raw),
        )
    if any(type(key) is not str for key in raw):
        raise StrategyDecodeError(
            "mapping keys must be str",
            path=path,
            expected="str keys",
            observed="non-str key",
        )

    hints = get_type_hints(cls)
    field_map = {f.name: f for f in fields(cls)}
    unknown = sorted(set(raw) - set(field_map) - {"kind"})
    # ``kind`` is virtual for schema-kind dataclasses; reject elsewhere.
    schema_kind = getattr(cls, "_schema_kind", None)
    if "kind" in raw:
        if not isinstance(schema_kind, str):
            raise StrategyDecodeError(
                "unexpected field 'kind'",
                path=f"{path}.kind",
                expected="no kind field",
                observed=repr(raw["kind"]),
            )
        if raw["kind"] != schema_kind:
            raise StrategyDecodeError(
                "kind does not match dataclass schema kind",
                path=f"{path}.kind",
                expected=schema_kind,
                observed=repr(raw["kind"]),
            )
    elif isinstance(schema_kind, str):
        # kind is optional when already selected by a parent union match,
        # but if present must match; when decoding a kinded class directly,
        # require kind for closed-union fidelity at the mapping boundary.
        pass

    if unknown:
        raise StrategyDecodeError(
            f"unknown field(s): {', '.join(unknown)}",
            path=path,
            expected=f"fields of {cls.__name__}",
            observed=", ".join(unknown),
        )

    kwargs: dict[str, object] = {}
    for name, field in field_map.items():
        field_path = f"{path}.{name}"
        if name not in raw:
            if field.default is not MISSING:
                kwargs[name] = field.default
                continue
            if field.default_factory is not MISSING:  # type: ignore[comparison-overlap]
                kwargs[name] = field.default_factory()
                continue
            raise StrategyDecodeError(
                f"missing required field {name!r}",
                path=field_path,
                expected=str(hints.get(name, field.type)),
                observed="<missing>",
            )
        kwargs[name] = _decode_value(raw[name], hints[name], path=field_path)

    try:
        return cls(**kwargs)
    except (TypeError, ValueError) as exc:
        raise StrategyDecodeError(
            f"failed to construct {cls.__name__}: {exc}",
            path=path,
            expected=cls.__name__,
            observed=_observed(raw),
        ) from exc


def _decode_union(raw: object, expected: Any, *, path: str) -> object:
    args = [_resolve_hint(arg) for arg in get_args(expected)]
    non_none = [arg for arg in args if arg is not type(None)]
    allows_none = any(arg is type(None) for arg in args)

    if raw is None:
        if allows_none:
            return None
        raise StrategyDecodeError(
            "null is not allowed",
            path=path,
            expected=_type_label(expected),
            observed="None",
        )

    if allows_none and len(non_none) == 1:
        return _decode_value(raw, non_none[0], path=path)

    dataclass_variants = [
        arg
        for arg in non_none
        if isinstance(arg, type) and is_dataclass(arg)
    ]
    if len(dataclass_variants) == len(non_none) and dataclass_variants:
        return _decode_dataclass_union(raw, dataclass_variants, path=path)

    # Mixed unions (rare): try each variant, require exactly one success.
    successes: list[object] = []
    errors: list[str] = []
    for arg in non_none:
        try:
            successes.append(_decode_value(raw, arg, path=path))
        except StrategyDecodeError as exc:
            errors.append(str(exc))
    if len(successes) == 1:
        return successes[0]
    if len(successes) == 0:
        raise StrategyDecodeError(
            "no union variant matched",
            path=path,
            expected=_type_label(expected),
            observed=_observed(raw),
        )
    raise StrategyDecodeError(
        "ambiguous union match",
        path=path,
        expected=_type_label(expected),
        observed=f"{len(successes)} matches",
    )


def _decode_dataclass_union(
    raw: object,
    variants: list[type],
    *,
    path: str,
) -> object:
    if not isinstance(raw, Mapping):
        raise StrategyDecodeError(
            "union variant requires a mapping",
            path=path,
            expected="mapping",
            observed=_observed(raw),
        )

    kinded = [cls for cls in variants if isinstance(getattr(cls, "_schema_kind", None), str)]
    unkinded = [cls for cls in variants if not isinstance(getattr(cls, "_schema_kind", None), str)]

    if "kind" in raw:
        kind = raw["kind"]
        if type(kind) is not str:
            raise StrategyDecodeError(
                "kind must be a str",
                path=f"{path}.kind",
                expected="str",
                observed=_observed(kind),
            )
        matches = [cls for cls in kinded if cls._schema_kind == kind]
        if len(matches) == 0:
            raise StrategyDecodeError(
                "unknown kind for closed union",
                path=f"{path}.kind",
                expected="one of "
                + ", ".join(sorted(cls._schema_kind for cls in kinded)),
                observed=repr(kind),
            )
        if len(matches) != 1:
            raise StrategyDecodeError(
                "ambiguous kind for closed union",
                path=f"{path}.kind",
                expected="exactly one variant",
                observed=repr(kind),
            )
        return _decode_dataclass(raw, matches[0], path=path)

    # Structural fallback only when some variants intentionally lack kind
    # (e.g. DurationValue | PaddingNotApplicable).
    if unkinded and not kinded:
        if len(unkinded) != 1:
            raise StrategyDecodeError(
                "ambiguous unkinded union",
                path=path,
                expected="exactly one unkinded variant",
                observed=str(len(unkinded)),
            )
        return _decode_dataclass(raw, unkinded[0], path=path)

    if unkinded and kinded:
        if len(unkinded) != 1:
            raise StrategyDecodeError(
                "ambiguous unkinded union variants",
                path=path,
                expected="exactly one unkinded variant",
                observed=str(len(unkinded)),
            )
        return _decode_dataclass(raw, unkinded[0], path=path)

    raise StrategyDecodeError(
        "closed dataclass union requires kind",
        path=path,
        expected="kind discriminator",
        observed="<missing kind>",
    )


def _decode_tuple(raw: object, expected: Any, *, path: str) -> tuple[object, ...]:
    if type(raw) is not list:
        raise StrategyDecodeError(
            "tuple fields require a YAML sequence (list)",
            path=path,
            expected="list",
            observed=_observed(raw),
        )
    args = get_args(expected)
    if len(args) == 2 and args[1] is Ellipsis:
        item_type = args[0]
        return tuple(
            _decode_value(item, item_type, path=f"{path}[{index}]")
            for index, item in enumerate(raw)
        )
    if len(args) == len(raw):
        return tuple(
            _decode_value(item, arg, path=f"{path}[{index}]")
            for index, (item, arg) in enumerate(zip(raw, args, strict=True))
        )
    raise StrategyDecodeError(
        "tuple arity mismatch",
        path=path,
        expected=f"tuple length {len(args)}",
        observed=f"list length {len(raw)}",
    )


def _decode_enum(raw: object, enum_cls: type[Enum], *, path: str) -> Enum:
    if type(raw) is not str:
        raise StrategyDecodeError(
            f"{enum_cls.__name__} requires a string value",
            path=path,
            expected="str",
            observed=_observed(raw),
        )
    try:
        return enum_cls(raw)
    except ValueError as exc:
        raise StrategyDecodeError(
            f"invalid {enum_cls.__name__} value",
            path=path,
            expected="|".join(member.value for member in enum_cls),
            observed=repr(raw),
        ) from exc


def _is_union(expected: Any) -> bool:
    origin = get_origin(expected)
    return origin is Union or isinstance(expected, UnionType)


def _resolve_hint(expected: Any) -> Any:
    if isinstance(expected, str):
        raise StrategyDecodeError(
            "unresolved forward reference",
            path="$",
            expected="evaluated type hint",
            observed=expected,
        )
    return expected


def _type_label(expected: Any) -> str:
    return getattr(expected, "__name__", repr(expected))


def _observed(raw: object) -> str:
    if isinstance(raw, Mapping):
        return f"mapping(keys={sorted(str(k) for k in raw)})"
    if type(raw) is list:
        return f"list(len={len(raw)})"
    if type(raw) is Decimal:
        return f"Decimal({format(raw, 'f')})"
    if type(raw) in {str, int, bool} or raw is None:
        return repr(raw)
    return type(raw).__name__
