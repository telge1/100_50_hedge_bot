from __future__ import annotations

from enum import Enum


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class ImpulseState(str, Enum):
    EARLY_PRESSURE = "EARLY_PRESSURE"
    CONFIRMING = "CONFIRMING"
    CONFIRMED = "CONFIRMED"
    FAILED_IMPULSE = "FAILED_IMPULSE"
    WHIPSAW_BLOCKED = "WHIPSAW_BLOCKED"
    INCONCLUSIVE_DATA = "INCONCLUSIVE_DATA"
    NO_EVIDENCE = "NO_EVIDENCE"
    MIXED = "MIXED"
    COOLDOWN = "COOLDOWN"
