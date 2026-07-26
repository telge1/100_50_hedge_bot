"""Config dataclass and JSON loading for Cobertura-0-Notional recovery."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Literal


DirectionMode = Literal["short_only", "long_only", "symmetric"]
StartPriceSource = Literal["config_start_price", "candle_open", "candle_close"]
FullExitTargetMode = Literal["legacy", "net_be"]
OverlayExitPolicy = Literal[
    "shared_be",
    "individual_tp",
    "individual_tp_scaled",
    "dynamic_long_equalization",
]
PostAddDistancePolicy = Literal["disabled", "skip", "scale_down"]


@dataclass
class IndividualTpStep:
    move_pct: float
    close_fraction: float


@dataclass
class CoberturaConfig:
    """All strategy and run parameters (grid-friendly)."""

    # Start position / market window
    symbol: str = "APTUSDT"
    timeframe: str = "5m"
    start_timestamp: str = "2026-01-19T03:55:00+00:00"
    end_timestamp: str | None = None
    candle_limit: int | None = 50_000
    start_price: float = 1.6456
    start_price_source: StartPriceSource = "config_start_price"

    core_long_qty: float = 395.153
    core_long_avg: float = 1.768355389945979
    core_short_qty: float = 395.153
    core_short_avg: float = 1.696714

    # Strategy
    direction_mode: DirectionMode = "short_only"
    activation_move_pct: float = 0.05
    first_add_move_pct: float = 0.06
    add_step_pct: float = 0.01
    add_size_pct: float = 0.40
    max_add_count: int = 8
    max_adds_per_candle: int = 4
    reset_reference_after_overlay_be: bool = True

    # Exposure
    max_overlay_qty_multiple: float | None = 4.0
    max_total_gross_notional: float | None = None
    max_net_notional: float | None = None
    minimum_total_short_avg_distance_pct: float | None = None
    minimum_overlay_avg_distance_pct: float | None = None
    # Research / safety distance guards (None / disabled = fingerprint-stable)
    minimum_start_distance_pct: float | None = None
    minimum_post_add_distance_pct: float | None = None
    post_add_distance_policy: PostAddDistancePolicy = "disabled"

    # Research fill / exit variants (defaults preserve fingerprint-stable baseline)
    # When True (legacy mode): full-exit entitlement from prior bars may fire
    # before adds; full exit that only becomes valid after same-bar adds is
    # deferred to the next candle (V1 / V3).
    defer_full_exit_after_same_bar_adds: bool = False
    # When True: gap-through adverse fill vs candle open before slippage (V2 / V3).
    gap_through_trigger_fills: bool = False

    # Fees / slippage
    fee_rate_open: float = 0.00055
    fee_rate_close: float = 0.00055
    slippage_bps_open: float = 0.0
    slippage_bps_close: float = 0.0
    fee_buffer_usdt: float = 0.0

    # Overlay exit policy
    overlay_exit_policy: OverlayExitPolicy = "shared_be"
    individual_tp_pct: float = 0.01
    individual_tp_close_fraction: float = 1.0
    individual_tp_fee_buffer_usdt: float = 0.0
    individual_tp_steps: list[IndividualTpStep] = field(default_factory=list)

    # Dynamic long equalization
    max_locked_spread_pct: float = 0.04
    long_equalization_fee_buffer_usdt: float = 0.0
    long_equalization_require_recovery: bool = True
    max_total_gross_notional_usdt: float | None = None
    max_long_qty_to_initial_core_ratio: float | None = None

    # Overlay BE (shared_be policy)
    overlay_be_enabled: bool = True
    overlay_be_min_fill_count: int = 1
    overlay_be_min_open_profit_usdt: float = 0.0
    overlay_be_target_usdt: float = 0.0
    overlay_be_close_all: bool = True

    # Full exit
    # legacy: target_total_pnl_usdt + target_profit_buffer_usdt (fingerprint-stable)
    # net_be: full_exit_target_usdt + full_exit_safety_buffer_usdt
    full_exit_target_mode: FullExitTargetMode = "legacy"
    full_exit_target_usdt: float = 0.0
    full_exit_safety_buffer_usdt: float = 0.0
    target_total_pnl_usdt: float = 0.0
    target_profit_buffer_usdt: float = 0.0
    pnl_tolerance_usdt: float = 0.01

    # Safety / instrument
    max_recovery_rounds: int | None = None
    max_recovery_duration_bars: int | None = None
    max_recovery_drawdown_usdt: float | None = None
    max_recovery_drawdown_pct: float | None = None
    min_notional: float = 5.0
    qty_step: float = 0.001
    tick_size: float = 0.0001

    # I/O
    output_dir: str | None = None
    run_id: str | None = None
    tags: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if self.core_long_qty <= 0.0 or self.core_short_qty <= 0.0:
            raise ValueError("core qty must be positive")
        if abs(self.core_long_qty - self.core_short_qty) > 1e-9:
            raise ValueError(
                f"start position must be qty-neutral: "
                f"long={self.core_long_qty} short={self.core_short_qty}"
            )
        if self.core_long_avg <= 0.0 or self.core_short_avg <= 0.0:
            raise ValueError("core averages must be positive")
        if self.start_price <= 0.0:
            raise ValueError("start_price must be positive")
        if self.activation_move_pct <= 0.0:
            raise ValueError("activation_move_pct must be > 0")
        if self.first_add_move_pct < self.activation_move_pct:
            raise ValueError("first_add_move_pct must be >= activation_move_pct")
        if self.add_step_pct <= 0.0:
            raise ValueError("add_step_pct must be > 0")
        if self.add_size_pct <= 0.0:
            raise ValueError("add_size_pct must be > 0")
        if self.max_add_count < 1:
            raise ValueError("max_add_count must be >= 1")
        if self.max_adds_per_candle < 1:
            raise ValueError("max_adds_per_candle must be >= 1")
        if self.direction_mode not in ("short_only", "long_only", "symmetric"):
            raise ValueError(f"unsupported direction_mode: {self.direction_mode}")
        if self.fee_rate_open < 0.0 or self.fee_rate_close < 0.0:
            raise ValueError("fee rates must be >= 0")
        if self.qty_step <= 0.0 or self.tick_size <= 0.0:
            raise ValueError("qty_step and tick_size must be > 0")
        if self.overlay_exit_policy not in (
            "shared_be",
            "individual_tp",
            "individual_tp_scaled",
            "dynamic_long_equalization",
        ):
            raise ValueError(f"unsupported overlay_exit_policy: {self.overlay_exit_policy}")
        if self.overlay_exit_policy == "individual_tp":
            if self.individual_tp_pct <= 0.0:
                raise ValueError("individual_tp_pct must be > 0")
            if not (0.0 < self.individual_tp_close_fraction <= 1.0):
                raise ValueError("individual_tp_close_fraction must be in (0, 1]")
        if self.overlay_exit_policy == "individual_tp_scaled":
            if not self.individual_tp_steps:
                raise ValueError("individual_tp_scaled requires individual_tp_steps")
            frac_sum = 0.0
            prev = 0.0
            for step in self.individual_tp_steps:
                if step.move_pct <= prev:
                    raise ValueError("individual_tp_steps.move_pct must increase")
                if step.close_fraction <= 0.0:
                    raise ValueError("individual_tp_steps.close_fraction must be > 0")
                frac_sum += float(step.close_fraction)
                prev = float(step.move_pct)
            if frac_sum > 1.0 + 1e-9:
                raise ValueError("individual_tp_steps close_fraction sum must be <= 1")
        if self.overlay_exit_policy == "dynamic_long_equalization":
            if self.max_locked_spread_pct < 0.0:
                raise ValueError("max_locked_spread_pct must be >= 0")
        if self.full_exit_target_mode not in ("legacy", "net_be"):
            raise ValueError(
                f"unsupported full_exit_target_mode: {self.full_exit_target_mode}"
            )
        if self.full_exit_safety_buffer_usdt < 0.0:
            raise ValueError("full_exit_safety_buffer_usdt must be >= 0")
        if self.post_add_distance_policy not in ("disabled", "skip", "scale_down"):
            raise ValueError(
                f"unsupported post_add_distance_policy: {self.post_add_distance_policy}"
            )
        if self.minimum_post_add_distance_pct is not None:
            if not (0.0 <= float(self.minimum_post_add_distance_pct) < 1.0):
                raise ValueError("minimum_post_add_distance_pct must be in [0, 1)")
        if self.minimum_start_distance_pct is not None:
            if not (0.0 <= float(self.minimum_start_distance_pct) < 1.0):
                raise ValueError("minimum_start_distance_pct must be in [0, 1)")
        # Alias USDT gross cap onto existing exposure field when set.
        if self.max_total_gross_notional_usdt is not None:
            if self.max_total_gross_notional is None:
                self.max_total_gross_notional = float(self.max_total_gross_notional_usdt)
            elif abs(
                float(self.max_total_gross_notional)
                - float(self.max_total_gross_notional_usdt)
            ) > 1e-9:
                raise ValueError(
                    "max_total_gross_notional and max_total_gross_notional_usdt disagree"
                )

    def core_qty(self) -> float:
        return float(self.core_long_qty)

    def overlay_add_qty_raw(self) -> float:
        return float(self.core_long_qty) * float(self.add_size_pct)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "CoberturaConfig":
        allowed = {f.name for f in fields(cls)}
        payload = {k: v for k, v in raw.items() if k in allowed}
        steps_raw = payload.get("individual_tp_steps")
        if steps_raw is not None:
            parsed: list[IndividualTpStep] = []
            for item in steps_raw:
                if isinstance(item, IndividualTpStep):
                    parsed.append(item)
                else:
                    parsed.append(
                        IndividualTpStep(
                            move_pct=float(item["move_pct"]),
                            close_fraction=float(item["close_fraction"]),
                        )
                    )
            payload["individual_tp_steps"] = parsed
        cfg = cls(**payload)
        cfg.validate()
        return cfg

    @classmethod
    def from_json(cls, path: str | Path) -> "CoberturaConfig":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("config JSON must be an object")
        return cls.from_dict(data)


def default_apt_example() -> CoberturaConfig:
    cfg = CoberturaConfig()
    cfg.validate()
    return cfg
