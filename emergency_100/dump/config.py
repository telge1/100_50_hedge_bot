from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class BridgeTarget:
    name: str
    target_short_ratio: float


@dataclass
class Emergency100Config:
    default_symbol: str = "BTCUSDT"

    # Fixed add size keeps the hedge growth linear.
    add_size_usdt: float = 20.0

    # Switch into the emergency hedge when spread and speed are both elevated.
    emergency_spread_trigger_pct: float = 0.025
    emergency_speed_trigger_pct: float = 0.02
    atr_speed_multiple: float = 1.2

    # Start bridging back to the normal strategy only once the spread has healed.
    bridge_resume_spread_pct: float = 0.01
    bridge_targets: Tuple[BridgeTarget, ...] = (
        BridgeTarget("100:80", 0.80),
        BridgeTarget("100:60", 0.60),
        BridgeTarget("100:50", 0.50),
    )

    # Small tolerance avoids oscillating around the same bridge level.
    ratio_tolerance: float = 0.02

    # Logging and loop behavior for the first dry-run runner.
    loop_interval_seconds: float = 2.0
    log_file: str = "logs/emergency_100.log"
    audit_log_file: str = "logs/emergency_100_audit.jsonl"
    runtime_state_file: str = "logs/emergency_100_runtime_state.json"
    runtime_history_file: str = "logs/emergency_100_runtime_history.jsonl"
