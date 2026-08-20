"""Bybit subscribe topic chunking (same size default as OI collector)."""

from __future__ import annotations


def chunk_topics(topics: list[str], chunk_size: int = 10) -> list[list[str]]:
    if chunk_size < 1:
        raise ValueError("chunk_size must be >= 1")
    # Preserve caller order (universe order). Do not sort alphabetically.
    unique: list[str] = []
    seen: set[str] = set()
    for topic in topics:
        if topic in seen:
            continue
        seen.add(topic)
        unique.append(topic)
    return [unique[i : i + chunk_size] for i in range(0, len(unique), chunk_size)]
