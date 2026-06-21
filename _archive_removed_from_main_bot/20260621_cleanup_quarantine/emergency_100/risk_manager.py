from __future__ import annotations

from dataclasses import dataclass

from strategy.config import StrategyConfig


@dataclass
class ExposureReport:
    total_notional: float
    used_margin: float
    free_margin: float
    exposure_pct: float


class RiskManager:
    def __init__(self, config: StrategyConfig) -> None:
        self.config = config
        self.used_margin: float = 0.0
        self.realized_pnl: float = 0.0

    def update_margins(self, total_notional: float) -> ExposureReport:
        self.used_margin = total_notional / self.config.safe_leverage()
        free_margin = self.config.initial_balance + self.realized_pnl - self.used_margin
        exposure_pct = min(1.0, total_notional / self.config.max_notional_allowed())
        return ExposureReport(
            total_notional=total_notional,
            used_margin=self.used_margin,
            free_margin=free_margin,
            exposure_pct=exposure_pct,
        )

    def check_exposure_limit(self, total_notional: float) -> bool:
        return total_notional <= self.config.max_notional_allowed()

    def record_realized_pnl(self, pnl: float) -> None:
        self.realized_pnl += pnl

    def max_rebuys_allowed(self, current_rebuys: int) -> bool:
        return current_rebuys < self.config.max_rebuy_loops
