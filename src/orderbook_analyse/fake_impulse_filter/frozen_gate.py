from __future__ import annotations

from enum import Enum
from typing import Any, Mapping

import math

from .thresholds import FrozenGateThresholds, FROZEN_DEFAULT


class FrozenGateLabel(str, Enum):
    NO_EVIDENCE = "NO_EVIDENCE"
    EARLY_PRESSURE = "EARLY_PRESSURE"
    PUMP_CONFIRMING = "PUMP_CONFIRMING"
    PUMP_CONFIRMED = "PUMP_CONFIRMED"
    EARLY_SELL_PRESSURE = "EARLY_SELL_PRESSURE"
    DUMP_CONFIRMING = "DUMP_CONFIRMING"
    DUMP_CONFIRMED = "DUMP_CONFIRMED"
    MIXED = "MIXED"


def _f(v: Any) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return float("nan")
    return x


def classify_long_frozen(
    feat: Mapping[str, Any],
    thr: FrozenGateThresholds = FROZEN_DEFAULT,
) -> FrozenGateLabel:
    """Frozen long multi-source gate (unchanged logic)."""
    tbr = _f(feat.get("taker_buy_ratio"))
    cvd5 = _f(feat.get("cvd_chg_5m"))
    cvd3 = _f(feat.get("cvd_chg_3m"))
    imb = _f(feat.get("imbalance_l50"))
    ofi5 = _f(feat.get("ofi_5m"))
    rv_rel = _f(feat.get("rv5_vs_prior30_med"))
    ret5 = _f(feat.get("ret_5m"))
    ret1 = _f(feat.get("ret_1m"))
    vol_acc = _f(feat.get("vol_vs_30m_mean"))

    buy_dom = math.isfinite(tbr) and tbr >= thr.tbr_thr
    cvd_up = math.isfinite(cvd5) and cvd5 > thr.cvd_thr and (not math.isfinite(cvd3) or cvd3 > 0)
    flow_ok = buy_dom or cvd_up
    flow_clean = flow_ok and (not math.isfinite(tbr) or tbr >= thr.flow_clean_tbr_min)
    ob_sup = (math.isfinite(imb) and imb > thr.imb_thr) and (not math.isfinite(ofi5) or ofi5 >= 0)
    ob_opp = (math.isfinite(imb) and imb < -0.05) or (
        math.isfinite(ofi5) and ofi5 < 0 and math.isfinite(tbr) and tbr < 0.45
    )
    vol_exp = math.isfinite(rv_rel) and rv_rel >= thr.rv_rel_thr
    vol_busy = math.isfinite(vol_acc) and vol_acc >= thr.vol_busy_thr
    price_up = math.isfinite(ret5) and ret5 >= thr.ret5_confirmed
    price_soft_up = math.isfinite(ret5) and ret5 >= 0.0

    if (math.isfinite(ret1) and ret1 > 0.001 and math.isfinite(tbr) and tbr < 0.40) or (
        price_up and ob_opp and not flow_clean
    ):
        return FrozenGateLabel.MIXED
    if flow_clean and ob_sup and price_up and (vol_exp or vol_busy):
        return FrozenGateLabel.PUMP_CONFIRMED
    if flow_clean and ob_sup and price_soft_up and (vol_busy or vol_exp or price_up):
        return FrozenGateLabel.PUMP_CONFIRMING
    if flow_clean:
        return FrozenGateLabel.EARLY_PRESSURE
    if ob_opp and (price_up or vol_busy):
        return FrozenGateLabel.MIXED
    return FrozenGateLabel.NO_EVIDENCE


def classify_short_frozen(
    feat: Mapping[str, Any],
    thr: FrozenGateThresholds = FROZEN_DEFAULT,
) -> FrozenGateLabel:
    """Exploratory exact mirror of frozen long gate (documented; not a new tuned rule)."""
    tbr = _f(feat.get("taker_buy_ratio"))
    cvd5 = _f(feat.get("cvd_chg_5m"))
    cvd3 = _f(feat.get("cvd_chg_3m"))
    imb = _f(feat.get("imbalance_l50"))
    ofi5 = _f(feat.get("ofi_5m"))
    rv_rel = _f(feat.get("rv5_vs_prior30_med"))
    ret5 = _f(feat.get("ret_5m"))
    ret1 = _f(feat.get("ret_1m"))
    vol_acc = _f(feat.get("vol_vs_30m_mean"))

    sell_dom = math.isfinite(tbr) and tbr <= thr.sell_tbr_max
    cvd_down = math.isfinite(cvd5) and cvd5 < -thr.cvd_thr and (not math.isfinite(cvd3) or cvd3 < 0)
    flow_ok = sell_dom or cvd_down
    flow_clean = flow_ok and (not math.isfinite(tbr) or tbr <= thr.flow_clean_tbr_max_short)
    ob_sup = (math.isfinite(imb) and imb < -thr.imb_thr) and (not math.isfinite(ofi5) or ofi5 <= 0)
    ob_opp = (math.isfinite(imb) and imb > 0.05) or (
        math.isfinite(ofi5) and ofi5 > 0 and math.isfinite(tbr) and tbr > 0.55
    )
    vol_exp = math.isfinite(rv_rel) and rv_rel >= thr.rv_rel_thr
    vol_busy = math.isfinite(vol_acc) and vol_acc >= thr.vol_busy_thr
    price_down = math.isfinite(ret5) and ret5 <= -thr.ret5_confirmed
    price_soft_down = math.isfinite(ret5) and ret5 <= 0.0

    if (math.isfinite(ret1) and ret1 < -0.001 and math.isfinite(tbr) and tbr > 0.60) or (
        price_down and ob_opp and not flow_clean
    ):
        return FrozenGateLabel.MIXED
    if flow_clean and ob_sup and price_down and (vol_exp or vol_busy):
        return FrozenGateLabel.DUMP_CONFIRMED
    if flow_clean and ob_sup and price_soft_down and (vol_busy or vol_exp or price_down):
        return FrozenGateLabel.DUMP_CONFIRMING
    if flow_clean:
        return FrozenGateLabel.EARLY_SELL_PRESSURE
    if ob_opp and (price_down or vol_busy):
        return FrozenGateLabel.MIXED
    return FrozenGateLabel.NO_EVIDENCE
