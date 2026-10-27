"""Causal P2 evaluation at decision closes (frozen R5/R6)."""

from __future__ import annotations

import copy
import sys
from datetime import datetime, timedelta, timezone
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
    ROOT / "tools/phase_bt_d62_intent_gate_replay_v1",
):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from ablation_v1 import ablation_flags, first_variant_drop  # noqa: E402
from density_bridge_v1 import create_density_session  # noqa: E402
from ema_definitions_v1 import EMA_PERIODS, ema_series_for_path  # noqa: E402
from gate_policies_v1 import evaluate_policies  # noqa: E402
from ohlc_loader_v1 import _iso, _parse_utc, load_5m_ohlc_ch  # noqa: E402
from pool_evolution_v1 import compute_pool_evolution_row, nearest_upper_above_close  # noqa: E402
from pool_evolution_v1 import _upper_rows  # noqa: E402
from regime_features_v1 import compute_regime_features_at_close  # noqa: E402
from regime_labels_v1 import research_regime  # noqa: E402
from validation_config_v1 import REFERENCE_WARMUP_HOURS  # noqa: E402


def load_ema_bundle(global_start: str, global_end: str) -> tuple[list[str], dict, dict[int, dict[str, float]]]:
    start = _parse_utc(global_start) - timedelta(hours=REFERENCE_WARMUP_HOURS + 24)
    end = _parse_utc(global_end)
    bar_keys, prices, _meta = load_5m_ohlc_ch(_iso(start), _iso(end))
    closes = [float(prices[b]["c"]) for b in bar_keys]
    emas: dict[int, dict[str, float]] = {p: {} for p in EMA_PERIODS}
    for period in EMA_PERIODS:
        series = ema_series_for_path("trp_research_charts", closes, period)
        for i, bar in enumerate(bar_keys):
            if series[i] is not None:
                emas[period][bar] = float(series[i])
    bars_regime = [b for b in bar_keys if b in emas[200]]
    return bars_regime, prices, emas


def _bars_in_window(prices: dict, start: str, end: str, stride: int) -> list[str]:
    s, e = _parse_utc(start), _parse_utc(end)
    out = []
    for t in sorted(prices.keys()):
        dt = _parse_utc(t)
        if s <= dt <= e:
            out.append(t)
    return out[:: max(1, stride)]


def _attach_pool(
    row: dict[str, Any],
    win: dict[str, Any],
    config: dict[str, Any],
    *,
    d54_snaps: dict[str, dict] | None,
    dens: Any | None,
    snap_cache: dict[str, dict],
    prior_entry: dict | None,
) -> dict | None:
    bar = row["decision_close_utc"]
    close = float(row["close"])
    mode = win["density_mode"]
    try:
        if mode == "d54_artifact":
            if not d54_snaps or bar not in d54_snaps:
                row["pool_validity"] = "UNAVAILABLE"
                return prior_entry
            snap = d54_snaps[bar]
        elif mode == "validated_cache":
            if dens is None:
                row["pool_validity"] = "UNAVAILABLE"
                return prior_entry
            if bar not in snap_cache:
                snap_cache[bar] = dens.collect_at_close(bar)
            snap = snap_cache[bar]
        elif mode == "canonical_provider":
            if dens is None:
                row["pool_validity"] = "UNAVAILABLE"
                return prior_entry
            if bar not in snap_cache:
                snap_cache[bar] = dens.collect_at_close(bar)
            snap = snap_cache[bar]
        else:
            row["pool_validity"] = "UNAVAILABLE"
            return prior_entry
        pf = compute_pool_evolution_row(
            decision_close_utc=bar,
            snapshot=snap,
            close=close,
            prior_entry=prior_entry,
            min_gap_pct=0.5,
        )
        row.update(pf)
        row["pool_validity"] = "OK"
        row["density_mode"] = mode
        uppers = _upper_rows(snap)
        entry = nearest_upper_above_close(uppers, close)
        row["baseline_upper_candidate"] = entry is not None
        return (
            {
                "pool_key": pf["nearest_upper_pool_key"],
                "pool_lower": pf["nearest_upper_pool_lower"],
                "pool_upper": pf["nearest_upper_pool_upper"],
                "origin_ids": pf["nearest_upper_origin_ids"],
            }
            if pf.get("nearest_upper_pool_key")
            else prior_entry
        )
    except Exception as exc:  # noqa: BLE001
        row["pool_validity"] = "UNAVAILABLE"
        row["pool_error"] = str(exc)[:200]
        return prior_entry


def evaluate_windows(
    windows: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    d46_intent_ts: set[str],
) -> list[dict[str, Any]]:
    if not windows:
        return []
    g_start = min(w["start_utc"] for w in windows)
    g_end = max(w["end_utc"] for w in windows)
    bars_regime, prices, emas = load_ema_bundle(g_start, g_end)

    from cache_session_v1 import ValidatedDensityCacheSessionV1  # noqa: E402
    from d54_snapshot_loader_v1 import load_d54_window_snapshots  # noqa: E402

    d54_merged: dict[str, dict] = {}
    for rel in (
        "results/phase_bt_d54_perfect_downtrend_short_entry_v1/window1_2026_09_09_1630_1725.json",
        "results/phase_bt_d54_perfect_downtrend_short_entry_v1/window2_2026_09_09_2055_2125.json",
    ):
        p = ROOT / rel
        if p.is_file():
            d54_merged.update(load_d54_window_snapshots(p))

    cache_bundle = ROOT / "results/phase_bt_d9_first_24h_short_pnl_v1/cache_bundle"
    cfg_cache = copy.deepcopy(config)
    cfg_cache["density"]["density_source"] = "validated_cache"
    cfg_cache["density"]["validated_cache"] = {"bundle_path": str(cache_bundle)}
    dens_cache = ValidatedDensityCacheSessionV1(cache_bundle, strategy_config=cfg_cache) if cache_bundle.is_dir() else None

    cfg_canon = copy.deepcopy(config)
    cfg_canon["density"]["density_source"] = "canonical_provider"
    dens_canon = create_density_session(cfg_canon)

    rows: list[dict[str, Any]] = []
    for win in windows:
        eval_bars = _bars_in_window(prices, win["start_utc"], win["end_utc"], int(win.get("bar_stride") or 1))
        snap_cache: dict[str, dict] = {}
        prior_entry = None
        dens = dens_cache if win["density_mode"] == "validated_cache" else dens_canon
        if win["density_mode"] == "d54_artifact":
            dens = None
        for bar in eval_bars:
            if bar not in prices or bar not in emas[200]:
                continue
            feats = compute_regime_features_at_close(bar=bar, bars=bars_regime, prices=prices, emas=emas)
            row: dict[str, Any] = {
                **feats,
                "decision_close_utc": bar,
                "window_id": win["window_id"],
                "evidence_class": win["evidence_class"],
                "market_hypothesis": win["market_hypothesis"],
                "ema_validity": "VALIDATED_TRP_D60",
                "ema_path": "trp_research_charts",
                "d46_baseline_intent": bar in d46_intent_ts,
            }
            from enrich_v1 import attach_ema_slopes  # noqa: E402

            attach_ema_slopes(row, bars_regime, emas)
            prior_entry = _attach_pool(
                row,
                win,
                config,
                d54_snaps=d54_merged if win["density_mode"] == "d54_artifact" else None,
                dens=dens,
                snap_cache=snap_cache,
                prior_entry=prior_entry,
            )
            flags = ablation_flags(row)
            row["R5"] = flags["variant_E_plus_no_stack_growth"]
            row["R6_sideways"] = research_regime(row) == "SIDEWAYS_CANDIDATE"
            row["research_regime"] = research_regime(row)
            row["first_variant_drop"] = first_variant_drop(flags)
            pol = evaluate_policies(
                {**row, "R5": row["R5"], "R6_sideways": row["R6_sideways"]},
                short_paused=False,
            )
            row["P2"] = pol["policy_p2_r6_veto"]
            row["P2_reason"] = pol["policy_p2_reason"]
            eligible = bool(row.get("baseline_upper_candidate")) and row.get("pool_validity") == "OK"
            if not eligible and not row.get("d46_baseline_intent"):
                row["P2"] = "UNCERTAIN"
                row["P2_reason"] = "NO_VERIFIED_UPPER_CANDIDATE_PROXY"
            if row.get("ema_validity") != "VALIDATED_TRP_D60":
                row["P2"] = "UNCERTAIN"
            rows.append(row)
    return rows
