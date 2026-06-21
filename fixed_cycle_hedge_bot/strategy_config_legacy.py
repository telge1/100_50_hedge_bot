from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Tuple


def _load_env_file(path: Path) -> Mapping[str, str]:
    pairs: dict[str, str] = {}
    if not path.exists():
        return pairs
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        pairs[key.strip().upper()] = value.strip().strip('"').strip("'").strip()
    return pairs


_ENV_PATH = Path(__file__).resolve().parents[1] / "env"
if _ENV_PATH.is_dir():
    found_env: Path | None = None
    for candidate_name in ("local.env", ".env.local", "env", "config.env"):
        candidate = _ENV_PATH / candidate_name
        if candidate.exists() and candidate.is_file():
            found_env = candidate
            break
    if found_env:
        _ENV_PATH = found_env
_ENV_VARS = _load_env_file(_ENV_PATH)


@dataclass(frozen=True)
class RecoveryRebuyBand:
    min_spread: float
    max_spread: float
    divider: float


@dataclass
class StrategyUserConfig:
    long_entry_size: float = 70.0  # Initial Long position size in quote currency (keep ≥70 $ for 0.3 rebuy)
    short_ratio: float = 0.5  # Short quantity relative to the long (0.5 = half-sized short)
    short_entry_buffer: float = 0.015  # How far below the long price the short becomes active

    spread_threshold: float = 0.020  # Spread level to switch into recovery RElong logic
    step_size_pct: float = 0.005  # Base percentage step for rebuys at tiny spreads

    tp_short_pct: float = 0.003  # Take-profit percent for the short leg once spread collapses
    tp_long_pct: float = 0.004  # Take-profit percent for the long leg
    long_percentage_tp: float = 0.01  # Additional long TP trigger based on percentage of position

    recovery_exit_spread_threshold: float = 0.012  # Spread below which we exit recovery
    recovery_ratio_tolerance: float = 0.05  # Allowed deviation from the short_ratio before exiting recovery

    recovery_base_step_pct: float | None = 0.005  # Minimal rebuy distance when spread is tiny
    recovery_max_rebuy_distance_pct: float | None = 0.04  # Cap for rebuy distance so we don’t overshoot
    recovery_rebuy_bands: Tuple[RecoveryRebuyBand, ...] = (
        RecoveryRebuyBand(0.0, 0.03, 3.0),
        RecoveryRebuyBand(0.03, 0.06, 4.0),
        RecoveryRebuyBand(0.06, 1.0, 5.0),
    )
    rebuy_size_multiplier_base: float = 0.50  # Base rebuy size multiplier (e.g., 50% of long)
    rebuy_size_multiplier_increment: float = 0.025  # Extra multiplier amount per spread step
    rebuy_size_multiplier_span: float = 0.005  # Spread delta triggering each multiplier increment
    spread_heal_trigger_pct: float = 1.5  # percent
    healing_add_pct: float = 0.10  # percent of current size per healing add
    healing_max_adds_per_cycle: int = 3
    structural_trigger_pct: float = 0.01  # confirmed market-structure move
    action_size_pct: float = 0.10  # partial reduce/add size per structural action
    size_balance_tolerance: float = 0.10  # +/- tolerance around 1.0 ratio
    enable_aggressive_heal_phase: bool = False
    aggressive_down_heal_step_pct: float = 0.01
    aggressive_down_heal_size_pct: float = 0.20
    enable_phase2_short_profit_long_reduce: bool = False
    enable_phase3_long_rebuild: bool = False
    long_rebuild_target_pct: float = 1.0
    enable_phase4_short_rebuild: bool = False
    short_rebuild_target_pct: float = 1.0
    enable_fine_heal_phase: bool = True
    fine_heal_size_pct: float = 0.10
    paired_partial_sl_long_buffer_pct: float = 0.002
    preplaced_heal_enabled: bool = False
    preplaced_heal_offset_pct: float = 0.0025


@dataclass
class StrategyConfig:
    # User-facing strategy controls (editable daily)
    user: StrategyUserConfig = field(default_factory=StrategyUserConfig)

    @property
    def long_entry_size(self) -> float:
        return self.user.long_entry_size

    @long_entry_size.setter
    def long_entry_size(self, value: float) -> None:
        self.user.long_entry_size = value

    @property
    def short_ratio(self) -> float:
        return self.user.short_ratio

    @short_ratio.setter
    def short_ratio(self, value: float) -> None:
        self.user.short_ratio = value

    @property
    def short_entry_buffer(self) -> float:
        return self.user.short_entry_buffer

    @short_entry_buffer.setter
    def short_entry_buffer(self, value: float) -> None:
        self.user.short_entry_buffer = value

    @property
    def spread_threshold(self) -> float:
        return self.user.spread_threshold

    @spread_threshold.setter
    def spread_threshold(self, value: float) -> None:
        self.user.spread_threshold = value

    @property
    def step_size_pct(self) -> float:
        return self.user.step_size_pct

    @step_size_pct.setter
    def step_size_pct(self, value: float) -> None:
        self.user.step_size_pct = value

    @property
    def tp_short_pct(self) -> float:
        return self.user.tp_short_pct

    @tp_short_pct.setter
    def tp_short_pct(self, value: float) -> None:
        self.user.tp_short_pct = value

    @property
    def tp_long_pct(self) -> float:
        return self.user.tp_long_pct

    @tp_long_pct.setter
    def tp_long_pct(self, value: float) -> None:
        self.user.tp_long_pct = value

    @property
    def long_percentage_tp(self) -> float:
        return self.user.long_percentage_tp

    @long_percentage_tp.setter
    def long_percentage_tp(self, value: float) -> None:
        self.user.long_percentage_tp = value

    @property
    def recovery_exit_spread_threshold(self) -> float:
        return self.user.recovery_exit_spread_threshold

    @recovery_exit_spread_threshold.setter
    def recovery_exit_spread_threshold(self, value: float) -> None:
        self.user.recovery_exit_spread_threshold = value

    @property
    def recovery_ratio_tolerance(self) -> float:
        return self.user.recovery_ratio_tolerance

    @recovery_ratio_tolerance.setter
    def recovery_ratio_tolerance(self, value: float) -> None:
        self.user.recovery_ratio_tolerance = value

    @property
    def recovery_base_step_pct(self) -> float | None:
        return self.user.recovery_base_step_pct

    @recovery_base_step_pct.setter
    def recovery_base_step_pct(self, value: float | None) -> None:
        self.user.recovery_base_step_pct = value

    @property
    def recovery_max_rebuy_distance_pct(self) -> float | None:
        return self.user.recovery_max_rebuy_distance_pct

    @recovery_max_rebuy_distance_pct.setter
    def recovery_max_rebuy_distance_pct(self, value: float | None) -> None:
        self.user.recovery_max_rebuy_distance_pct = value

    @property
    def recovery_rebuy_bands(self) -> Tuple[RecoveryRebuyBand, ...]:
        return self.user.recovery_rebuy_bands

    @recovery_rebuy_bands.setter
    def recovery_rebuy_bands(self, value: Tuple[RecoveryRebuyBand, ...]) -> None:
        self.user.recovery_rebuy_bands = value

    @property
    def rebuy_size_multiplier_base(self) -> float:
        return self.user.rebuy_size_multiplier_base

    @rebuy_size_multiplier_base.setter
    def rebuy_size_multiplier_base(self, value: float) -> None:
        self.user.rebuy_size_multiplier_base = value

    @property
    def rebuy_size_multiplier_increment(self) -> float:
        return self.user.rebuy_size_multiplier_increment

    @rebuy_size_multiplier_increment.setter
    def rebuy_size_multiplier_increment(self, value: float) -> None:
        self.user.rebuy_size_multiplier_increment = value

    @property
    def rebuy_size_multiplier_span(self) -> float:
        return self.user.rebuy_size_multiplier_span

    @rebuy_size_multiplier_span.setter
    def rebuy_size_multiplier_span(self, value: float) -> None:
        self.user.rebuy_size_multiplier_span = value

    @property
    def spread_heal_trigger_pct(self) -> float:
        return self.user.spread_heal_trigger_pct

    @spread_heal_trigger_pct.setter
    def spread_heal_trigger_pct(self, value: float) -> None:
        self.user.spread_heal_trigger_pct = value

    @property
    def healing_add_pct(self) -> float:
        return self.user.healing_add_pct

    @healing_add_pct.setter
    def healing_add_pct(self, value: float) -> None:
        self.user.healing_add_pct = value

    @property
    def healing_max_adds_per_cycle(self) -> int:
        return self.user.healing_max_adds_per_cycle

    @healing_max_adds_per_cycle.setter
    def healing_max_adds_per_cycle(self, value: int) -> None:
        self.user.healing_max_adds_per_cycle = value

    @property
    def structural_trigger_pct(self) -> float:
        return self.user.structural_trigger_pct

    @structural_trigger_pct.setter
    def structural_trigger_pct(self, value: float) -> None:
        self.user.structural_trigger_pct = value

    @property
    def action_size_pct(self) -> float:
        return self.user.action_size_pct

    @action_size_pct.setter
    def action_size_pct(self, value: float) -> None:
        self.user.action_size_pct = value

    @property
    def size_balance_tolerance(self) -> float:
        return self.user.size_balance_tolerance

    @size_balance_tolerance.setter
    def size_balance_tolerance(self, value: float) -> None:
        self.user.size_balance_tolerance = value

    @property
    def enable_aggressive_heal_phase(self) -> bool:
        return self.user.enable_aggressive_heal_phase

    @enable_aggressive_heal_phase.setter
    def enable_aggressive_heal_phase(self, value: bool) -> None:
        self.user.enable_aggressive_heal_phase = value

    @property
    def aggressive_down_heal_step_pct(self) -> float:
        return self.user.aggressive_down_heal_step_pct

    @aggressive_down_heal_step_pct.setter
    def aggressive_down_heal_step_pct(self, value: float) -> None:
        self.user.aggressive_down_heal_step_pct = value

    @property
    def aggressive_down_heal_size_pct(self) -> float:
        return self.user.aggressive_down_heal_size_pct

    @aggressive_down_heal_size_pct.setter
    def aggressive_down_heal_size_pct(self, value: float) -> None:
        self.user.aggressive_down_heal_size_pct = value

    @property
    def enable_phase2_short_profit_long_reduce(self) -> bool:
        return self.user.enable_phase2_short_profit_long_reduce

    @enable_phase2_short_profit_long_reduce.setter
    def enable_phase2_short_profit_long_reduce(self, value: bool) -> None:
        self.user.enable_phase2_short_profit_long_reduce = value

    @property
    def enable_phase3_long_rebuild(self) -> bool:
        return self.user.enable_phase3_long_rebuild

    @enable_phase3_long_rebuild.setter
    def enable_phase3_long_rebuild(self, value: bool) -> None:
        self.user.enable_phase3_long_rebuild = value

    @property
    def long_rebuild_target_pct(self) -> float:
        return self.user.long_rebuild_target_pct

    @long_rebuild_target_pct.setter
    def long_rebuild_target_pct(self, value: float) -> None:
        self.user.long_rebuild_target_pct = value

    @property
    def enable_phase4_short_rebuild(self) -> bool:
        return self.user.enable_phase4_short_rebuild

    @enable_phase4_short_rebuild.setter
    def enable_phase4_short_rebuild(self, value: bool) -> None:
        self.user.enable_phase4_short_rebuild = value

    @property
    def short_rebuild_target_pct(self) -> float:
        return self.user.short_rebuild_target_pct

    @short_rebuild_target_pct.setter
    def short_rebuild_target_pct(self, value: float) -> None:
        self.user.short_rebuild_target_pct = value

    @property
    def paired_partial_sl_long_buffer_pct(self) -> float:
        return self.user.paired_partial_sl_long_buffer_pct

    @paired_partial_sl_long_buffer_pct.setter
    def paired_partial_sl_long_buffer_pct(self, value: float) -> None:
        self.user.paired_partial_sl_long_buffer_pct = value

    @property
    def enable_fine_heal_phase(self) -> bool:
        return self.user.enable_fine_heal_phase

    @enable_fine_heal_phase.setter
    def enable_fine_heal_phase(self, value: bool) -> None:
        self.user.enable_fine_heal_phase = value

    @property
    def fine_heal_size_pct(self) -> float:
        return self.user.fine_heal_size_pct

    @fine_heal_size_pct.setter
    def fine_heal_size_pct(self, value: float) -> None:
        self.user.fine_heal_size_pct = value

    @property
    def preplaced_heal_enabled(self) -> bool:
        return self.user.preplaced_heal_enabled

    @preplaced_heal_enabled.setter
    def preplaced_heal_enabled(self, value: bool) -> None:
        self.user.preplaced_heal_enabled = value

    @property
    def preplaced_heal_offset_pct(self) -> float:
        return self.user.preplaced_heal_offset_pct

    @preplaced_heal_offset_pct.setter
    def preplaced_heal_offset_pct(self, value: float) -> None:
        self.user.preplaced_heal_offset_pct = value

    # System/safety controls (leave unless you know what you're doing)
    initial_balance: float = 100_000.0
    leverage: float = 8.0
    base_order_size: float = 1.0
    max_rebuy_loops: int = 20
    min_rebuy_interval: float = 0.05
    max_slippage_pct: float = 0.02
    max_short_deviation: float = 0.03

    max_total_exposure_pct: float = 0.5
    recovery_low: float = 0.7
    pool_fail_buffer: float = 0.005
    extend_enabled: bool = True
    extend_trigger_pct: float = 0.01

    tp_short_pct: float = 0.003
    tp_long_pct: float = 0.004
    long_percentage_tp: float = 0.01

    log_file: str = "../emergency_100/logs/psrh.log"
    max_total_notional: float = 10_000.0

    order_sync_interval_seconds: float = 8.0
    order_timeout_seconds: float = 30.0
    fast_poll_interval_seconds: float = 2.0
    min_order_value: float = 7.0
    status_log_interval_seconds: float = 30.0
    max_drawdown: float = 5_000.0
    fast_fill_rebuy_cooldown_seconds: float = 1.5

    api_key: str = _ENV_VARS.get("API_KEY", "")
    secret_key: str = _ENV_VARS.get("SECRET_KEY", "")
    default_symbol: str = "BTCUSDT"
    category: str = "linear"

    def max_notional_allowed(self) -> float:
        return self.initial_balance * self.max_total_exposure_pct

    def safe_leverage(self) -> float:
        return max(5.0, min(self.leverage, 10.0))
