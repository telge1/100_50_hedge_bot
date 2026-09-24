"""READY vs NO_PLAN classification. Does not invent baseline TP/SL."""

from __future__ import annotations

from typing import Any

from .schema import (
    REASON_DYNAMIC,
    REASON_NO_SL,
    REASON_NO_TP1,
    STATUS_NO_PLAN,
    STATUS_READY,
)


def classify_plan(plan: dict[str, Any] | None, *, replay: bool = False) -> dict[str, Any]:
    if not plan:
        return {"status": STATUS_NO_PLAN, "reason": REASON_NO_TP1, "ready": False}
    if replay or plan.get("TP1_UPDATED_FROM_DYNAMIC_POOL"):
        return {"status": STATUS_NO_PLAN, "reason": REASON_DYNAMIC, "ready": False}
    mode = str(plan.get("INITIAL_TARGET_MODE") or "")
    if mode == "ONE_VISIBLE_TARGET_WAIT_FOR_DYNAMIC_TP1":
        return {"status": STATUS_NO_PLAN, "reason": REASON_DYNAMIC, "ready": False}

    sl = plan.get("SL") or {}
    tp1 = plan.get("TP1") or {}
    tp2 = plan.get("TP2") or {}
    if not sl.get("available") or sl.get("SL_PRICE") is None:
        return {"status": STATUS_NO_PLAN, "reason": REASON_NO_SL, "ready": False}
    if not tp1.get("available") or tp1.get("TP1_PRICE") is None:
        return {"status": STATUS_NO_PLAN, "reason": REASON_NO_TP1, "ready": False}
    if mode not in ("TWO_VISIBLE_TARGETS", "ONE_VISIBLE_TARGET"):
        return {"status": STATUS_NO_PLAN, "reason": REASON_DYNAMIC, "ready": False}

    s1 = tp1.get("TP1_SIZE")
    s2 = tp2.get("TP2_SIZE")
    if mode == "TWO_VISIBLE_TARGETS":
        if float(s1 or 0) != 0.5 or float(s2 or 0) != 0.5:
            return {"status": STATUS_NO_PLAN, "reason": REASON_NO_TP1, "ready": False}
    else:
        if float(s1 or 0) != 1.0 or s2 not in (None, 0, 0.0):
            return {"status": STATUS_NO_PLAN, "reason": REASON_NO_TP1, "ready": False}

    return {
        "status": STATUS_READY,
        "reason": None,
        "ready": True,
        "mode": mode,
        "sl_too_wide": bool(sl.get("SL_TOO_WIDE")),
    }
