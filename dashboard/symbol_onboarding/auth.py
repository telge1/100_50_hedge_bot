"""Role checks for symbol onboarding (server-side only)."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException


def is_admin(user: dict[str, Any] | None) -> bool:
    if not user:
        return False
    return str(user.get("role") or "").strip().lower() == "admin"


def require_admin(user: dict[str, Any]) -> dict[str, Any]:
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="ADMIN_REQUIRED")
    return user


def auth_roles_for_onboard(user: dict[str, Any]) -> frozenset[str]:
    """Map dashboard roles onto OnboardService AuthContext roles."""
    roles = {"symbol_onboard"}
    if is_admin(user):
        roles.add("admin")
    return frozenset(roles)
