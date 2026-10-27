"""BT-D65 pure EMA gate adapter for isolated strategy."""

from __future__ import annotations

from typing import Any, Literal

from contracts_v1 import PureEmaConfirmationV1

EmaDecision = Literal["SHORT_OK", "SHORT_NO", "UNCERTAIN"]


def confirmation_from_classified_row(row: dict[str, Any]) -> PureEmaConfirmationV1:
    decision: EmaDecision
    if not row.get("ema_ready"):
        decision = "UNCERTAIN"
        reason = row.get("decision_reason_code") or "EMA_WARMUP_INCOMPLETE"
    else:
        decision = row.get("ema_structure_decision", "UNCERTAIN")
        reason = row.get("decision_reason_code", "UNKNOWN")
    provenance = {
        "ema_path": row.get("ema_path"),
        "bt_d65_rules_module": row.get("bt_d65_rules_module"),
        "decision_close_utc": row.get("decision_close_utc"),
        "ema9": row.get("ema9"),
        "ema20": row.get("ema20"),
        "ema59": row.get("ema59"),
        "ema200": row.get("ema200"),
        "ema59_slope_6b_pct": row.get("ema59_slope_6b_pct"),
        "ema59_slope_12b_pct": row.get("ema59_slope_12b_pct"),
        "ema59_slope_24b_pct": row.get("ema59_slope_24b_pct"),
        "ema200_slope_6b_pct": row.get("ema200_slope_6b_pct"),
        "ema200_slope_12b_pct": row.get("ema200_slope_12b_pct"),
        "ema200_slope_24b_pct": row.get("ema200_slope_24b_pct"),
        "ema_bear_stack_9_20_59_200": row.get("ema_bear_stack_9_20_59_200"),
        "ema_bull_stack_9_20_59_200": row.get("ema_bull_stack_9_20_59_200"),
    }
    return PureEmaConfirmationV1(
        decision=decision,
        reason_code=reason,
        chart_focus=row.get("chart_review_focus"),
        provenance=provenance,
        authorizes_new_pending=decision == "SHORT_OK",
    )


class PureEmaConfirmationGateV1:
    """Optional injectable gate (real provider or test stub)."""

    def __init__(self, provider: Any | None = None) -> None:
        self._provider = provider

    def at_close(self, decision_close_utc: str) -> PureEmaConfirmationV1:
        if self._provider is None:
            return PureEmaConfirmationV1(
                decision="UNCERTAIN",
                reason_code="EMA_PROVIDER_NOT_CONFIGURED",
                chart_focus=None,
                provenance={"decision_close_utc": decision_close_utc},
                authorizes_new_pending=False,
            )
        row = self._provider.confirm_at_close(decision_close_utc)
        return confirmation_from_classified_row(row)
