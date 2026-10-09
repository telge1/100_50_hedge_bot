#!/usr/bin/env python3
"""TUTUSDT short live dry-run outcome forensics (ClickHouse 1m, read-only)."""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "short_tut_20261004_forensics_v1"
SIGNALS = REPO / "bot/e1r_live_scanner/logs/signals.jsonl"

# Frozen short horizon: 48h on management bars (audit HORIZON_BARS = 48*4 @ 15m → 48*60 @ 1m).
HORIZON_HOURS = 48
HORIZON_1M_BARS = HORIZON_HOURS * 60


def _ensure_paths() -> None:
    for p in (str(REPO), str(REPO / "results/pool_scan/context_study_v1")):
        if p not in sys.path:
            sys.path.insert(0, p)
    from pool_pattern.market import ensure_paths

    ensure_paths()


def _load_signals() -> tuple[dict, dict]:
    s1 = s2 = None
    for line in SIGNALS.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("symbol") != "TUTUSDT" or row.get("event") == "wave_summary":
            continue
        if row.get("decision_time") == "2026-10-04T18:30:00+00:00" and row.get("signal_status") == "ALLOWED":
            s1 = row
        if row.get("decision_time") == "2026-10-04T18:45:00+00:00" and row.get("signal_status") == "BLOCKED":
            s2 = row
    if not s1 or not s2:
        raise SystemExit("missing expected TUT signals in signals.jsonl")
    return s1, s2


def _load_1m(symbol: str, start: datetime, end: datetime) -> list[dict]:
    from bot.long_v1_live_scanner.data_1m import load_1m_bars

    return load_1m_bars(symbol, start, end)


def _simulate_short(sig: dict, *, label: str) -> dict:
    decision = datetime.fromisoformat(sig["decision_time"])
    entry = float(sig["entry_price"])
    stop = float(sig["stop"])
    tp = float(sig["tp"])
    end_load = decision + timedelta(hours=HORIZON_HOURS + 2)
    bars = _load_1m(sig["symbol"], decision, end_load)
    path_bars = [b for b in bars if b["open_time"] >= decision]
    if not path_bars:
        raise SystemExit(f"no 1m bars after {decision}")

    mae = mfe = 0.0
    mae_t = mfe_t = None
    first_hit = "horizon"
    exit_time = None
    exit_price = None
    exit_bar_i = None
    intrabar = False
    bars_to_exit = None

    limit = min(len(path_bars), HORIZON_1M_BARS)
    rows_path: list[dict] = []

    for i in range(limit):
        bar = path_bars[i]
        adv = (bar["high"] - entry) / entry * 100.0
        fav = (entry - bar["low"]) / entry * 100.0
        if adv > mae:
            mae = adv
            mae_t = bar["close_time"]
        if fav > mfe:
            mfe = fav
            mfe_t = bar["close_time"]

        sl_hit = bar["high"] >= stop
        tp_hit = bar["low"] <= tp
        if first_hit == "horizon" and (sl_hit or tp_hit):
            bars_to_exit = i + 1
            exit_bar_i = i
            exit_time = bar["close_time"]
            if sl_hit and tp_hit:
                first_hit = "INTRABAR_AMBIGUOUS"
                intrabar = True
                exit_price = stop  # conservative (frozen audit)
            elif sl_hit:
                first_hit = "SL"
                exit_price = stop
            else:
                first_hit = "TP"
                exit_price = tp

        rows_path.append(
            {
                "time": bar["open_time"].isoformat(),
                "open": bar["open"],
                "high": bar["high"],
                "low": bar["low"],
                "close": bar["close"],
                "TP_hit": tp_hit,
                "SL_hit": sl_hit,
            }
        )
        if first_hit != "horizon":
            break

    if first_hit == "horizon" and path_bars:
        last = path_bars[min(limit, len(path_bars)) - 1]
        exit_time = last["close_time"]
        exit_price = last["close"]
        bars_to_exit = limit

    pnl = (entry - exit_price) / entry * 100.0 if exit_price is not None else None
    duration_min = bars_to_exit if bars_to_exit is not None else None

    # compact path: entry bar, mae bar, mfe bar, exit bar, plus neighbors
    key_times = {decision}
    if mae_t:
        key_times.add(mae_t)
    if mfe_t:
        key_times.add(mfe_t)
    if exit_time:
        key_times.add(exit_time)
    compact = []
    seen_t = set()
    for r in rows_path:
        ot = datetime.fromisoformat(r["time"])
        for kt in key_times:
            if abs((ot - kt).total_seconds()) <= 120 and r["time"] not in seen_t:
                compact.append(r)
                seen_t.add(r["time"])
    if rows_path and rows_path[0]["time"] not in seen_t:
        compact.insert(0, rows_path[0])
    if exit_bar_i is not None and rows_path[exit_bar_i]["time"] not in seen_t:
        compact.append(rows_path[exit_bar_i])

    compact = sorted({r["time"]: r for r in compact}.values(), key=lambda x: x["time"])

    return {
        "label": label,
        "symbol": sig["symbol"],
        "pool_id": sig["pool_id"],
        "entry_open": sig["entry_open"],
        "decision_time": sig["decision_time"],
        "entry_price": entry,
        "stop": stop,
        "tp": tp,
        "e1r_state": sig.get("e1r_state"),
        "signal_status": sig.get("signal_status"),
        "block_reason": sig.get("block_reason"),
        "first_hit": first_hit,
        "outcome": first_hit,
        "intrabar_ambiguous": intrabar,
        "pnl_pct": round(pnl, 6) if pnl is not None else None,
        "mae_pct": round(mae, 6),
        "mfe_pct": round(mfe, 6),
        "mae_time": mae_t.isoformat() if mae_t else None,
        "mfe_time": mfe_t.isoformat() if mfe_t else None,
        "exit_time": exit_time.isoformat() if exit_time else None,
        "exit_price": exit_price,
        "duration_minutes": duration_min,
        "horizon_hours": HORIZON_HOURS,
        "management_tf": "1m",
        "path_compact": compact,
        "path_all_count": len(rows_path),
    }


def main() -> None:
    _ensure_paths()
    OUT.mkdir(parents=True, exist_ok=True)
    s1, s2 = _load_signals()
    r1 = _simulate_short(s1, label="signal_1830_allowed")
    r2 = _simulate_short(s2, label="signal_1845_blocked_hypothetical")

    def write_csv(name: str, result: dict) -> None:
        path = OUT / name
        with path.open("w", newline="") as f:
            w = csv.DictWriter(
                f,
                fieldnames=["time", "open", "high", "low", "close", "TP_hit", "SL_hit"],
            )
            w.writeheader()
            for row in result["path_compact"]:
                w.writerow(row)

    write_csv("signal_1830_allowed.csv", r1)
    write_csv("signal_1845_blocked_hypothetical.csv", r2)

    # E1R assessment
    def classify_blocked(hyp: dict) -> str:
        fh = hyp["first_hit"]
        pnl = hyp["pnl_pct"] or 0
        if fh == "horizon":
            return "E1R_BLOCKED_UNRESOLVED"
        if fh in ("SL", "INTRABAR_AMBIGUOUS") or pnl < 0:
            return "E1R_BLOCK_SAVED_LOSS"
        if fh == "TP" or pnl > 0:
            return "E1R_BLOCKED_WINNER"
        return "E1R_BLOCKED_UNRESOLVED"

    e1r_class = classify_blocked(r2)
    if e1r_class == "E1R_BLOCK_SAVED_LOSS":
        verdict = "LIVE_E1R_BLOCK_HELPED"
    elif e1r_class == "E1R_BLOCKED_WINNER":
        verdict = "LIVE_E1R_BLOCK_HURT"
    else:
        verdict = "LIVE_E1R_BLOCK_INCONCLUSIVE"

    allowed_ok = None
    if r1["first_hit"] == "TP":
        allowed_ok = "ALLOW_CORRECT_TP"
    elif r1["first_hit"] in ("SL", "INTRABAR_AMBIGUOUS"):
        allowed_ok = "ALLOW_COSTLY_SL"
    else:
        allowed_ok = "ALLOW_UNRESOLVED"

    summary = {
        "signals_source": str(SIGNALS),
        "signal_1830_allowed": {k: v for k, v in r1.items() if k != "path_compact"},
        "signal_1845_hypothetical": {k: v for k, v in r2.items() if k != "path_compact"},
        "e1r_1830_allowed_assessment": allowed_ok,
        "e1r_1845_block_classification": e1r_class,
        "final_verdict": verdict,
    }
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2))
    (OUT / "method.md").write_text(
        f"""# TUTUSDT short live forensics 2026-10-04

- Signals from `bot/e1r_live_scanner/logs/signals.jsonl` (unchanged).
- 1m candles via ClickHouse `load_candles` (same as live scanners).
- Path starts at first 1m bar with `open_time >= decision_time`.
- Short: SL if `high >= stop`, TP if `low <= tp`.
- Same-bar both hits: `INTRABAR_AMBIGUOUS`, conservative exit at stop (frozen audit).
- Horizon: {HORIZON_HOURS}h = {HORIZON_1M_BARS} x 1m bars (48h frozen).
- MAE = max adverse `(high-entry)/entry`; MFE = max favorable `(entry-low)/entry`.
- PnL short: `(entry-exit)/entry*100`.
- No orders, no scanner changes.
"""
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
