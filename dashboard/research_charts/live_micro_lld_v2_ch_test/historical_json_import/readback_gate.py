"""Fail-closed pilot readback gate (post-import)."""

from __future__ import annotations

from .readback_errors import ReadbackGateFailure
from .readback_parity import run_pilot_readback


def run_pilot_readback_gate(
    *args,
    fail_closed: bool = True,
    **kwargs,
) -> list[ParityReport]:
    """
    Wraps ``run_pilot_readback``. When ``fail_closed`` and any report has ``ok=False``,
    raises ``ReadbackGateFailure`` (no warn-only path).
    """
    reports = run_pilot_readback(*args, **kwargs)
    if fail_closed:
        bad = [r for r in reports if not r.ok]
        if bad:
            raise ReadbackGateFailure(bad)
    return reports
