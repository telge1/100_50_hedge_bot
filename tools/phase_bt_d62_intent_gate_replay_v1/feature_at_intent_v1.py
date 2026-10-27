"""Build past-only feature rows at intent decision closes (BT-D60 EMA + pool)."""

from __future__ import annotations

import copy
import csv
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

for p in (
    ROOT / "tools/phase_bt_d61_regime_gegenprobe_audit_v1",
    ROOT / "tools/phase_bt_d60_ema_history_validation_v1",
    ROOT / "tools/phase_bt_d59_market_regime_pool_evolution_v1",
    ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1",
    ROOT / "tools/phase_bt_d8_canonical_density_cache_v1",
    ROOT / "tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1",
    ROOT / "tools/phase_bt_d23_pool_first_setup_scanner_v1",
):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from ablation_v1 import ablation_flags, first_variant_drop  # noqa: E402
from enrich_v1 import attach_ema_slopes, load_ema_context  # noqa: E402
from regime_features_v1 import compute_regime_features_at_close  # noqa: E402
from regime_labels_v1 import research_regime  # noqa: E402


def _load_d61_cache() -> dict[str, dict[str, Any]]:
    path = ROOT / "results/phase_bt_d61_regime_audit_v1/per_close_regime_comparison.csv"
    if not path.is_file():
        return {}
    out: dict[str, dict[str, Any]] = {}
    for r in csv.DictReader(path.open(encoding="utf-8")):
        out[r["decision_close_utc"]] = r
    return out


def _boolish(v: Any) -> bool:
    return v in (True, "True", "true", "1", 1)


def _floatish(row: dict, key: str) -> float | None:
    v = row.get(key)
    if v in (None, "", "None"):
        return None
    return float(v)


_NUM = (
    "chop_ratio_24b",
    "net_move_24b_pct",
    "gap_to_next_upper_pct",
    "ema59_slope_12b_pct",
    "ema200_slope_12b_pct",
    "ema59_slope_6b_pct",
    "ema200_slope_6b_pct",
    "close",
    "local_stack_density_0p35pct",
    "upper_bands_between_entry_and_next",
)


def _normalize_row(r: dict[str, Any]) -> dict[str, Any]:
    for k in list(r.keys()):
        if k.startswith("close_below") or k == "ema9_lt_20_lt_59_lt_200":
            r[k] = _boolish(r[k])
        if k in ("origin_ids_added", "origin_ids_removed"):
            r[k] = _boolish(r[k])
        if k in _NUM:
            r[k] = _floatish(r, k) if k in r else r.get(k)
        if k == "upper_bands_between_entry_and_next" and r.get(k) is not None:
            r[k] = int(float(r[k]))
        if k == "local_stack_density_0p35pct" and r.get(k) is not None:
            r[k] = int(float(r[k]))
    return r


def _pool_window_for(ts: str) -> dict[str, Any]:
    if ts.startswith("2026-09-09"):
        return {
            "density_source": "d54_canonical_artifact",
            "d54_json": "results/phase_bt_d54_perfect_downtrend_short_entry_v1/window1_2026_09_09_1630_1725.json",
        }
    if ts.startswith("2026-09-11") or ts.startswith("2026-09-12"):
        return {
            "density_source": "validated_cache",
            "cache_bundle": "results/phase_bt_d9_first_24h_short_pnl_v1/cache_bundle",
        }
    return {"density_source": "none"}


def _attach_pool(row: dict[str, Any], config: dict[str, Any]) -> None:
    from cache_session_v1 import ValidatedDensityCacheSessionV1  # noqa: E402
    from d54_snapshot_loader_v1 import load_d54_window_snapshots  # noqa: E402
    from pool_evolution_v1 import compute_pool_evolution_row  # noqa: E402

    spec = _pool_window_for(row["decision_close_utc"])
    if spec["density_source"] == "none":
        row["pool_validity"] = "OUT_OF_COVERAGE"
        return
    bar = row["decision_close_utc"]
    close = float(row["close"])
    try:
        if spec["density_source"] == "d54_canonical_artifact":
            wjson = spec["d54_json"]
            if "20:55" in bar or bar >= "2026-09-09T20:55:00Z":
                wjson = "results/phase_bt_d54_perfect_downtrend_short_entry_v1/window2_2026_09_09_2055_2125.json"
            snaps = load_d54_window_snapshots(ROOT / wjson)
            snap = snaps[bar]
        else:
            bundle = ROOT / spec["cache_bundle"]
            cfg = copy.deepcopy(config)
            cfg["density"]["density_source"] = "validated_cache"
            cfg["density"]["validated_cache"] = {"bundle_path": str(bundle)}
            dens = ValidatedDensityCacheSessionV1(bundle, strategy_config=cfg)
            snap = dens.collect_at_close(bar)
        pf = compute_pool_evolution_row(
            decision_close_utc=bar,
            snapshot=snap,
            close=close,
            prior_entry=None,
            min_gap_pct=0.5,
        )
        row.update(pf)
        row["pool_validity"] = "OK"
        row["density_source"] = spec["density_source"]
    except Exception as exc:  # noqa: BLE001
        row["pool_validity"] = "MISSING"
        row["pool_error"] = str(exc)


def build_features_at_intent(intents: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    d61 = _load_d61_cache()
    bar_keys, prices, emas = load_ema_context()
    bars_regime = [b for b in bar_keys if b in emas[200]]

    rows: list[dict[str, Any]] = []
    for intent in intents:
        ts = intent["decision_close_utc"]
        base = dict(intent)
        if ts in d61 and d61[ts].get("ema_validity") == "VALIDATED_TRP_D60":
            row = _normalize_row(dict(d61[ts]))
            row.update(base)
        elif ts in prices:
            idx = bar_keys.index(ts)
            feats = compute_regime_features_at_close(bar=ts, bars=bars_regime, prices=prices, emas=emas)
            row = {**base, **feats, "ema_path": "trp_research_charts"}
            attach_ema_slopes(row, bar_keys, emas)
            if row.get("pool_validity") not in ("OK", "OK_D59_CACHE"):
                _attach_pool(row, config)
        else:
            row = {**base, "ema_validity": "UNCERTAIN_HISTORY", "pool_validity": "OUT_OF_COVERAGE"}
        if row.get("ema_validity") != "VALIDATED_TRP_D60":
            if ts in prices:
                attach_ema_slopes(row, bar_keys, emas)
                if row.get("ema_validity") != "VALIDATED_TRP_D60":
                    row["ema_validity"] = "UNCERTAIN_HISTORY"
        flags = ablation_flags(row)
        row["ablation"] = flags
        row["first_variant_drop"] = first_variant_drop(flags)
        row["research_regime"] = research_regime(row)
        row["R1"] = flags["variant_A_ema_order"]
        row["R2"] = flags["variant_B_plus_slopes_12b"]
        row["R3"] = flags["variant_C_plus_direction_chop"]
        row["R4"] = flags["variant_D_plus_upper_isolation"]
        row["R5"] = flags["variant_E_plus_no_stack_growth"]
        row["R6_sideways"] = row["research_regime"] == "SIDEWAYS_CANDIDATE"
        rows.append(row)
    return rows
