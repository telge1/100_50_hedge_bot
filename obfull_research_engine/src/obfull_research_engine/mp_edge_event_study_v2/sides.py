"""Trade / fade / break side mapping (V2)."""

from __future__ import annotations


def fade_side_for(role: str) -> str:
    if role == "UPPER":
        return "SHORT"
    if role == "LOWER":
        return "LONG"
    raise ValueError(role)


def break_side_for(role: str) -> str:
    if role == "UPPER":
        return "LONG"
    if role == "LOWER":
        return "SHORT"
    raise ValueError(role)


def trade_side_for(role: str, label: str) -> tuple[str, str]:
    """Return (trade_side, trade_side_reason).

    Correct mapping:
      UPPER + ABSORB / FAILED_BREAK → SHORT
      UPPER + TRUE_BREAK → LONG
      LOWER + ABSORB / FAILED_BREAK → LONG
      LOWER + TRUE_BREAK → SHORT
    """
    if label in ("ABSORB", "FAILED_BREAK"):
        return fade_side_for(role), f"FADE_SIGNAL:{label}"
    if label == "TRUE_BREAK":
        return break_side_for(role), f"BREAK_CONTINUATION:{label}"
    return "", f"NO_TRADE:{label}"
