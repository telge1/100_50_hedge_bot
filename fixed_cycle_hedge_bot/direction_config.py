"""Minimal direction configuration that currently only knows the long primary bot."""

from dataclasses import dataclass


@dataclass(frozen=True)
class DirectionConfig:
    name: str
    primary_position_side: str
    hedge_position_side: str
    primary_bybit_side_for_open: str
    hedge_bybit_side_for_open: str
    long_position_idx: int
    short_position_idx: int
    cycle_first_leg: str
    cycle_second_leg: str
    refill_long_purpose: str
    refill_short_purpose: str
    long_exit_purpose: str
    short_exit_purpose: str


LONG_PRIMARY_DIRECTION = DirectionConfig(
    name="long_primary",
    primary_position_side="long",
    hedge_position_side="short",
    primary_bybit_side_for_open="Buy",
    hedge_bybit_side_for_open="Sell",
    long_position_idx=1,
    short_position_idx=2,
    cycle_first_leg="LONG_ADD",
    cycle_second_leg="SHORT_REDUCE",
    refill_long_purpose="REFILL_LONG",
    refill_short_purpose="REFILL_SHORT",
    long_exit_purpose="LONG_TP_EXIT",
    short_exit_purpose="SHORT_SL_EXIT",
)

SHORT_PRIMARY_DIRECTION = DirectionConfig(
    name="short_primary",
    primary_position_side="short",
    hedge_position_side="long",
    primary_bybit_side_for_open="Sell",
    hedge_bybit_side_for_open="Buy",
    long_position_idx=1,
    short_position_idx=2,
    cycle_first_leg="SHORT_REDUCE",
    cycle_second_leg="LONG_REDUCE",
    refill_long_purpose="REFILL_LONG",
    refill_short_purpose="REFILL_SHORT",
    long_exit_purpose="LONG_TP_EXIT",
    short_exit_purpose="SHORT_SL_EXIT",
)


def get_direction_config(name: str | None = "long_primary") -> DirectionConfig:
    normalized = str(name or "").strip().lower()
    if normalized == LONG_PRIMARY_DIRECTION.name:
        return LONG_PRIMARY_DIRECTION
    if normalized == SHORT_PRIMARY_DIRECTION.name:
        return SHORT_PRIMARY_DIRECTION
    raise ValueError(f"Unknown direction config: {name!r}")
