"""July 2026 catalog validation for smoke backtests (read-only)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .catalog import ProductionCatalog, discover_production_catalog
from .provider import HistoricalLmV2PoolStore, combo_folder, decision_close_to_stem


@dataclass
class JulyCatalogReport:
    ok: bool
    frozen_input_id: str
    month_status: str
    backtester_candidate: bool
    sample_15m_pd10_present: bool
    sample_4h_pd10_present: bool
    issues: list[str]


def validate_july_catalog(production_root: Path, *, sample_close: datetime) -> tuple[ProductionCatalog, JulyCatalogReport]:
    issues: list[str] = []
    cat = discover_production_catalog(production_root)
    month = "2026-07"
    rel = next((r for r in cat.releases if r.month == month), None)
    if rel is None:
        issues.append("july manifest missing")
        return cat, JulyCatalogReport(False, cat.frozen_input_id, "MISSING", False, False, False, issues)

    if rel.month_status != "COMPLETE":
        issues.append(f"MONTH_STATUS={rel.month_status}")
    if not rel.backtester_candidate:
        issues.append("BACKTESTER_CANDIDATE=false")

    store = HistoricalLmV2PoolStore(cat)
    sample_close = sample_close if sample_close.tzinfo else sample_close.replace(tzinfo=timezone.utc)
    stem = decision_close_to_stem(sample_close)
    p15 = store.snapshot_path("15m", 10, sample_close)
    p4 = store.snapshot_path("4h", 10, sample_close)
    s15 = p15.is_file()
    s4 = p4.is_file()
    if not s15:
        issues.append(f"missing 15m_PD10 {stem}")
    if not s4:
        issues.append(f"missing 4h_PD10 for same stem (guard may need earlier 4h close)")

    ok = not issues and rel.month_status == "COMPLETE" and rel.backtester_candidate
    return cat, JulyCatalogReport(
        ok=ok,
        frozen_input_id=cat.frozen_input_id,
        month_status=rel.month_status,
        backtester_candidate=rel.backtester_candidate,
        sample_15m_pd10_present=s15,
        sample_4h_pd10_present=s4,
        issues=issues,
    )


def count_closes_in_month(production_root: Path, timeframe: str, pool_detail: int, month: str) -> int:
    folder = production_root / "production" / "results" / month / combo_folder(timeframe, pool_detail)
    if not folder.is_dir():
        return 0
    return sum(1 for p in folder.glob("*.json") if not p.name.endswith(".tmp"))
