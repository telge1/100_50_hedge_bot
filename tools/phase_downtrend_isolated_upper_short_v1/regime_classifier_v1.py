"""Market regime classifier — skeleton returns UNCERTAIN (fail-closed)."""

from __future__ import annotations

from contracts_v1 import RegimeClassificationV1, RegimeContextV1, RegimeState

SKELETON_REASON = "REGIME_CLASSIFIER_SKELETON_V1_UNCERTAIN_DEFAULT"


class RegimeClassifierV1:
    """Pluggable regime gate; v1 skeleton does not classify DOWNTREND/SIDEWAYS yet."""

    def regime_at_close(self, context: RegimeContextV1) -> RegimeClassificationV1:
        evidence: list[str] = [SKELETON_REASON]
        if not context.decision_close_utc:
            evidence.append("MISSING_DECISION_CLOSE_UTC")
            return RegimeClassificationV1(
                regime="UNCERTAIN",
                evidence=evidence,
                blocks_new_short_intent=True,
            )
        if context.ohlc_to_close is None:
            evidence.append("MISSING_OHLC_AT_CLOSE")
        elif any(k not in context.ohlc_to_close for k in ("o", "h", "l", "c")):
            evidence.append("INCOMPLETE_OHLC_AT_CLOSE")

        # No EMA order, slope, range, or structure filters in skeleton v1.
        if context.ema_features is not None:
            evidence.append("EMA_FEATURES_IGNORED_NOT_VALIDATED")

        return RegimeClassificationV1(
            regime="UNCERTAIN",
            evidence=evidence,
            blocks_new_short_intent=True,
        )


def classify_regime(context: RegimeContextV1) -> RegimeClassificationV1:
    return RegimeClassifierV1().regime_at_close(context)
