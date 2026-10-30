"""Lower-pool identity for long pending cancel checks."""

from __future__ import annotations

from typing import Any


def parse_origin_id_set(origin_ids: Any) -> frozenset[str]:
    if origin_ids is None:
        return frozenset()
    return frozenset(x for x in str(origin_ids).split("|") if x)


def lower_pool_still_active(
    *,
    pool_lo: float,
    pool_hi: float,
    origin_ids: str,
    snapshot: dict[str, Any],
    tol: float = 1e-9,
) -> bool:
    wanted = parse_origin_id_set(origin_ids)
    for b in snapshot.get("lower_bands") or []:
        if str(b.get("pool_status") or "").upper() != "ACTIVE":
            continue
        lo, hi = float(b["lower_price"]), float(b["upper_price"])
        if abs(lo - pool_lo) < tol and abs(hi - pool_hi) < tol:
            return True
        present = parse_origin_id_set(b.get("origin_ids"))
        if wanted and wanted <= present and abs(hi - pool_hi) < tol * 1000:
            return True
    return False
