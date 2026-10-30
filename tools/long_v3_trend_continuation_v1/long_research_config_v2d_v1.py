"""Phase 2D research configs — support strength, SL reference, recovery mode."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from long_config_yaml_v1 import LongSupportQualificationYamlV1


SupportSelectionMode = Literal["V1_BASELINE", "V2D_STRONG", "V2E_QUALIFIED"]
SlReferenceMode = Literal["LEGACY_BELOW_P_LOWER", "DEEPER_Q_UPPER"]
RecoveryMode = Literal["NEXT_BAR", "SAME_BAR_TOUCH_CLOSE"]
PoolAgeSemanticsV2H = Literal["EXACT_ATOMIC_5", "CAUSAL_REGION_LINEAGE_5"]
FormationSemanticsV2I = Literal[
    "LEGACY_LINEAGE_START",
    "CAUSAL_BELOW_PRICE_SEGMENT",
    "EXACT_ATOMIC_DEFAULT",
]
TouchLifecycleSemanticsV2J = Literal["UNION_KEY_V2I", "LINEAGE_AWARE_V2J"]
BuyLimitSemanticsV2K = Literal["FROZEN_AT_PLACE_V2J", "DYNAMIC_UPPER_GROWTH_V2K"]


@dataclass(frozen=True)
class LongResearchConfigV2DV1:
    """Isolated research variant; V1 baseline unchanged when using V1_BASELINE + LEGACY."""

    support_mode: SupportSelectionMode = "V1_BASELINE"
    sl_mode: SlReferenceMode = "LEGACY_BELOW_P_LOWER"
    recovery_mode: RecoveryMode = "NEXT_BAR"
    max_risk_pct: float | None = None
    v2e_yaml: LongSupportQualificationYamlV1 | None = None
    pool_age_semantics: PoolAgeSemanticsV2H = "EXACT_ATOMIC_5"
    formation_semantics: FormationSemanticsV2I = "EXACT_ATOMIC_DEFAULT"
    touch_lifecycle: TouchLifecycleSemanticsV2J = "UNION_KEY_V2I"
    buy_limit_semantics: BuyLimitSemanticsV2K = "FROZEN_AT_PLACE_V2J"

    def label(self) -> str:
        base = f"sup={self.support_mode}|sl={self.sl_mode}|rec={self.recovery_mode}"
        if self.support_mode == "V2E_QUALIFIED":
            extra = f"|V2E|age={self.pool_age_semantics}|form={self.formation_semantics}"
            if self.touch_lifecycle != "UNION_KEY_V2I":
                extra += f"|life={self.touch_lifecycle}"
            if self.buy_limit_semantics != "FROZEN_AT_PLACE_V2J":
                extra += f"|buy={self.buy_limit_semantics}"
            return base + extra
        return base


# 2×2 matrix (recovery fixed NEXT_BAR for causal separation)
MATRIX_2D_BASELINE = (
    LongResearchConfigV2DV1("V1_BASELINE", "LEGACY_BELOW_P_LOWER", "NEXT_BAR"),
    LongResearchConfigV2DV1("V1_BASELINE", "DEEPER_Q_UPPER", "NEXT_BAR"),
    LongResearchConfigV2DV1("V2D_STRONG", "LEGACY_BELOW_P_LOWER", "NEXT_BAR"),
    LongResearchConfigV2DV1("V2D_STRONG", "DEEPER_Q_UPPER", "NEXT_BAR"),
)
