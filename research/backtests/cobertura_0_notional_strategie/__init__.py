"""Isolated Cobertura-0-Notional recovery research backtester.

Starts from a finished quantity-neutral core hedge. No scanner, no TEM cycles,
no runtime/bot integration.
"""

from .config import CoberturaConfig
from .runner import run_cobertura

__all__ = ["CoberturaConfig", "run_cobertura"]
