"""Rich past-only feature rows for audit cases."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
for p in (
    ROOT / "tools/phase_bt_d59_market_regime_pool_evolution_v1",
    ROOT / "tools/phase_bt_d60_ema_history_validation_v1",
    ROOT / "tools/phase_bt_d61_regime_gegenprobe_audit_v1",
    ROOT / "tools/phase_bt_d65_pure_ema_timestamp_review_v1",
):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from classification_v1 import classify_row  # noqa: E402
from ema_features_pure_v1 import compute_pure_ema_row  # noqa: E402
from enrich_v1 import build_trp_ema_maps  # noqa: E402
from ohlc_loader_v1 import _iso, _parse_utc, load_5m_ohlc_ch  # noqa: E402
from validation_config_v1 import REFERENCE_WARMUP_HOURS  # noqa: E402

MANDATORY = [
    ("2026-08-06T00:00:00Z", "mandatory_false_allow_suspect"),
    ("2026-08-12T08:30:00Z", "mandatory_false_allow_suspect"),
    ("2026-09-11T12:05:00Z", "mandatory_false_allow_suspect"),
]

CONTROLS = [
    ("2026-09-09T20:55:00Z", "positive_control_downtrend"),
    ("2026-08-05T03:30:00Z", "positive_control_downtrend"),
    ("2026-08-05T07:30:00Z", "positive_control_downtrend"),
    ("2026-08-13T10:30:00Z", "positive_control_downtrend"),
]

ALL_CASES = MANDATORY + CONTROLS


def load_series() -> tuple[list[str], dict, dict]:
    from datetime import timedelta

    start = "2026-08-01T00:00:00Z"
    end = "2026-09-30T00:00:00Z"
    lo = _parse_utc(start) - timedelta(hours=REFERENCE_WARMUP_HOURS + 48)
    keys, prices, _ = load_5m_ohlc_ch(_iso(lo), end)
    emas = build_trp_ema_maps(keys, prices)
    return keys, prices, emas


def feature_row(keys: list[str], prices: dict, emas: dict, ts: str) -> dict[str, Any]:
    if ts not in keys:
        return {"decision_close_utc": ts, "ema_ready": False}
    row = classify_row(compute_pure_ema_row(bar=ts, bar_keys=keys, prices=prices, emas=emas))
    idx = keys.index(ts)
    trail: list[str] = []
    for lb in (6, 12, 24):
        if idx >= lb:
            prev = keys[idx - lb]
            pr = classify_row(compute_pure_ema_row(bar=prev, bar_keys=keys, prices=prices, emas=emas))
            trail.append(f"t-{lb}b:{pr.get('ema_structure_decision')}")
    row["decision_trail_labels"] = "|".join(trail)
    row["close_minus_ema59_pct"] = (
        (float(row["close"]) - float(row["ema59"])) / float(row["ema59"]) * 100.0
        if row.get("ema59")
        else None
    )
    row["close_minus_ema200_pct"] = (
        (float(row["close"]) - float(row["ema200"])) / float(row["ema200"]) * 100.0
        if row.get("ema200")
        else None
    )
    return row


def exact_case_features(keys: list[str], prices: dict, emas: dict) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for ts, cohort in ALL_CASES:
        row = feature_row(keys, prices, emas, ts)
        row["cohort"] = cohort
        row["case_role"] = "mandatory" if cohort.startswith("mandatory") else "control"
        out.append(row)
    return out
