"""Synthetic SSOT snapshots for long entry unit tests."""

from __future__ import annotations

from typing import Any


def snap(
    lowers: list[tuple[float, float, str]],
    uppers: list[tuple[float, float, str]] | None = None,
) -> dict[str, Any]:
    lb = []
    for lo, hi, oids in lowers:
        n = len([x for x in oids.split("|") if x])
        lb.append(
            {
                "side": "lower",
                "lower_price": str(lo),
                "upper_price": str(hi),
                "unique_origin_count": max(n, 2),
                "origin_ids": oids,
                "pool_status": "ACTIVE",
            }
        )
    ub = []
    for lo, hi, oids in uppers or []:
        n = len([x for x in oids.split("|") if x])
        ub.append(
            {
                "side": "upper",
                "lower_price": str(lo),
                "upper_price": str(hi),
                "unique_origin_count": max(n, 2),
                "origin_ids": oids,
                "pool_status": "ACTIVE",
            }
        )
    return {"lower_bands": lb, "upper_bands": ub}
