"""Trade permission pause (separate from market regime)."""

from __future__ import annotations

from dataclasses import dataclass, field

from contracts_v1 import PauseState, RegimeContextV1, RequalificationResultV1, StopLossPauseEventV1

REQUALIFICATION_NOT_IMPLEMENTED = "REQUALIFICATION_NOT_VALIDATED"


@dataclass
class ShortPauseControllerV1:
    """Defaults to SHORT_PAUSED — no new short intents until explicit READY + validated requalify."""

    pause_state: PauseState = "SHORT_PAUSED"
    events: list[StopLossPauseEventV1] = field(default_factory=list)

    def blocks_new_short_intent(self) -> bool:
        return self.pause_state != "READY"

    def on_modeled_stop_loss(
        self,
        *,
        exit_bar_utc: str,
        position_id: str | None = None,
        causal_note: str = "MODELED_SL_EXIT",
    ) -> None:
        self.pause_state = "SHORT_PAUSED"
        self.events.append(
            StopLossPauseEventV1(
                exit_bar_utc=exit_bar_utc,
                position_id=position_id,
                causal_note=causal_note,
            )
        )

    def try_requalify(self, context: RegimeContextV1) -> RequalificationResultV1:
        """Future hook — skeleton never unlocks without validated rules."""
        _ = context
        return RequalificationResultV1(
            unlocked=False,
            reason=REQUALIFICATION_NOT_IMPLEMENTED,
            evidence=["NO_AUTO_UNLOCK", "NO_COOLDOWN_TIMER"],
        )

    def set_ready_for_research_only(self) -> None:
        """Test-only escape hatch; not used by production runner."""
        self.pause_state = "READY"
