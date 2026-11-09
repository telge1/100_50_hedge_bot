"""
Deterministic channel-up-cycle state for L1 V2 (variant D).

Research definition (not proven causal):
- After any bar where high >= main upper channel line, `upper_reached` is set.
- While `upper_reached`, new long entries are blocked (variant D).
- Reset when a new allowlist touch phase starts and the prior bar's close is at
  least MIN_CHANNEL_ROOM_PCT below the upper channel line price (price left upper zone).
- Uses only completed bars at or before the decision index.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from config import MIN_CHANNEL_ROOM_PCT


@dataclass
class ChannelCycleState:
    upper_reached: bool = False
    last_bar_advanced: int = -1
    upper_reached_bar: int | None = None

    def advance_to(
        self,
        candles: list[dict[str, Any]],
        to_idx: int,
        upper_price_at_bar,
    ) -> None:
        start = max(0, self.last_bar_advanced + 1)
        for j in range(start, to_idx + 1):
            if j >= len(candles):
                break
            up = upper_price_at_bar(candles[j])
            if up is not None and float(candles[j]["high"]) >= float(up):
                self.upper_reached = True
                self.upper_reached_bar = j
        self.last_bar_advanced = max(self.last_bar_advanced, to_idx)

    def maybe_reset_at_touch_start(
        self,
        candles: list[dict[str, Any]],
        contact_start_idx: int,
        upper_price_at_bar,
    ) -> None:
        if not self.upper_reached or contact_start_idx < 1:
            return
        prior = candles[contact_start_idx - 1]
        up = upper_price_at_bar(prior)
        if up is None or up <= 0:
            return
        room = (float(up) - float(prior["close"])) / float(prior["close"]) * 100.0
        if room >= MIN_CHANNEL_ROOM_PCT:
            self.upper_reached = False
            self.upper_reached_bar = None
