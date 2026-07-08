from __future__ import annotations

from pathlib import Path

from research.backtests.cycle2_short_reduce_shadow_audit import main as run_audit


def test_cycle2_shadow_identity_strict_start4000(tmp_path: Path) -> None:
    """Smoke/identity test: strict Start-4000-Lauf erfüllt Freeze-Identität."""
    # Der Audit wirft intern einen AssertionError, falls Baseline- und
    # Freeze-Shadow-PnL bei unveränderter Position nach C2 divergieren.
    exit_code = run_audit()
    assert exit_code == 0

