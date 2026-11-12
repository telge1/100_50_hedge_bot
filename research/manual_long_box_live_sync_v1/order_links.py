"""Stable orderLinkId allocation for manual long box order cycles."""

from __future__ import annotations

import secrets


def default_order_link_id(drawing_id: str) -> str:
    """Legacy single-link id (first cycle / dry-run tests)."""
    return f"mlb-sync-{drawing_id}"


def allocate_order_link_id(drawing_id: str) -> str:
    """New id for an explicitly confirmed order cycle (same drawing_id)."""
    return f"mlb-sync-{drawing_id}-{secrets.token_hex(8)}"


def resolve_order_link_id(
    drawing_id: str,
    *,
    activation_link_id: str | None,
    prev_link_id: str | None,
    prev_terminal: bool,
) -> str:
    if activation_link_id:
        return activation_link_id
    if prev_link_id and not prev_terminal:
        return prev_link_id
    return default_order_link_id(drawing_id)
