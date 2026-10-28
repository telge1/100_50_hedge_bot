"""Research-only pause controller with optional post-SL lockout (BT-D84)."""

from __future__ import annotations

from dataclasses import dataclass

from contracts_v1 import StopLossPauseEventV1
from short_pause_state_v1 import ShortPauseControllerV1

RESEARCH_PAUSE_CONTRACT = "research_pause_v1_post_sl_opt_out"


@dataclass
class ResearchPauseControllerV1(ShortPauseControllerV1):
    """
    When post_sl_pause_enabled is False (BT-D84 research only):
    modeled SL still closes the position and logs StopLossPauseEventV1,
    but pause_state stays READY so EMA V3 may authorize new Pending orders.
    Default True preserves production-equivalent SHORT_PAUSED after SL.
    """

    post_sl_pause_enabled: bool = True

    def on_modeled_stop_loss(
        self,
        *,
        exit_bar_utc: str,
        position_id: str | None = None,
        causal_note: str = "MODELED_SL_EXIT",
    ) -> None:
        self.events.append(
            StopLossPauseEventV1(
                exit_bar_utc=exit_bar_utc,
                position_id=position_id,
                causal_note=causal_note,
            )
        )
        if self.post_sl_pause_enabled:
            self.pause_state = "SHORT_PAUSED"
