"""TP/SL and P5A upgrade — freeze tpsl_for_tf / apply_upgrade_plan."""

from __future__ import annotations

from signal_generator.strategy.wave_fade.parameters import TPSL_BY_TF, TPSL_EXTRA_4H


def tpsl_for_tf(tf: str, *, extra_4h: bool = False) -> tuple[float, float]:
    if tf == "4h" and extra_4h:
        return float(TPSL_EXTRA_4H[0]), float(TPSL_EXTRA_4H[1])
    tp, sl = TPSL_BY_TF[tf]
    return float(tp), float(sl)


def apply_upgrade_plan(
    policy: str,
    old_tp: float,
    old_sl: float,
    new_tf: str,
    *,
    extra_4h: bool = False,
) -> tuple[float, float]:
    ntp, nsl = tpsl_for_tf(new_tf, extra_4h=extra_4h)
    if policy == "P5A":
        return ntp, nsl
    if policy == "P5B":
        return ntp, old_sl
    if policy == "P5C":
        return ntp, min(old_sl, nsl)
    return old_tp, old_sl
