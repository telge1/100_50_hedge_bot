"""Source-priority rules for candle upserts (no silent last-write-wins)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from research.regime_scanner.htf_freqtrade_equality_audit import (
    ABS_TOL,
    REL_TOL,
    values_equal_exact,
    values_within_tol,
)
from research.regime_scanner.mysql_candle_store.schema import (
    SOURCE_AGGREGATED_FROM_5M,
    SOURCE_FREQTRADE_DIRECT,
)

Action = Literal["insert", "update", "unchanged", "skip_protected", "conflict"]


@dataclass(frozen=True)
class ResolveResult:
    action: Action
    reason: str


def _ohlc_exact(existing: dict[str, Any], incoming: dict[str, Any]) -> bool:
    return all(
        values_equal_exact(float(existing[c]), float(incoming[c]))
        for c in ("open", "high", "low", "close")
    )


def _volume_ok(existing: dict[str, Any], incoming: dict[str, Any]) -> bool:
    a = float(existing["volume"])
    b = float(incoming["volume"])
    return values_equal_exact(a, b) or values_within_tol(
        a, b, abs_tol=ABS_TOL, rel_tol=REL_TOL
    )


def _payload_equal(existing: dict[str, Any], incoming: dict[str, Any]) -> bool:
    keys = (
        "close_time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "is_closed",
        "source",
        "source_timeframe",
        "source_hash",
    )
    for key in keys:
        left = existing.get(key)
        right = incoming.get(key)
        if key in ("open", "high", "low", "close", "volume"):
            if not values_equal_exact(float(left), float(right)):
                return False
        elif key == "is_closed":
            if bool(left) != bool(right):
                return False
        else:
            if left != right:
                return False
    return True


def resolve_candle_upsert(
    existing: dict[str, Any] | None,
    incoming: dict[str, Any],
) -> ResolveResult:
    """Decide insert/update/skip/conflict for one candle identity."""
    if existing is None:
        return ResolveResult("insert", "bucket_missing")

    existing_source = str(existing.get("source"))
    incoming_source = str(incoming.get("source"))

    # Same source: idempotent upsert.
    if existing_source == incoming_source:
        if _payload_equal(existing, incoming):
            return ResolveResult("unchanged", "same_source_identical")
        return ResolveResult("update", "same_source_refresh")

    # Case C: existing direct, incoming aggregated → never overwrite direct.
    if (
        existing_source == SOURCE_FREQTRADE_DIRECT
        and incoming_source == SOURCE_AGGREGATED_FROM_5M
    ):
        if _ohlc_exact(existing, incoming) and _volume_ok(existing, incoming):
            return ResolveResult(
                "skip_protected",
                "direct_preferred_equal_to_aggregated",
            )
        return ResolveResult(
            "conflict",
            "aggregated_differs_from_existing_direct",
        )

    # Case D: existing aggregated, incoming direct → promote only if OHLC matches.
    if (
        existing_source == SOURCE_AGGREGATED_FROM_5M
        and incoming_source == SOURCE_FREQTRADE_DIRECT
    ):
        if _ohlc_exact(existing, incoming) and _volume_ok(existing, incoming):
            return ResolveResult(
                "update",
                "promote_direct_over_aggregated_equal_ohlc",
            )
        return ResolveResult(
            "conflict",
            "direct_differs_from_existing_aggregated",
        )

    return ResolveResult("conflict", f"unsupported_source_transition:{existing_source}->{incoming_source}")
