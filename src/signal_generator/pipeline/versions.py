"""Version identifiers for Wave-Fade shadow generator.

Active execution (default live): ``wave_fade_no_be50_v1`` — BE50 disabled.
Frozen research baseline (immutable): ``wave_fade_frozen_f16ae32`` — BE50 ON.

BASE: wave_fade_frozen_f16ae32
CHANGE (active): BE50 disabled
UNCHANGED: Wave-Fade, Tier-A, Q4, Entry, TP/SL levels, SL_FIRST
"""

from __future__ import annotations

from signal_generator.strategy.wave_fade.parameters import BASELINE_LABEL, SOURCE_COMMIT

# Immutable Frozen BE50 research baseline — do not overwrite or mutate.
STRATEGY_VERSION_BE50_FROZEN = f"wave_fade_frozen_{SOURCE_COMMIT[:7]}"

# Active live strategy: same Wave-Fade / Tier-A / Entry / TP/SL / SL_FIRST, no BE50.
STRATEGY_VERSION_NO_BE50 = "wave_fade_no_be50_v1"

# Default active version for new live signals / processing state.
STRATEGY_VERSION = STRATEGY_VERSION_NO_BE50

GENERATOR_VERSION = "wave_fade_shadow_pipeline_v1"
EDGES_VERSION = "apt_is_q4_frozen_20260808"
SIGNAL_TYPE = "wave_fade"
MODE_SHADOW = "shadow"

# Confirm global frozen Tier-A policy (unchanged)
GLOBAL_FROZEN_TIER_A = True
BASELINE = BASELINE_LABEL

# Exit-policy documentation
EXIT_POLICY_NO_BE50 = "NO_BE50"
EXIT_POLICY_BE50 = "BE50"


def uses_be50_exit(strategy_version: str | None) -> bool:
    """True iff this signal's strategy_version uses the Frozen BE50 exit engine."""
    sv = str(strategy_version or "")
    if sv == STRATEGY_VERSION_NO_BE50:
        return False
    if sv == STRATEGY_VERSION_BE50_FROZEN:
        return True
    # Legacy frozen tags remain BE50
    return sv.startswith("wave_fade_frozen_")
