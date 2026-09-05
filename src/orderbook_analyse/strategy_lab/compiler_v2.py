"""Canonical StrategySpec V2 compilers and stable SHA256 strategy hashes (P5).

Trade-backtest compile validates full P4C including entry/exit.
Candidate-discovery compile validates a separate contract and never invents
trades, entries, exits, or PnL.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields, is_dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

from orderbook_analyse.strategy_lab.models.strategy_v2 import (
    AnyStrategySpecV2,
    CandidateDiscoveryStrategySpecV2,
    StrategySpecV2,
    TradeBacktestStrategySpecV2,
)
from orderbook_analyse.strategy_lab.validation.catalogs import CatalogBundleV2
from orderbook_analyse.strategy_lab.validation.p4c import (
    require_valid_candidate_discovery_v2,
    require_valid_strategy_v2_p4c,
)


class StrategyCompilationError(ValueError):
    """Raised when StrategySpec V2 compilation fails outside P4C validation."""


class CanonicalizationError(StrategyCompilationError):
    """Raised when a value cannot be represented in canonical JSON."""

    def __init__(self, message: str, *, path: str) -> None:
        self.path = path
        super().__init__(f"{message} (path={path})")


@dataclass(frozen=True, slots=True, kw_only=True)
class CompiledStrategyV2:
    """Immutable trade-backtest compile artifact."""

    canonical_bytes: bytes
    strategy_hash: str

    def __post_init__(self) -> None:
        if type(self.canonical_bytes) is not bytes:
            raise TypeError("canonical_bytes must be exact bytes")
        if type(self.strategy_hash) is not str or not self.strategy_hash:
            raise TypeError("strategy_hash must be a non-empty str")

    @property
    def canonical_json(self) -> str:
        return self.canonical_bytes.decode("utf-8")


@dataclass(frozen=True, slots=True, kw_only=True)
class CompiledCandidateDiscoveryV2:
    """Minimal candidate-discovery compile artifact (no trades / PnL)."""

    canonical_bytes: bytes
    strategy_hash: str
    candidate_states: tuple[str, ...]
    data_requirement_ids: tuple[str, ...]
    plugin_id: str | None

    def __post_init__(self) -> None:
        if type(self.canonical_bytes) is not bytes:
            raise TypeError("canonical_bytes must be exact bytes")
        if type(self.strategy_hash) is not str or not self.strategy_hash:
            raise TypeError("strategy_hash must be a non-empty str")

    @property
    def canonical_json(self) -> str:
        return self.canonical_bytes.decode("utf-8")


def compile_strategy_v2(
    spec: AnyStrategySpecV2,
    catalogs: CatalogBundleV2,
) -> CompiledStrategyV2:
    """Trade-backtest compile. Rejects candidate_discovery specs."""
    if isinstance(spec, CandidateDiscoveryStrategySpecV2):
        raise StrategyCompilationError(
            "CANDIDATE_DISCOVERY_NOT_TRADE_BACKTEST: "
            "candidate_discovery specs cannot use compile_strategy_v2; "
            "use compile_candidate_discovery_v2"
        )
    if not isinstance(spec, TradeBacktestStrategySpecV2):
        raise TypeError("spec must be a TradeBacktestStrategySpecV2 instance")
    require_valid_strategy_v2_p4c(spec, catalogs)
    canonical_bytes = render_canonical_strategy_v2_json(spec)
    return CompiledStrategyV2(
        canonical_bytes=canonical_bytes,
        strategy_hash=hash_canonical_strategy_v2_json(canonical_bytes),
    )


def compile_candidate_discovery_v2(
    spec: AnyStrategySpecV2,
    catalogs: CatalogBundleV2,
) -> CompiledCandidateDiscoveryV2:
    """Candidate-discovery compile — contract/hash only, no trade defaults."""
    if isinstance(spec, TradeBacktestStrategySpecV2):
        raise StrategyCompilationError(
            "TRADE_BACKTEST_NOT_CANDIDATE_DISCOVERY: "
            "trade_backtest specs cannot use compile_candidate_discovery_v2"
        )
    if not isinstance(spec, CandidateDiscoveryStrategySpecV2):
        raise TypeError("spec must be a CandidateDiscoveryStrategySpecV2 instance")
    require_valid_candidate_discovery_v2(spec, catalogs)
    canonical_bytes = render_canonical_strategy_v2_json(spec)
    plugin_id = None
    from orderbook_analyse.strategy_lab.models.signals import PluginSignalSpec

    if isinstance(spec.signal, PluginSignalSpec):
        plugin_id = spec.signal.plugin.plugin_id.value
    return CompiledCandidateDiscoveryV2(
        canonical_bytes=canonical_bytes,
        strategy_hash=hash_canonical_strategy_v2_json(canonical_bytes),
        candidate_states=tuple(s.value for s in spec.candidate_states),
        data_requirement_ids=tuple(
            r.requirement_id.value for r in spec.data_requirements
        ),
        plugin_id=plugin_id,
    )


def render_canonical_strategy_v2_json(spec: AnyStrategySpecV2) -> bytes:
    """Render deterministic UTF-8 JSON bytes for ``spec`` (no validation)."""
    if not isinstance(spec, (TradeBacktestStrategySpecV2, CandidateDiscoveryStrategySpecV2)):
        raise TypeError("spec must be a StrategySpec V2 root instance")
    payload = _canonicalize_value(spec, path="$")
    if not isinstance(payload, dict):
        raise CanonicalizationError(
            "canonical StrategySpec V2 root must be a mapping",
            path="$",
        )
    text = _render_json_value(payload, path="$")
    return text.encode("utf-8")


def hash_canonical_strategy_v2_json(data: bytes) -> str:
    """Return lowercase hex SHA256 of canonical JSON bytes."""
    if type(data) is not bytes:
        raise TypeError("data must be exact bytes")
    return hashlib.sha256(data).hexdigest()


def _canonicalize_value(value: object, *, path: str) -> object:
    if value is None:
        return None
    if isinstance(value, Enum):
        return value.value
    if type(value) is bool:
        return value
    if type(value) is int:
        return value
    if type(value) is str:
        return value
    if type(value) is Decimal:
        _validate_decimal(value, path=path)
        return value
    if type(value) is float:
        raise CanonicalizationError("float is not allowed", path=path)
    if type(value) is list:
        raise CanonicalizationError("list is not allowed; use tuple", path=path)
    if type(value) is set:
        raise CanonicalizationError("set is not allowed", path=path)
    if type(value) is tuple:
        return tuple(
            _canonicalize_value(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        )
    if is_dataclass(value) and not isinstance(value, type):
        return _canonicalize_dataclass(value, path=path)
    raise CanonicalizationError(
        f"unsupported type {type(value).__name__}",
        path=path,
    )


def _canonicalize_dataclass(value: object, *, path: str) -> dict[str, object]:
    payload: dict[str, object] = {}
    schema_kind = getattr(type(value), "_schema_kind", None)
    if isinstance(schema_kind, str):
        payload["kind"] = schema_kind
    for field in fields(value):
        field_path = f"{path}.{field.name}"
        payload[field.name] = _canonicalize_value(
            getattr(value, field.name),
            path=field_path,
        )
    return payload


def _validate_decimal(value: Decimal, *, path: str) -> None:
    if not value.is_finite():
        raise CanonicalizationError(
            "non-finite Decimal is not allowed",
            path=path,
        )


def _decimal_json_token(value: Decimal, *, path: str) -> str:
    """Return a JSON number token for Decimal (no exponent, no float)."""
    _validate_decimal(value, path=path)
    if value.is_zero():
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in {"", "-0", "+0", "-"}:
        return "0"
    # Guard against accidental exponent forms.
    if "e" in text.lower():
        raise CanonicalizationError(
            "Decimal JSON token must not use exponent notation",
            path=path,
        )
    return text


def _render_json_value(value: object, *, path: str) -> str:
    if value is None:
        return "null"
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int:
        return str(value)
    if type(value) is str:
        return json.dumps(value, ensure_ascii=False)
    if type(value) is Decimal:
        return _decimal_json_token(value, path=path)
    if type(value) is float:
        raise CanonicalizationError("float is not allowed", path=path)
    if type(value) is list:
        raise CanonicalizationError("list is not allowed; use tuple", path=path)
    if type(value) is set:
        raise CanonicalizationError("set is not allowed", path=path)
    if type(value) is tuple:
        items = [
            _render_json_value(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
        return "[" + ",".join(items) + "]"
    if type(value) is dict:
        parts: list[str] = []
        for key in sorted(value):
            if type(key) is not str:
                raise CanonicalizationError(
                    f"object keys must be str, got {type(key).__name__}",
                    path=path,
                )
            key_json = json.dumps(key, ensure_ascii=False)
            child_path = f"$.{key}" if path == "$" else f"{path}.{key}"
            parts.append(
                key_json + ":" + _render_json_value(value[key], path=child_path)
            )
        return "{" + ",".join(parts) + "}"
    raise CanonicalizationError(
        f"unsupported rendered type {type(value).__name__}",
        path=path,
    )
