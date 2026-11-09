"""P17AG — chart overlay from frozen P17AF restored strategy releases."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .bt_d12_latest_backtester import resolve_pools_ob_repo

SOURCE = "p17af_restored_strategy_july_v1"
STRATEGY_ID = "p17_restored_ema_watch_long_july_2026_research"
DRAWING_PREFIX = "p17ag-"
LAYER_DISPLAY_NAME = "P17 – Restored EMA→WATCH LONG"
P17AF_REL = (
    "results/phase_ema59_bt_p17_landscape_long_research_v1/"
    "p17af_restored_strategy_july_v1.json"
)
EXPECTED_RELEASES = {
    "6M-X-04": ("2026-07-07T11:10:00Z", 0.07448, 0.07359, 0.07558),
    "6M-X-07": ("2026-07-08T09:25:00Z", 0.07116, 0.07031, 0.07251),
    "6M-X-11": ("2026-07-17T02:00:00Z", 0.07212, 0.07126, 0.07283),
    "6M-X-20": ("2026-07-29T19:45:00Z", 0.07006, 0.06928, 0.07100),
}


def p17af_results_path(root: Path | None = None) -> Path:
    repo = root or resolve_pools_ob_repo()
    return repo / P17AF_REL


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(
        timezone.utc
    )


def load_restored_releases(root: Path | None = None) -> list[dict[str, Any]]:
    path = p17af_results_path(root)
    if not path.is_file():
        raise FileNotFoundError(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = list(payload.get("first_strategy_releases") or [])
    if len(rows) != 4:
        raise ValueError(f"P17AF expected 4 strategy releases, got {len(rows)}")
    seen: set[str] = set()
    for row in rows:
        cross_id = str(row.get("cross_id") or "")
        if cross_id not in EXPECTED_RELEASES:
            raise ValueError(f"unexpected P17AF release: {cross_id or '<missing>'}")
        expected_utc, entry, sl, tp = EXPECTED_RELEASES[cross_id]
        actual_utc = str(row.get("next_bar_entry_utc") or "")
        actual_sl = (row.get("sl") or {}).get("price")
        actual_tp = (row.get("upper") or {}).get("tp_lower")
        if (
            actual_utc != expected_utc
            or abs(float(row.get("entry_price")) - entry) > 1e-8
            or abs(float(actual_sl) - sl) > 1e-8
            or abs(float(actual_tp) - tp) > 1e-8
        ):
            raise ValueError(f"P17AF release values changed for {cross_id}")
        seen.add(cross_id)
    if seen != set(EXPECTED_RELEASES):
        raise ValueError("P17AF release set incomplete")
    return rows


def _position_spec(row: dict[str, Any], seq: int) -> dict[str, Any]:
    cross_id = str(row["cross_id"])
    entry_utc = str(row["next_bar_entry_utc"])
    start = _parse_dt(entry_utc)
    end = start + timedelta(minutes=30)
    entry = float(row["entry_price"])
    lower = row.get("lower") or {}
    sl = row.get("sl") or {}
    upper = row.get("upper") or {}
    sl_price = float(sl["price"])
    tp_price = float(upper["tp_lower"])
    sl_distance = float(sl["distance_pct"])
    tp_distance = float(upper["tp_distance_pct"])
    safe = entry_utc.replace(":", "").replace("-", "")
    status = (
        f"{LAYER_DISPLAY_NAME} · {cross_id} · RESEARCH_ONLY\n"
        f"Entry {entry:.6f} · SL {sl_price:.5f} (−{sl_distance:.3f}%) · "
        f"Upper TP {tp_price:.6f} (+{tp_distance:.3f}%)\n"
        f"EMA Cross {row.get('ema_cross_close_utc')} · "
        f"Lower [{lower.get('lower_edge')}–{lower.get('upper_edge')}]\n"
        f"Upper [{upper.get('tp_lower')}–{upper.get('tp_upper')}]"
    )
    return {
        "drawing_id": f"{DRAWING_PREFIX}pos-{seq:02d}-{safe}",
        "drawing_type": "long_position",
        "symbol": "DOGEUSDT",
        "timeframe": "5m",
        "start": start,
        "end": end,
        "entry": entry,
        "stop": sl_price,
        "target": tp_price,
        "metadata": {
            "origin": SOURCE,
            "strategy_id": STRATEGY_ID,
            "layer": LAYER_DISPLAY_NAME,
            "status_label": status,
            "research_disclaimer": "RESEARCH_ONLY — keine Exit-/PnL-Simulation",
            "research_only": True,
            "no_exit_simulated": True,
            "cross_id": cross_id,
            "ema_cross_utc": row.get("ema_cross_close_utc"),
            "watch_episode_id": row.get("watch_episode_id"),
            "entry_utc": entry_utc,
            "entry_price": entry,
            "sl_price": sl_price,
            "sl_distance_pct": sl_distance,
            "tp_price": tp_price,
            "tp_distance_pct": tp_distance,
            "lower_lo": lower.get("lower_edge"),
            "lower_hi": lower.get("upper_edge"),
            "lower_zone_id": lower.get("zone_id"),
            "upper_lo": upper.get("tp_lower"),
            "upper_hi": upper.get("tp_upper"),
            "near_touch_utc": row.get("near_touch_utc"),
            "bullish_confirm_utc": row.get("bullish_confirm_close_utc"),
            "visual_box_end_utc": end.isoformat().replace("+00:00", "Z"),
        },
    }


def _entry_marker(row: dict[str, Any], seq: int) -> dict[str, Any]:
    entry_utc = str(row["next_bar_entry_utc"])
    safe = entry_utc.replace(":", "").replace("-", "")
    cross_id = str(row["cross_id"])
    return {
        "drawing_id": f"{DRAWING_PREFIX}entry-{seq:02d}-{safe}",
        "symbol": "DOGEUSDT",
        "timestamp": entry_utc,
        "label": f"{cross_id} · P17AG RESEARCH_ONLY",
        "metadata": {
            "origin": SOURCE,
            "strategy_id": STRATEGY_ID,
            "marker_kind": "entry_long",
            "cross_id": cross_id,
            "ema_cross_utc": row.get("ema_cross_close_utc"),
            "entry_utc": entry_utc,
            "research_only": True,
            "color": "green",
        },
    }


def build_overlay_bundle(root: Path | None = None) -> dict[str, Any]:
    rows = load_restored_releases(root)
    positions = [_position_spec(row, i) for i, row in enumerate(rows, start=1)]
    markers = [_entry_marker(row, i) for i, row in enumerate(rows, start=1)]
    return {
        "contract_version": "p17ag_restored_long_chart_overlay_v1",
        "display_name": LAYER_DISPLAY_NAME,
        "source_p17af": P17AF_REL,
        "symbol": "DOGEUSDT",
        "timeframe": "5m",
        "source": SOURCE,
        "research_disclaimer": "RESEARCH_ONLY — keine Exit-/PnL-Simulation",
        "releases": rows,
        "position_specs": positions,
        "marker_specs": markers,
    }


__all__ = [
    "SOURCE",
    "STRATEGY_ID",
    "DRAWING_PREFIX",
    "LAYER_DISPLAY_NAME",
    "EXPECTED_RELEASES",
    "build_overlay_bundle",
    "load_restored_releases",
    "p17af_results_path",
]
