"""Thin dashboard adapter for orderbook_analyse.symbol_onboarding.OnboardService.

No second onboarding implementation lives here — only HTTP/auth/queue glue.
"""

from .api import build_router

__all__ = ["build_router"]
