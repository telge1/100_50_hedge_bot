"""Pool-first structural setup scanner — planned entry/SL/TP only (BT-D23)."""

from __future__ import annotations

import hashlib
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

BT_D7 = Path(__file__).resolve().parents[1] / "phase_bt_d7_config_driven_short_pnl_integration_v1"
BT_D6 = Path(__file__).resolve().parents[1] / "phase_bt_d6_config_driven_strategy_pnl_foundation_v1"
PHASE4D = Path(__file__).resolve().parents[1] / "phase4d_pool_landscape_backtester_v2"
for p in (str(BT_D7), str(BT_D6), str(PHASE4D)):
    if p not in sys.path:
        sys.path.insert(0, p)

from bands_adapter_v1 import snapshot_to_intervals  # noqa: E402
from config_validate_v1 import validate_config_dict  # noqa: E402
from density_bridge_v1 import create_density_session  # noqa: E402
from entry_price_v1 import compute_short_entry_level  # noqa: E402
from gap_detection_v1 import compute_side_gaps  # noqa: E402
from manifest_v1 import config_content_hash  # noqa: E402
from pool_first_signal_v1 import _structural_eligible_upper  # noqa: E402
from pool_identity_v1 import pool_still_active_for_pending  # noqa: E402
from price_feed_v1 import load_prices_from_ssot_window  # noqa: E402
from setup_dedup_v1 import (  # noqa: E402
    SetupDedupTrackerV1,
    SetupFingerprint,
    origin_set_identity_key,
)
from signal_types_v1 import ShortSignalV1, pool_key  # noqa: E402
from strategy_rules import select_short_tp_depth2, short_sl  # noqa: E402

SCANNER_CONTRACT = "bt_d23_pool_first_setup_scanner_v1"
TP_SELECTION_NOTE = (
    "next_eligible_band via select_short_tp_depth2 — not dense lower-cluster selection (BT-D23)"
)


def _parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _bars_in_range(
    prices: dict[str, dict[str, float]],
    start: str,
    end: str,
    *,
    end_exclusive: bool,
) -> list[str]:
    times = sorted(prices.keys())
    out: list[str] = []
    for t in times:
        if t < start:
            continue
        if end_exclusive and t >= end:
            break
        if not end_exclusive and t > end:
            break
        out.append(t)
    return out


def _find_band(snapshot: dict[str, Any], side: str, pool_lo: float, pool_hi: float) -> dict[str, Any] | None:
    key = pool_key(pool_lo, pool_hi)
    bands = snapshot.get("upper_bands" if side == "upper" else "lower_bands") or []
    for b in bands:
        if pool_key(float(b["lower_price"]), float(b["upper_price"])) == key:
            return b
    return None


def measure_upper_gap_pct(upper_bands: list[dict[str, Any]], pool_upper: float, min_gap_pct: float) -> float | None:
    """Largest qualifying upper-side gap with gap_bottom >= pool upper edge."""
    best: float | None = None
    for g in compute_side_gaps(upper_bands, side="upper", min_gap_pct=min_gap_pct):
        if g["gap_bottom_price"] >= pool_upper - 1e-15:
            pct = float(g["gap_pct"])
            if best is None or pct > best:
                best = pct
    return best


def _pct_distance(entry: float, other: float) -> float:
    return abs(other - entry) / entry * 100.0


def _risk_reward(entry: float, sl: float, tp: float | None) -> float | None:
    if tp is None:
        return None
    risk = sl - entry
    reward = entry - tp
    if risk <= 0 or reward <= 0:
        return None
    return reward / risk


def _setup_id(
    config_hash: str,
    decision_utc: str,
    origin_key: str,
    pool_lo: float,
    pool_hi: float,
    planned_entry: float,
) -> str:
    raw = f"{config_hash}|{decision_utc}|{origin_key}|{pool_lo:.17g}|{pool_hi:.17g}|{planned_entry:.17g}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


@dataclass
class SetupScanResultV1:
    contract_version: str
    run_id: str
    config_hash: str
    symbol: str
    timeframe: str
    pool_detail: int
    min_unique_origins: int
    scan_start_utc: str
    scan_end_utc: str
    tp_selection_note: str
    setups: list[dict[str, Any]]
    suppressed_events: list[dict[str, Any]]
    suppressed_events_total: int
    density_warmup_note: str


def scan_pool_first_setups(
    config: dict[str, Any],
    *,
    run_id: str,
    require_large_upper_gap: bool = True,
) -> SetupScanResultV1:
    validate_config_dict(config)
    se = config["short_entry"]
    if se.get("entry_pool_selection_mode") != "pool_first_structural_v1":
        raise ValueError("setup scanner requires entry_pool_selection_mode pool_first_structural_v1")

    start = config["backtest"]["start_utc"]
    end = config["backtest"]["end_utc"]
    end_exclusive = bool(config["backtest"].get("end_utc_exclusive"))
    symbol = config["backtest"]["symbols"][0]
    ch = config_content_hash(config)

    prices = load_prices_from_ssot_window(start, end, end_utc_exclusive=end_exclusive)
    bars = _bars_in_range(prices, start, end, end_exclusive=end_exclusive)
    density = create_density_session(config)
    dedup = SetupDedupTrackerV1()
    pool_age: dict[str, int] = {}
    setups: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []

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

        for band in uppers:
            ok, gate_reason = _structural_eligible_upper(
                config,
                band=band,
                decision_close_utc=bar,
                candle=candle,
                snapshot=snap,
                pool_age_bars=pool_age,
                require_large_upper_gap=require_large_upper_gap,
            )
            pool_lo = float(band["lower_price"])
            pool_hi = float(band["upper_price"])
            origin_key = origin_set_identity_key(band.get("origin_ids"))
            if not ok:
                if origin_key:
                    suppressed.append(
                        {
                            "decision_utc": bar,
                            "origin_set_key": origin_key,
                            "pool_lower": pool_lo,
                            "pool_upper": pool_hi,
                            "event": "STRUCTURAL_GATE_FAIL",
                            "gate_reason": gate_reason,
                        }
                    )
                continue

            entry_level = compute_short_entry_level(pool_lo, pool_hi, se)
            sl_planned = short_sl(pool_hi, sl_pct)
            intervals = snapshot_to_intervals(snap)
            tp_iv, tp_reason = select_short_tp_depth2(entry_level, intervals, min_tp_dist)
            tp_top = float(tp_iv["top"]) if tp_iv else None
            tp_bottom = float(tp_iv.get("bottom") or tp_iv.get("bottom_price")) if tp_iv else None
            fp = SetupFingerprint(pool_lo, pool_hi)
            decision = dedup.evaluate(origin_key, fp)
            if not decision.emit:
                suppressed.append(
                    {
                        "decision_utc": bar,
                        "origin_set_key": origin_key,
                        "pool_lower": pool_lo,
                        "pool_upper": pool_hi,
                        "event": "DEDUP_SUPPRESSED",
                        "dedup_reason": decision.reason,
                    }
                )
                continue

            upper_b = _find_band(snap, "upper", pool_lo, pool_hi)
            target_lower = None
            if tp_iv and tp_bottom is not None and tp_top is not None:
                target_lower = _find_band(snap, "lower", tp_bottom, tp_top)

            gap_pct = measure_upper_gap_pct(uppers, pool_hi, min_gap)
            dist_sl = _pct_distance(entry_level, sl_planned)
            dist_tp = _pct_distance(entry_level, tp_top) if tp_top is not None else None
            rr = _risk_reward(entry_level, sl_planned, tp_top)

            setup_row = {
                "setup_id": _setup_id(ch, bar, origin_key, pool_lo, pool_hi, entry_level),
                "status": "PLANNED",
                "symbol": symbol,
                "decision_utc": bar,
                "timeframe": d_cfg["timeframe"],
                "pool_detail": int(d_cfg["pool_detail"]),
                "min_unique_origins": int(d_cfg["min_unique_origins"]),
                "upper_entry_pool_id": pool_key(pool_lo, pool_hi),
                "upper_origin_ids": str(band.get("origin_ids") or ""),
                "origin_set_key": origin_key,
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
                "dedup_emit_reason": decision.reason,
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
            setups.append(setup_row)
            dedup.commit(origin_key, fp)

        dedup.end_bar_origin_inactive(
            config,
            snap,
            pool_still_active_for_pending=pool_still_active_for_pending,
        )

    return SetupScanResultV1(
        contract_version=SCANNER_CONTRACT,
        run_id=run_id,
        config_hash=ch,
        symbol=symbol,
        timeframe=str(d_cfg["timeframe"]),
        pool_detail=int(d_cfg["pool_detail"]),
        min_unique_origins=int(d_cfg["min_unique_origins"]),
        scan_start_utc=start,
        scan_end_utc=end,
        tp_selection_note=TP_SELECTION_NOTE,
        setups=setups,
        suppressed_events=suppressed[:5000],
        suppressed_events_total=len(suppressed),
        density_warmup_note=(
            "Density via validated cache / ResearchDensityRunV1 uses 168h warmup_hours "
            "plus older ACTIVE origins from full historical SSOT (unchanged provider contract)."
        ),
    )


def result_to_json_dict(result: SetupScanResultV1) -> dict[str, Any]:
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
        "tp_selection_note": result.tp_selection_note,
        "setup_count": len(result.setups),
        "density_warmup_note": result.density_warmup_note,
        "setups": result.setups,
        "suppressed_events": result.suppressed_events,
        "suppressed_events_total": result.suppressed_events_total,
    }
