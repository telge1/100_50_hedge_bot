"""Direction-neutral recovery-purpose mirroring for paired long/short backtests."""

from __future__ import annotations

import re

from fixed_cycle_hedge_bot import direction_config, purpose_mapping

_CYCLE_PURPOSE_RE = re.compile(r"^CYCLE_(\d+)_(LONG_ADD|SHORT_REDUCE|LONG_REDUCE|SHORT_ADD)$")


def _parse_cycle_purpose(purpose: str) -> tuple[int, str] | None:
    match = _CYCLE_PURPOSE_RE.match(str(purpose or "").strip().upper())
    if not match:
        return None
    return int(match.group(1)), match.group(2)


def _long_bot_leg_role(leg_suffix: str) -> str:
    long_cfg = direction_config.LONG_PRIMARY_DIRECTION
    if leg_suffix == long_cfg.cycle_first_leg:
        return "first_leg"
    if leg_suffix == long_cfg.cycle_second_leg:
        return "second_leg"
    raise ValueError(f"unsupported long-bot cycle leg suffix: {leg_suffix}")


def mirror_recovery_start_purpose(
    long_bot_purpose: str,
    *,
    target_bot: str = "short_primary",
) -> str:
    """
    Map a recovery reference purpose from the long-primary bot to the
    direction-neutral equivalent on the short-primary bot.

    Example:
        CYCLE_4_LONG_ADD (long bot cycle-4 first leg)
        -> CYCLE_4_SHORT_REDUCE (short bot cycle-4 first leg)
    """
    if str(target_bot).strip().lower() not in {"short", "short_primary"}:
        raise ValueError(f"unsupported target_bot for mirroring: {target_bot!r}")

    parsed = _parse_cycle_purpose(long_bot_purpose)
    if parsed is None:
        raise ValueError(f"cannot mirror non-cycle purpose: {long_bot_purpose!r}")

    cycle_index, leg_suffix = parsed
    leg_role = _long_bot_leg_role(leg_suffix)
    short_cfg = direction_config.SHORT_PRIMARY_DIRECTION
    if leg_role == "first_leg":
        mirrored_suffix = short_cfg.cycle_first_leg
    else:
        mirrored_suffix = short_cfg.cycle_second_leg

    if mirrored_suffix == "SHORT_REDUCE":
        return purpose_mapping.cycle_short_reduce(cycle_index)
    if mirrored_suffix == "LONG_REDUCE":
        return purpose_mapping.cycle_long_reduce(cycle_index)
    if mirrored_suffix == "LONG_ADD":
        return purpose_mapping.cycle_long_add(cycle_index)
    if mirrored_suffix == "SHORT_ADD":
        return purpose_mapping.cycle_short_add(cycle_index)
    raise ValueError(f"unsupported mirrored suffix: {mirrored_suffix}")
