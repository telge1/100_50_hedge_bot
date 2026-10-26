"""Read-only bridge to BT-D46 — does not import or mutate D46 loop semantics."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
D46 = ROOT / "tools/phase_bt_d46_integrated_short_backtester_v1"
if str(D46) not in sys.path:
    sys.path.insert(0, str(D46))

from integrated_backtester_v1 import CONTRACT as D46_CONTRACT  # noqa: E402
from integrated_backtester_v1 import run_integrated_backtest  # noqa: E402

ADAPTER_CONTRACT = "d46_readonly_adapter_v1"


def run_d46_baseline_reference(
    *,
    config: dict[str, Any],
    prices: dict[str, dict[str, float]],
    bars: list[str],
    setups_by_close: dict[str, dict[str, Any]],
    density: Any,
    pilot_end: str,
) -> Any:
    """Delegate to frozen D46 chronology (research comparison only)."""
    return run_integrated_backtest(
        config,
        prices,
        bars,
        setups_by_close,
        density,
        pilot_end,
    )


def d46_contract_id() -> str:
    return D46_CONTRACT
