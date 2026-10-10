"""Manifest-gated catalog for historical V2 production trees (read-only)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MonthRelease:
    month: str
    month_status: str
    backtester_candidate: bool
    frozen_input_id: str
    partial_combinations: tuple[str, ...]


@dataclass(frozen=True)
class ProductionCatalog:
    """One isolated production root (single frozen-input lineage per backtest run)."""

    name: str
    production_root: Path
    frozen_input_id: str
    releases: tuple[MonthRelease, ...]

    @property
    def results_root(self) -> Path:
        return self.production_root / "production" / "results"

    def allowed_months(self) -> frozenset[str]:
        return frozenset(
            r.month
            for r in self.releases
            if r.month_status == "COMPLETE" and r.backtester_candidate
        )


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def discover_production_catalog(production_root: Path, *, name: str | None = None) -> ProductionCatalog:
    """Load COMPLETE + BACKTESTER_CANDIDATE months only; reject PARTIAL at gate."""
    root = production_root.resolve()
    status_path = root / "production" / "status.json"
    frozen_input_id = ""
    if status_path.is_file():
        frozen_input_id = str(_load_json(status_path).get("FROZEN_INPUT_ID") or "")

    months_dir = root / "production" / "months"
    releases: list[MonthRelease] = []
    if months_dir.is_dir():
        for manifest_path in sorted(months_dir.glob("*/manifest.json")):
            m = _load_json(manifest_path)
            month = str(m.get("month") or manifest_path.parent.name)
            fid = str(m.get("FROZEN_INPUT_ID") or frozen_input_id)
            if fid and not frozen_input_id:
                frozen_input_id = fid
            releases.append(
                MonthRelease(
                    month=month,
                    month_status=str(m.get("MONTH_STATUS") or "UNKNOWN"),
                    backtester_candidate=bool(m.get("BACKTESTER_CANDIDATE")),
                    frozen_input_id=fid,
                    partial_combinations=tuple(m.get("PARTIAL_COMBINATIONS") or []),
                )
            )

    if not frozen_input_id and releases:
        frozen_input_id = releases[0].frozen_input_id

    catalog_name = name or root.name
    return ProductionCatalog(name=catalog_name, production_root=root, frozen_input_id=frozen_input_id, releases=tuple(releases))


def assert_month_allowed(catalog: ProductionCatalog, month: str, combo: str) -> None:
    from .errors import ManifestGateError

    rel = next((r for r in catalog.releases if r.month == month), None)
    if rel is None:
        raise ManifestGateError(f"month {month} not in catalog {catalog.name}")
    if rel.month_status != "COMPLETE":
        raise ManifestGateError(f"month {month} status {rel.month_status} (not COMPLETE)")
    if not rel.backtester_candidate:
        raise ManifestGateError(f"month {month} BACKTESTER_CANDIDATE=false")
    if combo in rel.partial_combinations:
        raise ManifestGateError(f"combo {combo} PARTIAL in month {month}")
