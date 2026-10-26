"""
Optional pre-signal density hook (disabled by default).

Set ``BT_D3_DENSITY_PROVIDER_HOOK=1`` to enable. Does not alter Phase 4D signals or engine.
"""

from __future__ import annotations

import os
from typing import Any

HOOK_ENV = "BT_D3_DENSITY_PROVIDER_HOOK"


def hook_enabled() -> bool:
    return os.environ.get(HOOK_ENV, "").strip().lower() in ("1", "true", "yes")


def attach_density_bands_if_enabled(
    symbol: str,
    decision_close_utc: str,
    **kwargs: Any,
) -> dict[str, Any] | None:
    if not hook_enabled():
        return None
    from density_provider_v1 import get_density_bands

    result = get_density_bands(symbol, decision_close_utc, **kwargs)
    return {
        "manifest": result.manifest,
        "bands": result.bands,
    }
