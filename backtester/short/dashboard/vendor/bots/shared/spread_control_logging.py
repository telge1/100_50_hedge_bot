"""
Shared logging helpers for spread-control decisions.
"""

from __future__ import annotations

from typing import Any


def _fmt_value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.6f}"
    return str(value)


def log_event(logger, tag: str, **fields: Any) -> None:
    payload = ", ".join(f"{key}={_fmt_value(value)}" for key, value in fields.items())
    if payload:
        logger.info(f"[{tag}] {payload}")
    else:
        logger.info(f"[{tag}]")


def log_projection(logger, stage: str, projection: dict[str, Any]) -> None:
    if not projection:
        logger.info(f"[SPREAD-PROJECTION] {stage}: -")
        return
    log_event(logger, "SPREAD-PROJECTION", stage=stage, **projection)
