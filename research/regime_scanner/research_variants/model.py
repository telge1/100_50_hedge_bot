"""Variant model and deterministic hashing."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from research.regime_scanner.research_runs.hashing import json_hash
from research.regime_scanner.research_runs.parameters import parameter_hash
from research.regime_scanner.research_variants.version import RUNNER_VERSION

# Variant hash serialization:
# json.dumps({name, description, tags, parameter_overrides (sorted keys),
#             resulting_parameter_hash}, sort_keys=True, separators=(",", ":"))
# Excludes run_id, timestamps, duration.


@dataclass(frozen=True)
class ResearchVariant:
    name: str
    description: str
    parameter_overrides: dict[str, object]
    tags: tuple[str, ...] = ()

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "tags": list(self.tags),
            "parameter_overrides": {
                str(k): self.parameter_overrides[k] for k in sorted(self.parameter_overrides)
            },
        }


@dataclass(frozen=True)
class ResearchVariantSet:
    name: str
    description: str
    variants: tuple[ResearchVariant, ...]

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "variants": [v.to_canonical_dict() for v in self.variants],
        }


def variant_hash(
    variant: ResearchVariant,
    *,
    resulting_parameter_hash: str,
) -> str:
    payload = {
        "name": variant.name,
        "description": variant.description,
        "tags": list(variant.tags),
        "parameter_overrides": {
            str(k): variant.parameter_overrides[k] for k in sorted(variant.parameter_overrides)
        },
        "resulting_parameter_hash": resulting_parameter_hash,
        "runner_version": RUNNER_VERSION,
    }
    return json_hash(payload)


def variant_set_hash(variant_set: ResearchVariantSet) -> str:
    return json_hash(variant_set.to_canonical_dict())


def variant_set_json(variant_set: ResearchVariantSet) -> str:
    return json.dumps(variant_set.to_canonical_dict(), sort_keys=True, separators=(",", ":"))
