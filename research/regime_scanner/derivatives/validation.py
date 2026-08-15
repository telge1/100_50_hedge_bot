"""Import validation gates — prevent false 'persisted' / empty writes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from research.regime_scanner.derivatives.aggregate_5m import AggregationResult, BucketRecord
from research.regime_scanner.derivatives.normalize import TECHNICAL_REASONS


@dataclass(frozen=True)
class ValidationGateResult:
    ok: bool
    status: str  # failed_validation | aborted | ok
    error_message: str | None = None
    details: dict[str, Any] | None = None


def reject_rate(rows_read: int, rows_rejected: int) -> float:
    if rows_read <= 0:
        return 0.0
    return rows_rejected / float(rows_read)


def summarize_rejects(rejects: Sequence[Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rejects:
        reason = getattr(r, "reason", None) or (r.get("reason") if isinstance(r, dict) else "unknown")
        out[str(reason)] = out.get(str(reason), 0) + 1
    return out


def technical_reject_count(rejects: Sequence[Any]) -> int:
    n = 0
    for r in rejects:
        reason = getattr(r, "reason", None) or (r.get("reason") if isinstance(r, dict) else "")
        if reason in TECHNICAL_REASONS or str(reason).startswith("technical_"):
            n += 1
        # Legacy reason from the failed persist run
        if reason == "bad_timestamp":
            n += 1
    return n


def validate_before_persist(
    *,
    mode: str,
    rows_read: int,
    agg: AggregationResult,
    buckets: list[BucketRecord],
    symbols_requested: list[str],
    reconciliation: list[dict[str, Any]],
    max_reject_rate: float = 0.05,
    baseline_buckets: int | None = None,
    baseline_tolerance_ratio: float = 0.05,
) -> ValidationGateResult:
    """Abort persist (and fail dry-run) when aggregation is pathological."""
    hard_rejects = agg.rows_rejected
    rate = reject_rate(rows_read, hard_rejects)
    details: dict[str, Any] = {
        "rows_read": rows_read,
        "buckets_generated": len(buckets),
        "rows_rejected": hard_rejects,
        "reject_rate": rate,
        "reject_reasons": summarize_rejects(agg.rejects),
        "technical_rejects": technical_reject_count(agg.rejects),
    }

    if rows_read > 0 and len(buckets) == 0:
        return ValidationGateResult(
            ok=False,
            status="failed_validation",
            error_message=(
                f"rows_read={rows_read} but buckets_generated=0 "
                f"(reject_rate={rate:.2%}); refusing to mark persisted"
            ),
            details=details,
        )

    if rows_read > 0 and rate > max_reject_rate:
        return ValidationGateResult(
            ok=False,
            status="failed_validation",
            error_message=f"reject_rate {rate:.2%} exceeds max {max_reject_rate:.2%}",
            details=details,
        )

    completed = {b.symbol for b in buckets}
    requested = set(symbols_requested)
    if requested and not (completed & requested):
        return ValidationGateResult(
            ok=False,
            status="failed_validation",
            error_message="all requested symbols produced 0 buckets",
            details=details,
        )

    if reconciliation:
        if not all(
            r.get("long_match")
            and r.get("short_match")
            and r.get("buy_match")
            and r.get("sell_match")
            for r in reconciliation
        ):
            return ValidationGateResult(
                ok=False,
                status="failed_validation",
                error_message="reconciliation not green",
                details=details,
            )

    if baseline_buckets is not None and rows_read > 0:
        lo = baseline_buckets * (1.0 - baseline_tolerance_ratio)
        hi = baseline_buckets * (1.0 + baseline_tolerance_ratio)
        n = len(buckets)
        if not (lo <= n <= hi):
            return ValidationGateResult(
                ok=False,
                status="failed_validation",
                error_message=(
                    f"buckets_generated={n} outside baseline {baseline_buckets} "
                    f"±{baseline_tolerance_ratio:.0%}"
                ),
                details={**details, "baseline_buckets": baseline_buckets},
            )

    # Persist-specific: must have something to write
    if mode == "persist" and len(buckets) == 0:
        return ValidationGateResult(
            ok=False,
            status="aborted",
            error_message="persist aborted: no buckets to write",
            details=details,
        )

    return ValidationGateResult(ok=True, status="ok", error_message=None, details=details)
