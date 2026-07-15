"""In-memory variant store for unit tests."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any


@dataclass
class InMemoryVariantStore:
    variant_sets: dict[str, dict[str, Any]] = field(default_factory=dict)
    variant_set_ids: dict[str, int] = field(default_factory=dict)
    variant_runs: dict[tuple[int, str], dict[str, Any]] = field(default_factory=dict)
    run_metrics: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    _next_id: int = 1

    def init_schema(self) -> None:
        return None

    def close(self) -> None:
        return None

    def ensure_variant_set(
        self,
        *,
        variant_set_hash: str,
        name: str,
        description: str,
        variants_json: str,
    ) -> int:
        if variant_set_hash in self.variant_set_ids:
            return self.variant_set_ids[variant_set_hash]
        vid = self._next_id
        self._next_id += 1
        self.variant_set_ids[variant_set_hash] = vid
        self.variant_sets[variant_set_hash] = {
            "id": vid,
            "variant_set_hash": variant_set_hash,
            "name": name,
            "description": description,
            "variants_json": variants_json,
        }
        return vid

    def upsert_variant_run(self, **kwargs: Any) -> None:
        key = (int(kwargs["variant_set_id"]), str(kwargs["variant_name"]))
        self.variant_runs[key] = copy.deepcopy(kwargs)

    def list_variant_runs(self, variant_set_id: int) -> list[dict[str, Any]]:
        rows = [r for (sid, _), r in self.variant_runs.items() if sid == int(variant_set_id)]
        rows.sort(key=lambda r: (r.get("rank_position") is None, r.get("rank_position"), r.get("variant_name")))
        return copy.deepcopy(rows)

    def get_variant_set_by_name(self, name: str) -> dict[str, Any] | None:
        for row in self.variant_sets.values():
            if row.get("name") == name:
                return copy.deepcopy(row)
        return None

    def update_rankings(self, variant_set_id: int, rankings: list[tuple[str, int]]) -> None:
        for variant_name, rank in rankings:
            key = (int(variant_set_id), variant_name)
            if key in self.variant_runs:
                self.variant_runs[key]["rank_position"] = int(rank)

    def append_run_metrics(self, run_id: str, metrics: list[dict[str, Any]]) -> None:
        bucket = self.run_metrics.setdefault(run_id, [])
        existing = {m["metric_name"] for m in bucket}
        for row in metrics:
            if row["metric_name"] in existing:
                bucket = [m for m in bucket if m["metric_name"] != row["metric_name"]]
                self.run_metrics[run_id] = bucket
            bucket.append(copy.deepcopy(row))
