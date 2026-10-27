"""EMA field parity across BT-D60/D65/D67 load paths."""

from __future__ import annotations

import copy
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

for p in (
    ROOT / "tools/phase_bt_d60_ema_history_validation_v1",
    ROOT / "tools/phase_bt_d59_market_regime_pool_evolution_v1",
    ROOT / "tools/phase_bt_d61_regime_gegenprobe_audit_v1",
    ROOT / "tools/phase_bt_d65_pure_ema_timestamp_review_v1",
    ROOT / "tools/phase_downtrend_isolated_upper_short_v1",
):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from classification_v1 import classify_row  # noqa: E402
from ema_features_pure_v1 import compute_pure_ema_row  # noqa: E402
from enrich_v1 import build_trp_ema_maps  # noqa: E402
from ohlc_loader_v1 import _iso, _parse_utc, load_5m_ohlc_ch  # noqa: E402
from validation_config_v1 import REFERENCE_WARMUP_HOURS  # noqa: E402

REFERENCE_TS = [
    "2026-09-09T16:30:00Z",
    "2026-09-09T20:55:00Z",
    "2026-09-11T12:00:00Z",
    "2026-09-11T12:05:00Z",
    "2026-09-11T12:30:00Z",
]

COMPARE_FIELDS = [
    "close",
    "ema9",
    "ema20",
    "ema59",
    "ema200",
    "close_below_ema20",
    "close_below_ema59",
    "ema_bear_stack_9_20_59_200",
    "ema59_slope_6b_pct",
    "ema59_slope_12b_pct",
    "ema59_slope_24b_pct",
    "ema200_slope_6b_pct",
    "ema200_slope_12b_pct",
    "ema200_slope_24b_pct",
    "ema_structure_decision",
    "decision_reason_code",
]


def _load_profile(name: str, start: str, end: str) -> tuple[str, list[str], dict, dict[int, dict[str, float]]]:
    lo = _parse_utc(start) - timedelta(hours=REFERENCE_WARMUP_HOURS + 48)
    keys, prices, meta = load_5m_ohlc_ch(_iso(lo), end)
    emas = build_trp_ema_maps(keys, prices)
    return name, keys, prices, emas


def row_at(keys: list[str], prices: dict, emas: dict, ts: str) -> dict[str, Any]:
    base = compute_pure_ema_row(bar=ts, bar_keys=keys, prices=prices, emas=emas)
    return classify_row(base)


def compare_profiles(rows_by_ts: dict[str, dict[str, dict[str, Any]]]) -> tuple[list[dict], str]:
    """Return flat comparison rows and first_divergence markdown snippet."""
    flat: list[dict] = []
    first_div = ""
    for ts in REFERENCE_TS:
        per = rows_by_ts.get(ts) or {}
        profiles = list(per.keys())
        if len(profiles) < 2:
            continue
        ref_name = profiles[0]
        ref = per[ref_name]
        for field in COMPARE_FIELDS:
            ref_v = ref.get(field)
            for other in profiles[1:]:
                oth_v = per[other].get(field)
                match = ref_v == oth_v or (isinstance(ref_v, float) and isinstance(oth_v, float) and abs(ref_v - oth_v) < 1e-12)
                flat.append(
                    {
                        "decision_close_utc": ts,
                        "field": field,
                        "profile_a": ref_name,
                        "profile_b": other,
                        "value_a": ref_v,
                        "value_b": oth_v,
                        "match": match,
                    }
                )
                if not match and not first_div:
                    first_div = (
                        f"First divergence at **{ts}** field `{field}`: "
                        f"`{ref_name}`={ref_v!r} vs `{other}`={oth_v!r}."
                    )
    return flat, first_div


def run_parity() -> dict[str, Any]:
    # D67/BT-D68 pilot window load (same as run_canonical_ema_replay)
    g_start = "2026-09-09T16:00:00Z"
    g_end = "2026-09-11T13:30:00Z"
    # D65 full review span (Aug–Sep exploration + anchors)
    d65_start = "2026-08-05T00:00:00Z"
    d65_end = "2026-09-26T12:00:00Z"

    profiles = [
        _load_profile("bt_d67_pilot_load", g_start, g_end),
        _load_profile("bt_d65_review_load", d65_start, d65_end),
    ]

    rows_by_ts: dict[str, dict[str, dict[str, Any]]] = {ts: {} for ts in REFERENCE_TS}
    for name, keys, prices, emas in profiles:
        for ts in REFERENCE_TS:
            if ts not in keys:
                rows_by_ts[ts][name] = {"ema_ready": False, "decision_reason_code": "BAR_NOT_IN_LOADED_SERIES"}
                continue
            rows_by_ts[ts][name] = row_at(keys, prices, emas, ts)
            rows_by_ts[ts][name]["ohlc_o"] = prices[ts]["o"]
            rows_by_ts[ts][name]["ohlc_h"] = prices[ts]["h"]
            rows_by_ts[ts][name]["ohlc_l"] = prices[ts]["l"]
            rows_by_ts[ts][name]["ohlc_c"] = prices[ts]["c"]
            rows_by_ts[ts][name]["load_profile"] = name
            rows_by_ts[ts][name]["bars_in_series"] = len(keys)
            rows_by_ts[ts][name]["series_first_bar"] = keys[0]
            rows_by_ts[ts][name]["series_last_bar"] = keys[-1]

    flat, first_div = compare_profiles(rows_by_ts)
    if not first_div:
        first_div = "No field divergence between `bt_d67_pilot_load` and `bt_d65_review_load` at reference timestamps."

    # Load frozen D65 CSV row if present
    d65_csv = ROOT / "results/phase_bt_d65_pure_ema_review_v1/ema_timestamp_classifications.csv"
    d65_frozen: dict[str, dict[str, str]] = {}
    if d65_csv.is_file():
        import csv

        with d65_csv.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r["decision_close_utc"] in REFERENCE_TS:
                    d65_frozen[r["decision_close_utc"]] = r

    return {
        "rows_by_ts": rows_by_ts,
        "flat_comparison": flat,
        "first_divergence": first_div,
        "d65_frozen_rows": d65_frozen,
        "ohlc_meta_note": "ClickHouse 5m via pd1_landscape_viewer _resample_5m_from_candles; TRP SMA-seed EMA (BT-D60).",
    }
