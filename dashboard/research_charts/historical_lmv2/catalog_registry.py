"""Server-side registry of allowed historical production catalogs (no client paths)."""

from __future__ import annotations

from pathlib import Path

from .catalog import ProductionCatalog, discover_production_catalog
from .errors import UnknownCatalogError

_CH_TEST = Path(__file__).resolve().parents[1] / "live_micro_lld_v2_ch_test"

REGISTERED_CATALOG_IDS: dict[str, Path] = {
    "doge_multi_month_2026": _CH_TEST / "doge_multi_month_2026_history",
    "doge_september_2026": _CH_TEST / "doge_september_2026_history",
}


def resolve_registered_catalog(catalog_id: str) -> ProductionCatalog:
    key = str(catalog_id or "").strip()
    root = REGISTERED_CATALOG_IDS.get(key)
    if root is None or not root.is_dir():
        raise UnknownCatalogError(f"unknown or missing catalog_id {catalog_id!r}")
    return discover_production_catalog(root, name=key)


def list_registered_catalog_ids() -> list[str]:
    return sorted(REGISTERED_CATALOG_IDS.keys())
