"""Multi-coin frozen validation of XRP-frozen EDC strategies (research-only).

Does not alter production gate defaults or live parameters.
"""

from .cli import build_parser, main
from .constants import CODE_STATUS, PRIMARY_CELLS, PRIMARY_REFERENCE_CELL_ID, SECONDARY_STRATEGIES
from .runner import run_dry_run, run_preflight, run_backtest, run_report_only

__all__ = [
    "CODE_STATUS",
    "PRIMARY_CELLS",
    "PRIMARY_REFERENCE_CELL_ID",
    "SECONDARY_STRATEGIES",
    "build_parser",
    "main",
    "run_dry_run",
    "run_preflight",
    "run_backtest",
    "run_report_only",
]
