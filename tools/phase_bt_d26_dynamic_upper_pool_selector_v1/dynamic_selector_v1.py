"""Dynamic upper-pool selector — BT-D21 lowest eligible, causal identity (BT-D26)."""

from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BT_D7 = Path(__file__).resolve().parents[1] / "phase_bt_d7_config_driven_short_pnl_integration_v1"
BT_D6 = Path(__file__).resolve().parents[1] / "phase_bt_d6_config_driven_strategy_pnl_foundation_v1"
BT_D23 = Path(__file__).resolve().parents[1] / "phase_bt_d23_pool_first_setup_scanner_v1"
PHASE4D = Path(__file__).resolve().parents[1] / "phase4d_pool_landscape_backtester_v2"
for p in (str(BT_D7), str(BT_D6), str(BT_D23), str(PHASE4D)):
    if p not in sys.path:
        sys.path.insert(0, p)

from bands_adapter_v1 import snapshot_to_intervals  # noqa: E402
from candidate_identity_v1 import candidate_from_signal_band  # noqa: E402
from candidate_state_v1 import DynamicCandidateStateV1  # noqa: E402
from config_validate_v1 import validate_config_dict  # noqa: E402
from density_bridge_v1 import create_density_session  # noqa: E402
from entry_price_v1 import compute_short_entry_level  # noqa: E402
from manifest_v1 import config_content_hash  # noqa: E402
from pool_first_signal_v1 import detect_short_signal_pool_first  # noqa: E402
from price_feed_v1 import load_prices_from_ssot_window  # noqa: E402
from setup_scanner_v1 import (  # noqa: E402
    _bars_in_range,
    _find_band,
    _pct_distance,
    _risk_reward,
    measure_upper_gap_pct,
)
from signal_types_v1 import pool_key  # noqa: E402
from strategy_rules import select_short_tp_depth2, short_sl  # noqa: E402

SCANNER_CONTRACT = "bt_d26_dynamic_upper_pool_selector_v1"
SELECTION_NOTE = (
    "REFERENCE: lowest pool_lower_edge among structurally eligible uppers (BT-D21 pool_first); "
    "not final approved economic relevance rule"
)
TP_SELECTION_NOTE = (
    "next_eligible_band via select_short_tp_depth2 — EXPERIMENTAL; dense lower-cluster not integrated"
)


def _setup_id(
    config_hash: str,
    decision_utc: str,
    origin_key: str,
    pool_lo: float,
    pool_hi: float,
    planned_entry: float,
    selection_reason: str,
) -> str:
    raw = (
        f"{config_hash}|{decision_utc}|{origin_key}|{pool_lo:.17g}|{pool_hi:.17g}|"
        f"{planned_entry:.17g}|{selection_reason}"
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


@dataclass
class DynamicSelectorResultV1:
    contract_version: str
    run_id: str
    config_hash: str
    symbol: str
    timeframe: str
    pool_detail: int
    min_unique_origins: int
    scan_start_utc: str
    scan_end_utc: str
    selection_note: str
    tp_selection_note: str
    setups: list[dict[str, Any]]
    candidate_transitions: list[dict[str, Any]]
    unresolved_events: list[dict[str, Any]]
    comparison_bt_d23: dict[str, Any] | None
    density_warmup_note: str


def _band_dict_from_snapshot(uppers: list[dict], pool_lo: float, pool_hi: float) -> dict[str, Any] | None:
    pk = pool_key(pool_lo, pool_hi)
    for b in uppers:
        if pool_key(float(b["lower_price"]), float(b["upper_price"])) == pk:
            return b
    return None


def scan_dynamic_upper_pool_setups(
    config: dict[str, Any],
    *,
    run_id: str,
    require_large_upper_gap: bool = True,
    bt_d23_setups_path: Path | None = None,
) -> DynamicSelectorResultV1:
    validate_config_dict(config)
    se = config["short_entry"]
    if se.get("entry_pool_selection_mode") != "pool_first_structural_v1":
        raise ValueError("dynamic selector requires entry_pool_selection_mode pool_first_structural_v1")

    start = config["backtest"]["start_utc"]
    end = config["backtest"]["end_utc"]
    end_exclusive = bool(config["backtest"].get("end_utc_exclusive"))
    symbol = config["backtest"]["symbols"][0]
    ch = config_content_hash(config)

    prices = load_prices_from_ssot_window(start, end, end_utc_exclusive=end_exclusive)
    bars = _bars_in_range(prices, start, end, end_exclusive=end_exclusive)
    density = create_density_session(config)
    state = DynamicCandidateStateV1()
    pool_age: dict[str, int] = {}
    setups: list[dict[str, Any]] = []

    d_cfg = config["density"]
    min_gap = float(se["upper_gap_min_pct"])
    sl_pct = float(config["risk"]["sl_above_upper_top_pct"])
    min_tp_dist = float(config["take_profit"]["min_distance_pct"])
    entry_mode = se.get("entry_price_reference") or se.get("entry_price_mode")
    offset_pct = float(se.get("entry_offset_pct") or 0)

    for bar in bars:
        try:
            snap = density.collect_at_close(bar)
        except KeyError:
            continue

        for b in snap.get("upper_bands") or []:
            pk = pool_key(float(b["lower_price"]), float(b["upper_price"]))
            pool_age[pk] = pool_age.get(pk, 0) + 1

        candle = prices.get(bar)
        if not candle:
            continue

        uppers = snap.get("upper_bands") or []
        sig = detect_short_signal_pool_first(
            config,
            decision_close_utc=bar,
            order_active_from_utc=bar,
            candle=candle,
            snapshot=snap,
            pool_age_bars=pool_age,
            require_large_upper_gap=require_large_upper_gap,
            blocked_pool_keys=None,
        )
        selected = None
        if sig is not None:
            band = _band_dict_from_snapshot(uppers, sig.pool_lower, sig.pool_upper)
            if band is not None:
                selected = candidate_from_signal_band(band)

        tr = state.process_bar(config, decision_utc=bar, snapshot=snap, selected=selected)
        if not tr.emit_setup or selected is None:
            continue

        pool_lo = selected.pool_lower
        pool_hi = selected.pool_upper
        entry_level = compute_short_entry_level(pool_lo, pool_hi, se)
        sl_planned = short_sl(pool_hi, sl_pct)
        intervals = snapshot_to_intervals(snap)
        tp_iv, tp_reason = select_short_tp_depth2(entry_level, intervals, min_tp_dist)
        tp_top = float(tp_iv["top"]) if tp_iv else None
        tp_bottom = float(tp_iv.get("bottom") or tp_iv.get("bottom_price")) if tp_iv else None
        target_lower = None
        if tp_iv and tp_bottom is not None and tp_top is not None:
            target_lower = _find_band(snap, "lower", tp_bottom, tp_top)

        gap_pct = measure_upper_gap_pct(uppers, pool_hi, min_gap)
        dist_sl = _pct_distance(entry_level, sl_planned)
        dist_tp = _pct_distance(entry_level, tp_top) if tp_top is not None else None
        rr = _risk_reward(entry_level, sl_planned, tp_top)

        setups.append(
            {
                "setup_id": _setup_id(
                    ch,
                    bar,
                    selected.origin_set_key,
                    pool_lo,
                    pool_hi,
                    entry_level,
                    tr.selection_reason or "",
                ),
                "status": "PLANNED",
                "symbol": symbol,
                "decision_utc": bar,
                "timeframe": d_cfg["timeframe"],
                "pool_detail": int(d_cfg["pool_detail"]),
                "min_unique_origins": int(d_cfg["min_unique_origins"]),
                "upper_entry_pool_id": selected.pool_key,
                "upper_origin_ids": selected.origin_ids,
                "origin_set_key": selected.origin_set_key,
                "pool_lower": pool_lo,
                "pool_upper": pool_hi,
                "upper_gap_pct": gap_pct,
                "entry_price_mode": str(entry_mode),
                "entry_offset_pct": offset_pct,
                "planned_entry": entry_level,
                "planned_sl": sl_planned,
                "lower_tp_origin_ids": (target_lower or {}).get("origin_ids"),
                "tp_pool_lower": tp_bottom,
                "tp_pool_upper": tp_top,
                "planned_tp": tp_top,
                "tp_selection_mode": config["take_profit"]["selection_mode"],
                "tp_selection_reason": tp_reason,
                "tp_selection_note": TP_SELECTION_NOTE,
                "distance_entry_to_sl_pct": dist_sl,
                "distance_entry_to_tp_pct": dist_tp,
                "planned_risk_reward": rr,
                "dynamic_state": tr.state,
                "selection_reason": tr.selection_reason,
                "invalidation_reason": tr.invalidation_reason,
                "selection_note": SELECTION_NOTE,
                "previous_candidate": tr.previous,
                "as_of_snapshot": {
                    "decision_close_utc": bar,
                    "pipeline_line_id": snap.get("pipeline_line_id"),
                    "density_config_hash": snap.get("config_hash"),
                },
                "config_hash": ch,
                "density_provenance": {
                    "pipeline_line_id": snap.get("pipeline_line_id"),
                    "provider_config_hash": snap.get("config_hash"),
                    "decision_close_utc": bar,
                    "density_source": d_cfg.get("density_source"),
                },
            }
        )

    comparison: dict[str, Any] | None = None
    if bt_d23_setups_path is not None and bt_d23_setups_path.is_file():
        import json

        d23 = json.loads(bt_d23_setups_path.read_text(encoding="utf-8"))
        comparison = {
            "bt_d23_run_id": d23.get("run_id"),
            "bt_d23_setup_count": d23.get("setup_count", len(d23.get("setups") or [])),
            "bt_d26_setup_count": len(setups),
            "bt_d26_candidate_events": len(setups),
            "bt_d26_transition_count": len(state.transitions),
        }

    transition_rows = [
        {
            "decision_utc": t.decision_utc,
            "state": t.state,
            "emit_setup": t.emit_setup,
            "selection_reason": t.selection_reason,
            "invalidation_reason": t.invalidation_reason,
            "previous": t.previous,
            "current": t.current,
        }
        for t in state.transitions
    ]

    return DynamicSelectorResultV1(
        contract_version=SCANNER_CONTRACT,
        run_id=run_id,
        config_hash=ch,
        symbol=symbol,
        timeframe=str(d_cfg["timeframe"]),
        pool_detail=int(d_cfg["pool_detail"]),
        min_unique_origins=int(d_cfg["min_unique_origins"]),
        scan_start_utc=start,
        scan_end_utc=end,
        selection_note=SELECTION_NOTE,
        tp_selection_note=TP_SELECTION_NOTE,
        setups=setups,
        candidate_transitions=transition_rows,
        unresolved_events=state.unresolved_events,
        comparison_bt_d23=comparison,
        density_warmup_note=(
            "Density via validated cache / ResearchDensityRunV1 uses 168h warmup_hours "
            "plus older ACTIVE origins from full historical SSOT (unchanged provider contract)."
        ),
    )


def result_to_json_dict(result: DynamicSelectorResultV1) -> dict[str, Any]:
    return {
        "contract_version": result.contract_version,
        "run_id": result.run_id,
        "config_hash": result.config_hash,
        "symbol": result.symbol,
        "timeframe": result.timeframe,
        "pool_detail": result.pool_detail,
        "min_unique_origins": result.min_unique_origins,
        "scan_start_utc": result.scan_start_utc,
        "scan_end_utc": result.scan_end_utc,
        "selection_note": result.selection_note,
        "tp_selection_note": result.tp_selection_note,
        "setup_count": len(result.setups),
        "density_warmup_note": result.density_warmup_note,
        "setups": result.setups,
        "candidate_transitions": result.candidate_transitions,
        "unresolved_events": result.unresolved_events,
        "comparison_bt_d23": result.comparison_bt_d23,
    }


def candidate_at_decision(
    result: DynamicSelectorResultV1,
    decision_utc: str,
) -> dict[str, Any] | None:
    """Last transition at or before decision_utc with a current candidate."""
    best = None
    for t in result.candidate_transitions:
        if t["decision_utc"] > decision_utc:
            break
        if t.get("current"):
            best = t
    return best
