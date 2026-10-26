"""Versioned upper-pool identity for pending-order cancel checks (BT-D21)."""

from __future__ import annotations

from typing import Any

POOL_CANCEL_EXACT_BAND_V1 = "exact_band_v1"
POOL_CANCEL_ORIGIN_SET_V1 = "origin_set_v1"

POOL_CANCEL_MODES = frozenset({POOL_CANCEL_EXACT_BAND_V1, POOL_CANCEL_ORIGIN_SET_V1})


def parse_origin_id_set(origin_ids: Any) -> frozenset[str]:
    if origin_ids is None:
        return frozenset()
    return frozenset(x for x in str(origin_ids).split("|") if x)


def pool_cancel_identity_mode(config: dict[str, Any]) -> str:
    se = config.get("short_entry") or {}
    mode = str(se.get("pool_cancel_identity_mode") or POOL_CANCEL_EXACT_BAND_V1).strip()
    if mode not in POOL_CANCEL_MODES:
        raise ValueError(f"unsupported pool_cancel_identity_mode: {mode}")
    return mode


def pool_still_active_for_pending(
    config: dict[str, Any],
    *,
    pool_lo: float,
    pool_hi: float,
    origin_ids: Any,
    snapshot: dict[str, Any],
    tol: float = 1e-9,
) -> bool:
    mode = pool_cancel_identity_mode(config)
    if mode == POOL_CANCEL_EXACT_BAND_V1:
        for b in snapshot.get("upper_bands") or []:
            lo, hi = float(b["lower_price"]), float(b["upper_price"])
            if abs(lo - pool_lo) < tol and abs(hi - pool_hi) < tol:
                return True
        return False

    wanted = parse_origin_id_set(origin_ids)
    if not wanted:
        for b in snapshot.get("upper_bands") or []:
            lo, hi = float(b["lower_price"]), float(b["upper_price"])
            if abs(lo - pool_lo) < tol and abs(hi - pool_hi) < tol:
                return True
        return False

    for b in snapshot.get("upper_bands") or []:
        present = parse_origin_id_set(b.get("origin_ids"))
        if wanted <= present:
            return True
    return False
