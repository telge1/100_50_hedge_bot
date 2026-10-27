"""Enrich BT-D60 Gegenprobe rows with TRP slopes and causal pool features."""

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
    ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1",
    ROOT / "tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1",
    ROOT / "tools/phase_bt_d8_canonical_density_cache_v1",
    ROOT / "tools/phase_bt_d23_pool_first_setup_scanner_v1",
):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from ema_definitions_v1 import EMA_PERIODS, ema_series_for_path  # noqa: E402
from ohlc_loader_v1 import _iso, _parse_utc, load_5m_ohlc_ch  # noqa: E402
from validation_config_v1 import REFERENCE_WARMUP_HOURS  # noqa: E402

MIN_GAP = 0.5


def _slope_pct(ema_by_bar: dict[str, float], bar: str, bars: list[str], lb: int) -> float | None:
    if bar not in ema_by_bar:
        return None
    idx = bars.index(bar)
    if idx < lb:
        return None
    prev = bars[idx - lb]
    if prev not in ema_by_bar:
        return None
    a, b = ema_by_bar[bar], ema_by_bar[prev]
    if b == 0:
        return None
    return (a - b) / b * 100.0


def build_trp_ema_maps(bar_keys: list[str], prices: dict[str, dict[str, float]]) -> dict[int, dict[str, float]]:
    closes = [float(prices[b]["c"]) for b in bar_keys]
    ema: dict[int, dict[str, float]] = {p: {} for p in EMA_PERIODS}
    for period in EMA_PERIODS:
        series = ema_series_for_path("trp_research_charts", closes, period)
        for i, bar in enumerate(bar_keys):
            v = series[i]
            if v is not None:
                ema[period][bar] = float(v)
    return ema


def load_ema_context() -> tuple[list[str], dict[str, dict[str, float]], dict[int, dict[str, float]]]:
    first = _parse_utc("2026-09-04T12:00:00Z")
    load_start = first - timedelta(hours=REFERENCE_WARMUP_HOURS + 24)
    load_end = _parse_utc("2026-09-11T17:00:00Z")
    bar_keys, prices, _meta = load_5m_ohlc_ch(_iso(load_start), _iso(load_end))
    emas = build_trp_ema_maps(bar_keys, prices)
    return bar_keys, prices, emas


def attach_ema_slopes(row: dict[str, Any], bar_keys: list[str], emas: dict[int, dict[str, float]]) -> None:
    bar = row["decision_close_utc"]
    if bar not in bar_keys or bar not in emas[200]:
        row["ema_validity"] = "UNCERTAIN_HISTORY"
        return
    row["ema_validity"] = "VALIDATED_TRP_D60"
    for period in (59, 200):
        for lb in (6, 12, 24):
            row[f"ema{period}_slope_{lb}b_pct"] = _slope_pct(emas[period], bar, bar_keys, lb)
    row["ema_compression_pct"] = None
    if row.get("ema59") and row.get("ema200"):
        row["ema_compression_pct"] = (float(row["ema59"]) - float(row["ema200"])) / float(row["ema200"]) * 100.0


def _merge_d59_pool_cache(rows: list[dict[str, Any]]) -> int:
    """Reuse verified D59 pool evolution rows where available (avoids 289x canonical replay)."""
    import csv

    path = ROOT / "results/phase_bt_d59_market_regime_v1/asof_upper_pool_evolution.csv"
    if not path.is_file():
        return 0
    pool_keys = {
        "nearest_upper_pool_lower",
        "nearest_upper_pool_upper",
        "nearest_upper_pool_key",
        "nearest_upper_origin_ids",
        "nearest_upper_origin_count",
        "gap_to_next_upper_pct",
        "upper_bands_between_entry_and_next",
        "local_stack_density_0p35pct",
        "total_upper_band_count",
        "structural_continuity_vs_prior",
        "origin_ids_added",
        "origin_ids_removed",
    }
    cache: dict[tuple[str, str], dict[str, Any]] = {}
    for r in csv.DictReader(path.open(encoding="utf-8")):
        cache[(r["window_id"], r["decision_close_utc"])] = r
    merged = 0
    for row in rows:
        hit = cache.get((row["window_id"], row["decision_close_utc"]))
        if not hit:
            continue
        for k in pool_keys:
            val = hit.get(k)
            if val == "":
                val = None
            if k in ("origin_ids_added", "origin_ids_removed"):
                row[k] = val in ("True", "true", "1", True)
            elif k in ("nearest_upper_origin_count", "upper_bands_between_entry_and_next", "local_stack_density_0p35pct", "total_upper_band_count"):
                row[k] = int(float(val)) if val not in (None, "") else None
            elif val is not None and k not in ("origin_ids_added", "origin_ids_removed"):
                try:
                    row[k] = float(val)
                except ValueError:
                    row[k] = val
            else:
                row[k] = val
        row["pool_validity"] = "OK_D59_CACHE"
        row["density_source"] = hit.get("data_source_density", "d59_cache")
        merged += 1
    return merged


def enrich_pool_for_windows(rows: list[dict[str, Any]], config: dict[str, Any]) -> None:
    from cache_session_v1 import ValidatedDensityCacheSessionV1  # noqa: E402
    from config_yaml_v1 import load_yaml_and_validate  # noqa: E402
    from d54_snapshot_loader_v1 import load_d54_window_snapshots  # noqa: E402
    from density_bridge_v1 import create_density_session  # noqa: E402
    from pool_evolution_v1 import compute_pool_evolution_row  # noqa: E402
    from research_windows_v1 import CONTROL_WINDOWS, REFERENCE_WINDOWS  # noqa: E402

    merged = _merge_d59_pool_cache(rows)
    windows = {w["window_id"]: w for w in REFERENCE_WINDOWS + CONTROL_WINDOWS}
    by_window: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        if r.get("pool_validity") in ("OK", "OK_D59_CACHE"):
            continue
        by_window.setdefault(r["window_id"], []).append(r)

    for wid, wrows in by_window.items():
        win = windows.get(wid)
        if not win:
            for r in wrows:
                r["pool_validity"] = "UNKNOWN_WINDOW"
            continue
        if wid == "sep4_sep5_sideways":
            for r in wrows:
                r["pool_validity"] = "UNCERTAIN_NO_DENSITY_AT_CLOSE"
                r["pool_note"] = (
                    "Sep-4/5 pool: use D59 stride subset where merged; "
                    "remaining bars EMA-only (no invented density)"
                )
            continue
        wrows.sort(key=lambda x: x["decision_close_utc"])
        prior_entry = None
        dens = None
        d54_snaps: dict[str, dict[str, Any]] = {}
        if win["density_source"] == "d54_canonical_artifact":
            d54_snaps = load_d54_window_snapshots(ROOT / win["d54_json"])
        elif win["density_source"] == "validated_cache":
            bundle = ROOT / win["cache_bundle"]
            cfg = copy.deepcopy(config)
            cfg["density"]["density_source"] = "validated_cache"
            cfg["density"]["validated_cache"] = {"bundle_path": str(bundle)}
            dens = ValidatedDensityCacheSessionV1(bundle, strategy_config=cfg)
        else:
            cfg = copy.deepcopy(config)
            cfg["density"]["density_source"] = "canonical_provider"
            dens = create_density_session(cfg)

        snap_cache: dict[str, dict[str, Any]] = {}
        for r in wrows:
            bar = r["decision_close_utc"]
            close = float(r["close"])
            try:
                if win["density_source"] == "d54_canonical_artifact":
                    snap = d54_snaps[bar]
                else:
                    if bar not in snap_cache:
                        snap_cache[bar] = dens.collect_at_close(bar)
                    snap = snap_cache[bar]
            except Exception as exc:  # noqa: BLE001
                r["pool_validity"] = "MISSING"
                r["pool_error"] = str(exc)
                continue
            pf = compute_pool_evolution_row(
                decision_close_utc=bar,
                snapshot=snap,
                close=close,
                prior_entry=prior_entry,
                min_gap_pct=MIN_GAP,
            )
            r.update(pf)
            r["pool_validity"] = "OK"
            r["density_source"] = win["density_source"]
            prior_entry = (
                {
                    "pool_key": pf["nearest_upper_pool_key"],
                    "pool_lower": pf["nearest_upper_pool_lower"],
                    "pool_upper": pf["nearest_upper_pool_upper"],
                    "origin_ids": pf["nearest_upper_origin_ids"],
                }
                if pf["nearest_upper_pool_key"]
                else prior_entry
            )
