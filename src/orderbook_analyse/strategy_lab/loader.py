"""Safe YAML loader for StrategySpec authoring documents (P2).

Loads YAML into a raw, type-preserving mapping structure only.
Does **not** validate against JSON Schema, compile to StrategySpec, or
canonicalize.

Security / authoring constraints (fail closed):
- SafeLoader only (no Python object construction)
- Single top-level mapping document
- No duplicate keys at any nesting level
- No anchors / aliases / merge keys
- No YAML 1.1 ambiguous bools (yes/no/on/off/y/n)
- No timestamps
- No sexagesimal numbers
- No NaN / Infinity
- Floats load as ``decimal.Decimal`` (never binary float)
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, TextIO

import yaml
from yaml.events import AliasEvent, NodeEvent
from yaml.loader import SafeLoader
from yaml.nodes import MappingNode, ScalarNode


class StrategyYamlLoadError(ValueError):
    """Invalid YAML or unsupported / unsafe constructs."""


_BOOL_PATTERN = re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$")
_NULL_PATTERN = re.compile(r"^(?:null|Null|NULL|~)$")
_AMBIGUOUS_BOOL_PATTERN = re.compile(
    r"^(?:y|Y|yes|Yes|YES|n|N|no|No|NO|on|On|ON|off|Off|OFF)$"
)
_INT_PATTERN = re.compile(r"^-?(?:0|[1-9][0-9]*)$")
_FORBIDDEN_FLOAT_TOKENS = frozenset(
    {
        ".nan",
        ".NaN",
        ".NAN",
        ".inf",
        ".Inf",
        ".INF",
        "-.inf",
        "-.Inf",
        "-.INF",
        "+.inf",
        "+.Inf",
        "+.INF",
        "nan",
        "NaN",
        "NAN",
        "inf",
        "Inf",
        "INF",
        "-inf",
        "-Inf",
        "-INF",
        "+inf",
        "+Inf",
        "+INF",
    }
)
_AMBIGUOUS_BOOL_TAG = "tag:orderbook-analyse.local,2026:ambiguous-bool"


class _StrategySafeLoader(SafeLoader):
    """Restricted SafeLoader for strategy authoring YAML."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        # Reject aliases/anchors before nodes are constructed.
        if self.check_event(AliasEvent):
            mark = self.peek_event().start_mark
            raise StrategyYamlLoadError(
                _format_mark(mark, "YAML aliases are not allowed")
            )
        event = self.peek_event()
        if isinstance(event, NodeEvent) and event.anchor is not None:
            raise StrategyYamlLoadError(
                _format_mark(event.start_mark, "YAML anchors are not allowed")
            )
        return super().compose_node(parent, index)

    def flatten_mapping(self, node: MappingNode) -> None:
        # Disable merge-key expansion; reject explicitly.
        for key_node, _value_node in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                raise StrategyYamlLoadError(
                    _format_mark(
                        key_node.start_mark,
                        "YAML merge keys (<<) are not allowed",
                    )
                )

    def construct_mapping(
        self, node: MappingNode, deep: bool = False
    ) -> dict[str, Any]:
        if not isinstance(node, MappingNode):
            raise StrategyYamlLoadError(
                f"expected a mapping node, got {type(node).__name__}"
            )
        self.flatten_mapping(node)
        mapping: dict[str, Any] = {}
        for key_node, value_node in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                raise StrategyYamlLoadError(
                    _format_mark(
                        key_node.start_mark,
                        "YAML merge keys (<<) are not allowed",
                    )
                )
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise StrategyYamlLoadError(
                    _format_mark(
                        key_node.start_mark,
                        f"mapping keys must be strings, got {type(key).__name__}",
                    )
                )
            if key == "<<":
                raise StrategyYamlLoadError(
                    _format_mark(
                        key_node.start_mark,
                        "YAML merge keys (<<) are not allowed",
                    )
                )
            if key in mapping:
                raise StrategyYamlLoadError(
                    _format_mark(
                        key_node.start_mark,
                        f"duplicate key {key!r} is not allowed",
                    )
                )
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def _format_mark(mark: Any, message: str) -> str:
    if mark is None:
        return message
    return f"{message} (line {mark.line + 1}, column {mark.column + 1})"


def _construct_bool(loader: SafeLoader, node: ScalarNode) -> bool:
    raw = loader.construct_scalar(node)
    if raw in {"true", "True", "TRUE"}:
        return True
    if raw in {"false", "False", "FALSE"}:
        return False
    raise StrategyYamlLoadError(
        _format_mark(
            node.start_mark,
            f"ambiguous boolean {raw!r} is not allowed; use true/false",
        )
    )


def _construct_ambiguous_bool(loader: SafeLoader, node: ScalarNode) -> None:
    raw = loader.construct_scalar(node)
    raise StrategyYamlLoadError(
        _format_mark(
            node.start_mark,
            f"ambiguous boolean {raw!r} is not allowed; use true/false",
        )
    )


def _construct_null(loader: SafeLoader, node: ScalarNode) -> None:
    return None


def _construct_int(loader: SafeLoader, node: ScalarNode) -> int:
    raw = loader.construct_scalar(node).replace("_", "")
    if ":" in raw:
        raise StrategyYamlLoadError(
            _format_mark(
                node.start_mark,
                f"sexagesimal integer {raw!r} is not allowed",
            )
        )
    if not _INT_PATTERN.fullmatch(raw):
        raise StrategyYamlLoadError(
            _format_mark(
                node.start_mark,
                f"unsupported integer scalar {raw!r}",
            )
        )
    return int(raw, 10)


def _construct_decimal(loader: SafeLoader, node: ScalarNode) -> Decimal:
    raw = loader.construct_scalar(node).replace("_", "")
    if raw in _FORBIDDEN_FLOAT_TOKENS:
        raise StrategyYamlLoadError(
            _format_mark(
                node.start_mark,
                f"non-finite number {raw!r} is not allowed",
            )
        )
    if ":" in raw:
        raise StrategyYamlLoadError(
            _format_mark(
                node.start_mark,
                f"sexagesimal number {raw!r} is not allowed",
            )
        )
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise StrategyYamlLoadError(
            _format_mark(
                node.start_mark,
                f"invalid decimal scalar {raw!r}",
            )
        ) from exc
    if not value.is_finite():
        raise StrategyYamlLoadError(
            _format_mark(
                node.start_mark,
                f"non-finite number {raw!r} is not allowed",
            )
        )
    return value


def _construct_undefined(loader: SafeLoader, node: Any) -> Any:
    raise StrategyYamlLoadError(
        _format_mark(
            getattr(node, "start_mark", None),
            f"unsupported YAML tag {getattr(node, 'tag', None)!r}",
        )
    )


def _configure_loader() -> None:
    resolvers: dict[Any, list[Any]] = {}
    for first, specs in _StrategySafeLoader.yaml_implicit_resolvers.items():
        kept = [
            (tag, regexp)
            for (tag, regexp) in specs
            if tag
            not in {
                "tag:yaml.org,2002:bool",
                "tag:yaml.org,2002:timestamp",
                "tag:yaml.org,2002:null",
            }
        ]
        if kept:
            resolvers[first] = kept
    _StrategySafeLoader.yaml_implicit_resolvers = resolvers

    _StrategySafeLoader.add_implicit_resolver(
        "tag:yaml.org,2002:bool",
        _BOOL_PATTERN,
        list("tTfF"),
    )
    _StrategySafeLoader.add_implicit_resolver(
        _AMBIGUOUS_BOOL_TAG,
        _AMBIGUOUS_BOOL_PATTERN,
        list("yYnNoO"),
    )
    _StrategySafeLoader.add_implicit_resolver(
        "tag:yaml.org,2002:null",
        _NULL_PATTERN,
        list("~nN"),
    )

    _StrategySafeLoader.add_constructor(
        "tag:yaml.org,2002:bool", _construct_bool
    )
    _StrategySafeLoader.add_constructor(
        _AMBIGUOUS_BOOL_TAG, _construct_ambiguous_bool
    )
    _StrategySafeLoader.add_constructor(
        "tag:yaml.org,2002:null", _construct_null
    )
    _StrategySafeLoader.add_constructor(
        "tag:yaml.org,2002:int", _construct_int
    )
    _StrategySafeLoader.add_constructor(
        "tag:yaml.org,2002:float", _construct_decimal
    )
    _StrategySafeLoader.yaml_constructors.pop("tag:yaml.org,2002:merge", None)
    _StrategySafeLoader.add_constructor(None, _construct_undefined)


_configure_loader()


def load_strategy_yaml(
    source: str | Path | TextIO,
    *,
    filename: str | None = None,
) -> dict[str, Any]:
    """Load YAML into a raw ``dict`` with Decimal-preserving numbers."""
    text, label = _read_source(source, filename=filename)
    if text.strip() == "":
        raise StrategyYamlLoadError(f"empty YAML document in {label}")

    try:
        documents = list(yaml.load_all(text, Loader=_StrategySafeLoader))
    except StrategyYamlLoadError:
        raise
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        detail = _format_mark(mark, f"invalid YAML in {label}: {exc}")
        raise StrategyYamlLoadError(detail) from exc

    if len(documents) == 0:
        raise StrategyYamlLoadError(f"empty YAML document in {label}")
    if len(documents) > 1:
        raise StrategyYamlLoadError(
            f"multiple YAML documents are not allowed in {label}"
        )

    loaded = documents[0]
    if loaded is None:
        raise StrategyYamlLoadError(f"empty YAML document in {label}")
    if not isinstance(loaded, dict):
        raise StrategyYamlLoadError(
            f"strategy YAML root must be a mapping, got "
            f"{type(loaded).__name__} in {label}"
        )
    return _sanitize_tree(loaded, path="$")


def load_strategy_yaml_path(path: str | Path) -> dict[str, Any]:
    """Load a strategy YAML file from ``path``."""
    return load_strategy_yaml(Path(path))


def _read_source(
    source: str | Path | TextIO,
    *,
    filename: str | None,
) -> tuple[str, str]:
    if isinstance(source, Path):
        return source.read_text(encoding="utf-8"), str(source)
    if hasattr(source, "read") and not isinstance(source, (str, Path)):
        text = source.read()
        if not isinstance(text, str):
            raise StrategyYamlLoadError("stream must yield str text")
        label = filename or getattr(source, "name", "<stream>")
        return text, str(label)
    if isinstance(source, str):
        path = Path(source)
        if "\n" not in source and path.is_file():
            return path.read_text(encoding="utf-8"), str(path)
        return source, filename or "<string>"
    raise StrategyYamlLoadError(
        f"unsupported YAML source type: {type(source).__name__}"
    )


def _sanitize_tree(value: Any, *, path: str) -> Any:
    if type(value) is float:
        raise StrategyYamlLoadError(
            f"binary float not allowed at {path}; "
            "use Decimal-preserving loader"
        )
    if isinstance(value, dict):
        if not all(isinstance(k, str) for k in value):
            raise StrategyYamlLoadError(
                f"mapping keys must be strings at {path}"
            )
        return {
            k: _sanitize_tree(v, path=f"{path}.{k}") for k, v in value.items()
        }
    if isinstance(value, list):
        return [
            _sanitize_tree(v, path=f"{path}[{i}]") for i, v in enumerate(value)
        ]
    if type(value) is bool:
        return value
    if type(value) is int:
        return value
    if type(value) is str:
        return value
    if type(value) is Decimal:
        if not value.is_finite():
            raise StrategyYamlLoadError(f"non-finite Decimal at {path}")
        return value
    if value is None:
        return None
    raise StrategyYamlLoadError(
        f"unsupported YAML value type {type(value).__name__} at {path}"
    )


def is_raw_mapping(value: object) -> bool:
    """Return True if ``value`` is a plain mapping (loaded root shape)."""
    return isinstance(value, Mapping)
