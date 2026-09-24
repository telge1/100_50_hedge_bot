#!/usr/bin/env python3
"""One-off audit: XRPUSDT 5m cluster-sweep signals 2026-08-19 morning UTC."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, "/home/telgenbuescher/projects/orderbook_analyse/src")

from research_charts.cluster_sweep_backtester import run_cluster_sweep_backtest

SYMBOL = "XRPUSDT"
TF = "5m"
DAY = datetime(2026, 8, 19, tzinfo=timezone.utc)
START = DAY.replace(hour=3, minute=0)
END = DAY.replace(hour=8, minute=0)
TARGETS = [("04:55", "05:00"), ("05:25", "05:30"), ("05:55", "06:00"), ("06:20", "06:25")]


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_hm(hm: str) -> datetime:
    h, m = map(int, hm.split(":"))
    return DAY.replace(hour=h, minute=m)


def fmt(dt: datetime | None) -> str:
    return dt.strftime("%H:%M UTC") if dt else "?"


def pct(x: float | None) -> str:
    return f"{100 * x:.1f}%" if x is not None else "n/a"


def summarize_window(w: dict) -> str:
    lines: list[str] = []
    ts = w.get("trades_status", "?")
    if ts == "VALID":
        br = w.get("taker_buy_ratio")
        delta = w.get("delta", 0)
        lines.append(
            f"    trades: {w.get('trade_count', 0)} | "
            f"buy={w.get('buy_notional', 0):,.0f} sell={w.get('sell_notional', 0):,.0f} | "
            f"taker_buy={pct(br)} delta={delta:+,.0f}"
        )
    else:
        lines.append(f"    trades: {ts}")

    ob = w.get("ob_status", "?")
    if ob == "VALID":
        imb = w.get("imbalance_l50_mean", 0)
        sp = w.get("spread_bps_mean", 0)
        lines.append(f"    orderbook: imbalance_l50={imb:+.3f} spread={sp:.2f} bps")
    else:
        lines.append(f"    orderbook: {ob}")

    oi = w.get("oi_status", "?")
    if oi == "VALID":
        lines.append(f"    open_interest: change={w.get('oi_change', 0):+,.0f}")
    else:
        lines.append(f"    open_interest: {oi}")

    liq = w.get("liq_status", "?")
    if liq == "VALID":
        lines.append(
            f"    liquidations: long={w.get('liq_long_notional')} "
            f"short={w.get('liq_short_notional')}"
        )
    else:
        lines.append(f"    liquidations: {liq}")
    return "\n".join(lines)


def match(events: list[dict], conf_hm: str, ent_hm: str) -> tuple[dict | None, float]:
    tc, te = parse_hm(conf_hm), parse_hm(ent_hm)
    best, score = None, 1e9
    for e in events:
        conf = parse_ts(e.get("confirmation_at"))
        ent = parse_ts(e.get("entry_at"))
        if not conf:
            continue
        sc = abs((conf - tc).total_seconds()) + (abs((ent - te).total_seconds()) if ent else 0)
        if sc < score:
            score, best = sc, e
    return best, score


def main() -> None:
    result = run_cluster_sweep_backtest(
        symbol=SYMBOL, timeframe=TF, start=START, end=END, minimum_cluster_pools=3
    )
    events = result["events"]
    cov = result.get("coverage") or {}

    print("=== DATEN-VERFUEGBARKEIT 03:00-08:00 UTC (19.08.2026) ===")
    for k in ["candles_1m", "public_trades", "ob200_v3", "open_interest_5s", "liquidations"]:
        v = cov.get(k, {})
        print(
            f"  {k:22} status={v.get('status')} rows={v.get('row_count')} "
            f"first={v.get('first_ts')} last={v.get('last_ts')}"
        )

    for i, (ch, eh) in enumerate(TARGETS, 1):
        e, score = match(events, ch, eh)
        print(f"\n{'=' * 72}")
        print(f"SIGNAL {i}: Confirm {ch} / Entry {eh} UTC  (match delta {score:.0f}s)")
        if not e:
            print("  Kein Event gefunden")
            continue
        confs = json.loads(e.get("confirmations_json") or "{}")
        fired = [k for k, v in confs.items() if v.get("fired")]
        ocov = json.loads(e.get("orderflow_coverage") or "{}")
        of = json.loads(e.get("orderflow_windows_json") or "{}")
        print(f"  Status: {e.get('final_status')} | {e.get('direction')}")
        print(f"  Bestaetigung: {e.get('confirmation_type')} @ {e.get('confirmation_at')}")
        print(f"  Entry: {e.get('entry_at')} @ {e.get('entry_price')}")
        print(
            f"  Cluster: {e.get('cluster_low'):.4f}-{e.get('cluster_high'):.4f} "
            f"pools={e.get('cluster_pool_count')} side={e.get('cluster_side')}"
        )
        print(f"  EMA 9/20/59: {e.get('ema_9')} / {e.get('ema_20')} / {e.get('ema_59')}")
        print(
            f"  Struktur intakt: 9>59={e.get('ema9_gt_ema59')} 20>59={e.get('ema20_gt_ema59')} "
            f"price>59={e.get('price_above_ema59')}"
        )
        print(
            f"  Ablauf: touch={fmt(parse_ts(e.get('first_touch_at')))} "
            f"max_sweep={fmt(parse_ts(e.get('max_sweep_at')))} "
            f"cross_ema59={fmt(parse_ts(e.get('price_cross_ema59_at')))}"
        )
        print(f"  Varianten: {', '.join(fired)}")
        print(f"  Event-Coverage: {ocov}")
        print(f"  Outcome 8 bars: MFE={e.get('mfe')} MAE={e.get('mae')}")
        for wname in ["before_contact", "during_sweep", "after_sweep_to_confirmation"]:
            w = of.get(wname)
            if w:
                print(f"  --- {wname} ({w.get('window_start')} -> {w.get('window_end')}) ---")
                print(summarize_window(w))

    print("\n=== ALLE EVENTS IM FENSTER ===")
    for e in sorted(events, key=lambda x: x.get("confirmation_at") or ""):
        ocov = json.loads(e.get("orderflow_coverage") or "{}")
        print(
            f"  conf={e.get('confirmation_at')} entry={e.get('entry_at')} "
            f"{e.get('direction')} {e.get('confirmation_type')} pools={e.get('cluster_pool_count')} "
            f"trades={ocov.get('trades')} ob={ocov.get('orderbook')} oi={ocov.get('oi')}"
        )


if __name__ == "__main__":
    main()
