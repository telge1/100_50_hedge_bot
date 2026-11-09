"""Research Charts: scanner pool engine is frozen (COMMON) — guards and parity."""

from __future__ import annotations

from typing import Any

SCANNER_ENTRY_TIMEFRAME = "15m"

READ_ONLY_SCANNER_ENGINE_MSG = (
    "Scanner Pool Engine is read-only and cannot be modified from Research settings."
)

_FORBIDDEN_TOP_LEVEL_KEYS = frozenset(
    {
        "scanner_engine",
        "frozen_config",
        "common_pool_config",
        "common_scanner_config",
        "scanner_pool_engine",
        "common_config",
        "frozen_lld_config",
        "common_causal_pools",
    }
)

_FORBIDDEN_LIQUIDITY_NESTED_KEYS = frozenset(
    {
        "scanner_engine",
        "frozen_config",
        "common_pool_config",
        "common_scanner_config",
        "config_version",
        "frozen",
        "common_frozen",
    }
)


def reject_scanner_engine_settings_keys(body: dict[str, Any] | None) -> None:
    """Raise ValueError if client attempts to mutate frozen scanner engine via settings."""
    if not body or not isinstance(body, dict):
        return
    for key in body:
        if key in _FORBIDDEN_TOP_LEVEL_KEYS:
            raise ValueError(READ_ONLY_SCANNER_ENGINE_MSG)
    liquidity = body.get("liquidity")
    if isinstance(liquidity, dict):
        for key in liquidity:
            if key in _FORBIDDEN_LIQUIDITY_NESTED_KEYS:
                raise ValueError(READ_ONLY_SCANNER_ENGINE_MSG)


def scanner_pool_engine_payload() -> dict[str, Any]:
    """Read-only frozen engine snapshot (not workspace)."""
    from .frozen_lld_config_v1 import frozen_lld_config_snapshot_v1

    snap = frozen_lld_config_snapshot_v1(SCANNER_ENTRY_TIMEFRAME)
    return {
        "source": "COMMON_FROZEN",
        "locked": True,
        "config_version": int(snap["version"]),
        "timeframe": str(snap["timeframe"]),
        "enabled": bool(snap["enabled"]),
        "amount_base": int(snap["amount_base"]),
        "amount_effective": int(snap["amount_effective"]),
        "highest_len": int(snap["highest_len"]),
        "lowest_len": int(snap["lowest_len"]),
    }


def workspace_frozen_engine_parity() -> dict[str, Any]:
    """Compare workspace Research LLD engine fields to frozen COMMON engine at 15m."""
    from .frozen_lld_config_v1 import frozen_lld_config_for_timeframe_v1
    from .lld_research_kernel import ui_lld_config

    tf = SCANNER_ENTRY_TIMEFRAME
    ws_cfg = ui_lld_config(tf)
    frozen_cfg = frozen_lld_config_for_timeframe_v1(tf)
    workspace_engine = {
        "enabled": bool(ws_cfg.enabled),
        "amount_effective": int(ws_cfg.amount),
        "highest_len": int(ws_cfg.highest_len),
        "lowest_len": int(ws_cfg.lowest_len),
    }
    frozen_engine = {
        "enabled": bool(frozen_cfg.enabled),
        "amount_effective": int(frozen_cfg.amount),
        "highest_len": int(frozen_cfg.highest_len),
        "lowest_len": int(frozen_cfg.lowest_len),
    }
    field_match = {
        name: workspace_engine[name] == frozen_engine[name] for name in workspace_engine
    }
    parity = all(field_match.values())
    return {
        "workspace_frozen_parity": parity,
        "workspace_frozen_parity_fields": field_match,
        "workspace_engine_15m": workspace_engine,
        "frozen_engine_15m": frozen_engine,
    }
