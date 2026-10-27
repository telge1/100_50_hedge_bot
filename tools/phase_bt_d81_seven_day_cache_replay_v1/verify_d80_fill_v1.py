"""Part A — reconcile BT-D80 modeled fill vs OHLC and execution contract."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
D80_OUT = ROOT / "results/phase_bt_d80_multichunk_cache_pilot_v1"
D60 = ROOT / "tools/phase_bt_d60_ema_history_validation_v1"
if str(D60) not in sys.path:
    sys.path.insert(0, str(D60))

from ohlc_loader_v1 import load_5m_ohlc_ch  # noqa: E402

FILL_BAR = "2026-09-23T12:30:00Z"
SIGNAL_BAR = "2026-09-23T12:25:00Z"
WINDOW_END = "2026-09-25T23:55:00Z"


def verify_d80_v3_fill() -> dict[str, Any]:
    import csv

    modeled = D80_OUT / "modeled_positions_v3.csv"
    pending = D80_OUT / "pending_chronology_v3.csv"
    row = None
    with modeled.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r.get("event") == "SIMULATED_FILL" and r.get("classifier") == "v3":
                row = r
                break
    if not row:
        return {"ok": False, "blocked_reason": "NO_V3_FILL_IN_D80_ARTIFACTS"}

    entry = float(row["entry_price"])
    sl = float(row["sl_price"])
    tp = float(row["tp_price"])
    fill_utc = row["decision_close_utc"]
    signal_utc = row["signal_close_utc"]

    keys, prices, meta = load_5m_ohlc_ch(fill_utc, WINDOW_END)
    sl_bar = None
    tp_touch_bar = None
    max_high = -1.0
    min_low = 1e9
    for k in keys:
        ohlc = prices[k]
        h, l = float(ohlc["h"]), float(ohlc["l"])
        max_high = max(max_high, h)
        min_low = min(min_low, l)
        if h >= sl and sl_bar is None:
            sl_bar = k
        if l <= tp and tp_touch_bar is None:
            tp_touch_bar = k

    pending_fill = None
    with pending.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r.get("event") == "FILLED" and r.get("order_id") == row["order_id"]:
                pending_fill = r
                break

    discrepancy_note = (
        "No Sep-24 12:30 fill in artifacts. pilot_id `selected_sep23_24` names the D73 window, "
        "not fill day. Sep-24 12:30 shows SHORT_OK in gate CSV but no pool signal/fill that bar. "
        "V1 classifier filled 2026-09-23T10:00:00Z (separate row in D73)."
    )

    isolated_models_tp_exit = False
    runner = ROOT / "tools/phase_downtrend_isolated_upper_short_v1/isolated_chronological_runner_v1.py"
    if runner.is_file():
        text = runner.read_text(encoding="utf-8")
        isolated_models_tp_exit = "SIMULATED_TP" in text or "_try_exit_tp" in text

    still_open_through_horizon = sl_bar is None
    ok = (
        fill_utc == FILL_BAR
        and signal_utc == SIGNAL_BAR
        and pending_fill is not None
        and pending_fill.get("decision_close_utc") == FILL_BAR
    )

    return {
        "ok": ok,
        "discrepancy_resolution": discrepancy_note,
        "modeled": {
            "fill_utc": fill_utc,
            "signal_utc": signal_utc,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "order_id": row["order_id"],
            "fill_detail": (pending_fill or {}).get("detail"),
        },
        "ohlc_through_d80_window_end": {
            "bars": len(keys),
            "max_high": max_high,
            "min_low": min_low,
            "first_sl_trigger_bar": sl_bar,
            "first_tp_touch_bar": tp_touch_bar,
            "isolated_runner_models_tp_exit": isolated_models_tp_exit,
            "position_open_at_horizon_per_runner": still_open_through_horizon,
        },
        "meta": meta,
    }


if __name__ == "__main__":
    print(json.dumps(verify_d80_v3_fill(), indent=2))
