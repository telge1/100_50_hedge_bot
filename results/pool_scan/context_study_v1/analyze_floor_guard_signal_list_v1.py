"""Per-signal list (entry/SL/TP, PnL%, running DD) for XRP/ADA/DOGE Jun–Jul 2026."""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[2]
SYMBOLS = ("XRPUSDT", "ADAUSDT", "DOGEUSDT")
REPORT_FROM = datetime(2026, 6, 1, tzinfo=timezone.utc)
REPORT_TO = datetime(2026, 7, 31, 23, 59, 59, tzinfo=timezone.utc)
PANE_FROM = datetime(2026, 2, 10, tzinfo=timezone.utc)
PANE_TO = datetime(2026, 9, 30, 23, 59, 59, tzinfo=timezone.utc)
OUT_JSON = ROOT / "floor_guard_signal_list_v1.json"
OUT_CSV = ROOT / "floor_guard_signal_list_v1.csv"
OUT_MD = ROOT / "floor_guard_signal_list_v1.md"


def _ensure_paths() -> None:
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from pool_pattern.market import ensure_paths

    ensure_paths()


def collect_signals(symbol: str) -> dict:
    from find_short_entry_15m_v1 import (
        HISTORY_WEEKS,
        build_15m_bars,
        down_4h,
        entry_setup,
        last_closed_index,
        load_market,
        row_for,
    )
    from dashboard.research_charts.lld_research_kernel import (
        load_pane_candles,
        scanner_pools_for_index,
        ui_lld_config,
    )
    from short_block_4h_guard import H4FloorGuard, guard_spec

    market = load_market(symbol)
    _packed, candles = load_pane_candles(
        symbol,
        "15m",
        from_unix=int(PANE_FROM.timestamp()),
        to_unix=int(PANE_TO.timestamp()),
        history_weeks=HISTORY_WEEKS,
    )
    lld_cfg = ui_lld_config("15m")
    bars15 = build_15m_bars(candles)
    by_open = {bar["open_time"]: bar for bar in bars15}

    guard = H4FloorGuard(symbol, PANE_FROM, PANE_TO)
    cache: dict[int, tuple] = {}
    seen_no_guard: set[str] = set()
    seen_with_guard: set[str] = set()
    no_guard: list[dict] = []
    with_guard: list[dict] = []
    floor_blocked_count = 0

    for bar in bars15:
        moment = bar["close_time"]
        blocked, _reason = guard.advance(bar, moment)
        if moment < REPORT_FROM or moment > REPORT_TO:
            continue
        idx = bar["candle_index"]
        pools, _ui, _proof = scanner_pools_for_index(candles, "15m", idx, lld_cfg, cache=cache)
        setup = entry_setup(pools, bar, moment)
        if setup is None:
            continue
        pool, upper, lower, gap = setup
        h4_index = last_closed_index(market["4h"]["bars"], moment)
        if not down_4h(market["4h"]["bars"], h4_index):
            continue
        h4 = market["4h"]["bars"][h4_index]
        source_t = pool.get("source")
        birth = by_open.get(source_t, bar) if source_t is not None else bar
        row = row_for(symbol, pool, birth, bar, h4, None, None, upper, lower, gap)
        if blocked:
            floor_blocked_count += 1
        would_new_no = pool["pool_id"] not in seen_no_guard
        would_new_with = pool["pool_id"] not in seen_with_guard and not blocked
        if would_new_no:
            seen_no_guard.add(pool["pool_id"])
            no_guard.append(row)
        if would_new_with:
            seen_with_guard.add(pool["pool_id"])
            with_guard.append(row)

    return {
        "bars15": bars15,
        "no_guard": no_guard,
        "with_guard": with_guard,
        "floor_blocked_count": floor_blocked_count,
        "guard_spec": guard_spec(),
    }


def enrich_and_drawdown(symbol: str, variant: str, rows: list[dict], bars15: list[dict]) -> list[dict]:
    from analyze_xrp_floor_guard_pnl_v1 import enrich_signals

    trades = enrich_signals(bars15, rows)
    peak = 0.0
    equity = 0.0
    for t in trades:
        t["symbol"] = symbol
        t["variant"] = variant
        step = t["pnl_pct"] if t["pnl_pct"] is not None else 0.0
        equity += step
        peak = max(peak, equity)
        t["cumulative_pnl_pct"] = round(equity, 4)
        t["equity_peak_pct"] = round(peak, 4)
        t["drawdown_from_peak_pct"] = round(peak - equity, 4)
    return trades


def write_csv(rows: list[dict]) -> None:
    fields = [
        "symbol",
        "variant",
        "signal_num",
        "entry_open",
        "entry_price",
        "stop",
        "tp",
        "first_hit",
        "pnl_pct",
        "cumulative_pnl_pct",
        "drawdown_from_peak_pct",
        "equity_peak_pct",
        "exit_time",
        "exit_price",
        "result_status",
        "pool_id",
    ]
    with OUT_CSV.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def write_md(by_symbol: dict) -> None:
    lines = [
        "# Signal-Liste Jun–Jul 2026 (XRP / ADA / DOGE)",
        "",
        "Spalten: Entry-Preis = 15m-Close; SL/TP aus Scanner; `pnl_pct` bei SL/TP-Treffer; "
        "`drawdown_from_peak_pct` = Abstand der kumulierten PnL zum bisherigen Peak (chronologisch).",
        "",
    ]
    for symbol in SYMBOLS:
        block = by_symbol[symbol]
        for variant in ("with_guard", "without_guard"):
            trades = block[variant]
            label = "mit Floor-Guard" if variant == "with_guard" else "ohne Floor-Guard"
            lines += [f"## {symbol} — {label} ({len(trades)} Signale)", ""]
            lines += [
                "| # | entry | entry | SL | TP | hit | pnl% | kum.% | DD% |",
                "|---|-------|-------|----|----|-----|------|-------|-----|",
            ]
            for t in trades:
                lines.append(
                    f"| {t['signal_num']} | {t['entry_open'][5:16]} | {t['entry_price']} | "
                    f"{t['stop']} | {t['tp']} | {t['first_hit']} | "
                    f"{t['pnl_pct'] if t['pnl_pct'] is not None else '—'} | "
                    f"{t['cumulative_pnl_pct']} | {t['drawdown_from_peak_pct']} |"
                )
            lines.append("")
    OUT_MD.write_text("\n".join(lines) + "\n")


def main() -> None:
    _ensure_paths()
    all_rows: list[dict] = []
    report_symbols: dict = {}
    for symbol in SYMBOLS:
        print(f"collect {symbol}...", flush=True)
        data = collect_signals(symbol)
        bars = data["bars15"]
        with_g = enrich_and_drawdown(symbol, "with_guard", data["with_guard"], bars)
        no_g = enrich_and_drawdown(symbol, "without_guard", data["no_guard"], bars)
        all_rows.extend(with_g)
        all_rows.extend(no_g)
        report_symbols[symbol] = {
            "with_guard": with_g,
            "without_guard": no_g,
            "counts": {"with_guard": len(with_g), "without_guard": len(no_g)},
            "floor_blocked_events": data["floor_blocked_count"],
        }
        print(
            symbol,
            "with",
            len(with_g),
            "without",
            len(no_g),
            "floor_blocked",
            data["floor_blocked_count"],
            flush=True,
        )

    write_csv(all_rows)
    write_md(report_symbols)
    OUT_JSON.write_text(
        json.dumps(
            {
                "window": [REPORT_FROM.isoformat(), REPORT_TO.isoformat()],
                "symbols": report_symbols,
            },
            indent=2,
        )
        + "\n"
    )
    print("wrote", OUT_CSV, OUT_MD, OUT_JSON)


if __name__ == "__main__":
    main()
