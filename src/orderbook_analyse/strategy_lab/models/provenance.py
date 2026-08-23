"""Provenance value objects for StrategySpec V1."""

from __future__ import annotations

from dataclasses import dataclass

from orderbook_analyse.strategy_lab.models.enums import CausalityStatus


@dataclass(frozen=True, slots=True, kw_only=True)
class PluginProvenanceRef:
    """Versioned plugin reference for provenance (not free-form dicts)."""

    plugin_id: str
    version: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ProvenanceSpec:
    """Required provenance on every StrategySpec.

    Git commit is an explicit authoring field — never auto-detected in P1.
    """

    source_of_truth_module: str
    source_of_truth_path: str
    git_commit: str
    strategy_ref: str
    policy_ref: str
    plugin_refs: tuple[PluginProvenanceRef, ...]
    causality_status: CausalityStatus
    causality_claim: str
    external_runtime_dependencies: tuple[str, ...]
    known_limitations: tuple[str, ...]
    notes: tuple[str, ...] = ()
