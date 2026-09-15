"""Baseline ↔ Silver hash parity helpers (100ms bucket semantics)."""

from __future__ import annotations

from typing import Any

BUCKET_NS = 100_000_000
START_NOT_ALIGNED_TO_100MS = "START_NOT_ALIGNED_TO_100MS"
UNRESOLVED_BASELINE_MISSING_SILVER_HASH = "UNRESOLVED_BASELINE_MISSING_SILVER_HASH"
UNRESOLVED_BASELINE_HASH_MISMATCH = "UNRESOLVED_BASELINE_HASH_MISMATCH"


def assert_start_aligned_to_100ms(start_ns: int) -> None:
    if int(start_ns) % BUCKET_NS != 0:
        raise RuntimeError(START_NOT_ALIGNED_TO_100MS)


def predecessor_bucket_start_ns(start_ns: int) -> int:
    """Silver bucket whose end equals analysis start_ns."""
    assert_start_aligned_to_100ms(start_ns)
    return int(start_ns) - BUCKET_NS


def merge_chunk_keys(*groups: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for g in groups:
        for k in g:
            seen[str(k)] = None
    return tuple(seen.keys())


def resolve_lc_price_band(
    *,
    reference_price: float | None,
    local_band_usd: float,
    mid_series: list[Any],
) -> tuple[float, float]:
    """Strategy/manual-with-ref: band around reference.

    Manual without reference: observed mid min/max ± local_band_usd.
    """
    band = float(local_band_usd)
    if reference_price is not None:
        ref = float(reference_price)
        return ref - band, ref + band
    if not mid_series:
        raise RuntimeError("STOP_LC_PRICE_BAND_NO_MIDS")
    mids = [float(m.mid) for m in mid_series]
    return min(mids) - band, max(mids) + band
