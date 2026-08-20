"""Offline / read-only EXECUTION_WALL detector (near-market microstructure).

STRUCTURE_WALL detection is unchanged; this package is a separate research path.
"""

from __future__ import annotations

from orderbook_analyse.execution_wall_detector.types import (
    DETECTOR_VERSION,
    ExecutionWallParams,
)

__all__ = ["DETECTOR_VERSION", "ExecutionWallParams"]
