"""Descriptive group tables. Never regroup after outcomes. Small n → INSUFFICIENT_SAMPLE."""

from __future__ import annotations

from collections import Counter
from typing import Any

from . import INSUFFICIENT_SAMPLE_N


def _flag(n: int) -> str:
    return "INSUFFICIENT_SAMPLE" if n < INSUFFICIENT_SAMPLE_N else "DESCRIPTIVE_ONLY"


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    if len(s) % 2:
        return float(s[mid])
    return (float(s[mid - 1]) + float(s[mid])) / 2.0


def group_rows(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        buckets.setdefault(str(row.get(key) or "UNKNOWN"), []).append(row)
    out = []
    for name, items in sorted(buckets.items()):
        n = len(items)
        directed = [r for r in items if r.get("reaction_direction") in {"BULLISH", "BEARISH"}]
        profit_first = sum(1 for r in directed if r.get("first_nonzero_direction") == "PROFIT_FIRST")
        adverse_first = sum(1 for r in directed if r.get("first_nonzero_direction") == "ADVERSE_FIRST")
        mfe = [float(r["mfe_300s"]) for r in directed if r.get("mfe_300s") is not None]
        mae = [float(r["mae_300s"]) for r in directed if r.get("mae_300s") is not None]
        give = [float(r["giveback_300s"]) for r in directed if r.get("giveback_300s") is not None]
        retained = [float(r["retained_300s"]) for r in directed if r.get("retained_300s") is not None]
        out.append(
            {
                "group_key": key,
                "group_value": name,
                "n": n,
                "n_directed": len(directed),
                "sample_warning": _flag(n),
                "profit_first_n": profit_first,
                "adverse_first_n": adverse_first,
                "median_mfe_300s": _median(mfe),
                "median_mae_300s": _median(mae),
                "median_giveback_300s": _median(give),
                "median_retained_300s": _median(retained),
                "no_significance_claim": True,
                "no_profitability_claim": True,
            }
        )
    return out


def summarize_all(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keys = (
        "highest_timeframe",
        "confluence_class",
        "profile_state_mix",
        "primary_tpo_type",
        "lld_confluence_type",
        "reaction_family",
        "full_ob_label",
        "footprint_label",
        "oi_label",
    )
    out: list[dict[str, Any]] = []
    for key in keys:
        out.extend(group_rows(rows, key))
    return out


def reaction_family(reaction_class: str) -> str:
    if reaction_class.startswith("REJECTED_"):
        return "Rejection"
    if reaction_class.startswith("RECLAIMED_"):
        return "Reclaim"
    if reaction_class.startswith("ACCEPTED_"):
        return "Acceptance"
    if reaction_class == "CHOPPED_AROUND_LEVEL":
        return "Chop"
    return "Unresolved"


def count_values(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    return dict(Counter(str(r.get(key) or "UNKNOWN") for r in rows))
