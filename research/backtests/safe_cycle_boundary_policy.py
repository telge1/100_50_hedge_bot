"""Research-only S0–S3 Safe Cycle Boundary Freeze variant catalog.

All variants run under B1 terminal-stop (recovered flat ends the coin series).
No live config / runtime changes.
"""

from __future__ import annotations

from dataclasses import dataclass

from .inventory_mtm_freeze import InventoryMtmFreezeConfig

# C1a parity targets from blocker_recovery_trigger_and_hybrid_audit_20260720.
C1A_REFERENCE_RECOVERED = 18
C1A_REFERENCE_SERIES_MTM = -75.87255981795414
C1A_REFERENCE_TRADES = 279
C1A_REFERENCE_CLOSED = 270
C1A_REFERENCE_BLOCKERS = 9
C1A_MTM_TOLERANCE = 1.0
C1A_THRESHOLD_USDT = -0.50


@dataclass(frozen=True)
class SafeBoundaryVariantSpec:
    name: str
    description: str
    freeze_config: InventoryMtmFreezeConfig


def build_s0_s3_specs() -> list[SafeBoundaryVariantSpec]:
    """Directed S0–S3 suite (not an optimizer)."""
    return [
        SafeBoundaryVariantSpec(
            name="S0",
            description="C1a parity: A1 at mtm<-0.50, immediate cycle block, B1 terminal",
            freeze_config=InventoryMtmFreezeConfig(
                variant="A1",
                threshold_usdt=C1A_THRESHOLD_USDT,
                safe_cycle_boundary=False,
                safe_boundary_variant="S0",
            ),
        ),
        SafeBoundaryVariantSpec(
            name="S1",
            description=(
                "Safe boundary: mtm<-0.50 → PENDING; activate after cycle-complete+exit; "
                "block next first-leg opener only"
            ),
            freeze_config=InventoryMtmFreezeConfig(
                variant="A1",
                threshold_usdt=C1A_THRESHOLD_USDT,
                safe_cycle_boundary=True,
                safe_boundary_arm_mode="mtm",
                safe_boundary_variant="S1",
            ),
        ),
        SafeBoundaryVariantSpec(
            name="S2",
            description="Stop after complete Cycle 1 + exit commit; block Cycle 2 opener",
            freeze_config=InventoryMtmFreezeConfig(
                variant="A1",
                threshold_usdt=C1A_THRESHOLD_USDT,
                use_mtm_trigger=False,
                safe_cycle_boundary=True,
                safe_boundary_arm_mode="stop_after_cycle",
                stop_after_cycle=1,
                safe_boundary_variant="S2",
            ),
        ),
        SafeBoundaryVariantSpec(
            name="S3",
            description="Stop after complete Cycle 2 + exit commit; block Cycle 3 opener",
            freeze_config=InventoryMtmFreezeConfig(
                variant="A1",
                threshold_usdt=C1A_THRESHOLD_USDT,
                use_mtm_trigger=False,
                safe_cycle_boundary=True,
                safe_boundary_arm_mode="stop_after_cycle",
                stop_after_cycle=2,
                safe_boundary_variant="S3",
            ),
        ),
    ]
