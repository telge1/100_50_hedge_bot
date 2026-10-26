"""BT-D16: Safe YAML front-end → BT-D6 JSON config (fail-closed)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from config_validate_v1 import CONFIG_VERSION, ConfigValidationError, validate_config_dict
from manifest_v1 import config_content_hash

YAML_SCHEMA_VERSION = "bt_d16_yaml_v1"

# Top-level keys allowed in configs/backtester.yaml (no free-form passthrough).
YAML_TOP_KEYS = frozenset(
    {
        "yaml_schema_version",
        "config_version",
        "strategy_identity",
        "density",
        "short_entry",
        "long_entry",
        "risk",
        "take_profit",
        "pnl",
        "backtest",
        "result_export",
    }
)

# YAML entry_price_mode / entry_mode → BT-D6 entry_price_reference + offset rules (BT-D17).
SHORT_ENTRY_PRICE_MODES = frozenset(
    {
        "pool_lower_edge",
        "pool_upper_edge",
        "pool_lower_edge_offset",
    }
)

MODE_TO_REFERENCE: dict[str, str] = {
    "pool_lower_edge": "upper_pool_lower_edge",
    "pool_upper_edge": "upper_pool_upper_edge",
    "pool_lower_edge_offset": "upper_pool_lower_edge",
}


class UniqueKeyLoader(yaml.SafeLoader):
    """Reject duplicate mapping keys (fail-closed)."""


def _construct_mapping(loader: yaml.Loader, node: yaml.MappingNode, deep: bool = False) -> dict:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ConfigValidationError(
                "DUPLICATE_YAML_KEY",
                f"duplicate YAML key: {key!r}",
                {"key": str(key)},
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


def _reject_unknown_top(raw: dict[str, Any]) -> None:
    extra = set(raw.keys()) - YAML_TOP_KEYS
    if extra:
        raise ConfigValidationError(
            "UNKNOWN_YAML_KEYS",
            "unknown top-level YAML keys",
            {"unknown": sorted(extra)},
        )


def _map_short_entry(se: dict[str, Any], long_entry: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(se, dict):
        raise ConfigValidationError("INVALID_SHORT_ENTRY", "short_entry must be mapping")
    allowed = frozenset(se.keys())
    extra = allowed - frozenset(
        {
            "entry_price_mode",
            "entry_mode",
            "entry_offset_pct",
            "entry_pool_selection_mode",
            "selection_mode",
            "entry_price_reference",
            "pool_min_age_bars",
            "upper_gap_min_pct",
            "gap_min_pct",
            "upper_gap_measurement_mode",
            "upper_pool_proximity_pct",
            "pool_proximity_pct",
            "pending_limit_enabled",
            "pending_limit",
            "pending_limit_expiry_bars",
            "pending_limit_fill_model",
            "pool_cancel_identity_mode",
            "signal_selection_mode",
        }
    )
    if extra:
        raise ConfigValidationError(
            "UNKNOWN_YAML_KEYS",
            "unknown short_entry keys",
            {"unknown": sorted(extra)},
        )

    le = long_entry or {}
    if le.get("enabled") is True:
        raise ConfigValidationError(
            "LONG_ENTRY_NOT_IMPLEMENTED",
            "long_entry.enabled must be false until a separate long strategy is approved",
        )

    mode_yaml = se.get("entry_price_mode")
    mode_legacy = se.get("entry_mode")
    if mode_yaml is not None and mode_legacy is not None and str(mode_yaml) != str(mode_legacy):
        raise ConfigValidationError(
            "ENTRY_MODE_CONFLICT",
            "entry_price_mode and entry_mode must match when both are set",
            {"entry_price_mode": mode_yaml, "entry_mode": mode_legacy},
        )
    mode = str(mode_yaml or mode_legacy or "pool_lower_edge").strip()
    if mode not in SHORT_ENTRY_PRICE_MODES:
        raise ConfigValidationError(
            "ENTRY_MODE_NOT_EXECUTABLE",
            f"unknown short entry price mode: {mode}",
            {"entry_price_mode": mode},
        )

    offset_raw = se.get("entry_offset_pct")
    offset = 0.0 if offset_raw is None else float(offset_raw)
    if offset < 0:
        raise ConfigValidationError("INVALID_ENTRY_OFFSET_PCT", "entry_offset_pct must be non-negative")

    if "entry_price_reference" in se:
        ref = str(se["entry_price_reference"])
        if ref != MODE_TO_REFERENCE[mode]:
            raise ConfigValidationError(
                "ENTRY_MODE_REFERENCE_CONFLICT",
                "entry_price_reference disagrees with entry_price_mode",
                {"mode": mode, "entry_price_reference": ref},
            )
    else:
        ref = MODE_TO_REFERENCE[mode]

    if mode == "pool_lower_edge" and offset != 0.0:
        raise ConfigValidationError(
            "ENTRY_MODE_OFFSET_CONFLICT",
            "pool_lower_edge requires entry_offset_pct 0 (use pool_lower_edge_offset for offset)",
            {"entry_offset_pct": offset},
        )
    if mode == "pool_upper_edge" and offset != 0.0:
        raise ConfigValidationError(
            "ENTRY_MODE_OFFSET_CONFLICT",
            "pool_upper_edge requires entry_offset_pct 0",
            {"entry_offset_pct": offset},
        )

    out = {
        "entry_pool_selection_mode": str(
            se.get("entry_pool_selection_mode")
            or se.get("signal_selection_mode")
            or se.get("selection_mode")
            or ""
        ).strip()
        or "nearest_upper_proximity_v1",
        "entry_price_reference": ref,
        "pool_min_age_bars": int(se.get("pool_min_age_bars") or 0),
        "upper_gap_min_pct": float(se.get("upper_gap_min_pct") if "upper_gap_min_pct" in se else se.get("gap_min_pct")),
        "upper_gap_measurement_mode": str(
            se.get("upper_gap_measurement_mode") or "adjacent_union_interval_gap_pct_v1"
        ),
        "upper_pool_proximity_pct": float(
            se.get("upper_pool_proximity_pct")
            if "upper_pool_proximity_pct" in se
            else se.get("pool_proximity_pct", 0.05)
        ),
        "pending_limit_enabled": bool(
            se["pending_limit_enabled"] if "pending_limit_enabled" in se else se.get("pending_limit", True)
        ),
        "pending_limit_expiry_bars": se.get("pending_limit_expiry_bars"),
    }
    fm = se.get("pending_limit_fill_model")
    if fm is not None and str(fm).strip():
        out["pending_limit_fill_model"] = str(fm).strip()
    if offset != 0.0:
        out["entry_offset_pct"] = offset
    pcm = se.get("pool_cancel_identity_mode")
    if pcm is not None and str(pcm).strip():
        out["pool_cancel_identity_mode"] = str(pcm).strip()
    return out


def _normalize_density(raw_density: dict[str, Any]) -> dict[str, Any]:
    """Match JSON configs: omit optional validated_cache keys when unset (null)."""
    d = dict(raw_density)
    vc = d.get("validated_cache")
    if isinstance(vc, dict):
        vc = dict(vc)
        if vc.get("required_config_hash") is None:
            vc.pop("required_config_hash", None)
        d["validated_cache"] = vc
    return d


def yaml_document_to_config_dict(raw: dict[str, Any]) -> dict[str, Any]:
    _reject_unknown_top(raw)
    schema = str(raw.get("yaml_schema_version") or YAML_SCHEMA_VERSION)
    if schema != YAML_SCHEMA_VERSION:
        raise ConfigValidationError(
            "YAML_SCHEMA_MISMATCH",
            f"expected yaml_schema_version={YAML_SCHEMA_VERSION}",
            {"got": schema},
        )
    cv = str(raw.get("config_version") or CONFIG_VERSION)
    if cv != CONFIG_VERSION:
        raise ConfigValidationError("CONFIG_VERSION_MISMATCH", f"expected {CONFIG_VERSION}", {"got": cv})

    for section in ("strategy_identity", "density", "risk", "take_profit", "pnl", "backtest"):
        if section not in raw:
            raise ConfigValidationError("MISSING_YAML_SECTION", f"missing section: {section}")

    cfg: dict[str, Any] = {
        "config_version": CONFIG_VERSION,
        "strategy_identity": dict(raw["strategy_identity"]),
        "density": _normalize_density(raw["density"]),
        "short_entry": _map_short_entry(raw["short_entry"], raw.get("long_entry")),
        "risk": dict(raw["risk"]),
        "take_profit": dict(raw["take_profit"]),
        "pnl": dict(raw["pnl"]),
        "backtest": dict(raw["backtest"]),
    }
    return validate_config_dict(cfg)


def load_yaml_text(text: str, *, source: str = "<string>") -> dict[str, Any]:
    try:
        data = yaml.load(text, Loader=UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise ConfigValidationError("YAML_PARSE_ERROR", str(exc), {"source": source}) from exc
    if not isinstance(data, dict):
        raise ConfigValidationError("YAML_ROOT_NOT_MAPPING", "YAML root must be a mapping", {"source": source})
    return yaml_document_to_config_dict(data)


def load_yaml_and_validate(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    return load_yaml_text(text, source=str(path))


def load_config_with_provenance(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    path = path.resolve()
    if path.suffix.lower() in (".yaml", ".yml"):
        cfg = load_yaml_and_validate(path)
        prov = {
            "format": "yaml",
            "yaml_schema_version": YAML_SCHEMA_VERSION,
            "source_path": str(path),
            "normalized_config_hash": config_content_hash(cfg),
        }
        return cfg, prov
    raw = json.loads(path.read_text(encoding="utf-8"))
    cfg = validate_config_dict(raw)
    prov = {
        "format": "json",
        "source_path": str(path),
        "normalized_config_hash": config_content_hash(cfg),
    }
    return cfg, prov
