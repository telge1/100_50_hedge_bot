"""Fixed-cycle hedge bot package.

Keep package imports lightweight.

Dashboard modules import helper files from this package, for example
fixed_cycle_hedge_bot.confirmed_pnl_path_logic. Importing those helpers must
not load runtime/websocket dependencies as a package side effect.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "STRATEGY_REGISTRY",
    "StrategyRegistration",
    "build_registered_runtime",
    "list_strategy_names",
]


def __getattr__(name: str) -> Any:
    """Lazy-load registry exports only when callers explicitly request them."""
    if name in __all__:
        from .registry import (
            STRATEGY_REGISTRY,
            StrategyRegistration,
            build_registered_runtime,
            list_strategy_names,
        )

        exports = {
            "STRATEGY_REGISTRY": STRATEGY_REGISTRY,
            "StrategyRegistration": StrategyRegistration,
            "build_registered_runtime": build_registered_runtime,
            "list_strategy_names": list_strategy_names,
        }
        return exports[name]

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
