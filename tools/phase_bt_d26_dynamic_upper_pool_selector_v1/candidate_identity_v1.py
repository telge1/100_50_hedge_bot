"""Upper-pool identity reconciliation for dynamic selector (BT-D26)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pool_identity_v1 import parse_origin_id_set, pool_still_active_for_pending
from signal_types_v1 import pool_key


@dataclass
class ResolvedUpperBand:
    pool_lower: float
    pool_upper: float
    origin_ids: str
    origin_set_key: str
    pool_key: str


@dataclass
class IdentityResolution:
    ok: bool
    band: ResolvedUpperBand | None = None
    reason: str | None = None
    ambiguous_matches: list[dict[str, Any]] | None = None


def origin_set_identity_key(origin_ids: Any) -> str:
    parts = sorted(parse_origin_id_set(origin_ids))
    if not parts:
        return ""
    return "|".join(parts)


def find_upper_bands_for_origin_set(
    snapshot: dict[str, Any],
    origin_set_key: str,
    *,
    tol: float = 1e-9,
) -> list[dict[str, Any]]:
    wanted = parse_origin_id_set(origin_set_key)
    if not wanted:
        return []
    out: list[dict[str, Any]] = []
    for b in snapshot.get("upper_bands") or []:
        present = parse_origin_id_set(b.get("origin_ids"))
        if wanted <= present:
            out.append(b)
    return out


def resolve_tracked_upper_band(
    config: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    pool_lo: float,
    pool_hi: float,
    origin_ids: Any,
) -> IdentityResolution:
    """Match tracked pool to current snapshot (split/merge / bound drift)."""
    mode = (config.get("short_entry") or {}).get("pool_cancel_identity_mode") or "exact_band_v1"
    origin_key = origin_set_identity_key(origin_ids)

    if mode == "exact_band_v1" or not origin_key:
        for b in snapshot.get("upper_bands") or []:
            lo, hi = float(b["lower_price"]), float(b["upper_price"])
            if abs(lo - pool_lo) < 1e-9 and abs(hi - pool_hi) < 1e-9:
                oid = str(b.get("origin_ids") or "")
                return IdentityResolution(
                    ok=True,
                    band=ResolvedUpperBand(
                        pool_lower=lo,
                        pool_upper=hi,
                        origin_ids=oid,
                        origin_set_key=origin_set_identity_key(oid),
                        pool_key=pool_key(lo, hi),
                    ),
                )
        return IdentityResolution(ok=False, reason="EXACT_BAND_ABSENT")

    matches = find_upper_bands_for_origin_set(snapshot, origin_key)
    if not matches:
        return IdentityResolution(ok=False, reason="ORIGIN_SET_ABSENT")
    if len(matches) > 1:
        amb = [
            {
                "pool_lower": float(m["lower_price"]),
                "pool_upper": float(m["upper_price"]),
                "origin_ids": m.get("origin_ids"),
            }
            for m in matches
        ]
        return IdentityResolution(
            ok=False,
            reason="ORIGIN_SET_AMBIGUOUS_MULTIPLE_BANDS",
            ambiguous_matches=amb,
        )
    b = matches[0]
    lo, hi = float(b["lower_price"]), float(b["upper_price"])
    oid = str(b.get("origin_ids") or "")
    return IdentityResolution(
        ok=True,
        band=ResolvedUpperBand(
            pool_lower=lo,
            pool_upper=hi,
            origin_ids=oid,
            origin_set_key=origin_key,
            pool_key=pool_key(lo, hi),
        ),
    )


def tracked_still_active(
    config: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    pool_lo: float,
    pool_hi: float,
    origin_ids: Any,
) -> bool:
    return pool_still_active_for_pending(
        config,
        pool_lo=pool_lo,
        pool_hi=pool_hi,
        origin_ids=origin_ids,
        snapshot=snapshot,
    )


def candidate_from_signal_band(band: dict[str, Any]) -> ResolvedUpperBand:
    lo = float(band["lower_price"])
    hi = float(band["upper_price"])
    oid = str(band.get("origin_ids") or "")
    return ResolvedUpperBand(
        pool_lower=lo,
        pool_upper=hi,
        origin_ids=oid,
        origin_set_key=origin_set_identity_key(oid),
        pool_key=pool_key(lo, hi),
    )
