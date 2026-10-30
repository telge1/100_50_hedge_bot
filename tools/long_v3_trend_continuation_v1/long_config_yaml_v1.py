"""Load LONG V3 research YAML (read-only)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class LongSupportQualificationYamlV1:
    min_pool_age_bars: int = 5
    min_pool_age_mode: str = "consecutive_active_depth2_v1"
    min_pool_height_pct: float = 0.1
    require_formation_below_price: bool = True
    min_unique_origins: int = 2
    selection_mode: str = "nearest_qualified_lower_support"
    lower_gap_threshold_pct: float = 0.6


@dataclass(frozen=True)
class LongResearchYamlV1:
    support: LongSupportQualificationYamlV1
    enabled: bool = False
    max_risk_pct: float | None = None
    recovery_mode: str = "next_bar"
    compare_same_bar_recovery: bool = True


def load_long_research_yaml(path: Path) -> LongResearchYamlV1:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    root = raw.get("long_trend_continuation") or {}
    sup = root.get("support") or {}
    entry = root.get("entry") or {}
    sl = root.get("stop_loss") or {}
    res = root.get("research") or {}
    gap = float(entry.get("lower_gap_threshold_pct") or sup.get("lower_gap_threshold_pct") or 0.6)
    support = LongSupportQualificationYamlV1(
        min_pool_age_bars=int(sup.get("min_pool_age_bars") or 5),
        min_pool_age_mode=str(sup.get("min_pool_age_mode") or "consecutive_active_depth2_v1"),
        min_pool_height_pct=float(sup.get("min_pool_height_pct") or 0.1),
        require_formation_below_price=bool(sup.get("require_formation_below_price", True)),
        min_unique_origins=int(sup.get("min_unique_origins") or 2),
        selection_mode=str(sup.get("selection_mode") or "nearest_qualified_lower_support"),
        lower_gap_threshold_pct=gap,
    )
    mr = sl.get("max_risk_pct")
    max_risk = None if mr is None or str(mr).lower() in ("null", "none", "") else float(mr)
    return LongResearchYamlV1(
        enabled=bool(root.get("enabled", False)),
        support=support,
        max_risk_pct=max_risk,
        recovery_mode=str(res.get("recovery_mode") or "next_bar"),
        compare_same_bar_recovery=bool(res.get("compare_same_bar_recovery", True)),
    )
