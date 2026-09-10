"""Non-negative mass-balance decomposition for aggregated L2 wall queues.

Q1 = Q0 + net_refill - residual_pull - X
Equivalently: X + residual_pull - net_refill = Q0 - Q1

Fields are intentionally named net_refill / residual_pull — never gross_refill
or exact_cancel_qty (L2 aggregation cannot uniquely separate gross add/cancel
in the same interval).
"""

from __future__ import annotations

from dataclasses import dataclass

from . import MASS_BALANCE_ABS_TOL


@dataclass(frozen=True)
class MassBalanceResult:
    queue_before: float
    queue_after: float
    attributed_hit_qty: float
    delta_queue: float
    net_passive_change: float
    net_refill_qty: float
    residual_pull_qty: float
    net_depletion_qty: float
    mass_balance_error: float
    identity_ok: bool
    aggregate_queue_survival_proxy: float


def decompose_mass_balance(
    *,
    queue_before: float,
    queue_after: float,
    attributed_hit_qty: float,
    tol: float = MASS_BALANCE_ABS_TOL,
) -> MassBalanceResult:
    q0 = float(queue_before)
    q1 = float(queue_after)
    x = float(attributed_hit_qty)
    if x < -tol:
        raise ValueError("attributed_hit_qty must be non-negative")
    x = max(x, 0.0)
    delta_q = q1 - q0
    net_passive = delta_q + x
    net_refill = max(net_passive, 0.0)
    residual_pull = max(-net_passive, 0.0)
    # Identity check: q0 + net_refill - residual_pull - x == q1
    reconstructed = q0 + net_refill - residual_pull - x
    err = abs(reconstructed - q1)
    # Also: x + residual_pull - net_refill == q0 - q1
    err2 = abs((x + residual_pull - net_refill) - (q0 - q1))
    mass_err = max(err, err2)
    net_depletion = x + residual_pull - net_refill
    survival = (q1 / q0) if q0 > tol else (1.0 if q1 <= tol else float("inf"))
    return MassBalanceResult(
        queue_before=q0,
        queue_after=q1,
        attributed_hit_qty=x,
        delta_queue=delta_q,
        net_passive_change=net_passive,
        net_refill_qty=net_refill,
        residual_pull_qty=residual_pull,
        net_depletion_qty=net_depletion,
        mass_balance_error=mass_err,
        identity_ok=mass_err <= tol,
        aggregate_queue_survival_proxy=survival,
    )
