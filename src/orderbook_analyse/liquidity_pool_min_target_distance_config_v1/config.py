"""Load and validate room_to_target configuration from strategy YAML."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from orderbook_analyse.liquidity_pool_min_target_distance_config_v1 import (
    CANONICAL_STRATEGY_YAML_REL,
    MAX_MIN_TARGET_DISTANCE_PCT,
    STRATEGY_RESEARCH_DOC_REL,
)


class RoomGateConfigError(ValueError):
    """Invalid or missing room_to_target configuration."""


class ConfigOwnerAmbiguousError(RuntimeError):
    """Multiple competing strategy YAML owners for LP market response."""


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class RoomToTargetConfig:
    enabled: bool
    min_target_distance_pct: float
    min_target_distance_bps: float
    measurement_origin: str
    target_edge_policy: str
    long_pool_side: str
    long_edge: str
    short_pool_side: str
    short_edge: str
    comparison: str
    overlap_policy: str
    missing_target_policy: str
    cost_scenarios_bps: tuple[float, ...]
    config_source_path: str
    config_loaded_at: str
    config_sha256: str


@dataclass(frozen=True)
class EffectiveRoomConfig:
    """Immutable room-gate config loaded once per audit run."""

    room: RoomToTargetConfig
    config_path_rel: str
    config_sha256: str


def repo_root_from(start: Path | None = None) -> Path:
    cur = (start or Path(__file__)).resolve()
    for parent in [cur, *cur.parents]:
        if (parent / "src" / "orderbook_analyse").is_dir() and (
            parent / "strategies" / "strategy_lab"
        ).is_dir():
            return parent
    raise FileNotFoundError("orderbook_analyse repo root not found")


def discover_lp_market_response_yaml_candidates(repo_root: Path) -> list[Path]:
    """Return strategy_lab YAML files that appear to own LP market response config."""
    lab = repo_root / "strategies" / "strategy_lab"
    if not lab.is_dir():
        return []
    hits: list[Path] = []
    for path in sorted(lab.glob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        name = path.name.lower()
        if "liquidity_pool" in name and "market_response" in name:
            hits.append(path)
            continue
        if "room_to_target:" in text and "liquidity_pool" in text.lower():
            hits.append(path)
    return hits


def resolve_config_yaml_path(repo_root: Path) -> tuple[Path, str]:
    """Resolve the single canonical strategy YAML for room_to_target config."""
    candidates = discover_lp_market_response_yaml_candidates(repo_root)
    canonical = repo_root / CANONICAL_STRATEGY_YAML_REL
    research_doc = repo_root / STRATEGY_RESEARCH_DOC_REL

    if len(candidates) > 1:
        raise ConfigOwnerAmbiguousError(
            "Multiple LP market response strategy YAML candidates: "
            + ", ".join(str(p.relative_to(repo_root)) for p in candidates)
        )
    if len(candidates) == 1:
        chosen = candidates[0]
        if chosen != canonical and canonical.exists():
            raise ConfigOwnerAmbiguousError(
                f"Competing owners: {chosen.relative_to(repo_root)} vs "
                f"{canonical.relative_to(repo_root)}"
            )
        rationale = (
            f"Single strategy_lab YAML matching liquidity pool market response "
            f"research doc {STRATEGY_RESEARCH_DOC_REL}"
        )
        return chosen, rationale
    if canonical.exists():
        rationale = (
            f"Canonical YAML created alongside research doc "
            f"{STRATEGY_RESEARCH_DOC_REL}; no competing LP market response YAML "
            f"in strategy_lab (other YAMLs are unrelated strategies)."
        )
        return canonical, rationale
    unrelated = sorted((repo_root / "strategies" / "strategy_lab").glob("*.yaml"))
    raise ConfigOwnerAmbiguousError(
        "ROOM_GATE_CONFIG_OWNER_NOT_UNAMBIGUOUS: no LP market response YAML. "
        f"Research doc: {research_doc if research_doc.exists() else STRATEGY_RESEARCH_DOC_REL}. "
        "Unrelated strategy_lab YAMLs: "
        + ", ".join(p.name for p in unrelated)
    )


def _require_mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RoomGateConfigError(f"{field} must be a mapping")
    return value


def _positive_finite(value: Any, field: str) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError) as exc:
        raise RoomGateConfigError(f"{field} must be numeric") from exc
    if not math.isfinite(num) or num <= 0:
        raise RoomGateConfigError(f"{field} must be finite and > 0")
    return num


def validate_room_to_target_block(block: dict[str, Any]) -> dict[str, Any]:
    """Validate room_to_target block; return normalized audit fields."""
    issues: list[str] = []

    enabled = block.get("enabled")
    if not isinstance(enabled, bool):
        issues.append("enabled must be boolean")

    try:
        min_pct = _positive_finite(block.get("min_target_distance_pct"), "min_target_distance_pct")
        if min_pct > MAX_MIN_TARGET_DISTANCE_PCT:
            issues.append(
                f"min_target_distance_pct must be <= {MAX_MIN_TARGET_DISTANCE_PCT}"
            )
    except RoomGateConfigError as exc:
        issues.append(str(exc))
        min_pct = None

    measurement_origin = block.get("measurement_origin")
    if measurement_origin != "mechanical_entry_price":
        issues.append("measurement_origin must be mechanical_entry_price")

    target_edge_policy = block.get("target_edge_policy")
    if target_edge_policy != "first_reachable_edge":
        issues.append("target_edge_policy must be first_reachable_edge")

    long_target = _require_mapping(block.get("long_target"), "long_target") if block.get("long_target") else {}
    short_target = _require_mapping(block.get("short_target"), "short_target") if block.get("short_target") else {}

    if long_target.get("pool_side") != "ask" or long_target.get("edge") != "lower":
        issues.append("long_target must be pool_side=ask edge=lower")
    if short_target.get("pool_side") != "bid" or short_target.get("edge") != "upper":
        issues.append("short_target must be pool_side=bid edge=upper")

    comparison = block.get("comparison")
    if comparison != "greater_than_or_equal":
        issues.append("comparison must be greater_than_or_equal")

    overlap_policy = block.get("overlap_policy")
    if overlap_policy != "block":
        issues.append("overlap_policy must be block")

    missing_target_policy = block.get("missing_target_policy")
    if missing_target_policy != "block":
        issues.append("missing_target_policy must be block")

    cost_raw = block.get("cost_scenarios_bps")
    cost_bps: list[float] = []
    if not isinstance(cost_raw, list) or not cost_raw:
        issues.append("cost_scenarios_bps must be a non-empty list")
    else:
        for i, item in enumerate(cost_raw):
            try:
                cost_bps.append(_positive_finite(item, f"cost_scenarios_bps[{i}]"))
            except RoomGateConfigError as exc:
                issues.append(str(exc))

    valid = not issues
    return {
        "valid": valid,
        "issues": issues,
        "min_target_distance_pct": min_pct,
        "min_target_distance_bps": (min_pct * 100.0) if min_pct is not None else None,
        "cost_scenarios_bps": cost_bps,
    }


def load_room_to_target_config(
    repo_root: Path | None = None,
    yaml_path: Path | None = None,
) -> RoomToTargetConfig:
    """Load room_to_target config from the canonical strategy YAML (fail-closed)."""
    root = repo_root or repo_root_from()
    loaded_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if yaml_path is None:
        path, _ = resolve_config_yaml_path(root)
    else:
        path = yaml_path

    if not path.is_file():
        raise RoomGateConfigError(f"strategy YAML not found: {path}")

    with path.open(encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    if not isinstance(doc, dict):
        raise RoomGateConfigError("strategy YAML must be a mapping")

    block = doc.get("room_to_target")
    if block is None:
        raise RoomGateConfigError("room_to_target block missing from strategy YAML")

    validation = validate_room_to_target_block(_require_mapping(block, "room_to_target"))
    if not validation["valid"]:
        raise RoomGateConfigError("; ".join(validation["issues"]))

    min_pct = float(validation["min_target_distance_pct"])
    cfg_sha = sha256_file(path)
    return RoomToTargetConfig(
        enabled=bool(block["enabled"]),
        min_target_distance_pct=min_pct,
        min_target_distance_bps=min_pct * 100.0,
        measurement_origin=str(block["measurement_origin"]),
        target_edge_policy=str(block["target_edge_policy"]),
        long_pool_side=str(block["long_target"]["pool_side"]),
        long_edge=str(block["long_target"]["edge"]),
        short_pool_side=str(block["short_target"]["pool_side"]),
        short_edge=str(block["short_target"]["edge"]),
        comparison=str(block["comparison"]),
        overlap_policy=str(block["overlap_policy"]),
        missing_target_policy=str(block["missing_target_policy"]),
        cost_scenarios_bps=tuple(float(x) for x in validation["cost_scenarios_bps"]),
        config_source_path=str(path.resolve()),
        config_loaded_at=loaded_at,
        config_sha256=cfg_sha,
    )


def load_effective_room_config(
    repo_root: Path | None = None,
    yaml_path: Path | None = None,
) -> EffectiveRoomConfig:
    root = repo_root or repo_root_from()
    if yaml_path is None:
        path, _ = resolve_config_yaml_path(root)
    else:
        path = yaml_path
    room = load_room_to_target_config(root, yaml_path=path)
    rel = str(path.resolve().relative_to(root.resolve()))
    return EffectiveRoomConfig(
        room=room,
        config_path_rel=rel,
        config_sha256=room.config_sha256,
    )
