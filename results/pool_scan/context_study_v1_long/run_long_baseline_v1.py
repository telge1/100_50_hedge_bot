#!/usr/bin/env python3
"""Run long baseline V1 Jun–Jul 2026 for XRP/ADA/DOGE."""

from __future__ import annotations

import csv
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

LONG_ROOT = Path(__file__).resolve().parent
SHORT_ROOT = LONG_ROOT.parent / "context_study_v1"
REPO = LONG_ROOT.parents[2]
OUT_DIR = LONG_ROOT / "long_baseline_v1"

PANE_FROM = datetime(2026, 2, 10, tzinfo=timezone.utc)
PANE_TO = datetime(2026, 9, 30, 23, 59, 59, tzinfo=timezone.utc)
REPORT_FROM = datetime(2026, 6, 1, tzinfo=timezone.utc)
REPORT_TO = datetime(2026, 7, 31, 23, 59, 59, tzinfo=timezone.utc)
SYMBOLS = ("XRPUSDT", "ADAUSDT", "DOGEUSDT")


def _ensure_paths() -> None:
    for p in (str(REPO), str(SHORT_ROOT), str(LONG_ROOT)):
        if p not in sys.path:
            sys.path.insert(0, p)
    from pool_pattern.market import ensure_paths

    ensure_paths()


def _coin_stats(trades: list[dict]) -> dict:
    empty = {
        "trades": 0,
        "TP": 0,
        "SL": 0,
        "unresolved": 0,
        "win_rate": None,
        "pnl_sum_pct": None,
        "profit_factor": None,
        "max_drawdown_pct": 0.0,
        "max_sl_streak": 0,
        "median_sl_distance_pct": None,
        "median_tp_distance_pct": None,
        "median_mae_pct": None,
        "median_mfe_pct": None,
    }
    if not trades:
        return empty
    pnls = [t["pnl_pct"] for t in trades if t.get("pnl_pct") is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    pf = round(sum(wins) / abs(sum(losses)), 4) if losses and wins else None
    ordered = sorted(trades, key=lambda t: t["entry_open"])
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    streak = 0
    max_streak = 0
    for t in ordered:
        p = t["pnl_pct"] if t.get("pnl_pct") is not None else 0.0
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        if t.get("first_hit") == "SL":
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    maes = [float(t["mae_pct"]) for t in trades if t.get("mae_pct") is not None]
    mfes = [float(t["mfe_pct"]) for t in trades if t.get("mfe_pct") is not None]
    sl_d = [t["stop_distance_pct"] for t in trades if t.get("stop_distance_pct") is not None]
    tp_d = [t["tp_distance_pct"] for t in trades if t.get("tp_distance_pct") is not None]
    return {
        "trades": len(trades),
        "TP": sum(1 for t in trades if t.get("first_hit") == "TP"),
        "SL": sum(1 for t in trades if t.get("first_hit") == "SL"),
        "unresolved": sum(1 for t in trades if t.get("first_hit") == "none"),
        "win_rate": round(len(wins) / len(pnls), 4) if pnls else None,
        "pnl_sum_pct": round(sum(pnls), 4) if pnls else None,
        "profit_factor": pf,
        "max_drawdown_pct": round(max_dd, 4),
        "max_sl_streak": max_streak,
        "median_sl_distance_pct": round(statistics.median(sl_d), 4) if sl_d else None,
        "median_tp_distance_pct": round(statistics.median(tp_d), 4) if tp_d else None,
        "median_mae_pct": round(statistics.median(maes), 4) if maes else None,
        "median_mfe_pct": round(statistics.median(mfes), 4) if mfes else None,
    }


def _audit_setup_at_bar(
    symbol: str,
    bar: dict,
    pools: list[dict],
    market: dict,
    *,
    expect_pool_id: str | None = None,
) -> dict:
    from find_long_entry_15m_v1 import (
        STOP_PAD,
        attached_lower,
        entry_setup_long,
        last_closed_index,
        up_4h,
    )

    moment = bar["close_time"]
    setup = entry_setup_long(pools, bar, moment)
    h4i = last_closed_index(market["4h"]["bars"], moment)
    if setup is None:
        return {"entry_open": bar["open_time"].isoformat(), "symbol": symbol, "setup": False}
    pool, bridge, upper, gap = setup
    stop = pool["bottom"] * (1.0 - STOP_PAD)
    tp = upper["bottom"]
    checks = {
        "entry_open": bar["open_time"].isoformat(),
        "symbol": symbol,
        "setup": True,
        "bearish_bar": bar["close"] < bar["open"],
        "close_above_pool_top": bar["close"] > pool["top"],
        "wick_touches_pool_top": bar["low"] <= pool["top"] + 1e-9,
        "attached_lower": attached_lower(pool, bar),
        "stop_below_entry": stop < bar["close"],
        "tp_above_entry": tp > bar["close"],
        "pool_known_causal": pool["known"] <= moment,
        "pool_not_broken": pool["break_at"] is None or pool["break_at"] > moment,
        "up_4h_at_signal": up_4h(market["4h"]["bars"], h4i),
        "pool_id": pool["pool_id"],
        "gap_pct": round(gap, 4),
    }
    if expect_pool_id is not None:
        checks["pool_id_matches_signal"] = pool["pool_id"] == expect_pool_id
    return checks


def _collect_setup_audit_samples(symbol: str) -> list[dict]:
    from dashboard.research_charts.lld_research_kernel import (
        load_pane_candles,
        scanner_pools_for_index,
        ui_lld_config,
    )
    from find_long_entry_15m_v1 import HISTORY_WEEKS, entry_setup_long
    from find_short_entry_15m_v1 import build_15m_bars, load_market

    market = load_market(symbol)
    _, candles = load_pane_candles(
        symbol,
        "15m",
        from_unix=int(PANE_FROM.timestamp()),
        to_unix=int(PANE_TO.timestamp()),
        history_weeks=HISTORY_WEEKS,
    )
    bars15 = build_15m_bars(candles)
    lld_cfg = ui_lld_config("15m")
    cache: dict = {}
    samples: list[dict] = []
    for bar in bars15:
        moment = bar["close_time"]
        if moment < REPORT_FROM or moment > REPORT_TO:
            continue
        idx = bar["candle_index"]
        pools, _, _ = scanner_pools_for_index(candles, "15m", idx, lld_cfg, cache=cache)
        if entry_setup_long(pools, bar, moment) is None:
            continue
        samples.append(_audit_setup_at_bar(symbol, bar, pools, market))
        if len(samples) >= 1:
            break
    return samples


def _geometry_setup_count(symbol: str) -> int:
    from dashboard.research_charts.lld_research_kernel import (
        load_pane_candles,
        scanner_pools_for_index,
        ui_lld_config,
    )
    from find_long_entry_15m_v1 import HISTORY_WEEKS, entry_setup_long
    from find_short_entry_15m_v1 import build_15m_bars

    _, candles = load_pane_candles(
        symbol,
        "15m",
        from_unix=int(PANE_FROM.timestamp()),
        to_unix=int(PANE_TO.timestamp()),
        history_weeks=HISTORY_WEEKS,
    )
    bars15 = build_15m_bars(candles)
    lld_cfg = ui_lld_config("15m")
    cache: dict = {}
    count = 0
    for bar in bars15:
        moment = bar["close_time"]
        if moment < REPORT_FROM or moment > REPORT_TO:
            continue
        pools, _, _ = scanner_pools_for_index(candles, "15m", bar["candle_index"], lld_cfg, cache=cache)
        if entry_setup_long(pools, bar, moment) is not None:
            count += 1
    return count


def main() -> None:
    _ensure_paths()
    from find_long_entry_15m_v1 import collect_long_signals
    from long_trade_outcome import enrich_long_signals
    from find_short_entry_15m_v1 import load_market

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    all_trades: list[dict] = []
    bars_by_sym: dict[str, list] = {}
    markets: dict[str, dict] = {}

    for symbol in SYMBOLS:
        rows, bars15 = collect_long_signals(symbol, PANE_FROM, PANE_TO, REPORT_FROM, REPORT_TO)
        bars_by_sym[symbol] = bars15
        markets[symbol] = load_market(symbol)
        trades = enrich_long_signals(bars15, rows)
        for t in trades:
            t["symbol"] = symbol
        all_trades.extend(trades)
        print(symbol, "signals", len(trades), flush=True)

    all_trades.sort(key=lambda t: (t["entry_open"], t["symbol"]))

    audit_results: list[dict] = []
    if all_trades:
        from dashboard.research_charts.lld_research_kernel import scanner_pools_for_index, ui_lld_config
        from find_long_entry_15m_v1 import HISTORY_WEEKS
        from dashboard.research_charts.lld_research_kernel import load_pane_candles

        pick = [all_trades[0]]
        if len(all_trades) > 1:
            pick.append(all_trades[len(all_trades) // 2])
        if len(all_trades) > 2:
            pick.append(all_trades[-1])
        lld_cfg = ui_lld_config("15m")
        for t in pick[:3]:
            sym = t["symbol"]
            _, candles = load_pane_candles(
                sym,
                "15m",
                from_unix=int(PANE_FROM.timestamp()),
                to_unix=int(PANE_TO.timestamp()),
                history_weeks=HISTORY_WEEKS,
            )
            bar = next(b for b in bars_by_sym[sym] if b["open_time"].isoformat() == t["entry_open"])
            pools, _, _ = scanner_pools_for_index(candles, "15m", bar["candle_index"], lld_cfg, cache={})
            audit_results.append(
                _audit_setup_at_bar(sym, bar, pools, markets[sym], expect_pool_id=t["pool_id"])
            )
    else:
        for sym in SYMBOLS:
            audit_results.extend(_collect_setup_audit_samples(sym))
        audit_results = audit_results[:3]

    per_coin = {sym: _coin_stats([t for t in all_trades if t["symbol"] == sym]) for sym in SYMBOLS}
    overall = _coin_stats(all_trades)

    from find_long_entry_15m_v1 import (
        ATTACH_PCT,
        MIN_BRIDGE_GAP_PCT,
        MIN_GAP_PCT,
        MIN_TP_PCT,
        STOP_PAD,
    )

    lookahead_pass = all(
        r.get("setup")
        and r.get("bearish_bar")
        and r.get("attached_lower")
        and r.get("stop_below_entry")
        and r.get("tp_above_entry")
        and r.get("pool_known_causal")
        and r.get("pool_not_broken")
        and (not all_trades or r.get("pool_id_matches_signal", True))
        for r in audit_results
    )
    note_zero = (
        "Jun–Jul 2026: entry setups exist but no 4h bar satisfied up_4h() in this window."
        if not all_trades
        else None
    )
    geometry_counts = {sym: _geometry_setup_count(sym) for sym in SYMBOLS}

    summary = {
        "report_from": REPORT_FROM.isoformat(),
        "report_to": REPORT_TO.isoformat(),
        "overall": overall,
        "per_coin": per_coin,
        "lookahead_audit": audit_results,
        "lookahead_audit_pass": lookahead_pass,
        "note_zero_trades": note_zero,
        "geometry_only_setups_jun_jul": geometry_counts,
    }

    with (OUT_DIR / "long_signals.json").open("w", encoding="utf-8") as f:
        json.dump(all_trades, f, indent=2, default=str)

    fields = list(all_trades[0].keys()) if all_trades else []
    if all_trades:
        with (OUT_DIR / "long_signals.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(all_trades)

    with (OUT_DIR / "long_summary.json").open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    method = f"""# Long baseline V1 method

Mirrored from `find_short_entry_15m_v1` short causal scanner.

## Entry
- Bearish 15m bar (`close < open`)
- Touch lower pool from above (`attached_lower`)
- Entry pool: max `top` among touched lowers (closest support under price)
- Bridge: `next_lower_bridge` with `MIN_BRIDGE_GAP_PCT` / `MIN_GAP_PCT`
- TP: `next_upper_tp` at `upper.bottom`, `MIN_TP_PCT`
- SL: `entry_lower.bottom * (1 - STOP_PAD)`
- Dedup: one signal per `pool_id`
- 4h: `up_4h()` mirror of `down_4h()`
- No FloorGuard, no E1R

## Constants
MIN_GAP_PCT={MIN_GAP_PCT}, MIN_BRIDGE_GAP_PCT={MIN_BRIDGE_GAP_PCT}, MIN_TP_PCT={MIN_TP_PCT}, STOP_PAD={STOP_PAD}, ATTACH_PCT={ATTACH_PCT}

## up_4h
- `close > ema20 > ema200`, `ema9 > ema200`, `ema59 > ema200`
- `depth = (ema59 - ema200) / ema200 * 100 >= 2.0`

## Outcome
Long first-hit on 15m forward bars (HORIZON from analyze_4h_lower_short_block_v1).
SL before TP on same bar (conservative).
"""
    (OUT_DIR / "long_method.md").write_text(method, encoding="utf-8")

    print("OVERALL", json.dumps(overall, indent=2))
    print("AUDIT", json.dumps(audit_results, indent=2))


if __name__ == "__main__":
    main()
