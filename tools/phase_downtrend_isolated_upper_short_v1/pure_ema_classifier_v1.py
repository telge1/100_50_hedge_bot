"""Selectable pure EMA classifiers — V1 frozen BT-D65, V3 BT-D69 research."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Literal

ROOT = Path(__file__).resolve().parents[2]
D65 = ROOT / "tools/phase_bt_d65_pure_ema_timestamp_review_v1"
D69 = ROOT / "tools/phase_bt_d69_pure_ema_false_allow_audit_v1"
for p in (D65, D69):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from classification_v1 import classify_row  # noqa: E402
from variant_classifiers_v1 import classify_variant  # noqa: E402

EmaClassifierVersion = Literal["v1", "v3"]

V1_MODULE = "phase_bt_d65_pure_ema_timestamp_review_v1.classification_v1"
V3_MODULE = "phase_bt_d69_pure_ema_false_allow_audit_v1.variant_classifiers_v1"
V3_VARIANT_ID = "v3_bear_stack_falling_59_200"


def normalize_classifier_version(raw: str | None) -> EmaClassifierVersion:
    v = (raw or "v1").strip().lower()
    if v in ("v1", "bt_d65", "d65"):
        return "v1"
    if v in ("v3", "bt_d69", "d69"):
        return "v3"
    raise ValueError(f"unsupported ema classifier version: {raw!r}")


def classify_feature_row(row: dict[str, Any], version: EmaClassifierVersion) -> dict[str, Any]:
    """Apply EMA structure decision to a feature row (no pool fields)."""
    if version == "v1":
        out = classify_row(row)
        out["ema_classifier_version"] = "v1"
        out["ema_classifier_module"] = V1_MODULE
        return out
    decision, reason = classify_variant(row, V3_VARIANT_ID)
    out = dict(row)
    out["ema_structure_decision"] = decision
    out["decision_reason_code"] = reason
    out["chart_review_focus"] = out.get("chart_review_focus")
    out["ema_classifier_version"] = "v3"
    out["ema_classifier_module"] = V3_MODULE
    return out
