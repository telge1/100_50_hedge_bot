"""Ensure long scanner package does not reference order execution."""

from __future__ import annotations

from pathlib import Path

FORBIDDEN = (
    "create_order",
    "place_order",
    "amend_order",
    "cancel_order",
    "set_leverage",
    "wallet",
    "pybit",
)

ROOT = Path(__file__).resolve().parents[1]


def test_no_order_api_in_long_scanner() -> None:
    hits: list[str] = []
    for path in ROOT.rglob("*.py"):
        if "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8").lower()
        for token in FORBIDDEN:
            if token in text:
                hits.append(f"{path.name}:{token}")
    assert not hits, hits
