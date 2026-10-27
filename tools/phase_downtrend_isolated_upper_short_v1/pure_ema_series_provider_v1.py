"""Causal TRP EMA series for BT-D65 confirmation (OHLC only)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
for p in (
    ROOT / "tools/phase_bt_d60_ema_history_validation_v1",
    ROOT / "tools/phase_bt_d61_regime_gegenprobe_audit_v1",
    ROOT / "tools/phase_bt_d65_pure_ema_timestamp_review_v1",
    ROOT / "tools/phase_bt_d59_market_regime_pool_evolution_v1",
):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from classification_v1 import classify_row  # noqa: E402
from ema_features_pure_v1 import compute_pure_ema_row  # noqa: E402
from enrich_v1 import build_trp_ema_maps  # noqa: E402

EMA_PATH = "trp_research_charts"
BT_D65_RULES_MODULE = "phase_bt_d65_pure_ema_timestamp_review_v1.classification_v1"


class PureEmaSeriesProviderV1:
    """Precomputed TRP EMA maps; causal evaluation at each decision close."""

    def __init__(self, *, bar_keys: list[str], prices: dict[str, dict[str, float]]) -> None:
        self.bar_keys = bar_keys
        self.prices = prices
        self.emas = build_trp_ema_maps(bar_keys, prices)

    def feature_row_at_close(self, decision_close_utc: str) -> dict[str, Any]:
        if decision_close_utc not in self.bar_keys or decision_close_utc not in self.prices:
            return {"decision_close_utc": decision_close_utc, "ema_ready": False}
        return compute_pure_ema_row(
            bar=decision_close_utc,
            bar_keys=self.bar_keys,
            prices=self.prices,
            emas=self.emas,
        )

    def confirm_at_close(self, decision_close_utc: str) -> dict[str, Any]:
        row = self.feature_row_at_close(decision_close_utc)
        out = classify_row(row)
        out["ema_path"] = EMA_PATH
        out["bt_d65_rules_module"] = BT_D65_RULES_MODULE
        return out
